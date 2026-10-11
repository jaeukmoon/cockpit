"""Local disposable Git remotes verify encrypted-state ownership under concurrency."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('publisher',ROOT/'scripts/cloud_monitor/publish_state.py')
publisher=importlib.util.module_from_spec(spec);spec.loader.exec_module(publisher)

class RefreshPublicationTests(unittest.TestCase):
    def git(self,root,*args):
        result=subprocess.run(['git','-C',str(root),*args],capture_output=True,text=True)
        if result.returncode:raise RuntimeError(result.stderr)
        return result.stdout.strip()
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.base=Path(temp.name)
        self.remote=self.base/'remote';self.remote.mkdir();self.git(self.remote,'init','--bare','--initial-branch=main')
        self.root=self.base/'source';self.root.mkdir();self.git(self.root,'init','--initial-branch=main')
        self.git(self.root,'config','user.name','Fixture');self.git(self.root,'config','user.email','fixture@example.test')
        self.original={name:b'old encrypted fixture' for name in publisher.OWNED}
        self.code='scripts/cloud_monitor/refresh.py'
        for name,raw in {**self.original,self.code:b'fixture source'}.items():
            p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(raw)
        self.git(self.root,'add','--',*publisher.OWNED,self.code);self.git(self.root,'commit','-m','fixture')
        self.git(self.root,'remote','add','origin',str(self.remote));self.git(self.root,'push','origin','HEAD:main')
        self.receipt={'status':'PARTIAL_HELD','before':{n:hashlib.sha256(v).hexdigest() for n,v in self.original.items()},
            'after':{},'source_hashes':{self.code:hashlib.sha256(b'fixture source').hexdigest()}}
        for name in publisher.OWNED:
            value=b'new encrypted fixture';(self.root/name).write_bytes(value);self.receipt['after'][name]=hashlib.sha256(value).hexdigest()
        (self.root/publisher.RECEIPT).write_text(json.dumps(self.receipt))
    def change_remote(self,path,value):
        peer=self.base/'peer';self.git(self.base,'clone',str(self.remote),str(peer))
        self.git(peer,'config','user.name','Other fixture');self.git(peer,'config','user.email','other@example.test')
        p=peer/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(value)
        self.git(peer,'add','--',path);self.git(peer,'commit','-m','other writer');self.git(peer,'push','origin','HEAD:main')
    def test_unrelated_main_change_is_preserved(self):
        self.change_remote('README.md',b'other writer')
        result=publisher.publish(self.root)
        self.assertEqual(result['data_state'],'PARTIAL_HELD')
        self.assertEqual(self.git(self.remote,'show','main:README.md'),'other writer')
        for name in publisher.OWNED:self.assertEqual(self.git(self.remote,'show','main:'+name),'new encrypted fixture')
    def test_overlapping_remote_state_is_not_overwritten(self):
        self.change_remote('monitoring/state.enc.json',b'foreign encrypted state')
        with self.assertRaisesRegex(ValueError,'Remote monitoring state changed'):publisher.publish(self.root)
        self.assertEqual(self.git(self.remote,'show','main:monitoring/state.enc.json'),'foreign encrypted state')
    def test_changed_collector_source_requires_recollection(self):
        self.change_remote(self.code,b'new algorithm')
        with self.assertRaisesRegex(ValueError,'Collector code changed'):publisher.publish(self.root)
    def test_changed_output_after_receipt_fails_closed(self):
        (self.root/'data/monitoring.enc.json').write_bytes(b'changed after validation')
        with self.assertRaisesRegex(ValueError,'output changed'):publisher.publish(self.root)
    def test_failed_or_unvalidated_refresh_never_attempts_publication(self):
        for status in ('IN_PROGRESS','FAILED_CLOSED','UNKNOWN'):
            with self.subTest(status=status):
                self.receipt['status']=status
                (self.root/publisher.RECEIPT).write_text(json.dumps(self.receipt))
                with patch.object(publisher,'git') as git:
                    with self.assertRaisesRegex(ValueError,'Validated scoped refresh receipt'):
                        publisher.publish(self.root)
                    git.assert_not_called()
    def test_publication_failure_is_not_downgraded_to_partial_success(self):
        old_tip=self.git(self.remote,'rev-parse','main')
        original_git=publisher.git
        def fail_push(root,*args,**kwargs):
            if args[0]=='push':raise RuntimeError('Synthetic push failure')
            return original_git(root,*args,**kwargs)
        with patch.object(publisher,'git',side_effect=fail_push):
            with self.assertRaisesRegex(RuntimeError,'Synthetic push failure'):
                publisher.publish(self.root)
        self.assertEqual(self.git(self.remote,'rev-parse','main'),old_tip)
        for name in publisher.OWNED:
            self.assertEqual(self.git(self.remote,'show','main:'+name),'old encrypted fixture')

if __name__=='__main__':unittest.main()
