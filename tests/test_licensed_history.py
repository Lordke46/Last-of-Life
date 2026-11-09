import json,unittest
from datetime import datetime,timedelta,timezone
from app.model.licensed_history import assess

class LicensedHistoryTests(unittest.TestCase):
    def data(self):
        start=datetime(2025,1,1,tzinfo=timezone.utc)
        rows=[]
        for i in range(160):
            h,a=('Alpha','Beta') if i%2 else ('Beta','Alpha')
            rows.append({'id':str(i),'kickoff':(start+timedelta(days=i)).date().isoformat(),
                         'home':h,'away':a,'home_goals':2,'away_goals':1,'completed':True})
        return json.dumps({'rows':rows,'source':'CC0 fixture','license':'CC0-1.0','season':'2024-25','imported_at':'2025-06-10T00:00:00Z'})

    def test_fresh_identified_history_produces_separate_provisional_estimates(self):
        out=assess({'home':'Alpha','away':'Beta'},self.data(),datetime(2025,6,15,tzinfo=timezone.utc))
        self.assertTrue(out['available'])
        self.assertFalse(out['affects_recommendation'])
        self.assertAlmostEqual(sum(out['markets'][k]['p'] for k in ('1','X','2')),1,places=3)
        self.assertEqual(out['confidence_status'],'not_validated')

    def test_old_or_unknown_history_does_not_invent_model_confidence(self):
        event={'home':'Alpha','away':'Beta'}
        out=assess(event,self.data(),datetime(2026,11,9,tzinfo=timezone.utc))
        self.assertFalse(out['available']);self.assertEqual(out['reason'],'team_history_stale')
        out=assess({'home':'Unknown','away':'Beta'},self.data(),datetime(2025,6,15,tzinfo=timezone.utc))
        self.assertEqual(out['reason'],'team_identity_unverified')

    def test_same_day_future_results_do_not_enter_history(self):
        out=assess({'home':'Alpha','away':'Beta'},self.data(),datetime(2025,4,1,12,tzinfo=timezone.utc))
        self.assertEqual(out['history_matches'],90)
        self.assertFalse(out['available'])
