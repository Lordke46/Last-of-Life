"""Regression tests for learning cutoffs, fallback, coherent mixtures and quote eligibility."""
import copy
import math
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from app.model.fitted import FittedGoals
from app.model import ensemble,adaptive_market,blend,calibration,decision_quality
from tests.test_prediction_audit import quote_prediction,NOW


def history():
    start=datetime(2024,1,1,tzinfo=timezone.utc)
    rows=[]
    for i in range(160):
        h,a=(('A','B') if i%2 else ('B','A'))
        at=start+timedelta(days=i)
        rows.append({'home':h,'away':a,'home_goals':3 if h=='A' else i%2,
                     'away_goals':2 if a=='A' else 0,'ts':at,'kickoff':at.isoformat(),'completed':1})
    return rows


class LearnedModelTest(unittest.TestCase):
    def test_fit_learns_strength_and_ignores_future_in_progress_and_void_results(self):
        rows=history();at=rows[-1]['ts']+timedelta(days=1)
        fit=FittedGoals(rows,at)
        changed=rows+[dict(rows[-1],ts=at,home_goals=99),dict(rows[-1],completed=0,home_goals=99),
                      dict(rows[-1],status='Cancelled',home_goals=99)]
        other=FittedGoals(changed,at)
        self.assertTrue(fit.converged)
        self.assertEqual(fit.params,other.params)
        self.assertTrue(fit.available('A','B'))
        self.assertFalse(fit.available('A','unknown'))
        self.assertGreater(fit.rates('A','B')[0],fit.rates('A','B')[1])
        mat=fit.matrix('A','B')
        self.assertAlmostEqual(sum(map(sum,mat)),1)
        self.assertGreaterEqual(min(map(min,mat)),0)

    def test_selector_requires_many_independent_blocks(self):
        lucky=[(.3,{.5:.2,1.:.1},'one-week') for _ in range(200)]
        self.assertEqual(ensemble.select(lucky)['fitted_weight'],0)
        supported=[(.3,{.5:.2,1.:.1},i//8) for i in range(160)]
        self.assertEqual(ensemble.select(supported)['fitted_weight'],1)
        losing=[(.1,{.5:.2,1.:.3},i//8) for i in range(160)]
        self.assertEqual(ensemble.select(losing)['fitted_weight'],0)

    def test_market_selector_rejects_training_winner_that_fails_later(self):
        start=datetime(2024,1,1,tzinfo=timezone.utc)
        rows=[]
        for i in range(180):
            rows.append({'at':(start+timedelta(days=i)).isoformat(),
                         'model':[.9,.1],'market':[.1,.9],
                         'actual':[1,0] if i<117 else [0,1]})
        choice=adaptive_market.choose(rows)
        self.assertEqual(choice['candidate_weight'],0)
        self.assertEqual(choice['weight'],.5)
        for r in rows:r['actual']=[0,1]
        self.assertEqual(adaptive_market.choose(rows)['weight'],1)

    def test_snapshot_weight_training_excludes_hindsight_and_future_results(self):
        import sqlite3,json
        from app import db
        from app.model.snapshots import SCHEMA
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
        self.addCleanup(conn.close);conn.executescript(db.SCHEMA);conn.executescript(SCHEMA)
        kickoff=(NOW-timedelta(days=1)).isoformat()
        payload=json.dumps({'model':{'1':.5,'X':.2,'2':.3},'market':{'1':.6,'X':.2,'2':.2}})
        for mid,captured,status,actual in (
            ('good',(NOW-timedelta(days=2)).isoformat(),'Final',kickoff),
            ('late',NOW.isoformat(),'Final',kickoff),
            ('void',(NOW-timedelta(days=2)).isoformat(),'Cancelled',kickoff),
            ('future',(NOW-timedelta(days=2)).isoformat(),'Final',(NOW+timedelta(days=1)).isoformat())):
            conn.execute('INSERT INTO matches(id,league,kickoff,home,away,completed,home_goals,away_goals,status) VALUES(?,?,?,?,?,1,2,0,?)',
                         (mid,'eng.1',actual,'a','b',status))
            conn.execute('INSERT INTO prediction_snapshots(match_id,league,kickoff,captured_at,model_version,parameters,payload) VALUES(?,?,?,?,?,?,?)',
                         (mid,'eng.1',kickoff,captured,'v','{}',payload))
        with patch.object(adaptive_market,'choose',return_value=None) as choose:
            adaptive_market.load(conn,NOW)
        self.assertEqual(len(choose.call_args_list),2)  # league + pool, one valid event each
        self.assertTrue(all(len(call.args[0])==1 for call in choose.call_args_list))

    def test_group_weights_preserve_complements_and_double_chance(self):
        from app.model.predict import markets,score_matrix
        mk=markets(score_matrix(1.8,.9))['markets']
        odds={'1':2.0,'X':3.5,'2':4.0,'Ü2.5':1.7,'A2.5':2.1}
        out,_=blend.blend_markets(mk,odds,group_weights={'1x2':1,'ou2.5':0})
        self.assertAlmostEqual(sum(out[k]['p'] for k in ('1','X','2')),1,places=3)
        self.assertAlmostEqual(out['1X']['p'],out['1']['p']+out['X']['p'],places=3)
        self.assertEqual(out['Ü2.5']['p'],mk['Ü2.5']['p'])
        self.assertAlmostEqual(out['1']['p'],blend.devig([2.,3.5,4.])[0],places=3)

    def test_pooled_calibration_needs_more_samples_and_discloses_scope(self):
        key=('*','A3.5',6)
        stats={key:{'n':99,'wins':90,'predicted_sum':60}}
        self.assertFalse(calibration.calibrate(.65,'tur.1','A3.5',stats)['applied'])
        stats[key]={'n':100,'wins':80,'predicted_sum':65}
        out=calibration.calibrate(.65,'tur.1','A3.5',stats)
        self.assertTrue(out['applied']);self.assertEqual(out['scope'],'pooled_leagues')
        self.assertLess(out['p']-.65,.08)

    def test_strongest_option_uses_real_fresh_odds_and_cannot_invent_high_confidence(self):
        p=quote_prediction()
        p['markets']={'A3.5':{'p':.65},'Ü1.5':{'p':.82}}
        p['iddaa_odds']={'A3.5':1.65,'Ü1.5':1.20}
        result=decision_quality.apply(p,NOW)
        self.assertEqual(result['pick']['market'],'A3.5')
        self.assertEqual(result['strongest_pick']['market'],'Ü1.5')
        self.assertNotEqual(result['strongest_pick']['confidence_label'],'Yüksek')
        p['iddaa_updated_at']=(NOW-timedelta(hours=4)).isoformat()
        self.assertIsNone(decision_quality.apply(p,NOW)['strongest_pick'])

    def test_new_family_does_not_borrow_legacy_calibration(self):
        stats={('*','A3.5',6):{'n':100,'wins':90,'predicted_sum':65}}
        self.assertFalse(calibration.calibrate(.65,'tur.1','A3.5',stats,family='fitted')['applied'])
        stats[('fitted:*','A3.5',6)]=stats[('*','A3.5',6)]
        self.assertTrue(calibration.calibrate(.65,'tur.1','A3.5',stats,family='fitted')['applied'])

    def test_incoherent_goal_threshold_blends_fall_back_to_coherent_model(self):
        from app.model.predict import markets,score_matrix
        mk=markets(score_matrix(2.2,1.8))['markets']
        odds={'Ü1.5':1.9,'A1.5':1.9,'Ü2.5':1.6,'A2.5':2.3}
        out,groups=blend.blend_markets(mk,odds,group_weights={'ou1.5':1,'ou2.5':0})
        self.assertGreaterEqual(out['Ü1.5']['p'],out['Ü2.5']['p'])
        self.assertEqual(out['Ü1.5']['blend_rejected_reason'],'incoherent_goal_thresholds')
        self.assertNotIn('ou1.5',groups)

    def test_main_pick_and_coupon_use_same_calibrated_probability(self):
        from app.model.coupons import _choices
        p=quote_prediction();p['markets']={'A3.5':{'p':.65,'suggested':1.5}}
        p['calibration_by_market']={'A3.5':{'p':.60,'n':40,'applied':True,'scope':'league'}}
        result=decision_quality.apply(p,NOW)
        self.assertEqual(result['pick']['p'],.60)
        self.assertEqual(result['pick']['raw_p'],.65)
        with patch('app.model.coupons._odds_fresh',return_value=True):
            self.assertEqual(_choices(p,p['iddaa_odds'])[0]['p'],result['pick']['p'])

    def test_new_goal_model_cannot_inherit_legacy_market_weight_validation(self):
        selections={('*','1x2'):{'weight':1}}
        self.assertEqual(adaptive_market.for_prediction({'ensemble':{'fitted_weight':.5}},selections),{})
        self.assertEqual(adaptive_market.for_prediction({'league':'tur.1'},selections)['1x2']['weight'],1)

if __name__=='__main__':unittest.main()
