import unittest
from datetime import datetime,timedelta,timezone
from app.model.decision_quality import apply

class V3Recommendations(unittest.TestCase):
    def test_valid_low_probability_choice_remains_explicit_best_available(self):
        now=datetime(2026,11,9,12,tzinfo=timezone.utc)
        p={'kickoff':(now+timedelta(hours=5)).isoformat(),'iddaa_updated_at':now.isoformat(),
           'iddaa_odds':{'1':1.9,'2':2.1},'markets':{'1':{'p':.51},'2':{'p':.49}},'data_n':20}
        result=apply(p,now)
        self.assertEqual(result['pick']['market'],'1')
        self.assertFalse(result['pick']['target_met'])
        self.assertEqual(result['pick']['selection_tier'],'best_available')
        p['iddaa_updated_at']=(now-timedelta(hours=4)).isoformat()
        self.assertIsNone(apply(p,now)['pick'])

    def test_bad_model_history_uses_verified_complete_market_group_without_model_confidence(self):
        from app.model.predict import markets,score_matrix
        from app.model.coupons import _choices,select_banko
        now=datetime.now(timezone.utc)
        p={'id':'one','league':'eng.1','home':'A','away':'B','kickoff':(now+timedelta(hours=5)).isoformat(),
           'iddaa_updated_at':now.isoformat(),'iddaa_odds':{'1':1.5,'X':4.,'2':5.},
           'markets':markets(score_matrix(2.,1.))['markets'],'data_n':20,
           'data_integrity':{'history_complete':False}}
        result=apply(p,now)
        self.assertEqual(result['pick']['probability_source'],'iddaa_market')
        self.assertIsNone(result['confidence'])
        choices=_choices(result,p['iddaa_odds'])
        self.assertTrue(choices)
        picks=select_banko([(result,choices)])
        self.assertTrue(picks)
        self.assertEqual(picks[0]['probability_source'],'iddaa_market')
        self.assertIsNone(picks[0]['confidence'])
        p['iddaa_updated_at']=(now-timedelta(hours=4)).isoformat()
        self.assertIsNone(apply(p,now)['pick'])

    def test_failed_poll_does_not_erase_still_valid_observed_quote(self):
        from app import quote_provenance as Q
        from app.match_state import describe
        now=datetime.now(timezone.utc)
        observed=(now-timedelta(minutes=65)).isoformat()
        p={'kickoff':(now+timedelta(hours=3)).isoformat(),'odds_current':True,'iddaa_odds':{'1':1.8},
           'quote_provenance':{'last_fetch_status':'failed','quotes':{'1':{'quote_observed_at':observed,'cache_status':'network_no_cache_evidence','odd':1.8}}}}
        self.assertTrue(Q.fresh(p,'1',now))
        self.assertEqual(describe(p,now)['price_status'],'recent_observation')
        self.assertFalse(Q.fresh(p,'1',now+timedelta(hours=2)))
        p['quote_provenance']['quotes']['1']['cache_status']='shared_cache'
        self.assertFalse(Q.fresh(p,'1',now))
