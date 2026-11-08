"""Integrity, source timing, decision eligibility and leakage regressions."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from app import db, quote_provenance as Q, odds_history, updater
from app.model import engine, blend, predict as P, extra_markets, calibration, snapshots, decision_quality as D
from app.scrapers import iddaa, team_context

NOW=datetime(2026,11,8,12,tzinfo=timezone.utc)


def quote_prediction():
    return {'id':'m','league':'tur.1','kickoff':(NOW+timedelta(hours=6)).isoformat(),
            'iddaa_updated_at':NOW.isoformat(),'iddaa_odds':{'A3.5':1.8},'data_n':20,
            'data_integrity':{'history_complete':True,'source_fresh':True},
            'markets':{'A3.5':{'p':.5966,'model_p':.7105,'market_p':.4828,
                        **D.comparison({'model_p':.7105,'market_p':.4828})}}}


class DecisionAudit(unittest.TestCase):
    def test_transport_reports_cache_evidence_without_inventing_provider_time(self):
        import io
        response=io.BytesIO(json.dumps({'isSuccess':True,'data':{}}).encode())
        response.headers={'Age':'9000','X-Cache':'HIT'}
        with patch.object(iddaa.urllib.request,'urlopen',return_value=response):
            result=iddaa.fetch_json(retries=1)
        self.assertEqual(result['_transport']['cache_status'],'shared_cache')
        self.assertTrue(Q.timestamp(result['_transport']['quote_observed_at']))
        self.assertNotIn('provider_price_updated_at',result['_transport'])

    def test_large_gap_is_review_not_automatic_value(self):
        pred=quote_prediction()
        result=D.apply(pred,NOW)
        self.assertIsNone(result['pick'])
        self.assertEqual(result['decision']['status'],'no_bet')
        self.assertEqual(pred['markets']['A3.5']['model_market_gap_pp'],22.77)
        self.assertIn('uncalibrated_model_market_disagreement',result['decision']['rejected_markets']['A3.5'])
        with patch.dict(D.MODEL,{'disagreement_review_pp':25}):
            pred['markets']['A3.5'].update(D.comparison(pred['markets']['A3.5']))
            result=D.apply(pred,NOW)
            self.assertEqual(result['pick']['market'],'A3.5')
            self.assertEqual(result['calibration_status'],'insufficient_data')
            self.assertNotEqual(result['confidence_label'],'Yüksek')

    def test_non_suspicious_alternative_remains_available(self):
        pred=quote_prediction()
        pred['markets']['KG Yok']={'p':.60,'model_p':.62,'market_p':.58}
        pred['iddaa_odds']['KG Yok']=1.8
        result=D.apply(pred,NOW)
        self.assertEqual(result['pick']['market'],'KG Yok')
        self.assertEqual(result['decision']['policy'],'balanced_v2')

    def test_no_recommendation_after_start_or_result_or_missing_price(self):
        for overrides in ({'kickoff':NOW.isoformat()},{'completed':1},{'iddaa_odds':{}},
                          {'status':'Postponed'},{'odds_current':False},
                          {'data_integrity':{'history_complete':False}}):
            pred=quote_prediction();pred['markets']['A3.5']['disagreement_review_required']=False
            pred.update(overrides)
            self.assertEqual(D.apply(pred,NOW)['decision']['status'],'no_bet')

    def test_real_besiktas_handicap_source_settlement_and_de_vig(self):
        source=json.loads((Path(__file__).parent/'fixtures/besiktas_genclerbirligi_20261108_quotes.json').read_text())
        event=iddaa.parse_event(source['event'])
        self.assertEqual((event['home'],event['away']),('Beşiktaş','Gençlerbirliği'))
        self.assertEqual(datetime.fromisoformat(event['kickoff']).hour,18)
        odds=event['odds'];keys=[f'Handikap -2 {v}' for v in ('1','X','2')]
        probs=blend.devig([odds[k] for k in keys])
        self.assertAlmostEqual(probs[2],.3701,places=3)
        extra=extra_markets.for_match(2.1,.9,odds)
        self.assertAlmostEqual(sum(extra[k]['p'] for k in keys),1,places=3)
        self.assertEqual(extra[keys[2]]['settlement_definition']['market_type'],'three_way_european_handicap')
        self.assertTrue(all(extra[k]['validation_status']=='experimental' for k in keys))
        self.assertEqual(D.comparison({'model_p':.6410,'market_p':probs[2]})['disagreement_review_required'],True)
        self.assertNotIn('Handikap -1.5 X',extra_markets.for_match(1.2,1,{'Handikap -1.5 X':2}))


class DatabaseAudit(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        for p in (patch.object(db,'DATABASE_URL',None),patch.object(db,'DB_PATH',Path(tmp.name)/'audit.db')):
            p.start();self.addCleanup(p.stop)
        db.init_db()

    def raw(self, at=NOW, price=1.8, mid=1, cache='network_no_cache_evidence'):
        return {'i':3279014,'ci':584,'d':int((NOW+timedelta(hours=6)).timestamp()),'hn':'Beşiktaş','an':'Gençlerbirliği',
                '_transport':{'quote_observed_at':at.isoformat(),'cache_status':cache,'quote_source':iddaa.EVENTS_URL},
                'm':[{'i':mid,'t':2,'st':101,'s':1,'sov':'3.5','o':[{'no':1,'odd':price},{'no':2,'odd':1.68}]}]}

    def test_unchanged_quote_updates_observation_not_change_time(self):
        with db.session() as c:
            a=iddaa.parse_event(self.raw());first=Q.save(c,[a],NOW.isoformat(),{})
            later=NOW+timedelta(minutes=15)
            b=iddaa.parse_event(self.raw(at=later));second=Q.save(c,[b],later.isoformat(),first)
            meta=Q.for_event(second,'3279014');quote=meta['quotes']['A3.5']
            self.assertEqual(meta['last_successful_fetch_at'],later.isoformat())
            self.assertEqual(quote['quote_observed_at'],later.isoformat())
            self.assertEqual(quote['last_observed_change_at'],NOW.isoformat())
            self.assertIsNone(quote['provider_price_updated_at'])
            pred={**quote_prediction(),'quote_provenance':meta}
            self.assertTrue(Q.fresh(pred,'A3.5',later))
            Q.failed(c,'offline',later.isoformat())
            self.assertFalse(Q.fresh({**pred,'quote_provenance':Q.for_event(Q.load(c),'3279014')},'A3.5',later))
            self.assertEqual(Q.load(c)['last_successful_fetch_at'],later.isoformat())

    def test_cache_old_timestamp_mismatched_or_missing_quote_is_not_fresh(self):
        with db.session() as c:
            for raw in (self.raw(cache='shared_cache'),self.raw(at=NOW-timedelta(hours=3))):
                state=Q.save(c,[iddaa.parse_event(raw)],NOW.isoformat(),{})
                self.assertFalse(Q.fresh({**quote_prediction(),'quote_provenance':Q.for_event(state,'3279014')},now=NOW))
            state=Q.save(c,[iddaa.parse_event(self.raw())],NOW.isoformat(),{})
            pred={**quote_prediction(),'quote_provenance':Q.for_event(state,'3279014')}
            self.assertFalse(Q.fresh({**pred,'iddaa_odds':{'A3.5':1.9}},now=NOW))
            self.assertFalse(Q.fresh(pred,'unknown',NOW))

    def test_source_team_ids_unify_changed_names_before_ratings(self):
        with db.session() as c:
            for mid,days,name in [('old',10,'Besiktas JK'),('new',5,'Beşiktaş')]:
                c.execute("INSERT INTO matches(id,league,kickoff,home,away,home_id,away_id,home_goals,away_goals,completed) VALUES(?,'tur.1',?,?,'Gençlerbirliği','1895','996',1,0,1)",(mid,(NOW-timedelta(days=days)).isoformat(),name))
            done,_=engine._load(c,'tur.1')
        self.assertEqual({m['home'] for m in done},{'Beşiktaş'})
        self.assertEqual(engine.LeagueModel('tur.1',done,[],{}).pos['Beşiktaş']['p'],2)

    def test_market_identity_changes_do_not_create_false_price_movements(self):
        with db.session() as c:
            c.execute("INSERT INTO iddaa_events(id,league,competition_id,kickoff,home,away,match_id,updated_at) VALUES('3279014','tur.1',584,?,'A','B','m',?)",((NOW+timedelta(hours=6)).isoformat(),NOW.isoformat()))
            first=iddaa.parse_event(self.raw());state=Q.save(c,[first],NOW.isoformat(),{})
            odds_history.record_snapshot(c,[first],NOW.isoformat())
            later=NOW+timedelta(minutes=15)
            second=iddaa.parse_event(self.raw(at=later,price=1.6,mid=2))
            Q.save(c,[second],later.isoformat(),state)
            odds_history.record_snapshot(c,[second],later.isoformat())
            c.execute("INSERT INTO iddaa_odds(event_id,market,odd) VALUES('3279014','A3.5',1.6)")
            self.assertEqual(odds_history.movements_for_matches(c,['m']),{})

    def test_failed_update_after_rollback_retains_prices_but_withdraws_recommendation(self):
        at=datetime.now(timezone.utc)
        with db.session() as c:
            event=iddaa.parse_event(self.raw(at=at))
            Q.save(c,[event],at.isoformat(),{})
            c.execute("INSERT INTO iddaa_events(id,league,competition_id,kickoff,home,away,updated_at) VALUES('3279014','tur.1',584,?,'A','B',?)",((at+timedelta(hours=6)).isoformat(),at.isoformat()))
            c.execute("INSERT INTO iddaa_odds(event_id,market,odd) VALUES('3279014','A3.5',1.8)")
            db.set_meta(c,'iddaa_last_update',at.isoformat())
            c.commit()
            with patch.object(iddaa,'fetch_json',side_effect=RuntimeError('offline')):
                ok,_=updater._sync_iddaa(c,1)
            self.assertFalse(ok)
            self.assertEqual(c.execute("SELECT odd FROM iddaa_odds").fetchone()['odd'],1.8)
            meta=Q.for_event(Q.load(c),'3279014')
            self.assertEqual(meta['quote_status'],'fetch_failed')
            self.assertFalse(Q.fresh({**quote_prediction(),'quote_provenance':meta},now=at))

    def test_forward_market_metrics_are_from_frozen_predictions(self):
        past=NOW-timedelta(days=1)
        with db.session() as c:
            c.execute("INSERT INTO matches(id,league,kickoff,home,away,home_goals,away_goals,completed) VALUES('m','tur.1',?,'A','B',1,0,1)",(past.isoformat(),))
            payload={'model':{'1':.7,'X':.2,'2':.1},'blend':{'1':.7,'X':.2,'2':.1},'market':{'1':.6,'X':.25,'2':.15}}
            c.execute("INSERT INTO prediction_snapshots(match_id,league,kickoff,captured_at,model_version,parameters,payload) VALUES('m','tur.1',?,?,'v','{}',?)",(past.isoformat(),(past-timedelta(hours=1)).isoformat(),json.dumps(payload)))
            with patch.object(snapshots,'now',return_value=NOW):result=snapshots.report(c)
            score=result['markets']['1']
            self.assertEqual(score['n'],1);self.assertAlmostEqual(score['brier'],.09)
            self.assertAlmostEqual(score['logloss'],.3567,places=4)
            self.assertEqual(score['reliability'][0]['actual'],1)
            self.assertEqual(score['status'],'insufficient_data')

    def test_actual_kickoff_correction_excludes_late_calibration(self):
        with db.session() as c:
            c.execute("INSERT INTO matches(id,league,kickoff,home,away,home_goals,away_goals,completed) VALUES('m','tur.1',?,'A','B',1,0,1)",((NOW-timedelta(days=1)).isoformat(),))
            c.execute("INSERT INTO prediction_snapshots(match_id,league,kickoff,captured_at,model_version,parameters,payload) VALUES('m','tur.1',?,?,'v','{}',?)",(NOW.isoformat(),(NOW-timedelta(hours=1)).isoformat(),json.dumps({'blend':{'1':.7}})))
            self.assertEqual(calibration.load(c,now=NOW+timedelta(days=1)),{})
            self.assertEqual(calibration.report({})['status'],'insufficient_data')
            self.assertEqual(snapshots.report(c)['evaluation_status'],'insufficient_data')

    def test_wins_draws_goals_and_disciplinary_points_are_distinguished(self):
        official={'p':10,'w':6,'d':1,'l':3,'gf':20,'ga':12,'gd':8,'pts':16}
        local={**official,'pts':19}
        check=team_context.consistency(official,local)
        self.assertTrue(check['results_match']);self.assertFalse(check['points_match'])
        self.assertEqual(check['possible_points_adjustment'],-3)
        self.assertFalse(team_context.consistency({**official,'gd':9},local)['results_match'])


class HistoricalAudit(unittest.TestCase):
    def game(self,mid,days,hg=1,ag=0,status='FT',league='tur.1',home='Beşiktaş'):
        at=NOW-timedelta(days=days)
        return {'id':mid,'league':league,'ts':at,'kickoff':at.isoformat(),'home':home,'away':'Gençlerbirliği',
                'home_id':'1895','away_id':'996','completed':1,'home_goals':hg,'away_goals':ag,'status':status}

    def test_later_results_tables_form_elo_and_news_do_not_leak(self):
        old=self.game('old',10)
        future=self.game('future',2,8,0)
        model=engine.LeagueModel('tur.1',[old,future],[],{'Beşiktaş':{'att':.5}})
        model.pos['Beşiktaş']={'p':99,'pts':999}
        at=NOW-timedelta(days=5)
        result=model.predict('Beşiktaş','Gençlerbirliği',at)
        baseline=engine.LeagueModel('tur.1',[old],[],{}).predict('Beşiktaş','Gençlerbirliği',at)
        self.assertEqual(result['markets'],baseline['markets'])
        self.assertEqual(result['factors']['table']['home']['p'],1)
        self.assertEqual(len(result['factors']['h2h']),1)
        self.assertFalse(result['data_integrity']['point_in_time'])
        self.assertEqual(result['factors']['news']['home_log'],0)

    def test_form_and_rest_exclude_non_played_and_separate_league_from_europe(self):
        from app.model.ratings import team_form,last_match_before
        games=[self.game('league',7),self.game('europe',3,league='uefa.europa',home='Besiktas JK'),
               self.game('friendly',2,league='club.friendly'),self.game('cancel',1,status='Cancelled'),
               self.game('postponed',.5,status='Postponed')]
        model=engine.LeagueModel('tur.1',[games[0]],[],{});model.all_history=games
        result=model.predict('Beşiktaş','Gençlerbirliği',NOW+timedelta(days=1))
        self.assertEqual(result['factors']['rest']['home_days'],4)
        self.assertEqual(len(result['factors']['form']['home']),2)
        self.assertEqual(len(result['factors']['form']['league_home']),1)
        self.assertEqual(result['factors']['news']['home_log'],0)
        self.assertFalse(result['factors']['rest']['coverage_verified'])
        self.assertEqual(team_form(games,'Beşiktaş',NOW,team_id='1895')[0]['competition'],'uefa.europa')
        self.assertEqual(last_match_before(games,'Beşiktaş',NOW,team_id='1895'),games[1]['ts'])
