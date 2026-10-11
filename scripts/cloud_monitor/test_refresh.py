"""Disposable synthetic fixtures; no broker or production data access."""
import copy
import base64
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import refresh
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
        self.assertEqual(result['research_data']['monthly']['series'], [])
        self.assertIsNone(result['research_data']['cohort'])
        self.assertNotIn('<script>unsafe', json.dumps(result))

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

    def test_provider_revision_preserves_published_prefix_and_appends(self):
        state, prices = fixture()
        self.run_fixture(state, prices)
        old = copy.deepcopy(state['observations'])
        prices['000001']['2026-09-16']['close'] = 99.9
        for history in prices.values():
            history['2026-09-17'] = {'close': 101., 'volume': 100}
        untouched = copy.deepcopy(prices)
        with patch('refresh.closed_session', return_value='2026-09-17'):
            result = advance(state, self.now, lambda c, d: prices[c], lambda c, d: {d: 100.})
        self.assertEqual(state['observations']['fixture'][:len(old['fixture'])], old['fixture'])
        self.assertEqual(state['observations']['fixture'][-1]['date'], '2026-09-17')
        self.assertEqual(prices, untouched)
        self.assertEqual(len(state['price_revisions']), 1)
        revision = state['price_revisions'][0]
        self.assertEqual((revision['date'], revision['code'], revision['recorded_close'], revision['provider_close']), ('2026-09-16', '000001', 100., 99.9))
        self.assertEqual(result['price_revision_count'], 1)
        with patch('refresh.closed_session', return_value='2026-09-17'):
            advance(state, self.now, lambda c, d: prices[c], lambda c, d: {d: 100.})
        self.assertEqual(len(state['price_revisions']), 1)

    def test_non_price_history_change_and_corporate_action_still_block(self):
        state, prices = fixture()
        self.run_fixture(state, prices)
        state['observations']['fixture'][0]['portfolios']['quant10']['return_pct'] = 42
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.run_fixture(state, prices)
        state, prices = fixture()
        self.run_fixture(state, prices)
        prices['000001']['2026-09-16']['close'] = 10
        with self.assertRaisesRegex(ValueError, 'Corporate action'):
            self.run_fixture(state, prices)

    def test_held_bar_cannot_mask_another_security_price_anomaly(self):
        state,prices=fixture();prices['000001']['2026-09-16']['volume']=0
        prices['000002']['2026-09-16']['close']=200
        with self.assertRaisesRegex(ValueError,'Corporate action'):
            self.run_fixture(state,prices)

    def test_missing_latest_and_interior_index_bar_blocks(self):
        for code, day in [('000020', '2026-09-16'), ('KOSPI', '2026-09-15')]:
            state, prices = fixture()
            del prices[code][day]
            with self.assertRaises(ValueError):
                self.run_fixture(state, prices)
    def test_one_nontrading_bar_holds_history_but_updates_independent_quotes(self):
        state,prices=fixture();self.run_fixture(state,prices)
        old=copy.deepcopy(state['observations']);success=state['last_success_at']
        for history in prices.values():history['2026-09-17']={'close':101.,'volume':100}
        prices['000001']['2026-09-17']['volume']=0
        with patch('refresh.closed_session',return_value='2026-09-17'):
            snapshot=advance(state,self.now+timedelta(days=1),lambda c,d:prices[c],lambda c,d:{d:102.})
        self.assertEqual(state['observations'],old);self.assertEqual(state['last_success_at'],success)
        self.assertEqual(snapshot['data_state'],'PARTIAL_HELD');self.assertEqual(snapshot['held_cohort_count'],1)
        self.assertEqual(snapshot['benchmarks'][0]['close'],102.);self.assertEqual(snapshot['us_as_of'],'2026-09-17')
        self.assertEqual(snapshot['research_data']['cohort']['state'],'STALE')
        self.assertFalse(snapshot['orders_enabled'])

    def test_closed_sessions_skip_weekend_and_intraday(self):
        self.assertEqual(closed_session('XKRX', datetime(2026,9,13,5,tzinfo=timezone.utc)), '2026-09-11')
        self.assertEqual(closed_session('XKRX', datetime(2026,9,14,4,tzinfo=timezone.utc)), '2026-09-11')
        self.assertEqual(closed_session('XNYS', datetime(2026,9,13,5,tzinfo=timezone.utc)), '2026-09-11')

    def test_order_enabled_rejected(self):
        state, prices = fixture()
        state['orders_enabled'] = True
        with self.assertRaises(ValueError):
            self.run_fixture(state, prices)

    def test_structured_research_preserves_monthly_costs_and_initial_budget(self):
        state, prices = fixture()
        state['cohorts'][0]['account_id'] = 'DO_NOT_EXPORT_ACCOUNT'
        snapshot = self.run_fixture(state, prices)
        self.assertIn('research_data', snapshot, 'Structured research is required by the redesigned UI')
        self.assertTrue('llm_html' not in snapshot, 'Retired HTML overlay must not be emitted')
        data = snapshot['research_data']
        self.assertEqual(data['schema_version'], 'wbq.cloud-research.v1')
        self.assertEqual(data['cohort_ids'], ['fixture'])
        self.assertEqual(data['generated_at'], snapshot['generated_at'])
        self.assertEqual(data['cohort']['initial_cash_pct'], 50)
        self.assertEqual(data['cohort']['position_count'], 5)
        series = {row['strategy_id']: row for row in data['monthly']['series']}
        llm = series['fixture:llm_selected']
        self.assertEqual(llm['stage'], 'simulation')
        self.assertEqual(llm['total_months'], len(llm['months']))
        self.assertAlmostEqual(llm['months'][0]['return_pct'], (.5 + .5/1.003 - 1)*100)
        self.assertEqual(llm['months'][0]['start_date'], '2026-09-14')
        self.assertEqual(llm['months'][0]['end_date'], '2026-09-16')
        self.assertNotIn('DO_NOT_EXPORT_ACCOUNT', json.dumps(data))
        self.assertNotIn('llm_html', data)
        self.assertNotIn('prices', data)

    def test_structured_waiting_does_not_invent_monthly_observations(self):
        state, prices = fixture()
        for history in prices.values():
            for day in list(history):
                if day > '2026-09-11':
                    del history[day]
        with patch('refresh.closed_session', return_value='2026-09-11'):
            snapshot = advance(state, self.now, lambda c, d: prices[c], lambda c, d: {d: 100.})
        self.assertIn('research_data', snapshot, 'Structured waiting state is required')
        data = snapshot['research_data']
        self.assertEqual(data['monthly']['series'], [])
        self.assertIsNone(data['cohort'])
        self.assertEqual(data['cohort_ids'], ['fixture'])


class RefreshExitTests(unittest.TestCase):
    """Exercise the CLI contract using only encrypted disposable fixtures."""
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state, self.prices = fixture()
        self.day = '2026-09-16'
        self.now = datetime(2026, 9, 16, 8, tzinfo=timezone.utc)
        self.key = b'x' * 32
        for name in ('refresh.py', 'llm_engine.py', 'research_views.py',
                     'research_data.py', 'cloud_monthly.py'):
            source = self.root / 'scripts/cloud_monitor' / name
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text('synthetic source identity', encoding='utf-8')

    def collect(self, state, now):
        with patch('refresh.closed_session', return_value=self.day):
            return advance(state, self.now, lambda c, d: self.prices[c],
                           lambda c, d: {d: 100. if d == '2026-09-16' else 102.})

    def write_inputs(self):
        owned = {'monitoring/state.enc.json': self.state,
                 'data/monitoring.enc.json': {'last_good': 'synthetic snapshot'}}
        for name, value in owned.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(encode(value, self.key)), encoding='utf-8')
        self.before = {name: (self.root / name).read_bytes() for name in owned}

    def run_cli(self, collector=None, key=None):
        out, err = io.StringIO(), io.StringIO()
        with patch('refresh.__file__', str(self.root / 'scripts/cloud_monitor/refresh.py')), \
             patch.dict('os.environ', {'MONITORING_DATA_KEY': key or base64.b64encode(self.key).decode()}), \
             patch('refresh.advance', side_effect=collector or self.collect), \
             redirect_stdout(out), redirect_stderr(err):
            result = refresh.cli()
        return result, out.getvalue(), err.getvalue()

    def read_output(self, name):
        return decode(json.loads((self.root / name).read_text(encoding='utf-8')), self.key)

    def assert_failed_closed(self, result):
        code, out, err = result
        self.assertEqual(code, 1)
        self.assertEqual(out, '')
        self.assertEqual(json.loads(err)['status'], 'FAILED_CLOSED')
        self.assertEqual(json.loads((self.root / '.refresh-receipt.json').read_text())['status'], 'IN_PROGRESS')
        self.assertEqual({name: (self.root / name).read_bytes() for name in self.before}, self.before)

    def test_ready_is_success_with_valid_receipt(self):
        self.write_inputs()
        code, out, err = self.run_cli()
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(json.loads(out)['status'], 'READY')
        receipt = json.loads((self.root / '.refresh-receipt.json').read_text())
        self.assertEqual(receipt['status'], 'READY')
        self.assertEqual(set(receipt['after']), set(self.before))

    def test_held_refresh_succeeds_without_advancing_held_window(self):
        # A second cohort uses independent candidates and can finish normally.
        other = copy.deepcopy(self.state['cohorts'][0])
        other.update(id='independent', end_date='2026-09-17')
        for candidate in other['candidates']:
            old_code = candidate['code']
            candidate['code'] = f"{int(old_code) + 100:06}"
            self.prices[candidate['code']] = copy.deepcopy(self.prices[old_code])
        self.state['cohorts'].append(other)
        self.collect(self.state, self.now)
        old = copy.deepcopy(self.state['observations']['fixture'])
        success = self.state['last_success_at']
        self.day = '2026-09-17'
        self.now += timedelta(days=1)
        for history in self.prices.values():
            history[self.day] = {'close': 101., 'volume': 100}
        self.prices['000001'][self.day]['volume'] = 0
        self.write_inputs()
        code, out, err = self.run_cli()
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(json.loads(out)['status'], 'PARTIAL_HELD')
        receipt = json.loads((self.root / '.refresh-receipt.json').read_text())
        self.assertEqual(receipt['status'], 'PARTIAL_HELD')
        state = self.read_output('monitoring/state.enc.json')
        snapshot = self.read_output('data/monitoring.enc.json')
        self.assertEqual(state['observations']['fixture'], old)
        self.assertEqual(state['last_success_at'], success)
        self.assertNotIn('fixture', state['completed'])
        self.assertIn('independent', state['completed'])
        self.assertEqual(state['observations']['independent'][-1]['date'], self.day)
        self.assertEqual(snapshot['held_cohort_count'], 1)
        self.assertEqual(snapshot['benchmarks'][0]['close'], 102.)
        self.assertFalse(snapshot['orders_enabled'])

    def test_missing_latest_or_index_close_is_fatal_and_preserves_last_good(self):
        for code, day in [('000020', '2026-09-16'), ('KOSPI', '2026-09-15')]:
            with self.subTest(code=code):
                self.state, self.prices = fixture()
                del self.prices[code][day]
                self.write_inputs()
                self.assert_failed_closed(self.run_cli())

    def test_hold_cannot_downgrade_a_corporate_action_failure(self):
        self.prices['000001'][self.day]['volume'] = 0
        self.prices['000002'][self.day]['close'] = 200.
        self.write_inputs()
        self.assert_failed_closed(self.run_cli())

    def test_non_price_history_change_is_fatal(self):
        self.collect(self.state, self.now)
        self.state['observations']['fixture'][0]['portfolios']['quant10']['return_pct'] = 42
        self.write_inputs()
        self.assert_failed_closed(self.run_cli())

    def test_invalid_key_and_contract_are_fatal(self):
        self.write_inputs()
        self.assert_failed_closed(self.run_cli(key=base64.b64encode(b'x' * 31).decode()))
        self.state['orders_enabled'] = True
        self.write_inputs()
        self.assert_failed_closed(self.run_cli())

    def test_provider_failure_stays_fatal_without_logging_private_body(self):
        def failed(state, now):
            raise refresh.requests.ConnectionError('PRIVATE_PROVIDER_BODY')
        self.write_inputs()
        result = self.run_cli(collector=failed)
        self.assert_failed_closed(result)
        self.assertNotIn('PRIVATE_PROVIDER_BODY', result[2])

    def test_unknown_outcome_cannot_write_a_success_receipt(self):
        self.write_inputs()
        self.assert_failed_closed(self.run_cli(collector=lambda s, n: {'data_state': 'UNKNOWN'}))


if __name__ == '__main__':
    unittest.main()
