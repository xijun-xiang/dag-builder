"""Advance one approved wave only; invoke through the confirmed B1 connection.

No daemon, no resubmission, no uncertain retry. Slurm and application audits must
both pass before another wave. Imported wave zero is backed by a recovery audit.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess

from pals_validation.io import digest, read, save, verify
from pals_validation.e3.expanded import ROOT, implementation_hashes
from pals_validation.e3.continuation import verify_imports
from pals_validation.e3.schema import require


def terminal(job):
    text = subprocess.check_output(['sacct','-j',str(job),'--format=JobID,State,ExitCode','-Pn'],text=True)
    expected = {str(job), str(job)+'.batch', str(job)+'.0'}
    rows = {r[0]:r[1:] for line in text.splitlines() if (r:=line.split('|'))[0] in expected}
    active = {'PENDING','RUNNING','CONFIGURING','COMPLETING','SUSPENDED','RESIZING'}
    for state, code in rows.values():
        require(state in active or (state=='COMPLETED' and code=='0:0'), 'FAILED_JOB_NO_RETRY: '+str(job)+' '+str(rows))
    return set(rows)==expected and all(r==['COMPLETED','0:0'] for r in rows.values())


def accepted(run, slot, wave, manifest):
    a=read(run/slot/'audits'/f'wave-{wave:03d}.json')
    require(a['status']=='PASS' and a['expansion_id']==manifest['expansion_id'], 'audit identity failure')
    verify(run/slot,a['files'])
    require(a['coverage_gate'] is True,'COVERAGE_NEEDS_DECISION')


def advance(run):
    code=Path(__file__).resolve().parents[1]
    for p in (ROOT,run,code):
        require(p.resolve(strict=True)==p and (p==ROOT or ROOT in p.parents),'outside PALS')
    require(not subprocess.check_output(['git','-C',str(code),'status','--porcelain'],text=True).strip(),'dirty code snapshot')
    m=read(run/'manifest.json')
    require(m['expansion_id']==digest({k:v for k,v in m.items() if k!='expansion_id'}),'manifest changed')
    require(m['implementation_hashes']==implementation_hashes(),'frozen implementation changed')
    verify_imports(run,m)
    ready=read(run/'continuation-ready.json')
    require(ready['status']=='PASS' and ready['expansion_id']==m['expansion_id'],'import not ready')
    require(m['counts']==dict(gpqa=198,gsm8k=1319,humaneval=164,livecodebench=175,mmlu=100),'scope changed')
    require(digest(m['selected_ids'])=='2fdbd1594482bd03dcb045f0b8f1c1e0d83d1be301994042df4d6008bc93a865','cohort changed')
    for o in m['sources'].values():
        require(read(o['config']['model']['files_manifest'])==read(Path(o['source'])/'protocol.json')['model_files'],
                'model manifest changed')
        require(len(o['waves'])==32,'wave count changed')
    source_roots={str(Path(o['source']).parent) for o in m['sources'].values()}
    require(len(source_roots)==1,'source roots differ')
    ledger=run/'submissions';ledger.mkdir(mode=0o700,exist_ok=True)
    fd=os.open(ledger/'controller.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    try:
        for position,(slot,wave) in enumerate((s,w) for w in range(32) for s in ('phi4mini','qwen25','qwen3')):
            if slot=='phi4mini' and wave==0:
                require(m['continuation']['completed_waves']=={'phi4mini':[0]},'unexpected imported wave')
                accepted(run,slot,wave,m)
                continue
            record=ledger/f'{position:03d}-{slot}-wave-{wave:03d}.json'
            attempt=ledger/(record.stem+'-attempt.json')
            if record.exists():
                saved=read(record)
                require(saved['expansion_id']==m['expansion_id'] and saved['slot']==slot and saved['wave']==wave,'submission differs')
                states=[terminal(j) for j in saved['jobs'].values()]
                if not all(states):
                    return dict(status='WAITING',slot=slot,wave=wave,jobs=saved['jobs'])
                accepted(run,slot,wave,m)
                continue
            require(not attempt.exists(),'UNCERTAIN_SUBMISSION_NO_RETRY')
            names=subprocess.check_output(['squeue','-h','-u','xijun','-o','%j'],text=True).splitlines()
            require(not any(n.startswith(('e3x-','e3c-')) for n in names),'unexpected active expansion job')
            save(attempt,dict(expansion_id=m['expansion_id'],slot=slot,wave=wave))
            env={**os.environ,'PALS_REPO':str(code),'PALS_EXPANSION':str(run),
                 'PALS_SOURCE_ROOT':next(iter(source_roots)),'PALS_SLOT':slot,'PALS_WAVE':str(wave)}
            jobs={};prior=None
            for stage in ('generate','score','evaluate'):
                name=f'e3c-{slot}-{wave:03d}-{stage}';log=run/slot/'logs'
                command=['sbatch','--parsable','--job-name='+name,
                         '--output='+str(log/('%j-'+name+'.out')),'--error='+str(log/('%j-'+name+'.err'))]
                if stage=='evaluate':
                    command+=['--cpus-per-task=16','--mem=64G','--time=02:00:00','--gres=none']
                else:
                    command+=['--gpus-per-node=8','--cpus-per-task=32','--mem=256G',
                              '--time='+('04:00:00' if stage=='generate' else '01:00:00')]
                if prior:
                    command+=['--dependency=afterok:'+prior,'--kill-on-invalid-dep=yes']
                command.append(str(code/'scripts/b1-e3-expanded.sbatch'))
                save(ledger/f'{record.stem}-{stage}-attempt.json',dict(command=command,expansion_id=m['expansion_id']))
                prior=subprocess.check_output(command,env={**env,'PALS_STAGE':stage},cwd=run/slot,text=True).strip().split(';')[0]
                require(prior.isdigit(),'uncertain scheduler reply')
                jobs[stage]=prior
                save(ledger/f'{record.stem}-{stage}-submitted.json',dict(job_id=prior,command=command))
            result=dict(status='SUBMITTED',expansion_id=m['expansion_id'],slot=slot,wave=wave,jobs=jobs)
            save(record,result)
            return result
        return dict(status='ALL_96_WAVES_AUDITED')
    finally:
        os.close(fd)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run',type=Path,required=True)
    os.umask(0o077)
    os.environ['PATH']='/cm/local/apps/slurm/current/bin:'+os.environ['PATH']
    os.environ['SLURM_CONF']='/cm/shared/apps/slurm/etc/slurm/slurm.conf'
    print(json.dumps(advance(p.parse_args().run),ensure_ascii=False),flush=True)
