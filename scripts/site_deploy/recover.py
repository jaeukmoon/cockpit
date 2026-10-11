"""Recover only obsolete/stalled main runs of the dedicated site publisher."""
import json
import os
from datetime import datetime,timezone,timedelta
import time
import urllib.request
from pathlib import Path

WORKFLOW='publish-pages.yml'
ACTIVE={'queued','pending','waiting','in_progress','requested'}
STATE_PATH=Path('.recovery/status.json')

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):return None

class GitHub:
    def __init__(self,repo,token):
        if repo!='jaeukmoon/cockpit' or not token:raise ValueError('Declared repository and Actions token required')
        self.base='https://api.github.com/repos/'+repo+'/';self.token=token
    def call(self,path,method='GET'):
        request=urllib.request.Request(self.base+path,method=method,headers={'Authorization':'Bearer '+self.token,'Accept':'application/vnd.github+json','User-Agent':'WBQDeploymentRecovery'})
        with urllib.request.build_opener(NoRedirect()).open(request,timeout=15) as response:
            raw=response.read();return json.loads(raw) if raw else {}
    def get(self,run):return self.call('actions/runs/'+str(run))
    def cancel(self,run):return self.call('actions/runs/'+str(run)+'/cancel','POST')
    def rerun(self,run):return self.call('actions/runs/'+str(run)+'/rerun','POST')
    def runs(self):
        path='actions/workflows/'+WORKFLOW+'/runs?branch=main&per_page=100'
        result=self.call(path);records={r['id']:r for r in result['workflow_runs']}
        if result['total_count']>100:
            for status in sorted(ACTIVE):
                active=self.call(path+'&status='+status)
                if active['total_count']>100:raise ValueError('Active deployment inventory truncated')
                records.update({r['id']:r for r in active['workflow_runs']})
        return list(records.values())

def validate_state(state):
    if not isinstance(state,dict) or set(state)!={'schema','pending_reruns'} or state['schema']!=1:raise ValueError('Invalid recovery state')
    ids=state['pending_reruns']
    if not isinstance(ids,list) or any(type(i)!=int or i<=0 for i in ids) or len(ids)!=len(set(ids)):raise ValueError('Invalid pending deployment IDs')
    return state

def save_state(state):
    validate_state(state);STATE_PATH.parent.mkdir(parents=True,exist_ok=True)
    temp=STATE_PATH.with_suffix('.tmp');temp.write_text(json.dumps(state),encoding='utf-8');temp.replace(STATE_PATH)

def owns(run):
    return run.get('path')=='.github/workflows/'+WORKFLOW and run.get('head_branch')=='main'

def age(run,now):
    value=run.get('run_started_at') if run.get('run_attempt',1)>1 else run.get('created_at')
    if not value:raise ValueError('Run attempt timing unavailable')
    start=datetime.fromisoformat(value.replace('Z','+00:00'))
    if start.tzinfo is None or start>now:raise ValueError('Invalid run attempt timing')
    return now-start

def recover(client,runs,now,*,sleep=time.sleep,state=None,save=lambda state:None):
    state=validate_state(state if state is not None else {'schema':1,'pending_reruns':[]})
    owned=[r for r in runs if owns(r)]
    selected=[r for r in owned if r.get('status') in ACTIVE]
    if not owned:
        if state['pending_reruns']:raise ValueError('Latest deployment identity unavailable')
        return []
    selected.sort(key=lambda r:(r['created_at'],r['id']))
    newest=max(owned,key=lambda r:(r['created_at'],r['id']));actions=[]
    def clear(run_id):
        state['pending_reruns'].remove(run_id);save(state)
    def finish(run_id):
        stopped=client.get(run_id)
        if not owns(stopped):raise ValueError('Run ownership changed')
        fresh=[r for r in client.runs() if owns(r)]
        if not fresh:raise ValueError('Latest deployment identity unavailable')
        latest=max(fresh,key=lambda r:(r['created_at'],r['id']))
        if latest['id']!=run_id or stopped.get('run_attempt')!=1:
            clear(run_id);return
        if stopped.get('status')=='completed':
            if stopped.get('conclusion')=='cancelled':
                client.rerun(run_id);actions.append({'run_id':run_id,'action':'rerun_once'})
            clear(run_id)
    # Only cancellations requested by this watchdog are eligible on later checks.
    for run_id in list(state['pending_reruns']):finish(run_id)
    for candidate in selected:
        current=client.get(candidate['id'])
        if not owns(current) or current.get('status') not in ACTIVE:continue
        stale=age(current,now)>timedelta(minutes=20)
        obsolete=current['id']!=newest['id']
        if not stale and not obsolete:continue
        retry=not obsolete and current.get('run_attempt')==1
        if retry and current['id'] not in state['pending_reruns']:
            state['pending_reruns'].append(current['id']);save(state)
        client.cancel(current['id']);actions.append({'run_id':current['id'],'action':'cancel_obsolete' if obsolete else 'cancel_stalled'})
        if not retry:continue
        # Cancellation must actually finish. Re-fetch ownership/attempt before rerun.
        for _ in range(6):
            sleep(2);stopped=client.get(current['id'])
            if not owns(stopped):raise ValueError('Run ownership changed')
            if stopped.get('status')=='completed':break
        finish(current['id'])
    return actions

def main():
    client=GitHub(os.environ['GITHUB_REPOSITORY'],os.environ['GH_TOKEN'])
    state=validate_state(json.loads(STATE_PATH.read_text(encoding='utf-8'))) if STATE_PATH.exists() else {'schema':1,'pending_reruns':[]}
    runs=client.runs()
    actions=recover(client,runs,datetime.now(timezone.utc),state=state,save=save_state)
    print(json.dumps({'status':'CANCELLATION_PENDING' if state['pending_reruns'] else 'DEPLOYMENT_QUEUE_CHECKED','actions':actions}))

if __name__=='__main__':main()
