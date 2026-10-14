"""Synthetic provider and encrypted-state fixtures; no network or production key."""
import base64
import copy
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import refresh
from test_refresh import fixture


def chart(days=('2026-10-12', '2026-10-13'), values=(100., 101.), symbol='VOO'):
    return {'chart': {'error': None, 'result': [{
        'meta': {'symbol': symbol, 'exchangeTimezoneName': 'America/New_York'},
        'timestamp': [int(datetime.fromisoformat(day + 'T13:30:00+00:00').timestamp()) for day in days],
        'indicators': {'quote': [{'close': list(values)}]}}]}}


def response(body=None, status=200):
    result = Mock(status_code=status)
    result.json.return_value = body
    if status >= 400:
        result.raise_for_status.side_effect = refresh.requests.HTTPError('PRIVATE_HTTP_BODY', response=result)
    return result


class USProviderTests(unittest.TestCase):
    def fetch(self, responses, symbol='VOO'):
        with patch('refresh.requests.get', side_effect=responses) as get, \
             patch('refresh.time.sleep') as sleep, redirect_stdout(io.StringIO()):
            result = refresh.fetch_us(symbol, '2026-10-13')
        return result, get, sleep

    def test_missing_session_recovers_on_retry(self):
        rows, get, sleep = self.fetch([response(chart(values=(100., None))), response(chart())])
        self.assertEqual(rows['2026-10-13'], 101.)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(sleep.call_count, 1)
        params = get.call_args.kwargs['params']
        self.assertGreater(params['period2'], params['period1'])

    def test_missing_session_exhausts_with_safe_dates_and_symbol(self):
        out = io.StringIO()
        with patch('refresh.requests.get', return_value=response(chart(values=(100., None)))) as get, \
             patch('refresh.time.sleep') as sleep, redirect_stdout(out):
            with self.assertRaises(refresh.USDataUnavailable) as raised:
                refresh.fetch_us('VOO', '2026-10-13')
        self.assertEqual(get.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        issue = raised.exception.diagnostic
        self.assertEqual((issue['symbol'], issue['expected_session'], issue['received_latest_session']),
                         ('VOO', '2026-10-13', '2026-10-12'))
        self.assertEqual(issue['attempts'], 3)
        self.assertNotIn('PRIVATE', out.getvalue())

    def test_timeout_and_rate_limit_recover_but_authorization_is_fatal(self):
        for first in (refresh.requests.Timeout('PRIVATE_TIMEOUT'), response(status=429)):
            with self.subTest(first=type(first).__name__):
                rows, get, _ = self.fetch([first, response(chart())])
                self.assertEqual(rows['2026-10-13'], 101.)
                self.assertEqual(get.call_count, 2)
        with patch('refresh.requests.get', return_value=response(status=403)) as get, \
             patch('refresh.time.sleep') as sleep:
            with self.assertRaises(refresh.USProviderError):
                refresh.fetch_us('VOO', '2026-10-13')
        self.assertEqual(get.call_count, 1)
        sleep.assert_not_called()

    def test_transport_and_server_failure_exhaustion_is_bounded(self):
        for failure in (refresh.requests.ConnectionError('PRIVATE_CONNECTION'), response(status=503)):
            with self.subTest(failure=type(failure).__name__), \
                 patch('refresh.requests.get',side_effect=[failure,failure,failure]) as get, \
                 patch('refresh.time.sleep') as sleep,redirect_stdout(io.StringIO()):
                with self.assertRaises(refresh.USDataUnavailable) as raised:
                    refresh.fetch_us('SPMO','2026-10-13')
                self.assertEqual(raised.exception.diagnostic['attempts'],3)
                self.assertEqual(raised.exception.diagnostic['symbol'],'SPMO')
                self.assertEqual(get.call_count,3);self.assertEqual(sleep.call_count,2)

    def test_non_session_target_never_queries_provider(self):
        with patch('refresh.requests.get') as get:
            with self.assertRaises(ValueError):refresh.fetch_us('VOO','2026-07-03')
            get.assert_not_called()

    def test_later_network_failure_retains_received_history_for_integrity_check(self):
        with patch('refresh.requests.get',side_effect=[response(chart(values=(99.,None))),
                 refresh.requests.Timeout(),refresh.requests.Timeout()]), \
             patch('refresh.time.sleep'),redirect_stdout(io.StringIO()):
            with self.assertRaises(refresh.USDataUnavailable) as raised:
                refresh.fetch_us('VOO','2026-10-13')
        self.assertEqual(raised.exception.observations,{'2026-10-12':99.})
        self.assertEqual(raised.exception.diagnostic['received_latest_session'],'2026-10-12')

    def test_malformed_data_never_retries(self):
        cases = []
        bad = chart(); bad['chart']['result'][0]['indicators']['quote'][0]['close'].pop(); cases.append(bad)
        bad = chart(); bad['chart']['result'][0]['timestamp'][0] = True; cases.append(bad)
        bad = chart(); bad['chart']['result'][0]['meta']['symbol'] = 'OTHER'; cases.append(bad)
        bad = chart(); bad['chart']['result'][0]['meta']['exchangeTimezoneName'] = 'UTC'; cases.append(bad)
        cases.extend([chart(values=(100., float('nan'))), chart(values=(100., 0)),
                      chart(values=(100., True)), chart(days=('2026-10-13', '2026-10-13')),
                      {'chart': {'result': None, 'error': {'description': 'PRIVATE_PROVIDER_BODY'}}},
                      {'chart': None}, None])
        for index,body in enumerate(cases):
            with self.subTest(body=index), \
                 patch('refresh.requests.get', return_value=response(body)) as get, \
                 patch('refresh.time.sleep') as sleep:
                with self.assertRaises(refresh.USProviderError) as raised:
                    refresh.fetch_us('VOO', '2026-10-13')
                self.assertNotIn('PRIVATE', str(raised.exception))
                self.assertEqual(get.call_count, 1)
                sleep.assert_not_called()

    def test_timezone_date_and_future_rows_are_not_expected_close(self):
        body = chart(days=('2026-10-13', '2026-10-14'))
        body['chart']['result'][0]['timestamp'][0] = int(datetime(2026,10,13,0,30,tzinfo=timezone.utc).timestamp())
        with patch('refresh.requests.get', return_value=response(body)), patch('refresh.time.sleep'), redirect_stdout(io.StringIO()):
            with self.assertRaises(refresh.USDataUnavailable) as raised:
                refresh.fetch_us('VOO', '2026-10-13')
        self.assertEqual(raised.exception.diagnostic['received_latest_session'], '2026-10-12')

    def test_close_buffer_dst_and_holidays(self):
        cases = [
            ('2026-10-13T20:14:00+00:00', '2026-10-12'),
            ('2026-10-13T20:15:00+00:00', '2026-10-13'),
            ('2026-03-06T21:14:00+00:00', '2026-03-05'),
            ('2026-03-09T20:15:00+00:00', '2026-03-09'),
            ('2026-07-03T23:00:00+00:00', '2026-07-02'),
            ('2026-11-27T18:14:00+00:00', '2026-11-25'),
            ('2026-11-27T18:15:00+00:00', '2026-11-27'),
        ]
        for stamp, expected in cases:
            with self.subTest(stamp=stamp):
                self.assertEqual(refresh.closed_session('XNYS', datetime.fromisoformat(stamp)), expected)


class DelayedSnapshotTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.root = Path(directory.name); self.key = b'x' * 32
        self.state, self.prices = fixture()
        self.now = datetime(2026,9,17,8,tzinfo=timezone.utc)
        with patch('refresh.closed_session', return_value='2026-09-16'):
            self.snapshot = refresh.advance(self.state, self.now-timedelta(days=1),
                lambda c,d:self.prices[c], lambda c,d:{d:100.})
        for name in ('refresh.py','llm_engine.py','research_views.py','research_data.py','cloud_monthly.py'):
            path=self.root/'scripts/cloud_monitor'/name; path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('fixture source')
        self.write_inputs()

    def write_inputs(self):
        for name,value in {'monitoring/state.enc.json':self.state,'data/monitoring.enc.json':self.snapshot}.items():
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(json.dumps(refresh.encode(value,self.key)))
        self.before={name:(self.root/name).read_bytes() for name in ('monitoring/state.enc.json','data/monitoring.enc.json')}

    def read_snapshot(self):
        return refresh.decode(json.loads((self.root/'data/monitoring.enc.json').read_text()),self.key)

    def issue(self):
        return refresh.USDataUnavailable('VOO','2026-09-17','2026-09-16','missing_session',3)

    def run_cli(self, collector):
        out,err=io.StringIO(),io.StringIO()
        with patch('refresh.__file__',str(self.root/'scripts/cloud_monitor/refresh.py')), \
             patch.dict('os.environ',{'MONITORING_DATA_KEY':base64.b64encode(self.key).decode()}), \
             patch('refresh.datetime',wraps=datetime) as clock, \
             patch('refresh.advance',side_effect=collector), redirect_stdout(out),redirect_stderr(err):
            clock.now.return_value=self.now
            code=refresh.cli()
        return code,out.getvalue(),err.getvalue()

    def test_exhaustion_preserves_ledger_financial_data_and_generation_time(self):
        def delayed(state,now):
            state['observations']['fixture'].append({'uncommitted_attempt':'must be discarded'})
            raise self.issue()
        code,out,err=self.run_cli(delayed)
        self.assertEqual((code,err),(0,''));self.assertEqual(json.loads(out)['status'],'DELAYED')
        self.assertEqual((self.root/'monitoring/state.enc.json').read_bytes(),self.before['monitoring/state.enc.json'])
        snapshot=self.read_snapshot()
        for key in ('generated_at','kr_as_of','us_as_of','benchmarks','research_data','prices'):
            self.assertEqual(snapshot[key],self.snapshot[key])
        self.assertEqual(snapshot['data_state'],'DELAYED')
        self.assertEqual(snapshot['expected_us_as_of'],'2026-09-17')
        self.assertEqual(snapshot['last_attempt_at'],self.now.isoformat())
        receipt=json.loads((self.root/'.refresh-receipt.json').read_text())
        self.assertEqual(receipt['status'],'DELAYED')
        self.assertEqual(receipt['before']['monitoring/state.enc.json'],receipt['after']['monitoring/state.enc.json'])

    def test_repeat_delay_retains_last_good_timestamp_and_recovery_clears_delay(self):
        self.assertEqual(self.run_cli(lambda s,n:(_ for _ in ()).throw(self.issue()))[0],0)
        first=self.read_snapshot();self.snapshot=first;self.write_inputs()
        self.now+=timedelta(minutes=1)
        self.assertEqual(self.run_cli(lambda s,n:(_ for _ in ()).throw(self.issue()))[0],0)
        self.assertEqual(self.read_snapshot()['data_state'],'DELAYED')
        self.assertEqual(self.read_snapshot()['generated_at'],first['generated_at'])
        for history in self.prices.values():history['2026-09-17']={'close':101.,'volume':100}
        original_advance=refresh.advance
        def recovered(state,now):
            with patch('refresh.closed_session',return_value='2026-09-17'):
                return original_advance(state,now,lambda c,d:self.prices[c],lambda c,d:{d:102.})
        code,out,err=self.run_cli(recovered)
        self.assertEqual((code,err),(0,''));self.assertEqual(json.loads(out)['status'],'READY')
        self.assertNotIn('refresh_delay',self.read_snapshot())
        self.assertEqual(self.read_snapshot()['us_as_of'],'2026-09-17')

    def test_corrupt_or_inconsistent_last_good_cannot_be_published(self):
        for corrupt in ('encryption','identity','observation','return','missing_count','future_time'):
            with self.subTest(corrupt=corrupt):
                old=copy.deepcopy(self.snapshot)
                if corrupt=='identity':self.snapshot['orders_enabled']=True
                if corrupt=='observation':self.snapshot['benchmarks'][0]['close']=42.
                if corrupt=='return':self.snapshot['benchmarks'][0]['return_pct']=42.
                if corrupt=='missing_count':del self.snapshot['held_cohort_count']
                if corrupt=='future_time':self.snapshot['generated_at']='2099-01-01T00:00:00+00:00'
                self.write_inputs()
                if corrupt=='encryption':
                    (self.root/'data/monitoring.enc.json').write_text(json.dumps(refresh.encode(old,b'y'*32)))
                    self.before['data/monitoring.enc.json']=(self.root/'data/monitoring.enc.json').read_bytes()
                code,out,err=self.run_cli(lambda s,n:(_ for _ in ()).throw(self.issue()))
                self.assertEqual(code,1);self.assertEqual(out,'')
                self.assertEqual(json.loads(err)['status'],'FAILED_CLOSED')
                self.assertEqual({n:(self.root/n).read_bytes() for n in self.before},self.before)
                self.snapshot=old

    def test_malformed_collector_output_leaves_no_valid_receipt(self):
        code,out,err=self.run_cli(lambda s,n:{'data_state':'READY','schema':1,'orders_enabled':False})
        self.assertEqual(code,1);self.assertEqual(out,'')
        self.assertEqual(json.loads(err)['status'],'FAILED_CLOSED')
        self.assertEqual({n:(self.root/n).read_bytes() for n in self.before},self.before)
        self.assertEqual(json.loads((self.root/'.refresh-receipt.json').read_text())['status'],'IN_PROGRESS')

    def test_serialization_defect_cannot_overwrite_last_good_files(self):
        def invalid(state,now):
            state['last_attempt_at']=now.isoformat()
            result=copy.deepcopy(self.snapshot);result['unexpected_nonfinite']=float('nan')
            return result
        code,out,err=self.run_cli(invalid)
        self.assertEqual(code,1);self.assertEqual(out,'')
        self.assertEqual(json.loads(err)['status'],'FAILED_CLOSED')
        self.assertEqual({n:(self.root/n).read_bytes() for n in self.before},self.before)

    def test_missing_other_etf_cannot_mask_price_revision_or_malformed_data(self):
        for history in self.prices.values():history['2026-09-17']={'close':101.,'volume':100}
        for invalid in ('revision','malformed'):
            with self.subTest(invalid=invalid):
                state=copy.deepcopy(self.state);before=copy.deepcopy(state)
                def loader(symbol,day):
                    if symbol=='VOO':raise self.issue()
                    return {'2026-09-16':99.,day:102.} if invalid=='revision' else {day:float('nan')}
                with patch('refresh.closed_session',return_value='2026-09-17'):
                    with self.assertRaises(ValueError):
                        refresh.advance(state,self.now,lambda c,d:self.prices[c],loader)
                self.assertEqual(state,before)

    def test_missing_session_cannot_hide_same_etf_history_revision(self):
        for history in self.prices.values():history['2026-09-17']={'close':101.,'volume':100}
        state=copy.deepcopy(self.state);before=copy.deepcopy(state)
        def loader(symbol,day):
            if symbol=='VOO':
                return refresh.parse_us(chart(days=('2026-09-16',),values=(99.,)),symbol,day)
            return {day:102.}
        with patch('refresh.closed_session',return_value='2026-09-17'):
            with self.assertRaisesRegex(ValueError,'history revision'):
                refresh.advance(state,self.now,lambda c,d:self.prices[c],loader)
        self.assertEqual(state,before)


if __name__=='__main__':unittest.main()
