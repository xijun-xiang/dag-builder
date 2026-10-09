"""One-shot held chain: CPU initialization, four GPU/CPU pairs. No retries."""
import argparse
import importlib.util
import os
from pathlib import Path
import subprocess

from pals_validation.io import read,save,sha256,verify
from pals_validation.locking import exclusive_lock
from pals_validation.recovery.e2_formal import ROOT,OLD,RECOVERY,PROJECT,BENCHES,check as runtime_check
from pals_validation.recovery.e2_eos import require

REPO=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('legacy_submission',REPO/'scripts/e2_eos_submit.py')
legacy=importlib.util.module_from_spec(spec);spec.loader.exec_module(legacy)
STAGES=(('prepare',None),)+tuple((action,b) for b in BENCHES for action in ('launch','audit'))
LAUNCHER=REPO/'scripts/b1-e2-formal.sbatch'
EXCLUDE='tko-b1-nv-dgx06'

def safe(p):
    require(p.is_absolute() and p.resolve()==p and PROJECT in p.parents and PROJECT.resolve()==PROJECT,'project boundary')
    return p

def active(allowed=()):
    for line in subprocess.check_output(['squeue','-h','-u','xijun','-o','%i %j'],text=True).splitlines():
        jid,name=line.split(maxsplit=1)
        require(not name.startswith(('pals-llama-','pals-e2-','e3g-','e3c-','e3x-')) or jid in allowed,'related active chain')

def prior():
    for jid in ('115115','115116','115117'):legacy.successful(jid)
    a=read(RECOVERY/'audit.json')
    require(a['status']=='PASS' and a['scored_new']==491 and a['generation_calls']==0 and
            a['protocol_id']=='845a18f3fa2c31516d73be43205ea37b7cd980bda32cf7f8eb1429b405b9c864','recovery identity')
    states=legacy.scheduler_rows(['115010','115011','115012','115013'])
    require(states.get('115010')==['FAILED','1:0'] and all(states.get(j,[''])[0].startswith('CANCELLED') for j in ('115011','115012','115013')),'prior slots changed')
    for b in BENCHES:require(not (OLD/b/'e2/llama3-8b/formal-e2').exists(),'old formal attempt')
    return sha256(RECOVERY/'audit.json')

def prepare():
    safe(ROOT);safe(REPO);active();source=prior()
    require(not ROOT.exists(),'existing deployment')
    require(not subprocess.check_output(['git','-C',str(REPO),'status','--porcelain'],text=True).strip(),'source not clean')
    ROOT.mkdir(mode=0o700)
    for name in ('submissions','logs'):(ROOT/name).mkdir()
    for i,_ in enumerate(STAGES):
        for name in ('enroot-cache','enroot-data','enroot-runtime','tmp','hf','hf-modules','cache','cuda'):
            (ROOT/'scratch'/str(i)/name).mkdir(parents=True)
    files=[*sorted((REPO/'src').rglob('*.py')),LAUNCHER,Path(__file__),REPO/'scripts/e2_eos_submit.py']
    save(ROOT/'deployment.json',{'repo':str(REPO),'source_audit_sha256':source,
         'files':{str(p.relative_to(REPO)):sha256(p) for p in files},'stages':list(STAGES)})

def check():
    safe(ROOT);safe(REPO);d=read(ROOT/'deployment.json')
    require(d['repo']==str(REPO) and d['source_audit_sha256']==sha256(RECOVERY/'audit.json'),'deployment identity')
    verify(REPO,d['files'])

def command(i,previous):
    action,b=STAGES[i];gpu=action=='launch';name='pals-e2-formal-'+str(i)
    cmd=['sbatch','--parsable','--hold','--no-requeue','--partition=defq','--reservation=code-agent',
         '--nodes=1','--ntasks=1','--cpus-per-task='+('32' if gpu else '8'),'--mem='+('256G' if gpu else '64G'),
         '--time='+('08:00:00' if gpu else '01:00:00'),'--exclude='+EXCLUDE,'--job-name='+name,
         '--chdir='+str(ROOT),'--export=ALL','--output='+str(ROOT/'logs'/('%j-'+name+'.out')),
         '--error='+str(ROOT/'logs'/('%j-'+name+'.err')),'--gpus-per-node=8' if gpu else '--gres=none']
    if previous:cmd+=['--dependency=afterok:'+previous,'--kill-on-invalid-dep=yes']
    return cmd+[str(LAUNCHER)]

def held(r,previous):
    i=r['index'];jid=r['job_id'];action,b=STAGES[i];gpu=action=='launch';name='pals-e2-formal-'+str(i)
    require(r['command']==command(i,previous),'command receipt')
    text=subprocess.check_output(['scontrol','show','job',jid,'-o'],text=True).strip()
    require(len(text.splitlines())==1,'ambiguous state')
    f=dict(s.split('=',1) for s in text.split() if '=' in s)
    expected={'JobId':jid,'JobName':name,'JobState':'PENDING','Priority':'0','Partition':'defq','Reservation':'code-agent',
        'ExcNodeList':EXCLUDE,'NumTasks':'1','NumCPUs':'32' if gpu else '8','CPUs/Task':'32' if gpu else '8',
        'TimeLimit':'08:00:00' if gpu else '01:00:00','Requeue':'0','Restarts':'0','Command':str(LAUNCHER),
        'WorkDir':str(ROOT),'StdOut':str(ROOT/'logs'/(jid+'-'+name+'.out')),'StdErr':str(ROOT/'logs'/(jid+'-'+name+'.err'))}
    require(all(f.get(k)==v for k,v in expected.items()),'held resource/path mismatch')
    require(f.get('UserId','').startswith('xijun(') and f.get('NumNodes') in ('1','1-1'),'owner/nodes')
    tres=dict(s.split('=',1) for s in f.get('ReqTRES','').split(',') if '=' in s)
    require(tres.get('cpu')==expected['NumCPUs'] and tres.get('mem')==('256G' if gpu else '64G') and tres.get('node')=='1','resources')
    require(tres.get('gres/gpu')=='8' if gpu else not any('gpu' in k and v!='0' for k,v in tres.items()),'GPU resources')
    if previous:require(f.get('Dependency') in ('afterok:'+previous,'afterok:'+previous+'(unfulfilled)') and f.get('KillOInInvalidDependent')=='Yes','dependency')
    else:require(f.get('Dependency')=='(null)','unexpected dependency')
    return f

def submit():
    check();prior();ledger=ROOT/'submissions'
    with exclusive_lock(ledger/'submit.lock'):
        require(not list(ledger.glob('*attempt.json')),'existing/uncertain attempt');active()
        previous=None;records=[]
        for i,(action,b) in enumerate(STAGES):
            cmd=command(i,previous)
            env={k:v for k,v in os.environ.items() if not k.startswith(('PALS_','SBATCH_'))}
            env.update(PALS_REPO=str(REPO),PALS_FORMAL_INDEX=str(i))
            save(ledger/(str(i)+'-attempt.json'),{'command':cmd})
            jid=subprocess.check_output(cmd,cwd=ROOT,env=env,text=True).strip().split(';')[0]
            require(jid.isdigit(),'uncertain submission')
            r={'job_id':jid,'index':i,'action':action,'benchmark':b,'command':cmd}
            save(ledger/(str(i)+'-submitted.json'),r);records.append(r);previous=jid
        save(ledger/'chain.json',records)

def release():
    check();prior();ledger=ROOT/'submissions'
    with exclusive_lock(ledger/'submit.lock'):
        require(not list(ledger.glob('release-*-attempt.json')),'release attempted')
        records=read(ledger/'chain.json')
        require([r['index'] for r in records]==list(range(9)) and len({r['job_id'] for r in records})==9,'incomplete chain')
        active({r['job_id'] for r in records});previous=None;states={}
        for r in records:
            require(r==read(ledger/(str(r['index'])+'-submitted.json')),'receipt mismatch')
            states[r['job_id']]=held(r,previous);previous=r['job_id']
        save(ledger/'held-audit.json',{'status':'PASS','states':states})
        for r in reversed(records):
            jid=r['job_id'];save(ledger/('release-'+jid+'-attempt.json'),{'job_id':jid})
            subprocess.run(['scontrol','release',jid],check=True)
            save(ledger/('release-'+jid+'-done.json'),{'job_id':jid})
        save(ledger/'released.json',{'status':'RELEASED','jobs':[r['job_id'] for r in records]})

def admit(i):
    check();records=read(ROOT/'submissions/chain.json');r=records[i]
    require(r['job_id']==os.environ.get('SLURM_JOB_ID') and r['index']==i,'allocation identity')
    if i:legacy.successful(records[i-1]['job_id'])
    action,b=STAGES[i]
    if i:
        runtime_check(ROOT)
        require(read(ROOT/'ready.json')['status']=='PASS','CPU ready')
        if action=='audit':require(read(ROOT/b/'gpu-complete.json')['status']=='PASS','GPU application')
        elif i>1:require(read(ROOT/STAGES[i-1][1]/'audit.json')['status']=='PASS','prior audit')
    save(ROOT/(str(i)+'-admitted.json'),{'status':'PASS','job_id':r['job_id']})

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('action',choices=('prepare','submit','release','admit'));ap.add_argument('--index',type=int)
    a=ap.parse_args();os.umask(0o077)
    os.environ['PATH']='/cm/local/apps/slurm/current/bin:'+os.environ['PATH'];os.environ['SLURM_CONF']='/cm/shared/apps/slurm/etc/slurm/slurm.conf'
    if a.action=='admit':admit(a.index)
    else:globals()[a.action]()
    print({'status':'PASS','action':a.action},flush=True)
