"""Disposable synthetic fixtures; no broker or production data access."""
import copy
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

from refresh import advance, closed_session, decode, encode


def fixture():
    cohort = {"id": "fixture", "name": "Synthetic research", "screen_date": "2026-09-11",
              "decided_at": "2026-09-12T21:00:00+09:00", "entry_not_before": "2026-09-14",
              "end_date": "2026-10-14", "cost": .003, "orders_enabled": False,
              "selection_weight_cap": .1,
              "candidates": [{"code": f"{i:06}", "name": "<script>unsafe</script>", "rank": i,
                              "selected": i <= 5, "reason": "Frozen", "falsifier": "Next check"}
                             for i in range(1, 21)]}
    days = [(datetime(2026, 8, 1) + timedelta(days=i)).date().isoformat() for i in range(47)]
    prices = {code: {d: {"close": 100., "volume": 100} for d in days}
              for code in [r["code"] for r in cohort["candidates"]] + ["069500", "KOSPI"]}
    state = {"schema": 1, "orders_enabled": False, "cohorts": [cohort]}
    return state, prices


class RefreshTests(unittest.TestCase):
    now = datetime(2026, 9, 16, 8, tzinfo=timezone.utc)

    def run_fixture(self, state, prices):
        with patch('refresh.closed_session', return_value='2026-09-16'):
            return advance(state, self.now, lambda c, d: prices[c], lambda c, d: {d: 100.})

    def test_encryption_and_tampering(self):
        key = b'x' * 32
        encrypted = encode({'private': 'fixture'}, key)
        self.assertNotIn('fixture', json.dumps(encrypted))
        self.assertEqual(decode(encrypted, key), {'private': 'fixture'})
        with self.assertRaises(Exception):
            decode(encrypted, b'y' * 32)

    def test_waiting_is_not_zero_and_escaping(self):
        state, prices = fixture()
        for h in prices.values():
            for d in list(h):
                if d > '2026-09-11':
                    del h[d]
        with patch('refresh.closed_session', return_value='2026-09-11'):
            result = advance(state, self.now, lambda c, d: prices[c], lambda c, d: {d: 100.})
        self.assertEqual(state['observations']['fixture'], [])
        self.assertIn('미집계', result['llm_html'])
        self.assertNotIn('<script>unsafe', result['llm_html'])

    def test_cost_and_cash_equal_control_and_idempotence(self):
        state, prices = fixture()
        first = self.run_fixture(state, prices)
        observations = copy.deepcopy(state['observations'])
        row = observations['fixture'][0]
        self.assertAlmostEqual(row['llm_matched_excess_pct'], 0)
        self.assertAlmostEqual(row['portfolios']['llm_selected']['return_pct'], (.5 + .5/1.003 - 1)*100)
        self.run_fixture(state, prices)
        self.assertEqual(state['observations'], observations)
        self.assertFalse(first['orders_enabled'])

    def test_history_revision_blocks(self):
        state, prices = fixture()
        self.run_fixture(state, prices)
        old = copy.deepcopy(state['observations'])
        prices['000001']['2026-09-16']['close'] = 110
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.run_fixture(state, prices)
        self.assertEqual(state['observations'], old)

    def test_missing_latest_and_interior_index_bar_blocks(self):
        for code, day in [('000020', '2026-09-16'), ('KOSPI', '2026-09-15')]:
            state, prices = fixture()
            del prices[code][day]
            with self.assertRaises(ValueError):
                self.run_fixture(state, prices)

    def test_closed_sessions_skip_weekend_and_intraday(self):
        self.assertEqual(closed_session('XKRX', datetime(2026,9,13,5,tzinfo=timezone.utc)), '2026-09-11')
        self.assertEqual(closed_session('XKRX', datetime(2026,9,14,4,tzinfo=timezone.utc)), '2026-09-11')
        self.assertEqual(closed_session('XNYS', datetime(2026,9,13,5,tzinfo=timezone.utc)), '2026-09-11')

    def test_order_enabled_rejected(self):
        state, prices = fixture()
        state['orders_enabled'] = True
        with self.assertRaises(ValueError):
            self.run_fixture(state, prices)


if __name__ == '__main__':
    unittest.main()
