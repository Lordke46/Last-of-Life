import unittest
from datetime import datetime,timedelta,timezone
from app.match_state import describe
from app import explain

NOW=datetime(2026,11,9,10,tzinfo=timezone.utc)

class MatchLifecycleTest(unittest.TestCase):
    def event(self,**values):
        return {'home':'A','away':'B','kickoff':(NOW+timedelta(hours=2)).isoformat(),
                'iddaa_odds':{'1':1.8},'iddaa_updated_at':NOW.isoformat(),**values}

    def test_start_time_does_not_imply_completion(self):
        state=describe(self.event(kickoff=NOW.isoformat()),NOW)
        self.assertEqual(state['phase'],'started_result_pending')
        self.assertEqual(state['price_status'],'historical')
        self.assertFalse(state['can_offer_prematch'])

    def test_future_removed_event_is_distinct_from_stale_current_event(self):
        removed=describe(self.event(bulletin_present=False),NOW)
        stale=describe(self.event(iddaa_updated_at=(NOW-timedelta(hours=4)).isoformat()),NOW)
        self.assertEqual(removed['price_status'],'removed_from_bulletin')
        self.assertEqual(stale['price_status'],'unverified_or_stale')

    def test_valid_current_fixture_remains_available(self):
        self.assertTrue(describe(self.event(),NOW)['can_offer_prematch'])

    def test_result_and_void_are_explicit(self):
        self.assertEqual(describe(self.event(completed=1),NOW)['phase'],'completed')
        self.assertEqual(describe(self.event(status='Postponed'),NOW)['phase'],'inactive')
