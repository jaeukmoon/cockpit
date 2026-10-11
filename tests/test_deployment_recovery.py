"""Disposable deployment, queue and publication failure fixtures; no real API."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime,timedelta,timezone

ROOT=Path(__file__).resolve().parents[1]
def module(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
site=module('static_site','scripts/site_deploy/static_site.py')
recovery=module('recovery','scripts/site_deploy/recover.py')

class StaticTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)/'source';self.root.mkdir()
        for name in site.FILES:
            p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(('fixture:'+name).encode())
        self.out=Path(temp.name)/'site'
    def test_exact_assets_and_no_private_source(self):
        (self.root/'.env').write_text('PRIVATE_SENTINEL')
        m=site.package(self.root,self.out,'a'*40)
        self.assertEqual(set(m['files']),set(site.FILES));self.assertFalse((self.out/'.env').exists())
        getter=lambda url:(self.out/url.split('/cockpit/',1)[1]).read_bytes()
        self.assertTrue(site.verify_once(m,'https://jaeukmoon.github.io/cockpit/',getter))
    def test_fresh_manifest_with_stale_encrypted_asset_fails(self):
        m=site.package(self.root,self.out,'a'*40)
        def get(url):return b'old' if url.endswith('cockpit.enc.json') else (self.out/url.split('/cockpit/',1)[1]).read_bytes()
        with self.assertRaisesRegex(ValueError,'Served file'):site.verify_once(m,'https://jaeukmoon.github.io/cockpit/',get)
    def test_stale_manifest_fails_without_claiming_success(self):
        m=site.package(self.root,self.out,'a'*40)
        with self.assertRaises(ValueError):site.verify_once(m,'https://jaeukmoon.github.io/cockpit/',lambda url:b'{}')
    def test_unexpected_public_directory_file_fails(self):
        (self.root/'data/private.txt').write_text('PRIVATE')
        with self.assertRaises(ValueError):site.package(self.root,self.out,'a'*40)
        self.assertFalse(self.out.exists())
    def test_missing_file_fails_not_zero(self):
        (self.root/'index.html').rename(self.root/'index.backup')
        with self.assertRaises(ValueError):site.package(self.root,self.out,'a'*40)
    def test_timeout_is_non_success(self):
        m=site.package(self.root,self.out,'a'*40);ticks=iter([0,0,0,20])
        with self.assertRaises(RuntimeError):site.verify(m,'https://jaeukmoon.github.io/cockpit/',timeout=1,fetcher=lambda url:b'{}',clock=lambda:next(ticks),sleep=lambda t:None)

class FakeGitHub:
    def __init__(self,runs):self.records={r['id']:dict(r) for r in runs};self.actions=[]
    def get(self,run):return dict(self.records[run])
    def runs(self):return list(self.records.values())
    def cancel(self,run):self.actions.append(('cancel',run));self.records[run].update(status='completed',conclusion='cancelled')
    def rerun(self,run):self.actions.append(('rerun',run));self.records[run].update(status='queued',run_attempt=2,run_started_at='2026-10-11T13:00:00+00:00')

class RecoveryTests(unittest.TestCase):
    now=datetime(2026,10,11,13,tzinfo=timezone.utc)
    def run_record(self,id,minutes=30,attempt=1,path=None):
        return {'id':id,'head_branch':'main','path':path or '.github/workflows/publish-pages.yml','status':'queued','conclusion':None,'run_attempt':attempt,'created_at':(self.now-timedelta(minutes=minutes)).isoformat(),'run_started_at':(self.now-timedelta(minutes=minutes)).isoformat()}
    def test_collector_never_cancelled_and_old_site_superseded(self):
        records=[self.run_record(1,40,path='.github/workflows/research-monitor.yml'),self.run_record(2,30),self.run_record(3,1)]
        c=FakeGitHub(records);recovery.recover(c,records,self.now,sleep=lambda t:None)
        self.assertEqual(c.actions,[('cancel',2)])
    def test_stalled_latest_reruns_once_and_attempt_time_is_used(self):
        records=[self.run_record(2)];c=FakeGitHub(records)
        recovery.recover(c,records,self.now,sleep=lambda t:None)
        self.assertEqual(c.actions,[('cancel',2),('rerun',2)])
        recovery.recover(c,c.runs(),self.now,sleep=lambda t:None)
        self.assertEqual(len(c.actions),2)
        c.records[2]['run_started_at']=(self.now-timedelta(minutes=25)).isoformat()
        recovery.recover(c,c.runs(),self.now,sleep=lambda t:None)
        self.assertEqual(c.actions[-1],('cancel',2));self.assertEqual(sum(a[0]=='rerun' for a in c.actions),1)
    def test_newer_completed_run_prevents_old_rerun(self):
        old=self.run_record(2);new=self.run_record(3,1);new.update(status='completed',conclusion='success')
        c=FakeGitHub([old,new]);recovery.recover(c,c.runs(),self.now,sleep=lambda t:None)
        self.assertEqual(c.actions,[('cancel',2)])
    def test_cancel_race_cannot_target_another_workflow(self):
        old=self.run_record(2);c=FakeGitHub([old]);c.records[2]['path']='.github/workflows/research-monitor.yml'
        recovery.recover(c,[old],self.now,sleep=lambda t:None);self.assertEqual(c.actions,[])

    def test_delayed_cancellation_is_recovered_on_next_check_only_once(self):
        class Delayed(FakeGitHub):
            def cancel(self,run):self.actions.append(('cancel',run))
        c=Delayed([self.run_record(2)]);state={'schema':1,'pending_reruns':[]};saved=[]
        recovery.recover(c,c.runs(),self.now,state=state,save=lambda s:saved.append(json.loads(json.dumps(s))),sleep=lambda t:None)
        self.assertEqual(c.actions,[('cancel',2)]);self.assertEqual(state['pending_reruns'],[2])
        self.assertEqual(saved[0]['pending_reruns'],[2])
        c.records[2].update(status='completed',conclusion='cancelled')
        recovery.recover(c,c.runs(),self.now,state=state,sleep=lambda t:None)
        self.assertEqual(c.actions,[('cancel',2),('rerun',2)]);self.assertEqual(state['pending_reruns'],[])
        recovery.recover(c,c.runs(),self.now,state=state,sleep=lambda t:None)
        self.assertEqual(len(c.actions),2)

    def test_unmarked_human_cancellation_is_not_retried(self):
        record=self.run_record(2);record.update(status='completed',conclusion='cancelled');c=FakeGitHub([record])
        recovery.recover(c,c.runs(),self.now,sleep=lambda t:None);self.assertEqual(c.actions,[])

    def test_pending_retry_is_discarded_when_newer_deployment_completed(self):
        old=self.run_record(2);old.update(status='completed',conclusion='cancelled')
        new=self.run_record(3,1);new.update(status='completed',conclusion='success')
        c=FakeGitHub([old,new]);state={'schema':1,'pending_reruns':[2]}
        recovery.recover(c,c.runs(),self.now,state=state,sleep=lambda t:None)
        self.assertEqual(c.actions,[]);self.assertEqual(state['pending_reruns'],[])

    def test_invalid_persistent_state_fails_closed(self):
        c=FakeGitHub([self.run_record(2)])
        with self.assertRaises(ValueError):recovery.recover(c,c.runs(),self.now,state={'schema':1,'pending_reruns':['2']})
        self.assertEqual(c.actions,[])

class WorkflowTests(unittest.TestCase):
    def test_refresh_has_no_deployment_dependency(self):
        source=(ROOT/'.github/workflows/research-monitor.yml').read_text()
        self.assertNotIn('package-site:',source);self.assertNotIn('deploy-pages',source)
        self.assertIn('group: encrypted-research-refresh',source);self.assertIn('ref: main',source)
        self.assertIn('publish_state.py',source);self.assertNotIn('continue-on-error:',source)
    def test_site_uses_independent_latest_committed_artifact_and_verification(self):
        source=(ROOT/'.github/workflows/publish-pages.yml').read_text()
        for required in ['cancel-in-progress: true','workflow_run:','ref: main','source_sha:','timeout-minutes: 5','static_site.py verify','git ls-remote']:
            self.assertIn(required,source)
        self.assertNotIn('needs: refresh',source);self.assertNotIn('MONITORING_DATA_KEY',source)
    def test_watchdog_is_separate_and_scoped(self):
        source=(ROOT/'.github/workflows/recover-pages.yml').read_text()
        self.assertIn("cron: '*/15",source);self.assertIn('static-pages-recovery',source)
        self.assertIn('actions: write',source);self.assertNotIn('contents: write',source)
        self.assertIn('actions/cache/restore@v4',source);self.assertIn('actions/cache/save@v4',source)
        self.assertEqual(source.count('key: pages-recovery-${{ github.run_id }}-${{ github.run_attempt }}'),2)

if __name__=='__main__':unittest.main()
