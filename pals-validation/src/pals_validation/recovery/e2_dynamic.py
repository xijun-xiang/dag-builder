"""MMLU-only whole-cell work stealing; frozen scientific inputs are unchanged."""
import argparse
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from ..analyze import check_score, e2_analysis
from ..backend import HFBackend
from ..campaign import canary_coverage, stop_children, terminate_as_exception
from ..io import digest, encoded, read, save, sha256, verify
from ..metrics import repeats
from ..protocol import parse_step
from ..run import now, validate_run
from .e2_eos import require, implementation_files, model_stats, numeric_reference
from .e2_eos_v2 import VERSION, select_target
from .e2_formal import ROOT as SOURCE, PROJECT, RECOVERY, dual_rows

ROOT = PROJECT/'runs/20261010-llama-e2-mmlu-dynamic-v1'
MODULE = 'pals_validation.recovery.e2_dynamic'
SCHEDULER = 'whole-cell-eight-workers-v1'
SOURCE_PROTOCOL = 'ce201e08ecef568c9230af0d611f692fa0e8b26728fce0ac462bf81054345e40'


def source_identity():
    p = read(SOURCE/'manifest.json')
    require(p['protocol_id'] == SOURCE_PROTOCOL == digest({k:v for k,v in p.items() if k!='protocol_id'}), 'source protocol')
    require(read(SOURCE/'ready.json')['manifest_sha256'] == sha256(SOURCE/'manifest.json'), 'source ready')
    require(not list((SOURCE/'mmlu/worker-logs').glob('*-attempt.json')), 'old MMLU worker attempted')
    for phase in ('canary','formal'):
        for name in ('generation-attempts','generations','results','eos-results'):
            require(not list((SOURCE/'mmlu'/phase/name).glob('*.json')), 'old MMLU already attempted')
    return p


def prepare(root):
    old = source_identity(); slot = root/'mmlu'
    require(not slot.exists() and not (root/'manifest.json').exists(), 'already/partly prepared')
    files = {n:h for n,h in old['files'].items() if n.startswith('mmlu/')}
    require(files, 'empty source manifest'); verify(SOURCE, files)
    for phase in ('canary','formal'):
        prefix='mmlu/'+phase+'/'
        protocol=read(SOURCE/prefix/'protocol.json')
        require(protocol['protocol_id']==digest({k:v for k,v in protocol.items() if k!='protocol_id'}),'source phase protocol')
        verify(SOURCE/prefix,protocol['files'])
        for name,h in protocol['files'].items():
            require(prefix+name not in files or files[prefix+name]==h,'source manifest disagreement')
            files[prefix+name]=h
    for name in files:
        dest = root/name; dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE/name, dest)
    verify(root, files)
    cfg = read(slot/'config.json')
    require(cfg['temperatures']==[.3,.7,1.2] and cfg['repeats']==8 and cfg['seed']==2026091507
            and cfg['max_new_tokens']==4096 and cfg['max_context']==8192, 'frozen science')
    verify(Path(cfg['model_path']), old['model_files'])
    counts = {}
    for phase in ('canary','formal'):
        run = slot/phase; protocol, config = validate_run(run)
        require(config==cfg and protocol['model_files']==old['model_files'], 'phase identity')
        jobs = read(run/'jobs.json'); require(len({j['job_id'] for j in jobs})==len(jobs), 'duplicate cells')
        for name in ('workers','generations','results','eos-results','generation-attempts','dispatch'):
            (run/name).mkdir(exist_ok=True)
        save(run/'dispatch/state.json', {'next':8, 'jobs_sha256':sha256(run/'jobs.json')})
        counts[phase] = len(jobs)
    require(counts=={'canary':24,'formal':12906}, 'unchanged MMLU denominator')
    (slot/'worker-logs').mkdir()
    queue_selftest(root)
    manifest = {'version':VERSION, 'scheduler':SCHEDULER, 'created':now(), 'files':files,
                'implementation':implementation_files(), 'source_protocol':SOURCE_PROTOCOL,
                'source_manifest_sha256':sha256(SOURCE/'manifest.json'), 'source_audit_sha256':old['source_audit_sha256'],
                'model_files':old['model_files'], 'model_stats':model_stats(Path(cfg['model_path']),old['model_files']),
                'counts':counts, 'formal_draws':103248, 'canary_draws':192,
                'change':'Only whole-cell scheduling/ownership; original batch, RNG seed, parser and scorer retained.'}
    save(root/'manifest.json', {**manifest,'protocol_id':digest(manifest)})
    save(root/'ready.json', {'status':'PASS','job_id':os.environ['SLURM_JOB_ID'], 'manifest_sha256':sha256(root/'manifest.json')})


def check(root):
    p = read(root/'manifest.json')
    require(p['protocol_id']==digest({k:v for k,v in p.items() if k!='protocol_id'}) and
            p['scheduler']==SCHEDULER and p['version']==VERSION, 'manifest identity')
    require(p['implementation']==implementation_files(), 'implementation drift')
    verify(root,p['files'])
    require(sha256(SOURCE/'manifest.json')==p['source_manifest_sha256'] and
            sha256(RECOVERY/'audit.json')==p['source_audit_sha256'], 'source drift')
    return p


def replace_cursor(path, value):
    """Only the auxiliary queue cursor is mutable; scientific records are exclusive."""
    fd, temp = tempfile.mkstemp(prefix='.cursor-', dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(encoded(value)); f.flush(); os.fsync(f.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def claim(run, jobs, config, worker, first=False, jobs_sha256=None):
    require(0<=worker<8 and len(jobs)>=8, 'eight-way claim')
    fd = os.open(run/'dispatch/queue.lock', os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        state = read(run/'dispatch/state.json')
        require(state['jobs_sha256']==(jobs_sha256 or sha256(run/'jobs.json')) and 8<=state['next']<=len(jobs), 'queue identity')
        index = worker if first else state['next']
        if index==len(jobs): return None
        job = jobs[index]; jid=job['job_id']
        require(not any((run/folder/(jid+'.json')).exists() for folder in ('generations','results','eos-results')), 'existing result; no retry')
        receipt = {'job_id':jid, 'seed':int(digest([config['seed'],jid])[:8],16), 'worker':worker,
                   'original_index':index, 'original_static_worker':index%8, 'slurm_job_id':os.environ['SLURM_JOB_ID'],
                   'started':now(), 'scheduler':SCHEDULER}
        save(run/'generation-attempts'/(jid+'.json'),receipt)
        if not first: replace_cursor(run/'dispatch/state.json',{**state,'next':index+1})
        return job,receipt
    finally:
        os.close(fd)


def queue_selftest(root):
    """Bounded CPU-only lock/exclusive-claim check on the actual run filesystem."""
    from concurrent.futures import ThreadPoolExecutor
    run=root/'queue-selftest';run.mkdir()
    for name in ('dispatch','generation-attempts'):(run/name).mkdir()
    jobs=[{'job_id':str(i)} for i in range(64)];save(run/'jobs.json',jobs)
    job_hash=sha256(run/'jobs.json');save(run/'dispatch/state.json',{'next':8,'jobs_sha256':job_hash})
    def exercise(i):
        result=[claim(run,jobs,{'seed':1},i,True,job_hash)[0]['job_id']]
        while True:
            taken=claim(run,jobs,{'seed':1},i,False,job_hash)
            if taken is None:return result
            result.append(taken[0]['job_id'])
    with ThreadPoolExecutor(max_workers=8) as pool:groups=list(pool.map(exercise,range(8)))
    all_claims=[j for group in groups for j in group]
    require([g[0] for g in groups]==[str(i) for i in range(8)] and len(all_claims)==len(set(all_claims))==64,'queue selftest')
    save(run/'result.json',{'status':'PASS','scientific_evidence':False,'claims':64,'workers':8,'generation_calls':0})


def process_phase(root,phase,index,backend):
    run=root/'mmlu'/phase; protocol,config=validate_run(run)
    jobs=read(run/'jobs.json'); cases={c['item_id']:c for c in read(run/'inputs/cases.json')}
    folder=run/'workers'/str(index); folder.mkdir()
    owned=[]; first=True; jobs_hash=sha256(run/'jobs.json')
    while True:
        taken=claim(run,jobs,config,index,first,jobs_hash); first=False
        if taken is None: break
        job,receipt=taken; jid=job['job_id'];case=cases[job['item_id']]
        print(now(),phase,jid,'generate-start',flush=True)
        generated=backend.generate(case,job['prefix_ids'],job['temperature'],config['repeats'],receipt['seed'])
        save(run/'generations'/(jid+'.json'),{'job_id':jid,'protocol_id':protocol['protocol_id'],**generated})
        print(now(),phase,jid,'score-start',flush=True)
        strict,eos=dual_rows(generated,case,job,backend)
        base={'job':job,'protocol_id':protocol['protocol_id'],'scientific_evidence':protocol['scientific_evidence'],
              'status':'ok','generation_sha256':sha256(run/'generations'/(jid+'.json'))}
        save(run/'results'/(jid+'.json'),{**base,'rows':strict,'summary':repeats(strict,8)})
        save(run/'eos-results'/(jid+'.json'),{**base,'parser_version':VERSION,'rows':eos,'summary':repeats(eos,8)})
        owned.append(jid);print(now(),phase,jid,'completed',flush=True)
    save(folder/'completion.json',{'status':'complete','worker':index,'jobs':len(owned),'ended':now(),
         'result_hashes':{j:sha256(run/'results'/(j+'.json')) for j in owned},
         'eos_result_hashes':{j:sha256(run/'eos-results'/(j+'.json')) for j in owned}})


def wait_gate(path):
    while not path.exists():time.sleep(.25)
    require(read(path)['status']=='PASS','gate failed')


def worker(root,index):
    p=check(root);slot=root/'mmlu';cfg=read(slot/'config.json')
    save(slot/'worker-logs'/f'{index}-attempt.json',{'job_id':os.environ['SLURM_JOB_ID'],'worker':index})
    require(model_stats(Path(cfg['model_path']),p['model_files'])==p['model_stats'],'model drift')
    backend=HFBackend(cfg['model_path'],cfg)
    j=read(slot/'formal/jobs.json')[index];cases={c['item_id']:c for c in read(slot/'formal/inputs/cases.json')}
    probe=backend.score(cases[j['item_id']],j['prefix_ids'],j['deleted_id'],'A deterministic scoring check.')
    save(slot/'worker-logs'/f'{index}-reference.json',numeric_reference(backend,probe['evidence']))
    wait_gate(slot/'numeric-gate.json')
    process_phase(root,'canary',index,backend)
    wait_gate(slot/'canary-gate.json')
    process_phase(root,'formal',index,backend)
    save(slot/'worker-logs'/f'{index}-complete.json',{'status':'PASS','worker':index,'job_id':os.environ['SLURM_JOB_ID']})


def coverage(slot):
    records=[read(f) for f in sorted((slot/'canary/eos-results').glob('*.json'))]
    require(len(records)==len(read(slot/'canary/jobs.json')),'canary set')
    groups={}
    for r in records:groups.setdefault(r['job']['item_id'],[]).append(r['summary']['complete'])
    return canary_coverage({'cells':[r['summary'] for r in records],
        'complete_questions':sum(len(v)==3 and all(v) for v in groups.values()),'planned_questions':len(groups)},read(slot/'config.json'))


def launch(root):
    p=check(root);slot=root/'mmlu'
    require(read(root/'ready.json')['manifest_sha256']==sha256(root/'manifest.json'),'ready identity')
    visible=os.environ.get('CUDA_VISIBLE_DEVICES','').split(',')
    require(len(visible)==len(set(visible))==8 and all(visible),'8 allocated GPUs')
    require(not list((slot/'worker-logs').glob('*-attempt.json')),'already attempted')
    children=[]
    def wait_until(test):
        while not test():
            require(all(proc.poll() in (None,0) for proc,_ in children),'worker failure')
            time.sleep(.5)
    try:
        for i,gpu in enumerate(visible):
            log=(slot/'worker-logs'/f'{i}.log').open('x')
            proc=subprocess.Popen([sys.executable,'-m',MODULE,'worker','--root',str(root),'--worker',str(i)],
                stdout=log,stderr=subprocess.STDOUT,env={**os.environ,'CUDA_VISIBLE_DEVICES':gpu})
            children.append((proc,log))
        wait_until(lambda:len(list((slot/'worker-logs').glob('*-reference.json')))==8)
        for i in range(8):
            ref=read(slot/'worker-logs'/f'{i}-reference.json')
            require(ref['status']=='PASS' and ref['repeat_max_abs']<=1e-5 and all(v<=.005 for v in ref['masked_loss_errors'].values()),'numeric gate')
        save(slot/'numeric-gate.json',{'status':'PASS','workers':8})
        wait_until(lambda:len(list((slot/'canary/workers').glob('*/completion.json')))==8)
        save(slot/'canary-gate.json',{'status':'PASS','coverage':coverage(slot)})
        wait_until(lambda:all(proc.poll() is not None for proc,_ in children))
        require(all(proc.returncode==0 for proc,_ in children),'worker exit')
    finally:stop_children(children)
    save(slot/'gpu-complete.json',{'status':'PASS','job_id':os.environ['SLURM_JOB_ID'],'protocol_id':p['protocol_id']})


def owners(run,jobs):
    expected={j['job_id'] for j in jobs}; result={}
    require(len(expected)==len(jobs),'duplicate jobs')
    for folder in ('generation-attempts','generations','results','eos-results'):
        require({f.stem for f in (run/folder).glob('*.json')}==expected,'missing/extra '+folder)
    for i in range(8):
        done=read(run/'workers'/str(i)/'completion.json')
        require(done['status']=='complete' and done['worker']==i and done['jobs']==len(done['result_hashes'])
                and set(done['result_hashes'])==set(done['eos_result_hashes']),'worker receipt')
        for jid,h in done['result_hashes'].items():
            require(jid in expected and jid not in result,'duplicate/extra ownership')
            require(sha256(run/'results'/(jid+'.json'))==h and sha256(run/'eos-results'/(jid+'.json'))==done['eos_result_hashes'][jid],'result hash')
            result[jid]=i
    require(set(result)==expected,'missing owner')
    state=read(run/'dispatch/state.json')
    require(state=={'next':len(jobs),'jobs_sha256':sha256(run/'jobs.json')},'cursor incomplete')
    for ix,j in enumerate(jobs):
        a=read(run/'generation-attempts'/(j['job_id']+'.json'))
        require(a['worker']==result[j['job_id']] and a['original_index']==ix and a['original_static_worker']==ix%8
                and a['job_id']==j['job_id'] and a['scheduler']==SCHEDULER,'claim ownership')
        if ix<8:require(a['worker']==ix,'first wave not eight workers')
    return result


def audit(root):
    from transformers import AutoTokenizer
    p=check(root);slot=root/'mmlu';cfg=read(slot/'config.json');gpu=read(slot/'gpu-complete.json')
    require(gpu['status']=='PASS' and gpu['protocol_id']==p['protocol_id'],'GPU application')
    verify(Path(cfg['model_path']),p['model_files'])
    tok=AutoTokenizer.from_pretrained(cfg['model_path'],local_files_only=True,trust_remote_code=False)
    backend=object.__new__(HFBackend);backend.tokenizer=tok;backend.config=cfg
    enc=lambda text:tok.encode(text,add_special_tokens=False)
    outputs={}
    for phase in ('canary','formal'):
        run=slot/phase;protocol,config=validate_run(run);require(config==cfg,'configuration drift')
        jobs=read(run/'jobs.json');owners(run,jobs);cases={c['item_id']:c for c in read(run/'inputs/cases.json')}
        strict_results=[];eos_results=[]
        for job in jobs:
            jid=job['job_id'];raw=read(run/'generations'/(jid+'.json'));strict=read(run/'results'/(jid+'.json'));eos=read(run/'eos-results'/(jid+'.json'))
            claim_row=read(run/'generation-attempts'/(jid+'.json'));seed=int(digest([cfg['seed'],jid])[:8],16)
            require(claim_row['seed']==seed and claim_row['slurm_job_id']==gpu['job_id'],'seed/allocation')
            require(raw['job_id']==jid and raw['protocol_id']==protocol['protocol_id'],'raw identity')
            for result in (strict,eos):
                require(result['job']==job and result['protocol_id']==protocol['protocol_id'] and result['status']=='ok'
                        and result['scientific_evidence']==protocol['scientific_evidence'] and
                        result['generation_sha256']==sha256(run/'generations'/(jid+'.json')),'result identity')
                require([r['repeat'] for r in result['rows']]==list(range(8)),'repeat slots')
            require(len(raw['rows'])==8 and eos['parser_version']==VERSION,'raw repeats/parser')
            case=cases[job['item_id']];require(job['prefix_ids'].count(job['deleted_id'])==1,'deletion')
            prompt=backend.context(case,job['prefix_ids'],for_generation=True)
            require(raw['prompt']==prompt and raw['prompt_token_ids']==enc(prompt),'prompt')
            contract=raw['generation_contract']
            require(contract['max_new_tokens']==4096 and contract['max_context']==8192 and contract['prompt_version']==cfg.get('generation_prompt_version','v1')
                    and contract['prompt_tokens']==len(enc(prompt)) and contract['prompt_tokens']+4096<=8192 and
                    contract['boundary']=='</step>' and contract['constrained_decoding'] is False,'generation contract')
            contexts={'full_context_ids':enc(backend.context(case,job['prefix_ids'])),
                      'deleted_context_ids':enc(backend.context(case,[n for n in job['prefix_ids'] if n!=job['deleted_id']]))}
            for index,(r,s,e) in enumerate(zip(raw['rows'],strict['rows'],eos['rows'])):
                require(r['repeat']==index and r['seed']==seed and len(r['generated_token_ids'])<=4096 and
                        tok.decode(r['generated_token_ids'],skip_special_tokens=True)==r['raw_text'],'raw token/seed')
                require(all(s[k]==e[k]==v for k,v in r.items()),'raw modification')
                parsed=parse_step(r['raw_text'],case.get('task_type'));good=parsed['valid'] and r['finish_reason']=='boundary'
                require(parsed==r['parse'] and (s['status']=='ok')==good,'strict validity')
                d=select_target(r,contract,case.get('task_type'))
                require(e['recovery']==d and (e['status']=='ok')==(d['target'] is not None),'EOS validity')
                if e['status']=='ok':
                    check_score(e);require(e['evidence']['target_text']==d['target'] and e['evidence']['target_ids']==enc(d['target'])
                        and all(e['evidence'][k]==v for k,v in contexts.items()),'target/context')
                if good:require({k:v for k,v in e.items() if k!='recovery'}==s and s['evidence']['target_text']==parsed['body'],'strict equality')
            strict_results.append(strict);eos_results.append(eos)
        strict_analysis=e2_analysis(strict_results,cfg);eos_analysis=e2_analysis(eos_results,cfg)
        save(slot/(phase+'-strict-analysis.json'),strict_analysis);save(slot/(phase+'-eos-analysis.json'),eos_analysis)
        outputs[phase]={'strict_complete':strict_analysis['complete_questions'],'eos_complete':eos_analysis['complete_questions'],
                        'planned_questions':eos_analysis['planned_questions'],'coverage':eos_analysis['generation_coverage_by_temperature']}
    require(read(slot/'canary-gate.json')=={'status':'PASS','coverage':coverage(slot)},'canary gate replay')
    require(read(slot/'numeric-gate.json')=={'status':'PASS','workers':8},'numeric gate identity')
    for i in range(8):
        ref=read(slot/'worker-logs'/f'{i}-reference.json');check_score(ref['probe']);check_score(ref['repeat_probe'])
        actual=max(abs(x-y) for k in ('full_logprobs','deleted_logprobs') for x,y in zip(ref['probe']['evidence'][k],ref['repeat_probe']['evidence'][k]))
        require(ref['status']=='PASS' and actual==ref['repeat_max_abs'] and actual<=1e-5 and
                all(abs(ref['native_losses'][k]-ref['probe']['score'][k+'_nll'])==ref['masked_loss_errors'][k]<=.005 for k in ('full','deleted')),'numeric evidence')
        require(read(slot/'worker-logs'/f'{i}-complete.json')=={'status':'PASS','worker':i,'job_id':gpu['job_id']},'worker identity')
    save(slot/'audit.json',{'status':'PASS','protocol_id':p['protocol_id'],'scheduler':SCHEDULER,'version':VERSION,
         'results':outputs,'files':{str(f.relative_to(slot)):sha256(f) for f in sorted(slot.rglob('*.json'))},
         'note':'Dynamic worker ownership; unchanged whole-cell scientific protocol. Strict/EOS cohorts separate.'})


def main():
    ap=argparse.ArgumentParser();ap.add_argument('action',choices=('prepare','launch','worker','audit'))
    ap.add_argument('--root',type=Path,required=True);ap.add_argument('--worker',type=int)
    a=ap.parse_args();os.umask(0o077)
    require(a.root==ROOT and ROOT.resolve()==ROOT and PROJECT.resolve()==PROJECT and os.environ.get('SLURM_JOB_ID'),'allocated project boundary')
    with terminate_as_exception():
        if a.action=='worker':worker(a.root,a.worker)
        else:globals()[a.action](a.root)


if __name__=='__main__':main()
