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
