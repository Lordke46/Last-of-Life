"""Research trio selection and the opt-in scheduled queue bridge (unittest, as CI runs)."""
import io
import json
import unittest
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.research_trio import select
from app import research_auto

NOW = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)


def entry(i, odds=1.5, p=.8):
    prediction = {'id': str(i), 'league': 'test', 'home': f'H{i}', 'away': f'A{i}',
                  'kickoff': (NOW + timedelta(hours=5)).isoformat(), 'markets': {}, 'iddaa_odds': {}}
    choice = {'market': '1', 'p': p, 'raw_p': p, 'odds': odds, 'odds_source': 'iddaa', 'min_matches': 1}
    return prediction, [choice]


def context(mid):
    return {'match': {'id': mid, 'league': 'UEFA', 'home': f'H{mid}', 'away': f'A{mid}',
                      'kickoff': (NOW + timedelta(hours=5)).isoformat()}, 'prices': {}, 'prediction_snapshot': None}


def recording_session(executed):
    class Rows:
        def fetchall(self):
            return []

    class Conn:
        def execute(self, query, params=None):
            executed.append((query, params))
            return Rows()

    @contextmanager
    def session():
        yield Conn()
    return session


def guarded(executed, enqueue_result=None):
    """Patch the Postgres-only collaborators so enqueue_candidates runs without a database."""
    return (patch.object(research_auto.research_store, 'postgres', return_value=True),
            patch.object(research_auto.research_store, 'session', recording_session(executed)),
            patch.object(research_auto.research_context, 'read', side_effect=lambda conn, mid, at: context(mid)),
            patch.object(research_auto.research_service, 'enqueue', return_value=enqueue_result or {'status': 'queued'}))


class TrioSelectionTests(unittest.TestCase):
    def test_trio_requires_three_distinct_upcoming_real_priced_matches(self):
        result = select([entry(1), entry(2), entry(3)], now=NOW)
        self.assertEqual(len(result['picks']), 3)
        self.assertEqual(result['total_odds'], 3.38)
        self.assertEqual(result['total_probability'], .512)
        self.assertFalse(select([entry(1), entry(1), entry(2)], now=NOW)['picks'])
        self.assertFalse(select([entry(1, 3), entry(2, 3), entry(3, 3)], now=NOW)['picks'])
        missing = entry(3)
        missing[1][0]['odds_source'] = 'claude'
        self.assertFalse(select([entry(1), entry(2), missing], now=NOW)['picks'])

    def test_research_fallback_when_no_trio_meets_target(self):
        entries = [entry(1, 1.3, .85), entry(2, 1.3, .80), entry(3, 1.3, .75), entry(4, 1.3, .70)]
        picks, mode = research_auto._research_picks(entries, NOW)
        self.assertEqual(mode, 'research_fallback')
        self.assertEqual([p['id'] for p in picks], ['1', '2', '3'])

    def test_research_fallback_ignores_duplicate_and_unpriced_matches(self):
        entries = [entry(1, 1.3, .8), entry(1, 1.3, .9), entry(2, 1.3, .7), entry(3, 1.3, .6)]
        entries[2][1][0]['odds_source'] = 'claude'
        picks, mode = research_auto._research_picks(entries, NOW)
        self.assertEqual(mode, 'research_fallback')
        self.assertEqual([p['id'] for p in picks], ['1', '3'])

    def test_research_prefers_target_trio(self):
        picks, mode = research_auto._research_picks([entry(1), entry(2), entry(3)], NOW)
        self.assertEqual(mode, 'priced_trio')
        self.assertEqual(len(picks), 3)


class AutoQueueTests(unittest.TestCase):
    def test_auto_disabled_without_touching_database(self):
        out = io.StringIO()
        with patch.dict('os.environ', {'RESEARCH_AUTO_ENABLED': '0'}), \
             patch.object(research_auto.coupons, '_day_entries', side_effect=AssertionError), redirect_stdout(out):
            self.assertEqual(research_auto.main(), 0)
        self.assertIn('disabled', out.getvalue())

    def test_auto_never_uses_unguarded_sqlite(self):
        with patch.object(research_auto.research_store, 'postgres', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'PostgreSQL'):
                research_auto.enqueue_candidates('pilot', [entry(1), entry(2), entry(3)], at=NOW)

    def test_auto_uses_existing_planner_and_guarded_enqueue(self):
        executed = []
        postgres, session, read, enqueue = guarded(executed)
        with postgres, session, read, enqueue as enqueue_mock, patch.dict('os.environ', {}, clear=True):
            result = research_auto.enqueue_candidates('pilot', [entry(1), entry(2), entry(3)], at=NOW)
        self.assertEqual(result['selected'], 3)
        self.assertEqual(result['identities_registered'], 0)
        self.assertEqual(enqueue_mock.call_count, 3)
        self.assertTrue(all(call.args[2] == 'full' for call in enqueue_mock.call_args_list))
        self.assertFalse([q for q, _ in executed if q.startswith('INSERT')])

    def test_auto_registers_exact_identity_when_enabled(self):
        executed = []
        postgres, session, read, enqueue = guarded(executed)
        with postgres, session, read, enqueue, patch.dict('os.environ', {'RESEARCH_AUTO_IDENTITY': '1'}):
            result = research_auto.enqueue_candidates('pilot', [entry(1), entry(2), entry(3)], at=NOW)
        inserts = [(q, p) for q, p in executed if q.startswith('INSERT')]
        self.assertEqual(result['identities_registered'], 3)
        self.assertEqual(len(inserts), 6)
        identity, alias = inserts[0], inserts[1]
        self.assertIn('ai_research.match_identities', identity[0])
        self.assertIn('DO NOTHING', identity[0])
        self.assertEqual(identity[1][:4], ('1', 'UEFA', 'H1', 'A1'))
        self.assertIn('ai_research.match_aliases', alias[0])
        self.assertEqual(alias[1][:2], ('1', '1'))
        self.assertEqual(alias[1][3:7], ('UEFA', 'H1', 'A1', context('1')['match']['kickoff']))
        self.assertEqual(alias[1][2], research_auto.IDENTITY_PROVIDER)

    def test_identity_registration_skips_incomplete_match_and_requires_postgres(self):
        executed = []
        incomplete = context('7')
        incomplete['match']['home'] = ''

        class Conn:
            def execute(self, query, params=None):
                executed.append(query)
        with patch.dict('os.environ', {'RESEARCH_AUTO_IDENTITY': '1'}):
            with patch.object(research_auto.research_store, 'postgres', return_value=True):
                self.assertEqual(research_auto.register_identities(Conn(), [incomplete]), 0)
            with patch.object(research_auto.research_store, 'postgres', return_value=False):
                with self.assertRaisesRegex(RuntimeError, 'PostgreSQL'):
                    research_auto.register_identities(Conn(), [context('8')])
        self.assertEqual(executed, [])


class GrokBotWebhookTests(unittest.TestCase):
    ENV = {'GROK_BOT_WEBHOOK_URL': 'https://bot.example/hook', 'GROK_BOT_WEBHOOK_KEY': 'routine-key',
           'RESEARCH_PUBLIC_BASE_URL': 'https://app.example'}

    def test_notify_only_posts_queued_jobs_with_bearer(self):
        seen = []

        @contextmanager
        def opener(request, timeout):
            seen.append(request)
            yield type('R', (), {'status': 200})()
        output = {'results': [{'status': 'queued'}, {'status': 'skipped'}]}
        with patch.dict('os.environ', self.ENV, clear=True):
            result = research_auto.notify_grok_bot(output, opener=opener)
            none = research_auto.notify_grok_bot({'results': [{'status': 'skipped'}]}, opener=opener)
        with patch.dict('os.environ', {}, clear=True):
            unset = research_auto.notify_grok_bot(output, opener=opener)
        self.assertEqual(result, {'notified': True, 'queued': 1, 'status': 200})
        self.assertEqual(none, {'notified': False, 'queued': 0})
        self.assertEqual(unset, {'notified': False, 'queued': 1})
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].full_url, 'https://bot.example/hook')
        self.assertEqual(seen[0].get_header('Authorization'), 'Bearer routine-key')
        body = json.loads(seen[0].data)
        self.assertEqual((body['event'], body['queued'], body['research_api']),
                         ('research_jobs_queued', 1, 'https://app.example'))

    def test_notify_rejects_plain_http_and_hides_errors(self):
        output = {'results': [{'status': 'queued'}]}
        with patch.dict('os.environ', dict(self.ENV, GROK_BOT_WEBHOOK_URL='http://bot.example/hook'), clear=True):
            with self.assertRaisesRegex(ValueError, 'HTTPS'):
                research_auto.notify_grok_bot(output)

        def failing(request, timeout):
            raise OSError('refused https://bot.example/hook?key=secret')
        with patch.dict('os.environ', self.ENV, clear=True):
            result = research_auto.notify_grok_bot(output, opener=failing)
        self.assertEqual(result, {'notified': False, 'queued': 1, 'error': 'webhook_unreachable'})


class ConfigurationCheckTests(unittest.TestCase):
    GOOD = {'DATABASE_URL': 'postgresql://src.example/app', 'RESEARCH_DATABASE_URL': 'postgresql://q.example/app',
            'RESEARCH_AUTO_OWNER': 'pilot', 'GROK_BOT_WEBHOOK_URL': 'https://bot.example/h', 'GROK_BOT_WEBHOOK_KEY': 'k'}

    def test_reports_shape_problems_without_connecting(self):
        self.assertEqual(research_auto.check_configuration(self.GOOD), [])
        same = dict(self.GOOD, RESEARCH_DATABASE_URL=self.GOOD['DATABASE_URL'])
        self.assertIn('source and research connections must be distinct', research_auto.check_configuration(same))
        problems = research_auto.check_configuration({'DATABASE_URL': 'sqlite:///x.db', 'GROK_BOT_WEBHOOK_URL': 'http://b/h'})
        for expected in ('missing RESEARCH_DATABASE_URL', 'missing RESEARCH_AUTO_OWNER',
                         'DATABASE_URL must be a PostgreSQL URL with a host', 'GROK_BOT_WEBHOOK_URL must use HTTPS',
                         'GROK_BOT_WEBHOOK_URL and GROK_BOT_WEBHOOK_KEY must be set together'):
            self.assertIn(expected, problems)

    def test_main_check_mode_exits_nonzero_on_problems(self):
        out = io.StringIO()
        with patch.dict('os.environ', {}, clear=True), redirect_stdout(out):
            self.assertEqual(research_auto.main(['--check']), 1)
            self.assertEqual(research_auto.main(['--check']), 1)
        self.assertIn('missing DATABASE_URL', out.getvalue())
        with patch.dict('os.environ', self.GOOD, clear=True), redirect_stdout(io.StringIO()):
            self.assertEqual(research_auto.main(['--check']), 0)
        with self.assertRaises(SystemExit):
            research_auto.main(['--bogus'])


if __name__ == '__main__':
    unittest.main()
