import json
import unittest
from unittest.mock import patch
from tests import test_research_api as support
from tests import test_grok_skill_adapter as skill_tests

class GrokDisplayTests(unittest.TestCase):
    setUp=support.ResearchApiTest.setUp
    headers=support.ResearchApiTest.headers
    job=support.ResearchApiTest.job
    claim=support.ResearchApiTest.claim
    skill=skill_tests.SkillAdapterTest.skill

    def test_requires_auth_and_is_disabled_by_default(self):
        with patch('app.auth.current_user',return_value=None),patch.dict('os.environ',{'RESEARCH_UI_ENABLED':'1','RESEARCH_UI_OWNER':'grok-a'}):
            self.assertEqual(self.client.get('/api/grok-intelligence/m1').status_code,401)
        with patch('app.auth.current_user',return_value={'id':'u','role':'admin'}),patch.dict('os.environ',{'RESEARCH_UI_ENABLED':'0'}):
            self.assertEqual(self.client.get('/api/grok-intelligence/m1').status_code,503)

    def test_reports_owned_and_empty_without_key_leak(self):
        job=self.job(); lease=self.claim(job)
        headers=self.headers(**{'X-Research-Claim':lease,'Idempotency-Key':'id','X-Skill-Version':'v1'})
        r=self.client.post('/api/research/jobs/'+job['id']+'/skill-results',json=self.skill(job),headers=headers)
        self.assertEqual(r.status_code,200,r.text)
        with patch('app.auth.current_user',return_value={'id':'u','role':'admin'}),patch.dict('os.environ',{'RESEARCH_UI_ENABLED':'1','RESEARCH_UI_OWNER':'grok-a'}):
            response=self.client.get('/api/grok-intelligence/m1');self.assertEqual(response.status_code,200)
            reports=response.json()['reports'];self.assertEqual(len(reports),1)
            self.assertEqual(reports[0]['report']['findings'][0]['original_status'],'confirmed_official')
            self.assertEqual(reports[0]['revision'],1);self.assertIn('no-store',response.headers['cache-control'])
            self.assertNotIn(support.KEY_A,response.text);self.assertNotIn(lease,response.text)
            self.assertEqual(self.client.get('/api/grok-intelligence/missing').json()['reports'],[])
        with patch('app.auth.current_user',return_value={'id':'u','role':'admin'}),patch.dict('os.environ',{'RESEARCH_UI_ENABLED':'1','RESEARCH_UI_OWNER':'grok-b'}):
            self.assertEqual(self.client.get('/api/grok-intelligence/m1').json()['reports'],[])

    def test_prior_revisions_are_available(self):
        for request_id in ('first','second'):
            job=self.job(request_id=request_id); lease=self.claim(job)
            response=self.client.post('/api/research/jobs/'+job['id']+'/skill-results',json=self.skill(job),
                headers=self.headers(**{'X-Research-Claim':lease,'Idempotency-Key':request_id,'X-Skill-Version':'v1'}))
            self.assertEqual(response.status_code,200,response.text)
        with patch('app.auth.current_user',return_value={'id':'u','role':'admin'}),patch.dict('os.environ',{'RESEARCH_UI_ENABLED':'1','RESEARCH_UI_OWNER':'grok-a'}):
            reports=self.client.get('/api/grok-intelligence/m1').json()['reports']
            self.assertEqual({r['revision'] for r in reports},{1,2})

    def test_real_admin_and_user_sessions_are_enforced_even_when_site_public(self):
        from app import auth
        job=self.job(); lease=self.claim(job)
        response=self.client.post('/api/research/jobs/'+job['id']+'/skill-results',json=self.skill(job),
            headers=self.headers(**{'X-Research-Claim':lease,'Idempotency-Key':'visibility','X-Skill-Version':'v1'}))
        report_id=response.json()['report_id']
        admin=auth.create_user('admin@example.test','synthetic-password-123',role='admin')
        user=auth.create_user('user@example.test','synthetic-password-123',role='user')
        with patch.dict('os.environ',{'AUTH_REQUIRED':'0','RESEARCH_UI_ENABLED':'1','RESEARCH_UI_OWNER':'grok-a'}):
            self.client.cookies.clear()
            self.assertEqual(self.client.get('/api/grok-intelligence/m1').status_code,401)
            self.client.cookies.set(auth.SESSION_COOKIE,auth.start_session(user))
            self.assertEqual(self.client.get('/api/grok-intelligence/m1').status_code,403)
            self.assertEqual(self.client.get('/api/grok-intelligence/'+report_id).status_code,403)
            self.assertEqual(self.client.get('/api/research/reports/'+report_id).status_code,401)
            self.client.cookies.set(auth.SESSION_COOKIE,auth.start_session(admin))
            self.assertEqual(len(self.client.get('/api/grok-intelligence/m1').json()['reports']),1)

if __name__=='__main__':unittest.main()
