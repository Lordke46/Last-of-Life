"""Explicit, in-memory client for a single Research API job.

The caller supplies a vault-fetched API key and an httpx-compatible client. No
CLI, credential storage, automatic research, automatic claim or background task.
Do not enable HTTP header/body logging in the caller or transport.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import re
from urllib.parse import quote, urlsplit


class PilotError(Exception):
    """Sanitized error: never includes request, response body or credentials."""


class RecoveryRequired(PilotError):
    """A claim or renewal may have succeeded but its token was lost."""


class SubmissionUncertain(PilotError):
    """The result may have been saved; retry the exact request in this process."""


class ValidationRejected(PilotError):
    """Safe, allowlisted server validation details; never the submitted value."""
    def __init__(self, path, reason):
        self.path, self.reason = path, reason
        super().__init__(f'HTTP 422: {path}: {reason}')


VALIDATION_REASONS = frozenset((
    'Geçersiz JSON', 'Nesne bekleniyor', 'Desteklenmeyen Skill alanı', 'Desteklenmeyen Skill sürümü',
    'Tam olarak bir maç gerekli', 'research_time gerekli', 'Bilinmeyen doğrulama durumu',
    'Kaynak URL gerekli', 'Kaynak erişim zamanı veya checked_at gerekli', 'Yayın tarihi metin olmalı',
    'Yayın tarihi araştırma tarihinden sonraya ait', 'Aynı kaynak için çelişkili metadata',
    'Doğrulama iddiası kaynak gerektirir', 'Bağımsız doğrulama en az iki bağımsız kaynak bildirimi gerektirir',
    'Çelişki için iki kaynak gerekli', 'Desteklenmeyen bulgu kategorisi', 'lineups nesne olmalı',
    'Dönüştürülen sonuç boyut sınırını aşıyor', 'Doğrulanmış sonuç boyut sınırını aşıyor',
    'Dönüştürülmüş alan tür, tarih, uzunluk veya kanıt kurallarına uymuyor',
    'Gizli bilgi alanlarına izin verilmez', 'Gizli bilgi içeren metin reddedildi',
    'Kimlik bilgisi içeren URL reddedildi', 'Girdi çok derin', 'Geçersiz tür, tarih veya alan değeri'))
SAFE_PATH_FIELDS = frozenset(('converted','schema_version','mode','research_time','timezone','matches',
    'match_id','competition','home_team','away_team','kickoff','venue','fixture_verification','model_prior',
    'findings','form','lineups','lineup_surprises','key_developments','not_found','limitations','id','team',
    'category','player','claim','sources','independent_source_count','status','conflict_note','qualitative_impact',
    'checked_at','url','publisher','title','tier','published_at','origin','kind','accessed_at','job_id',
    'input_version','home','away','type','players','source','statement','verification_status','source_ids',
    'observed_at','researched_at','published_date','skill_details','<alan>'))


def load_skill_json(stream):
    """Read raw Skill JSON from a caller-selected stream; no path is assumed."""
    try:
        data = stream.read(131073)
        if not isinstance(data, (str, bytes)) or len(data.encode('utf-8') if isinstance(data, str) else data) > 131072:
            raise PilotError('Skill JSON exceeds 128 KiB or is not text')
        value = json.loads(data)
    except PilotError:
        raise
    except (ValueError, TypeError, UnicodeError):
        raise PilotError('Skill JSON is invalid') from None
    if not isinstance(value, dict):
        raise PilotError('Skill JSON must be an object')
    return value


def secure_http_client(base_url):
    """Construct a verified HTTPS client; never put credentials into the URL."""
    try:
        url = urlsplit(base_url)
        valid = (url.scheme == 'https' and bool(url.hostname) and not url.username
                 and not url.password and url.path in ('', '/') and not url.query and not url.fragment)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise PilotError('A credential-free HTTPS origin is required')
    try:
        import httpx
    except ModuleNotFoundError:
        import httpx2 as httpx
    return httpx.Client(base_url=base_url, verify=True, follow_redirects=False,
                        trust_env=False, timeout=15)


def _time(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.tzinfo is not None else None
    except ValueError:
        return None


class ResearchPilotClient:
    """The provided client must use HTTPS, verify TLS and disable redirects.

    Keep this object alive across claim, optional renewal, and submission. API
    credentials and leases are retained only in memory and never represented.
    """
    def __init__(self, http, api_key, job_id):
        if not isinstance(api_key, str) or not api_key or not isinstance(job_id, str) or not job_id:
            raise PilotError('Service identity and job ID are required')
        self._http = http
        self._api_key = api_key
        self._job_id = job_id
        self._path = '/api/research/jobs/' + quote(job_id, safe='')
        self._lease = None
        self._job_verified = False
        self._expiry = None
        self._pending = None
        self._context = None
        self._rejected = False

    def __repr__(self):
        return '<ResearchPilotClient credentials=redacted>'

    def _headers(self):
        return {'Authorization': 'Bearer ' + self._api_key}

    @staticmethod
    def list_jobs(http, api_key):
        try:
            response = http.get('/api/research/jobs', params={'limit':100}, headers={'Authorization':'Bearer '+api_key})
        except Exception:
            raise PilotError('Unable to list jobs') from None
        if response.status_code != 200:
            raise PilotError(f'Job list failed with HTTP {response.status_code}')
        jobs = ResearchPilotClient._json(response).get('jobs')
        if not isinstance(jobs, list): raise PilotError('Invalid job list')
        return jobs

    def fetch_context(self, match_id):
        try:
            response = self._http.get('/api/research/matches/'+quote(match_id,safe='')+'/context',
                                      params={'job_id':self._job_id},headers=self._headers())
        except Exception:
            raise PilotError('Unable to fetch context') from None
        if response.status_code != 200:
            raise PilotError(f'Context failed with HTTP {response.status_code}')
        data = self._json(response)
        job, context = data.get('job'), data.get('context')
        if (not isinstance(job,dict) or job.get('id') != self._job_id or job.get('match_id') != match_id
                or not isinstance(context,dict) or not isinstance(context.get('match'),dict)
                or context['match'].get('id') != match_id):
            raise PilotError('Context does not match the assigned job')
        self._context = deepcopy(data)
        return deepcopy(data)

    def report(self, report_id):
        if not isinstance(report_id,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', report_id):
            raise PilotError('Invalid report ID')
        try:
            response = self._http.get('/api/research/reports/'+quote(report_id,safe=''),headers=self._headers())
        except Exception:
            raise PilotError('Unable to fetch report') from None
        if response.status_code != 200:
            raise PilotError(f'Report failed with HTTP {response.status_code}')
        return self._json(response)

    def context_warnings(self):
        if not self._context: raise PilotError('Fetch assigned match context first')
        context = self._context['context']
        prices = context.get('prices') if isinstance(context.get('prices'),dict) else {}
        at = _time(prices.get('observed_at'))
        now = datetime.now(timezone.utc)
        warnings = []
        if not at or at > now or now-at > timedelta(hours=2):
            warnings.append('Oran gözlem zamanı eksik, gelecekte veya 2 saatten eski; güncel sayılmamalı.')
        else:
            warnings.append('Gözlem zamanı tek başına oranların güncelliğini kanıtlamaz; provenance kontrol edilmeli.')
        if context.get('prediction_snapshot') is None:
            warnings.append('Bağımsız model snapshotı yok; tahmin uydurulmamalı.')
        return warnings

    def _check_research(self, raw):
        if not self._context: raise PilotError('Fetch assigned match context before submitting')
        job, match = self._context['job'], self._context['context']['match']
        now = datetime.now(timezone.utc)
        kickoff, deadline, created, researched = (_time(match.get('kickoff')), _time(job.get('deadline')),
                                                   _time(job.get('created_at')), _time(raw.get('research_time')))
        matches = raw.get('matches')
        if not isinstance(matches,list) or len(matches)!=1 or not isinstance(matches[0],dict) or matches[0].get('match_id') != job.get('match_id'):
            raise PilotError('Skill match does not match the assigned job')
        if raw.get('mode',job.get('mode')) != job.get('mode'):
            raise PilotError('Skill mode does not match the assigned job')
        if (not kickoff or not deadline or not created or not researched or now >= kickoff or now >= deadline
                or researched >= kickoff or researched > now+timedelta(minutes=5)
                or researched < created-timedelta(minutes=5)):
            raise PilotError('Research or job is outside its permitted pre-match period')
        if match.get('completed') or any(s in str(match.get('status') or '').lower() for s in ('postpon','cancel','abandon','suspend')):
            raise PilotError('The match is not eligible')

    def prepare_skill(self, stream):
        """Read caller-selected Skill output and validate timing before claiming."""
        raw = load_skill_json(stream)
        self._check_research(raw)
        return raw

    @staticmethod
    def _json(response):
        try:
            value = response.json()
        except (ValueError, TypeError):
            raise PilotError('Invalid API response') from None
        if not isinstance(value, dict):
            raise PilotError('Invalid API response')
        return value

    def _accept_lease(self, response):
        try:
            data = self._json(response)
        except PilotError:
            self._lease = None
            self._job_verified = False
            raise RecoveryRequired('Claim outcome uncertain; check the job and wait for lease expiry') from None
        token = data.get('claim_token')
        if not isinstance(token, str) or not token:
            self._lease = None
            self._job_verified = False
            raise RecoveryRequired('Claim outcome uncertain; check the job and wait for lease expiry')
        # Retain the token before touching metadata: optional fields must not lose it.
        self._lease = token
        self._job_verified = False
        job = data.get('job')
        if not isinstance(job, dict) or job.get('id') != self._job_id:
            raise PilotError('Claim response job is missing or mismatched; retain this process and stop')
        self._job_verified = True
        self._expiry = _time(job.get('lease_expires_at'))
        return {'lease_expires_at': self._expiry.isoformat() if self._expiry else None}

    def claim(self):
        if self._lease is not None:
            raise PilotError('A lease is already held in this process')
        try:
            response = self._http.post(self._path + '/claim', headers=self._headers())
        except Exception:
            raise RecoveryRequired('Claim outcome uncertain; check the job and wait for lease expiry') from None
        if response.status_code >= 500:
            raise RecoveryRequired('Claim outcome uncertain; check the job and wait for lease expiry')
        if response.status_code != 200:
            raise PilotError(f'Claim failed with HTTP {response.status_code}')
        try:
            return self._accept_lease(response)
        except PilotError:
            raise
        except Exception:
            raise RecoveryRequired('Claim outcome uncertain; check the job and wait for lease expiry') from None

    def renew(self):
        if not self._lease or not self._job_verified:
            raise PilotError('A verified in-memory lease is required')
        headers = self._headers() | {'X-Research-Claim': self._lease}
        # Renewal rotates the token. On a lost response the old token may be invalid.
        self._lease = None
        self._job_verified = False
        try:
            response = self._http.post(self._path + '/renew', headers=headers)
        except Exception:
            raise RecoveryRequired('Renewal outcome uncertain; check the job and wait for lease expiry') from None
        if response.status_code != 200:
            raise RecoveryRequired(f'Renewal failed with HTTP {response.status_code}; check the job before reclaiming')
        try:
            return self._accept_lease(response)
        except PilotError:
            raise
        except Exception:
            raise RecoveryRequired('Renewal outcome uncertain; check the job and wait for lease expiry') from None

    def reclaim_after_loss(self, now=None):
        """Explicit operator action only; never reopen a job or infer a missing expiry."""
        if self._lease:
            raise PilotError('A lease is still held in this process')
        try:
            response = self._http.get('/api/research/jobs', params={'limit':100}, headers=self._headers())
        except Exception:
            raise PilotError('Unable to check job state') from None
        if response.status_code != 200:
            raise PilotError(f'Job check failed with HTTP {response.status_code}')
        jobs = self._json(response).get('jobs')
        if not isinstance(jobs, list):
            raise PilotError('Invalid job list')
        job = next((item for item in jobs if isinstance(item, dict) and item.get('id') == self._job_id), None)
        if not job or job.get('status') not in ('claimed', 'queued'):
            raise PilotError('Job is unavailable or closed')
        at = now or datetime.now(timezone.utc)
        deadline = _time(job.get('deadline'))
        if not deadline or at >= deadline:
            raise PilotError('Job deadline has passed or is unknown')
        expiry = _time(job.get('lease_expires_at'))
        if job.get('status') == 'claimed' and (not expiry or at < expiry):
            raise PilotError('Lease is still active or expiry is unknown')
        return self.claim()

    def submit(self, raw_skill_json, idempotency_key, skill_version):
        if not self._lease or not self._job_verified:
            raise PilotError('A verified in-memory lease is required')
        if self._pending is not None:
            raise PilotError('Use retry_submission for the existing request')
        if not isinstance(raw_skill_json, dict) or not idempotency_key or not skill_version:
            raise PilotError('Skill JSON, idempotency key and version are required')
        try:
            encoded = json.dumps(raw_skill_json, ensure_ascii=False, allow_nan=False).encode('utf-8')
        except (ValueError, TypeError, UnicodeError):
            raise PilotError('Skill JSON is invalid') from None
        if len(encoded) > 131072: raise PilotError('Skill JSON exceeds 128 KiB')
        self._check_research(raw_skill_json)
        self._pending = (deepcopy(raw_skill_json), idempotency_key, skill_version)
        self._rejected = False
        return self.retry_submission()

    def discard_rejected(self):
        """Only a definitive 422 can be replaced; never discard an uncertain request."""
        if not self._rejected: raise PilotError('No definitively rejected request to discard')
        self._pending = None
        self._rejected = False

    def retry_submission(self):
        if not self._pending or not self._lease or not self._job_verified:
            raise PilotError('No retryable in-memory submission')
        body, idem, version = self._pending
        if self._rejected: raise PilotError('Fix or discard the rejected request before retrying')
        self._check_research(body)
        headers = self._headers() | {'X-Research-Claim': self._lease,
                   'Idempotency-Key': idem, 'X-Skill-Version': version}
        try:
            response = self._http.post(self._path + '/skill-results', json=body, headers=headers)
        except Exception:
            raise SubmissionUncertain('Result may have been saved; retry the same in-memory request') from None
        if response.status_code >= 500:
            raise SubmissionUncertain('Result may have been saved; retry the same in-memory request')
        if response.status_code == 422:
            self._rejected = True
            detail = self._json(response).get('detail')
            if not isinstance(detail,dict): detail = {}
            path, reason = detail.get('path'), detail.get('reason')
            if not isinstance(path,str) or not re.fullmatch(r'\$(?:\.[A-Za-z_][A-Za-z_0-9]*|\.<alan>|\[\d+\])*',path) or len(path)>300:
                path = '$'
            elif any(field not in SAFE_PATH_FIELDS for field in re.findall(r'\.([A-Za-z_][A-Za-z_0-9]*|<alan>)',path)):
                path = '$'
            if not isinstance(reason,str) or reason not in VALIDATION_REASONS: reason = 'Doğrulama reddedildi'
            raise ValidationRejected(path,reason)
        if response.status_code != 200:
            raise PilotError(f'Result submission failed with HTTP {response.status_code}; in-memory request retained')
        result = self._json(response)
        # No raw response or credentials are printed; caller decides how to handle IDs.
        return {k: result.get(k) for k in ('report_id', 'revision', 'duplicate', 'freshness_status', 'validation_status')}
