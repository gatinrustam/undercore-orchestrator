#!/usr/bin/env python3
"""Package a committed snapshot; never include runtime data or the working tree."""
import argparse,hashlib,io,json,re,subprocess,tarfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def git(*args): return subprocess.check_output(['git','-C',str(ROOT),*args])
def allowed(path):
    return (path in ('VERSION','requirements.lock') or
            any(path.startswith(prefix) for prefix in ('orchestrator/','deploy/','config/','contracts/','scripts/')))
def main():
    p=argparse.ArgumentParser();p.add_argument('--ref',default='HEAD');p.add_argument('--output',type=Path,default=ROOT/'dist');a=p.parse_args()
    commit=git('rev-parse','--verify','--end-of-options',a.ref+'^{commit}').decode().strip()
    assert re.fullmatch(r'[a-f0-9]{40}',commit)
    files={}
    for path in git('ls-tree','-rz','--name-only',commit).decode().split('\0'):
        if path and allowed(path):
            if path.endswith(('.token','.key','.db','.sqlite3')) or path=='config/settings.json': raise ValueError('Runtime file in release')
            files[path]=git('show',commit+':'+path)
    version=files['VERSION'].decode().strip();assert re.fullmatch(r'\d+\.\d+\.\d+',version)
    metadata={'version':version,'commit':commit,'journal_schema':1,'files':{k:hashlib.sha256(v).hexdigest() for k,v in files.items()}}
    files['release.json']=(json.dumps(metadata,indent=2)+'\n').encode()
    a.output.mkdir(parents=True,exist_ok=True)
    name='undercore-orchestrator-v'+version+'.tar.gz';target=a.output/name
    with tarfile.open(target,'w:gz') as tar:
        for path,data in sorted(files.items()):
            info=tarfile.TarInfo(path);info.size=len(data);info.mode=0o644;tar.addfile(info,io.BytesIO(data))
    digest=hashlib.sha256(target.read_bytes()).hexdigest()
    (a.output/'SHA256SUMS').write_text(digest+'  '+name+'\n')
    print(json.dumps({'archive':str(target),'sha256':digest,**{k:v for k,v in metadata.items() if k!='files'}}))
if __name__=='__main__':main()
