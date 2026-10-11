"""Package explicit public files and verify the bytes served by GitHub Pages."""
import argparse
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin

PUBLIC_TOP=('index.html','dashboard.html','analysis.html','og.png','robots.txt','.nojekyll')
PUBLIC_DATA=('analysis.enc.json','assets.enc.json','cockpit.enc.json','portfolio.enc.json','monitor-key.enc.json','monitoring.enc.json','guru_13f.json')
PUBLIC_ASSETS=('monitoring.js',)
MANIFEST='deployment-version.json'
FILES=(*PUBLIC_TOP,*('data/'+n for n in PUBLIC_DATA),*('assets/'+n for n in PUBLIC_ASSETS))

def digest(raw):return hashlib.sha256(raw).hexdigest()

def package(root,out,source_sha):
    root=Path(root).resolve();out=Path(out).resolve()
    if out.exists():raise ValueError('Preserve existing artifact; choose an empty output directory')
    if len(source_sha)!=40 or any(c not in '0123456789abcdef' for c in source_sha):raise ValueError('Invalid source commit')
    for directory,names in (('data',PUBLIC_DATA),('assets',PUBLIC_ASSETS)):
        if {p.name for p in (root/directory).iterdir()}!=set(names):raise ValueError('Unexpected or missing public files: '+directory)
    contents={}
    for name in FILES:
        path=root/name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):raise ValueError('Unsafe/missing public file: '+name)
        raw=path.read_bytes()
        if len(raw)>16_000_000:raise ValueError('Public file exceeds verification bound')
        contents[name]=raw
    out.mkdir(parents=True)
    for name,raw in contents.items():
        target=out/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
    manifest={'schema':1,'source_sha':source_sha,'files':{n:digest(v) for n,v in contents.items()}}
    (out/MANIFEST).write_text(json.dumps(manifest,sort_keys=True,indent=2)+'\n',encoding='utf-8')
    return manifest

def fetch(url):
    request=urllib.request.Request(url,headers={'User-Agent':'WBQDeploymentVerifier','Cache-Control':'no-cache'})
    with urllib.request.urlopen(request,timeout=10) as response:
        if response.status!=200:raise ValueError('HTTP response is not successful')
        raw=response.read(16_000_001)
        if len(raw)>16_000_000:raise ValueError('HTTP content exceeds verification bound')
        return raw

def verify_once(manifest,base,fetcher=fetch):
    if base!='https://jaeukmoon.github.io/cockpit/':raise ValueError('Unexpected deployment destination')
    if manifest.get('schema')!=1 or set(manifest.get('files',{}))!=set(FILES):raise ValueError('Invalid expected deployment manifest')
    if json.loads(fetcher(urljoin(base,MANIFEST)))!=manifest:raise ValueError('Served manifest is stale or differs')
    for name,expected in manifest['files'].items():
        if digest(fetcher(urljoin(base,name)))!=expected:raise ValueError('Served file is stale or differs: '+name)
    return True

def verify(manifest,base,*,timeout=180,fetcher=fetch,clock=time.monotonic,sleep=time.sleep):
    start=clock();attempt=0
    while True:
        attempt+=1
        try:
            verify_once(manifest,base,fetcher)
            return {'status':'SERVED_ASSETS_VERIFIED','source_sha':manifest['source_sha'],'files':len(manifest['files']),'attempts':attempt}
        except (ValueError,urllib.error.URLError,TimeoutError) as error:
            if clock()-start>=timeout:raise RuntimeError('Deployment verification timed out: '+str(error)) from None
            sleep(min(10,max(0,timeout-(clock()-start))))

def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    pack=sub.add_parser('package');pack.add_argument('--root',default='.');pack.add_argument('--out',default='_site');pack.add_argument('--source-sha',required=True)
    check=sub.add_parser('verify');check.add_argument('--manifest',required=True);check.add_argument('--url',default='https://jaeukmoon.github.io/cockpit/')
    args=parser.parse_args()
    result=package(args.root,args.out,args.source_sha) if args.action=='package' else verify(json.loads(Path(args.manifest).read_text()),args.url)
    print(json.dumps(result if args.action=='verify' else {'status':'PACKAGED','source_sha':result['source_sha'],'files':len(result['files'])}))

if __name__=='__main__':main()
