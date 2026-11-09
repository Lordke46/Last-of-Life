import unittest
from app.live import _parse_event

class LiveScoreParsing(unittest.TestCase):
    def event(self,h,a):
        return {'id':'m','competitions':[{'competitors':[
            {'homeAway':'home','team':{'displayName':'A'},'score':h},
            {'homeAway':'away','team':{'displayName':'B'},'score':a}]}],
                'status':{'type':{'state':'post','shortDetail':'FT'}}}

    def test_missing_or_invalid_scores_are_not_fabricated_zeroes(self):
        for h,a in ((None,None),('',0),('bad',1),(-1,2),(True,1)):
            self.assertIsNone(_parse_event(self.event(h,a),'eng.1'))

    def test_string_zero_and_provider_score_objects_are_valid(self):
        score=_parse_event(self.event('0',{'value':2}),'eng.1')
        self.assertEqual((score['home_goals'],score['away_goals'],score['state']),(0,2,'post'))

    def test_provider_minute_suffix_is_not_duplicated(self):
        event=self.event('1','0')
        event['status']={'displayClock':"50'",'type':{'state':'in'}}
        self.assertEqual(_parse_event(event,'all')['minute'],"50'")
