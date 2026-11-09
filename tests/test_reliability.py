import io
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from app import db, updater, ops, live, backtest_jobs
from app.model import snapshots, coupons, small_leagues, engine


def settled_report(conn,start):
    with patch.object(snapshots,'now',return_value=start+timedelta(hours=4)):
        return snapshots.report(conn)


class DatabaseCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for p in (patch.object(db, 'DATABASE_URL', None), patch.object(db, 'DB_PATH', Path(tmp.name)/'test.db')):
            p.start()
            self.addCleanup(p.stop)
        db.init_db()
        ops._cache.clear()

    def test_old_owner_cannot_release_or_commit_after_takeover(self):
        with db.session() as c:
            old = updater.acquire_lock(c, 3)
            db.set_meta(c, 'update_lock', (datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat())
        with db.session() as c:
            new = updater.acquire_lock(c, 3)
            self.assertNotEqual(new, old)
            self.assertFalse(updater.release_lock(c, old))
        with self.assertRaises(updater.LostLease), updater.leased_session(old, 3) as c:
            db.set_meta(c, 'must_rollback', 'yes')
        with db.session() as c:
            self.assertIsNone(db.get_meta(c, 'must_rollback'))
            self.assertEqual(db.get_meta(c, 'update_lock_owner'), new)
            self.assertTrue(updater.release_lock(c, new))

    def test_cancelled_worker_cannot_write(self):
        with db.session() as c:
            token = updater.acquire_lock(c, 3)
        cancelled = threading.Event()
        with self.assertRaises(updater.LostLease), updater.leased_session(token, 3, cancelled) as c:
            db.set_meta(c, 'late_write', 'yes')
            cancelled.set()
            c.commit()
        with db.session() as c:
            self.assertIsNone(db.get_meta(c, 'late_write'))

    def test_ttl_and_kickoff_expire_without_data_change(self):
        calls = []
        def build():
            calls.append(1)
            return {'kickoff': (datetime.now(timezone.utc)+timedelta(seconds=2)).isoformat()}
        with patch.object(ops.time, 'monotonic', return_value=10):
            ops.cached(('time',), build)
            ops.cached(('time',), build)
        self.assertEqual(len(calls), 1)
        with patch.object(ops.time, 'monotonic', return_value=13):
            ops.cached(('time',), build)
        self.assertEqual(len(calls), 2)

    def test_job_is_durable_deduplicated_and_worker_persists_result(self):
        one = backtest_jobs.request()
        self.assertEqual(backtest_jobs.request()['id'], one['id'])
        self.assertEqual(backtest_jobs.status()['status'], 'queued')
        self.assertTrue(backtest_jobs.run_pending(lambda: {'eng.1': {'n': 5}}))
        self.assertEqual(backtest_jobs.status()['status'], 'succeeded')
        self.assertFalse(backtest_jobs.run_pending(lambda: self.fail('duplicate work')))
        self.assertEqual(engine.cached_backtest()['results']['eng.1']['n'], 5)

    def test_worker_records_failure_and_allows_retry(self):
        backtest_jobs.request()
        with self.assertRaises(RuntimeError):
            backtest_jobs.run_pending(lambda: (_ for _ in ()).throw(RuntimeError('private details')))
        self.assertEqual(backtest_jobs.status()['error'], 'RuntimeError')
        self.assertEqual(backtest_jobs.request()['status'], 'queued')

    def test_expired_worker_can_be_requeued(self):
        first = backtest_jobs.request()
        with db.session() as c:
            db.set_meta(c, backtest_jobs.KEY, json.dumps({**first, 'status':'running',
                'expires_at':(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()}))
        self.assertEqual(backtest_jobs.status()['status'], 'expired')
        self.assertNotEqual(backtest_jobs.request()['id'], first['id'])

    def test_walkforward_never_uses_simultaneous_result(self):
        start = datetime(2026,1,1,tzinfo=timezone.utc)
        done = [{'id':str(i),'home':'A','away':'B','home_goals':1,'away_goals':0,
                 'ts':start+timedelta(days=min(i,4)),
                 'kickoff':(start+timedelta(days=min(i,4))).isoformat()} for i in range(6)]
        histories = []
        def predict(self, home, away, kickoff, **kw):
            histories.append((kickoff,kw['history']))
            return {'markets':{k:{'p':p} for k,p in zip(('1','X','2'),(.6,.25,.15))}}
        with patch.object(engine.LeagueModel,'predict',predict):
            list(engine.walkforward_samples('eng.1',done,3))
        self.assertTrue(histories)
        for at, hist in histories:
            self.assertTrue(all(m['ts'] <= at-timedelta(hours=3) for m in hist))

    def test_replaced_job_cannot_be_overwritten_by_old_worker(self):
        backtest_jobs.request()
        def replaced():
            with db.session() as c:
                db.set_meta(c, backtest_jobs.KEY, json.dumps({'id':'new', 'status':'succeeded'}))
            return {'bad': True}
        self.assertFalse(backtest_jobs.run_pending(replaced))
        self.assertEqual(backtest_jobs.status()['id'], 'new')
        self.assertIsNone(engine.cached_backtest())

    def test_small_coupon_filters_started_next_day_and_missing_group(self):
        target = datetime.now(coupons.TZ).date() + timedelta(days=1)
        begin = datetime.combine(target, datetime.min.time(), coupons.TZ).astimezone(timezone.utc)
        from app import quote_provenance as Q
        observed_at=datetime.now(timezone.utc).isoformat()
        verified=[]
        with db.session() as c:
            for mid, kickoff, odds in [('past', begin-timedelta(hours=1), [1.3, 6, 9]),
                                       ('next', begin+timedelta(days=1), [1.3, 6, 9]),
                                       ('missing', begin+timedelta(hours=1), [1.3]),
                                       ('good1', begin+timedelta(hours=2), [1.6, 4.2, 5]),
                                       ('good2', begin+timedelta(hours=3), [1.6, 4.2, 5])]:
                c.execute('INSERT INTO iddaa_events(id,league,competition_id,kickoff,home,away,is_small,updated_at) VALUES(?,?,?,?,?,?,1,?)',
                          (mid,'small:test:league',1,kickoff.isoformat(),'A','B',datetime.now(timezone.utc).isoformat()))
                for market, odd in zip(('1','X','2'), odds):
                    c.execute('INSERT INTO iddaa_odds(event_id,market,odd) VALUES(?,?,?)',(mid,market,odd))
                verified.append({'id':mid,'quote_metadata':{market:{'identity':[mid,market],'odd':odd,
                    'cache_status':'network_no_cache_evidence','quote_observed_at':observed_at}
                    for market,odd in zip(('1','X','2'),odds)}})
            Q.save(c,verified,observed_at,{})
        with patch.object(coupons, 'LEAGUES', {}):
            result = coupons.build_coupons(target)
        self.assertEqual({p['id'] for p in result['small_leagues']['picks']}, {'good1','good2'})
        self.assertTrue(all(p['odds_source']=='iddaa' for p in result['small_leagues']['picks']))
        missing = small_leagues.predict_event({'id':'m', 'iddaa_odds': {}})
        self.assertIsNone(missing['markets']['1']['p'])
        self.assertIsNone(missing['lambda_home'])

    def test_snapshot_is_immutable_prematch_and_evaluates_same_cohort(self):
        start = datetime.now(timezone.utc)+timedelta(hours=1)
        pred = {'id':'m','league':'eng.1','kickoff':start.isoformat(), 'markets': {
            k:{'p':p, 'model_p':p, 'market_p':p} for k,p in zip(('1','X','2'),(.6,.25,.15))}}
        with db.session() as c:
            c.execute('INSERT INTO matches(id,league,kickoff,home,away,completed,home_goals,away_goals) VALUES(?,?,?,?,?,1,2,0)',
                      ('m','eng.1',start.isoformat(),'A','B'))
            snapshots.record(c,[pred])
            pred['markets']['1']['p'] = .1
            snapshots.record(c,[pred])
            saved = c.execute('SELECT * FROM prediction_snapshots').fetchone()
            self.assertEqual(json.loads(saved['payload'])['blend']['1'], .6)
            self.assertTrue(saved['model_version'])
            self.assertTrue(saved['parameters'])
            forward = settled_report(c, start)
            self.assertEqual(forward['variants']['blend']['metrics']['n'],1)
            self.assertAlmostEqual(forward['variants']['blend']['metrics']['brier'],.245, places=3)
            pred['id']='started';pred['kickoff']=(start-timedelta(hours=2)).isoformat()
            snapshots.record(c,[pred])
            self.assertEqual(c.execute('SELECT COUNT(*) n FROM prediction_snapshots').fetchone()['n'],1)

    def test_confidence_report_uses_recorded_pick_and_separates_versions(self):
        start = datetime.now(timezone.utc)+timedelta(hours=1)
        with db.session() as c:
            for mid, league, label, market, probability, hg, ag in (
                ('won', 'eng.1', 'Yüksek', 'A3.5', .9, 1, 0),
                ('lost', 'eng.1', 'Yüksek', '1', .8, 0, 1),
                ('other', 'tur.1', 'Orta', 'X', .6, 0, 0),
            ):
                c.execute('INSERT INTO matches(id,league,kickoff,home,away,completed,home_goals,away_goals) VALUES(?,?,?,?,?,1,?,?)',
                          (mid,league,start.isoformat(),'A','B',hg,ag))
                pred = {'id':mid, 'league':league, 'kickoff':start.isoformat(),
                        'markets':{'1':{'p':.5}, 'X':{'p':.3}, '2':{'p':.2}},
                        'pick':{'market':market,'p':probability,'policy':'hit_rate_v1'},
                        'confidence_label':label, 'confidence':.8}
                snapshots.record(c,[pred])
                pred['pick']={'market':'2','p':.99}  # Sonraki öneri ilk kaydı değiştiremez.
                snapshots.record(c,[pred])
            rows = settled_report(c, start)['recommendations']['groups']
            high = next(r for r in rows if r['league']=='eng.1')
            self.assertEqual((high['n'],high['won'],high['hit_rate']), (2,1,.5))
            self.assertAlmostEqual(high['predicted_probability'],.85)
            self.assertLess(high['interval95'][0], .5)
            self.assertGreater(high['interval95'][1], .5)
            c.execute("UPDATE prediction_snapshots SET model_version='other-version' WHERE match_id='lost'")
            self.assertEqual(len(settled_report(c, start)['recommendations']['groups']),3)
            c.execute("UPDATE prediction_snapshots SET payload=? WHERE match_id='other'",
                      (json.dumps({'model':{}, 'blend':{}, 'market':{}}),))
            self.assertEqual(settled_report(c, start)['recommendations']['legacy_without_confidence'],1)
            # Başlangıç sonrasında alınmış tahmin ölçüme giremez.
            c.execute('UPDATE prediction_snapshots SET captured_at=?', ((start+timedelta(minutes=1)).isoformat(),))
            self.assertEqual(settled_report(c, start)['recommendations']['groups'],[])

    def test_schema_versions_unique(self):
        files = list((Path(__file__).resolve().parent.parent/'supabase/migrations').glob('*.sql'))
        self.assertEqual(len(files),len({p.name.split('_',1)[0] for p in files}))


class LiveTests(unittest.TestCase):
    def setUp(self):
        p=patch.object(live, '_cache', {'at':0,'leagues':{},'sources':{},'updated_at':None})
        p.start();self.addCleanup(p.stop)

    def test_failure_keeps_last_good_data_and_timestamp(self):
        with patch.object(live,'LEAGUES',{'eng.1':{}}), patch.object(live,'_fetch',return_value={'events':[]}):
            good=live.get_live_snapshot()
        live._cache['at']=0
        live._cache['leagues']['eng.1']=[{'id':'old'}]
        with patch.object(live,'LEAGUES',{'eng.1':{}}), patch.object(live,'_fetch',side_effect=OSError('offline')):
            failed=live.get_live_snapshot()
        self.assertTrue(failed['stale'])
        self.assertEqual(failed['matches'],[{'id':'old'}])
        self.assertEqual(failed['updated_at'],good['updated_at'])

    def test_total_deadline_does_not_wait_for_every_league(self):
        def slow(league, day):
            time.sleep(.15)
            return {'events':[]}
        with patch.object(live,'LEAGUES',{str(i):{} for i in range(11)}), patch.object(live,'_fetch',side_effect=slow), patch.object(live,'LIVE_DEADLINE_SECONDS',.02):
            before=time.monotonic();out=live.get_live_snapshot()
        self.assertLess(time.monotonic()-before,.12)
        self.assertTrue(out['stale'])
