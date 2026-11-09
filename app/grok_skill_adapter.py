"""Convert the documented single-match Skill output; never persist the raw input."""
import hashlib
import json
import re
from datetime import date, datetime
from urllib.parse import parse_qsl, urlsplit

from fastapi import HTTPException
from pydantic import ValidationError
from .research_models import ResearchResult

STATUS = {'confirmed_official':'confirmed', 'confirmed_independent':'confirmed',
          'single_source':'reported', 'conflicting':'conflicting',
          'unverified':'unknown', 'not_found':'unknown', 'outdated':'unknown'}
SECRET_KEY = re.compile(r'(?i)(authorization|cookie|password|secret|token|api[_-]?key|credential)')
SECRET_QUERY = {'key','auth','session','session_id','sessionid','signature','sig','access_key','private_key'}
SECRET_VALUE = re.compile(r'(?i)(bearer\s+\S+|(?:api[_-]?key|access[_-]?token|password)\s*[:=]\s*\S+|\bsk-[A-Za-z0-9_-]{16,})')


def check_safe(value, depth=0):
    if depth > 20:
        raise ValueError('Girdi çok derin')
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or SECRET_KEY.search(key):
                raise ValueError('Gizli bilgi alanlarına izin verilmez')
            check_safe(item, depth+1)
    elif isinstance(value, list):
        for item in value: check_safe(item, depth+1)
    elif isinstance(value, str):
        if SECRET_VALUE.search(value): raise ValueError('Gizli bilgi içeren metin reddedildi')
        if value.startswith(('http://','https://')):
            url = urlsplit(value)
            if (url.username or url.password or SECRET_KEY.search(url.query)
                    or any(k.lower() in SECRET_QUERY for k, _ in parse_qsl(url.query, keep_blank_values=True))):
                raise ValueError('Kimlik bilgisi içeren URL reddedildi')


def _date(value):
    if value is None: return None, None
    if not isinstance(value, str): raise ValueError('Yayın tarihi metin olmalı')
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        date.fromisoformat(value)
        return None, value
    if re.fullmatch(r'\d{2}[./]\d{2}[./]\d{4}', value):
        return None, datetime.strptime(value.replace('.', '/'), '%d/%m/%Y').date().isoformat()
    return value, None


def _only(value, keys):
    if not isinstance(value, dict): raise ValueError('Nesne bekleniyor')
    if set(value) - set(keys): raise ValueError('Desteklenmeyen Skill alanı')
    return value


def adapt(raw, job, skill_version):
    try:
        check_safe(raw)
        _only(raw, ('schema_version','mode','research_time','timezone','matches','job_id','input_version'))
        if raw.get('schema_version') != '1.0': raise ValueError('Desteklenmeyen Skill sürümü')
        for key in ('job_id','input_version','mode'):
            if key in raw and raw[key] != (job['id'] if key == 'job_id' else job[key]):
                raise HTTPException(409, 'Skill iş kimliği, modu veya girdi sürümüyle eşleşmiyor')
        matches = raw.get('matches')
        if not isinstance(matches, list) or len(matches) != 1:
            raise ValueError('Tam olarak bir maç gerekli')
        match = _only(matches[0], ('match_id','competition','home_team','away_team','kickoff','venue',
            'fixture_verification','model_prior','findings','form','lineups','lineup_surprises','key_developments','not_found','limitations'))
        if match.get('match_id') != job['match_id']:
            raise HTTPException(409, 'Skill maç kimliği atanmış maçla eşleşmiyor')
        research_time = raw.get('research_time')
        if not isinstance(research_time, str): raise ValueError('research_time gerekli')
        sources, source_by_url, findings = [], {}, []
        for f in match.get('findings', []):
            _only(f, ('id','team','category','player','claim','sources','independent_source_count','status',
                      'conflict_note','qualitative_impact','checked_at'))
            status = f.get('status', 'unverified')
            if status not in STATUS: raise ValueError('Bilinmeyen doğrulama durumu')
            refs, observations = [], []
            for s in f.get('sources', []):
                if isinstance(s, str): s = {'url':s}
                _only(s, ('url','publisher','title','tier','published_at','origin','kind','accessed_at'))
                url = s.get('url')
                if not isinstance(url, str) or not url.startswith(('http://','https://')):
                    raise ValueError('Kaynak URL gerekli')
                accessed = s.get('accessed_at') or f.get('checked_at')
                if not accessed: raise ValueError('Kaynak erişim zamanı veya checked_at gerekli')
                published_at, published_date = _date(s.get('published_at'))
                if published_date and date.fromisoformat(published_date) > datetime.fromisoformat(research_time.replace('Z', '+00:00')).date():
                    raise ValueError('Yayın tarihi araştırma tarihinden sonraya ait')
                sid = 'src-' + hashlib.sha256(url.encode()).hexdigest()[:24]
                entry = {'id':sid,'url':url,'accessed_at':accessed,'published_at':published_at,
                         'published_date':published_date,'publisher':s.get('publisher'),'title':s.get('title'),
                         'kind':s.get('kind','unknown'),'origin':s.get('origin'),'tier':s.get('tier')}
                if url in source_by_url:
                    previous = source_by_url[url]
                    if any(previous[k] != entry[k] for k in ('published_at','published_date','publisher','title','kind','tier')
                           if previous[k] is not None and entry[k] is not None
                           and not (k == 'kind' and 'unknown' in (previous[k], entry[k]))):
                        raise ValueError('Aynı kaynak için çelişkili metadata')
                    for field in ('publisher','title','tier','published_at','published_date','origin','kind'):
                        if previous[field] is None or (field == 'kind' and previous[field] == 'unknown'):
                            previous[field] = entry[field]
                if url not in source_by_url:
                    sources.append(entry); source_by_url[url] = entry
                observations.append({'source_id':sid, 'accessed_at':accessed, 'origin':s.get('origin'),
                                     'published_at':published_at, 'published_date':published_date})
                if sid not in refs: refs.append(sid)
            if STATUS[status] != 'unknown' and not refs:
                raise ValueError('Doğrulama iddiası kaynak gerektirir')
            if status == 'confirmed_independent' and (f.get('independent_source_count', 0) < 2 or len(refs) < 2):
                raise ValueError('Bağımsız doğrulama en az iki bağımsız kaynak bildirimi gerektirir')
            if status == 'conflicting' and len(refs) < 2:
                raise ValueError('Çelişki için iki kaynak gerekli')
            category = f.get('category','other')
            if category not in ('news','injury','suspension','lineup','form','fixture_load','other'):
                raise ValueError('Desteklenmeyen bulgu kategorisi')
            details = {k:f[k] for k in ('id','independent_source_count','conflict_note') if k in f}
            if observations: details['source_observations'] = observations
            mapped = STATUS[status]
            if status == 'confirmed_official' and not any(
                    s['id'] in refs and s['kind'] in ('club','league','federation') for s in sources):
                mapped = 'reported'
                details['downgrade_reason'] = 'Resmî kaynak türü açıkça belirtilmemiş.'
            findings.append({'category':category,'team':f.get('team'),'player':f.get('player'),
                'statement':f.get('claim'),'verification_status':mapped, 'original_status':status,
                'observed_at':f.get('checked_at'),'source_ids':refs,'qualitative_impact':f.get('qualitative_impact'),
                'skill_details':details or None})
        # The provided Skill emits structured form, lineup and prior objects. Preserve
        # them as explicitly labelled, unverified findings; do not infer sources/times.
        for key, category in [('fixture_verification','other'),('form','form'),('lineups','lineup'),
                              ('lineup_surprises','lineup'),('model_prior','other'),('key_developments','other'),('not_found','other')]:
            item = match.get(key)
            if item not in (None, {}, []):
                findings.append({'category':category, 'statement':'Skill alanı: '+key,
                    'verification_status':'unknown','source_ids':[], 'skill_details':{'field':key,'value':item}})
        lineups = match.get('lineups') or {}
        if not isinstance(lineups, dict): raise ValueError('lineups nesne olmalı')
        types = {v.get('type') for v in lineups.values() if isinstance(v, dict)}
        lineup_assessment = {'lineup_status':'unknown'}
        if 'official' in types:
            official_refs = []
            for side in ('home','away'):
                item = lineups.get(side)
                if not isinstance(item, dict) or item.get('type') != 'official': break
                source = source_by_url.get(item.get('source'))
                if not source or source['kind'] not in ('club','league','federation'): break
                if not any(source['id'] in finding['source_ids'] and finding['original_status'] == 'confirmed_official'
                           and finding['category'] == 'lineup'
                           for finding in findings if finding.get('original_status')): break
                official_refs.append(source['id'])
            if len(official_refs) == 2:
                lineup_assessment = {'lineup_status':'official','verification_status':'confirmed',
                                     'source_ids':list(dict.fromkeys(official_refs))}
                limitations = []
            else:
                limitations = ['Skill resmî kadro iddiası içeriyor; kaynak bağlamı yeterli değil.']
        else:
            lineup_assessment['lineup_status'] = 'predicted' if types.intersection({'probable','predicted'}) else 'unknown'
            limitations = []
        limitations.extend(match.get('limitations') or [])
        result = {'schema_version':'1.0','job_id':job['id'],'match_id':job['match_id'],
                  'mode':job['mode'],'input_version':job['input_version'],
                  'skill_name':'football-match-intel','skill_version':skill_version,'researched_at':research_time,
                  'sources':sources,'findings':findings,'lineup_assessment':lineup_assessment,
                  'limitations':limitations}
        if len(json.dumps(result,ensure_ascii=False,allow_nan=False).encode('utf-8')) > 128 * 1024:
            raise ValueError('Dönüştürülen sonuç boyut sınırını aşıyor')
        # JSON mode is required by the existing strict datetime validator.
        converted = ResearchResult.model_validate_json(json.dumps(result,ensure_ascii=False,allow_nan=False))
        if len(converted.model_dump_json().encode('utf-8')) > 128 * 1024:
            raise ValueError('Doğrulanmış sonuç boyut sınırını aşıyor')
        return converted
    except HTTPException:
        raise
    except (ValueError, TypeError, ValidationError, KeyError):
        raise HTTPException(422, 'Skill çıktısı uyumsuz, eksik veya güvensiz') from None
