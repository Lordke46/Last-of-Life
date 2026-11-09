import copy
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

from app import research_service as service
from app.grok_skill_adapter import adapt, STATUS
from tests import test_research_api as support
from fastapi import HTTPException


class SkillAdapterTest(unittest.TestCase):
    setUp = support.ResearchApiTest.setUp
    headers = support.ResearchApiTest.headers
    job = support.ResearchApiTest.job
    claim = support.ResearchApiTest.claim

    def skill(self, job):
        stamp = service.now().isoformat()
        source = {'url':'https://example.com/club','publisher':'Kulüp', 'tier':1,
                  'published_at':'09/11/2026','origin':'Kulüp açıklaması'}
        return {'schema_version':'1.0','mode':job['mode'],'research_time':stamp,'timezone':'Europe/Istanbul',
                'matches':[{'match_id':job['match_id'],'competition':'Örnek Lig','home_team':'Home','away_team':'Away',
                 'kickoff':self.kickoff,'venue':'Örnek', 'fixture_verification':{'status':'unverified','sources':[]},
                 'findings':[{'id':'F1','team':'Away','category':'injury','player':'Oyuncu',
                    'claim':'Durum bildirimi','sources':[source], 'independent_source_count':1,
                    'status':'confirmed_official','conflict_note':None,'qualitative_impact':'Belirsizlik var','checked_at':stamp}],
                 'model_prior':[{'team':'Home','claim':'Önceki değerlendirme', 'source':'model_knowledge',
                                 'comparison':'consistent','related_finding_ids':['F1']}],
                 'form':{'home':{'last5':[{'date':'2026-11-06','source':'https://example.com/form'}]}},
                 'lineups':{'home':{'type':'probable','players':['Oyuncu 1'],'source':'https://example.com/lineup'},
                            'away':{'type':'not_found','players':[],'source':''}},
                 'lineup_surprises':[],'key_developments':['F1'],'not_found':['Resmi kadro'],
                 'limitations':['Resmi kadro açıklanmadı']} ]}

    def submit(self, job, lease, payload, idem='skill-1'):
        return self.client.post('/api/research/jobs/'+job['id']+'/skill-results',json=payload,
             headers=self.headers(**{'X-Research-Claim':lease,'Idempotency-Key':idem,'X-Skill-Version':'1.0'}))

    def test_full_sample_preserves_source_date_provenance_and_model_prior(self):
        job=self.job(); lease=self.claim(job); raw=self.skill(job)
        raw['matches'][0]['findings'][0]['sources'].append(
            {'url':'https://example.com/agency','publisher':'Örnek Ajans','tier':2,
             'published_at':'2026-11-09','origin':'Kulüp açıklaması'})
        raw['matches'][0]['form']['away']={'last5':[],'last_match_date':'2026-11-07','rest_days':3,
                                           'next_match':{'date':'2026-11-15','opponent':'Sample Town'}}
        raw['matches'][0]['model_prior'][0]['comparison']='disagree'
        result=self.submit(job,lease,raw)
        self.assertEqual(result.status_code,200,result.text)
        report=self.client.get('/api/research/reports/'+result.json()['report_id'],headers=self.headers()).json()['report']
        self.assertNotIn('matches',report)
        self.assertNotIn('research_time',report)
        self.assertEqual(report['sources'][0]['published_date'],'2026-11-09')
        self.assertIsNone(report['sources'][0]['published_at'])
        self.assertEqual(report['sources'][0]['origin'],'Kulüp açıklaması')
        self.assertEqual(report['sources'][0]['tier'],1)
        first=report['findings'][0]
        self.assertEqual((first['verification_status'],first['original_status']),('reported','confirmed_official'))
        self.assertEqual(first['qualitative_impact'],'Belirsizlik var')
        self.assertEqual(report['lineup_assessment']['lineup_status'],'predicted')
        self.assertTrue(any(f.get('skill_details',{}).get('field')=='model_prior' for f in report['findings'] if f.get('skill_details')))
        prior=next(f for f in report['findings'] if f.get('skill_details',{}).get('field')=='model_prior')
        self.assertEqual(prior['skill_details']['value'][0]['comparison'],'disagree')
        self.assertEqual(prior['verification_status'],'unknown')
        self.assertTrue(self.submit(job,lease,raw).json()['duplicate'])

    def test_lineup_check_and_unknown_official_remain_distinct(self):
        job=self.job(mode='lineup_check'); raw=self.skill(job)
        self.assertEqual(adapt(raw,job,'1').mode,'lineup_check')
        raw['matches'][0]['lineups']['home']['type']='official'
        out=adapt(raw,job,'1')
        self.assertEqual(out.lineup_assessment.lineup_status,'unknown')
        self.assertEqual(next(f for f in out.findings if f.skill_details and f.skill_details.get('field')=='lineups').skill_details['value']['home']['type'],'official')
        self.assertTrue(out.limitations)
        raw['matches'][0]['findings'][0]['sources'][0]['kind']='club'
        lineup_finding=copy.deepcopy(raw['matches'][0]['findings'][0])
        lineup_finding.update(id='F-lineup',category='lineup',claim='Kadro açıklandı')
        raw['matches'][0]['findings'].append(lineup_finding)
        raw['matches'][0]['lineups']={'home':{'type':'official','source':'https://example.com/club'},
                                       'away':{'type':'official','source':'https://example.com/club'}}
        self.assertEqual(adapt(raw,job,'1').lineup_assessment.lineup_status,'official')

    def test_all_statuses_and_no_invented_evidence(self):
        job=self.job()
        for original, mapped in STATUS.items():
            raw=self.skill(job); finding=raw['matches'][0]['findings'][0]; finding['status']=original
            if original == 'confirmed_official': finding['sources'][0]['kind']='club'
            if original in ('confirmed_independent','conflicting'):
                finding['sources'].append({'url':'https://example.com/independent','publisher':'Bağımsız','tier':2})
                finding['independent_source_count']=2
            with self.subTest(original=original):
                result=adapt(raw,job,'1').findings[0]
                self.assertEqual((result.original_status,result.verification_status),(original,mapped))
        raw=self.skill(job); raw['matches'][0]['findings'][0]['sources']=[]
        with self.assertRaises(HTTPException):adapt(raw,job,'1')

    def test_source_deduplication_and_origin_relationships(self):
        job=self.job(); raw=self.skill(job)
        another=copy.deepcopy(raw['matches'][0]['findings'][0]);another['id']='F2';another['claim']='İkinci bulgu'
        another['sources'][0]['origin']='Diğer referans'
        raw['matches'][0]['findings'][0]['sources'][0]['published_at']=None
        raw['matches'][0]['findings'].append(another)
        result=adapt(raw,job,'1')
        self.assertEqual(len(result.sources),1)
        self.assertEqual(result.sources[0].published_date,'2026-11-09')
        self.assertIsNone(result.findings[0].skill_details['source_observations'][0]['published_date'])
        self.assertEqual(result.findings[0].source_ids,result.findings[1].source_ids)
        self.assertEqual(result.findings[1].skill_details['source_observations'][0]['origin'],'Diğer referans')

    def test_adapter_does_not_mutate_core_model_or_coupon_records(self):
        from app import db
        tables=['matches','news','iddaa_events','iddaa_odds','prediction_snapshots','saved_coupons']
        def snapshot():
            with db.session() as conn:
                return {t:[tuple(r) for r in conn.execute('SELECT * FROM '+t)] for t in tables}
        before=snapshot(); job=self.job(); response=self.submit(job,self.claim(job),self.skill(job))
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(snapshot(),before)

    def test_missing_times_dates_unknown_and_sensitive_fields(self):
        job=self.job(); raw=self.skill(job)
        raw['matches'][0]['findings'][0]['sources'][0]['published_at']=None
        self.assertIsNone(adapt(raw,job,'1').sources[0].published_at)
        raw['matches'][0]['findings'][0].pop('checked_at')
        with self.assertRaises(HTTPException):adapt(raw,job,'1')
        raw=self.skill(job);raw['matches'][0]['authorization']='Bearer sensitive-value'
        with self.assertRaises(HTTPException):adapt(raw,job,'1')
        raw=self.skill(job);raw['matches'][0]['findings'][0]['sources'][0]['url']='https://example.com/?token=sensitive'
        with self.assertRaises(HTTPException):adapt(raw,job,'1')
        raw=self.skill(job);raw['matches'][0]['findings'][0]['sources'][0]['published_at']='2099-01-01'
        with self.assertRaises(HTTPException):adapt(raw,job,'1')

    def test_wrong_match_job_version_and_multiple_matches_rejected(self):
        job=self.job(); lease=self.claim(job)
        for key,value in [('job_id','wrong'),('input_version','f'*64),('mode','lineup_check')]:
            raw=self.skill(job);raw[key]=value
            self.assertEqual(self.submit(job,lease,raw).status_code,409)
        raw=self.skill(job);raw['matches'][0]['match_id']='wrong'
        self.assertEqual(self.submit(job,lease,raw).status_code,409)
        raw=self.skill(job);raw['matches'].append(copy.deepcopy(raw['matches'][0]))
        self.assertEqual(self.submit(job,lease,raw).status_code,422)

    def test_renew_rotates_token_and_old_one_cannot_submit_or_renew(self):
        job=self.job(); old=self.claim(job)
        url='/api/research/jobs/'+job['id']+'/renew'
        response=self.client.post(url,headers=self.headers(**{'X-Research-Claim':old}))
        self.assertEqual(response.status_code,200,response.text)
        new=response.json()['claim_token']
        self.assertNotEqual(old,new)
        self.assertEqual(self.client.post(url,headers=self.headers(**{'X-Research-Claim':old})).status_code,409)
        raw=self.skill(job)
        self.assertEqual(self.submit(job,old,raw).status_code,409)
        self.assertEqual(self.submit(job,new,raw).status_code,200)
        self.assertEqual(self.submit(job,old,raw).status_code,409)
        self.assertTrue(self.submit(job,new,raw).json()['duplicate'])

    def test_renew_expired_lease_and_deadline(self):
        job=self.job(); lease=self.claim(job)
        url='/api/research/jobs/'+job['id']+'/renew'
        raw=self.skill(job)
        with patch('app.research_service.now',return_value=self.at+timedelta(minutes=21)):
            self.assertEqual(self.client.post(url,headers=self.headers(**{'X-Research-Claim':lease})).status_code,409)
            self.assertEqual(self.submit(job,lease,raw).status_code,409)
        with patch('app.research_service.now',return_value=self.at+timedelta(hours=3)):
            self.assertEqual(self.client.post(url,headers=self.headers(**{'X-Research-Claim':lease})).status_code,410)

    def test_simultaneous_renewals_only_one_token_is_valid(self):
        job=self.job(); lease=self.claim(job)
        principal={'id':'grok-a','scopes':list(service.SCOPES)}
        def renew(_):
            try: service.renew_claim(principal,job['id'],lease); return 200
            except HTTPException as exc:return exc.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(renew,range(2))),[200,409])


if __name__=='__main__': unittest.main()
