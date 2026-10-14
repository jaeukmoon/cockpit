"""Publish only a validated collector's two files without adopting remote overlap."""
import hashlib
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile

OWNED=('monitoring/state.enc.json','data/monitoring.enc.json')
RECEIPT='.refresh-receipt.json'

def git(root,*args,env=None):
    p=subprocess.run(['git','-C',str(root),*args],capture_output=True,text=True,timeout=90,env=env)
    if p.returncode:raise RuntimeError('Scoped state publication failed at git '+args[0])
    return p.stdout.strip()

def publish(root):
    root=Path(root);receipt=json.loads((root/RECEIPT).read_text(encoding='utf-8'))
    if receipt.get('status') not in ('READY','PARTIAL_HELD','DELAYED') or set(receipt.get('before',{}))!=set(OWNED) or set(receipt.get('after',{}))!=set(OWNED):
        raise ValueError('Validated scoped refresh receipt required')
    if receipt['status']=='DELAYED' and receipt['before']['monitoring/state.enc.json']!=receipt['after']['monitoring/state.enc.json']:
        raise ValueError('Delayed refresh must preserve the last-good ledger')
    contents={name:(root/name).read_bytes() for name in OWNED}
    for name,raw in contents.items():
        if hashlib.sha256(raw).hexdigest()!=receipt['after'][name]:raise ValueError('Collector output changed after validation')
    git(root,'fetch','origin','main')
    tip=git(root,'rev-parse','origin/main')
    for name in OWNED:
        p=subprocess.run(['git','-C',str(root),'show',tip+':'+name],capture_output=True,timeout=20)
        if p.returncode or hashlib.sha256(p.stdout).hexdigest()!=receipt['before'][name]:raise ValueError('Remote monitoring state changed; recollect latest state')
    source_hashes=receipt.get('source_hashes',{})
    if not source_hashes:raise ValueError('Collector source identity unavailable')
    for name,expected in source_hashes.items():
        if not name.startswith('scripts/cloud_monitor/') or Path(name).suffix!='.py' or '..' in Path(name).parts:raise ValueError('Unexpected collector source scope')
        p=subprocess.run(['git','-C',str(root),'show',tip+':'+name],capture_output=True,timeout=20)
        if p.returncode or hashlib.sha256(p.stdout).hexdigest()!=expected:raise ValueError('Collector code changed; recollect with latest source')
    auth_env=dict(os.environ)
    if os.environ.get('GH_TOKEN'):
        value=base64.b64encode(('x-access-token:'+os.environ['GH_TOKEN']).encode()).decode()
        auth_env.update(GIT_CONFIG_COUNT='1',GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',GIT_CONFIG_VALUE_0='AUTHORIZATION: basic '+value)
    # New local commits or dirty files outside OWNED cannot enter this transaction.
    with tempfile.TemporaryDirectory(prefix='wbq-refresh-publish-') as directory:
        git(directory,'clone','--shared','--no-checkout',str(root),'.')
        git(directory,'checkout','--detach',tip)
        git(directory,'remote','set-url','origin',git(root,'remote','get-url','origin'))
        for name,raw in contents.items():(Path(directory)/name).write_bytes(raw)
        git(directory,'add','--',*OWNED)
        names=git(directory,'diff','--cached','--name-only').splitlines()
        if any(name not in OWNED for name in names):raise ValueError('Unexpected publication scope')
        if not names:return {'status':'UNCHANGED','commit':tip}
        git(directory,'-c','user.name=github-actions[bot]','-c','user.email=41898282+github-actions[bot]@users.noreply.github.com','commit','-m','data: append validated closing observations')
        commit=git(directory,'rev-parse','HEAD');git(directory,'push','origin','HEAD:main',env=auth_env)
        if git(directory,'ls-remote','origin','refs/heads/main',env=auth_env).split()[0]!=commit:raise ValueError('Remote refresh commit unconfirmed')
        return {'status':'SCOPED_REFRESH_PUBLISHED','commit':commit,'data_state':receipt['status']}

if __name__=='__main__':print(json.dumps(publish(Path(__file__).resolve().parents[2])))
