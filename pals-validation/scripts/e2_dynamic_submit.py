"""Replace only the user's pending MMLU pair after HOLD audit; no duplicate runs."""
import argparse
import importlib.util
import os
from pathlib import Path
import subprocess
import time

from pals_validation.io import read, save, sha256, verify
from pals_validation.locking import exclusive_lock
from pals_validation.recovery.e2_dynamic import ROOT, SOURCE, PROJECT, SCHEDULER, source_identity, check as runtime_check
from pals_validation.recovery.e2_eos import require

REPO=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('eos_submission',REPO/'scripts/e2_eos_submit.py')
legacy=importlib.util.module_from_spec(spec);spec.loader.exec_module(legacy)
STAGES=('prepare','launch','audit')
OLD_JOBS=('115308','115309')
PREDECESSOR='115307'
LAUNCHER=REPO/'scripts/b1-e2-dynamic.sbatch'
EXCLUDE='tko-b1-nv-dgx06'


def safe(path):
    require(path.is_absolute() and path.resolve()==path and PROJECT in path.parents and PROJECT.resolve()==PROJECT,'project boundary')


def state(jid):
    text=subprocess.check_output(['scontrol','show','job',jid,'-o'],text=True).strip()
    require(len(text.splitlines())==1,'ambiguous job')
    return dict(s.split('=',1) for s in text.split() if '=' in s)


def old_pending():
    source_identity();receipt=read(SOURCE/'submissions/chain.json')
    output={}
    for index,jid in zip((7,8),OLD_JOBS):
        require(receipt[index]['job_id']==jid and receipt[index]['benchmark']=='mmlu','old receipt')
        f=state(jid)
        require(f.get('JobState')=='PENDING' and f.get('Priority')=='0' and f.get('RunTime')=='00:00:00'
                and f.get('Restarts')=='0' and f.get('UserId','').startswith('xijun(') and
                f.get('WorkDir')==str(SOURCE) and f.get('JobName')=='pals-e2-formal-'+str(index),'old MMLU not held/unstarted')
        output[jid]=f
    return output


def active(allowed=()):
    allowed=set(allowed)|{'115306','115307',*OLD_JOBS}
    for line in subprocess.check_output(['squeue','-h','-u','xijun','-o','%i %j'],text=True).splitlines():
        jid,name=line.split(maxsplit=1)
        require(not name.startswith(('pals-llama-','pals-e2-','e3g-','e3c-','e3x-')) or jid in allowed,'other related chain')


def predecessor_available():
    rows=legacy.scheduler_rows(['115306',PREDECESSOR])
    for jid in ('115306',PREDECESSOR):
        require(jid in rows and (rows[jid][0] in ('PENDING','RUNNING') or rows[jid]==['COMPLETED','0:0']), 'LCB predecessor failed/unknown')


def prepare():
    safe(ROOT);safe(REPO);active();predecessor_available();old=old_pending()
    require(not ROOT.exists(),'existing/uncertain deployment')
    require(not subprocess.check_output(['git','-C',str(REPO),'status','--porcelain'],text=True).strip(),'unclean frozen code')
    ROOT.mkdir(mode=0o700)
    for name in ('logs','submissions'):(ROOT/name).mkdir()
    for stage in STAGES:
        for name in ('enroot-cache','enroot-data','enroot-runtime','tmp','hf','hf-modules','cache','cuda'):
            (ROOT/'scratch'/stage/name).mkdir(parents=True)
    paths=[*sorted((REPO/'src').rglob('*.py')),LAUNCHER,Path(__file__),REPO/'scripts/e2_eos_submit.py']
    save(ROOT/'deployment.json',{'repo':str(REPO),'scheduler':SCHEDULER,'old_held':old,
         'source_manifest_sha256':sha256(SOURCE/'manifest.json'),'files':{str(p.relative_to(REPO)):sha256(p) for p in paths}})


def check():
    safe(ROOT);safe(REPO);d=read(ROOT/'deployment.json')
    require(d['repo']==str(REPO) and d['scheduler']==SCHEDULER and d['source_manifest_sha256']==sha256(SOURCE/'manifest.json'),'deployment identity')
    verify(REPO,d['files'])


def command(i,previous):
    stage=STAGES[i];gpu=stage=='launch';name='pals-e2-mmlu-dynamic-'+stage
    return ['sbatch','--parsable','--hold','--no-requeue','--partition=defq','--reservation=code-agent',
        '--nodes=1','--ntasks=1','--cpus-per-task='+('32' if gpu else '8'),'--mem='+('256G' if gpu else '64G'),
        '--time='+('08:00:00' if gpu else '01:00:00'),'--exclude='+EXCLUDE,'--job-name='+name,
        '--chdir='+str(ROOT),'--export=ALL','--output='+str(ROOT/'logs'/('%j-'+name+'.out')),
        '--error='+str(ROOT/'logs'/('%j-'+name+'.err')),'--gpus-per-node=8' if gpu else '--gres=none',
        '--dependency=afterok:'+previous,'--kill-on-invalid-dep=yes',str(LAUNCHER)]


def held(r,previous):
    i=r['index'];jid=r['job_id'];stage=STAGES[i];gpu=stage=='launch';name='pals-e2-mmlu-dynamic-'+stage
    require(r['command']==command(i,previous) and r['stage']==stage,'command receipt')
    f=state(jid)
    expected={'JobId':jid,'JobName':name,'JobState':'PENDING','Priority':'0','Partition':'defq','Reservation':'code-agent',
        'ExcNodeList':EXCLUDE,'NumTasks':'1','NumCPUs':'32' if gpu else '8','CPUs/Task':'32' if gpu else '8',
        'TimeLimit':'08:00:00' if gpu else '01:00:00','Requeue':'0','Restarts':'0','Command':str(LAUNCHER),
        'WorkDir':str(ROOT),'StdOut':str(ROOT/'logs'/(jid+'-'+name+'.out')),'StdErr':str(ROOT/'logs'/(jid+'-'+name+'.err'))}
    require(all(f.get(k)==v for k,v in expected.items()),'held effective fields')
    require(f.get('UserId','').startswith('xijun(') and f.get('NumNodes') in ('1','1-1'),'owner/nodes')
    tres=dict(s.split('=',1) for s in f.get('ReqTRES','').split(',') if '=' in s)
    require(tres.get('cpu')==expected['NumCPUs'] and tres.get('mem')==('256G' if gpu else '64G') and
            tres.get('node')=='1' and (tres.get('gres/gpu')=='8' if gpu else not any('gpu' in k and v!='0' for k,v in tres.items())),'resources')
    previous_rows=legacy.scheduler_rows([previous])
    dependency=f.get('Dependency')
    require(dependency in ('afterok:'+previous,'afterok:'+previous+'(unfulfilled)','afterok:'+previous+'(fulfilled)') or
            (dependency=='(null)' and previous_rows.get(previous)==['COMPLETED','0:0']),'dependency')
    require(f.get('KillOInInvalidDependent')=='Yes','invalid dependency cancellation')
    return f


def submit():
    check();old_pending();active();predecessor_available();ledger=ROOT/'submissions'
    with exclusive_lock(ledger/'submit.lock'):
        require(not list(ledger.glob('*attempt.json')),'existing/uncertain attempt')
        previous=PREDECESSOR;records=[]
        for i,stage in enumerate(STAGES):
            cmd=command(i,previous);env={k:v for k,v in os.environ.items() if not k.startswith(('PALS_','SBATCH_'))}
            env.update(PALS_REPO=str(REPO),PALS_DYNAMIC_ACTION=stage)
            save(ledger/(str(i)+'-attempt.json'),{'command':cmd})
            jid=subprocess.check_output(cmd,env=env,cwd=ROOT,text=True).strip().split(';')[0]
            require(jid.isdigit(),'uncertain sbatch')
            r={'job_id':jid,'index':i,'stage':stage,'command':cmd};save(ledger/(str(i)+'-submitted.json'),r)
            records.append(r);previous=jid
        save(ledger/'chain.json',records)


def release():
    check();ledger=ROOT/'submissions'
    with exclusive_lock(ledger/'submit.lock'):
        require(not list(ledger.glob('release-*-attempt.json')) and not (ledger/'replace-attempt.json').exists(),'replacement/release already attempted')
        records=read(ledger/'chain.json');require([r['index'] for r in records]==list(range(3)) and len({r['job_id'] for r in records})==3,'incomplete chain')
        active(r['job_id'] for r in records);predecessor_available();old=old_pending();previous=PREDECESSOR;states={}
        for r in records:
            require(r==read(ledger/(str(r['index'])+'-submitted.json')),'saved receipt')
            states[r['job_id']]=held(r,previous);previous=r['job_id']
        save(ledger/'held-audit.json',{'status':'PASS','states':states,'replaced_old':old})
        save(ledger/'replace-attempt.json',{'old_jobs':OLD_JOBS,'new_jobs':[r['job_id'] for r in records]})
        # Both old jobs are held and verified unstarted; keep all old files.
        subprocess.run(['scancel',*reversed(OLD_JOBS)],check=True)
        for poll in range(10):
            rows=legacy.scheduler_rows(list(OLD_JOBS))
            if all(rows.get(j,[''])[0].startswith('CANCELLED') for j in OLD_JOBS):break
            time.sleep(1)
        require(all(rows.get(j,[''])[0].startswith('CANCELLED') for j in OLD_JOBS),'old cancellation not confirmed; no release')
        save(ledger/'replaced.json',{'old_jobs':rows,'preserved_source':str(SOURCE)})
        for r in reversed(records):
            jid=r['job_id'];save(ledger/('release-'+jid+'-attempt.json'),{'job_id':jid})
            subprocess.run(['scontrol','release',jid],check=True)
            save(ledger/('release-'+jid+'-done.json'),{'job_id':jid})
        save(ledger/'released.json',{'status':'RELEASED','jobs':[r['job_id'] for r in records]})


def admit(stage):
    check();records=read(ROOT/'submissions/chain.json');i=STAGES.index(stage);r=records[i]
    require(r['job_id']==os.environ.get('SLURM_JOB_ID'),'allocation identity')
    legacy.successful(PREDECESSOR if i==0 else records[i-1]['job_id'])
    prior=read(SOURCE/'livecodebench/audit.json')
    require(prior['status']=='PASS' and prior['protocol_id']==read(SOURCE/'manifest.json')['protocol_id'],'LCB audit')
    require((ROOT/'submissions/replaced.json').exists(),'old pair not replaced')
    if i:
        runtime_check(ROOT)
        require(read(ROOT/'ready.json')['status']=='PASS','CPU ready')
    if stage=='audit':require(read(ROOT/'mmlu/gpu-complete.json')['status']=='PASS','GPU application')
    save(ROOT/(stage+'-admitted.json'),{'status':'PASS','job_id':r['job_id']})


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('prepare','submit','release','admit'));p.add_argument('--stage',choices=STAGES)
    a=p.parse_args();os.umask(0o077)
    os.environ['PATH']='/cm/local/apps/slurm/current/bin:'+os.environ['PATH'];os.environ['SLURM_CONF']='/cm/shared/apps/slurm/etc/slurm/slurm.conf'
    if a.action=='admit':admit(a.stage)
    else:globals()[a.action]()
    print({'status':'PASS','action':a.action},flush=True)
