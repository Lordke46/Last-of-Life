from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import unittest
from contextlib import redirect_stdout
from io import StringIO

from app.research_trio import select
from app import research_auto

NOW = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)


def entry(i, odds=1.5, p=.8):
    prediction = {'id': str(i), 'league': 'test', 'home': f'H{i}', 'away': f'A{i}',
                  'kickoff': (NOW + timedelta(hours=5)).isoformat(), 'markets': {}, 'iddaa_odds': {}}
    choice = {'market': '1', 'p': p, 'raw_p': p, 'odds': odds, 'odds_source': 'iddaa', 'min_matches': 1}
    return prediction, [choice]



class ResearchTrioAutoTests(unittest.TestCase):
    def test_trio_requires_three_distinct_upcoming_real_priced_matches(self):
        result = select([entry(1), entry(2), entry(3)], now=NOW)
        assert len(result['picks']) == 3
        assert result['total_odds'] == 3.38
        assert result['total_probability'] == .512
        assert not select([entry(1), entry(1), entry(2)], now=NOW)['picks']
        assert not select([entry(1, 3), entry(2, 3), entry(3, 3)], now=NOW)['picks']
        missing = entry(3)
        missing[1][0]['odds_source'] = 'claude'
        assert not select([entry(1), entry(2), missing], now=NOW)['picks']


    def test_auto_disabled_without_touching_database(self):
        out = StringIO()
        with redirect_stdout(out), patch.dict('os.environ', {'RESEARCH_AUTO_ENABLED': '0'}), patch.object(research_auto.coupons, '_day_entries', side_effect=AssertionError):
            research_auto.main()
        assert 'disabled' in out.getvalue()


    def test_auto_never_uses_unguarded_sqlite(self):
        with patch.object(research_auto.research_store, 'postgres', return_value=False):
            with unittest.TestCase().assertRaisesRegex(RuntimeError, 'PostgreSQL'):
                research_auto.enqueue_candidates('pilot', [entry(1), entry(2), entry(3)], at=NOW)


    def test_auto_uses_existing_planner_and_guarded_enqueue(self):
        from contextlib import contextmanager
        class Conn:
            def execute(self, query):
                class Rows:
                    def fetchall(self):
                        return []
                return Rows()
        @contextmanager
        def session():
            yield Conn()
        def context_read(conn, mid, at):
            return {'match': {'id': mid, 'kickoff': (NOW + timedelta(hours=5)).isoformat()},
                    'prices': {}, 'prediction_snapshot': None}
        with patch.object(research_auto.research_store, 'postgres', return_value=True), \
             patch.object(research_auto.research_store, 'queue_session', session), \
             patch.object(research_auto.research_context, 'read', side_effect=context_read), \
             patch.object(research_auto.research_service, 'enqueue', return_value={'status': 'queued'}) as enqueue:
            result = research_auto.enqueue_candidates('pilot', [entry(1), entry(2), entry(3)], at=NOW)
        assert result['selected'] == 3
        assert enqueue.call_count == 3
        assert all(call.args[2] == 'full' for call in enqueue.call_args_list)



    def test_research_fallback_when_no_trio_meets_target(self):
        entries = [entry(1, 1.3, .85), entry(2, 1.3, .80),
                   entry(3, 1.3, .75), entry(4, 1.3, .70)]
        picks, mode = research_auto._research_picks(entries, NOW)
        assert mode == 'research_fallback'
        assert [p['id'] for p in picks] == ['1', '2', '3']


    def test_research_fallback_ignores_duplicate_and_unpriced_matches(self):
        entries = [entry(1, 1.3, .8), entry(1, 1.3, .9),
                   entry(2, 1.3, .7), entry(3, 1.3, .6)]
        entries[2][1][0]['odds_source'] = 'claude'
        picks, mode = research_auto._research_picks(entries, NOW)
        assert mode == 'research_fallback'
        assert [p['id'] for p in picks] == ['1', '3']


    def test_research_prefers_target_trio(self):
        picks, mode = research_auto._research_picks(
            [entry(1), entry(2), entry(3)], NOW)
        assert mode == 'priced_trio'
        assert len(picks) == 3
