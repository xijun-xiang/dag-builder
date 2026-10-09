"""Four untouched E2 cohorts; original generator, dual parsers, no resampling."""
import argparse
from collections import Counter
import os
from pathlib import Path
import subprocess
import sys
import time

from ..analyze import analyze, check_score, e2_analysis
from ..backend import HFBackend
from ..campaign import canary_coverage, stop_children, terminate_as_exception
from ..io import digest, read, save, sha256, verify
from ..metrics import repeats
from ..run import init_run, validate_run, now
from .e2_eos import require, implementation_files, model_stats, numeric_reference
from .e2_eos_v2 import VERSION, select_target

PROJECT = Path('/work/projects/polyullm/xxj/PALS')
OLD = PROJECT/'runs/20261009-llama-fivebench-e1e2-v2'
RECOVERY = PROJECT/'runs/20261009-llama-e2-eos-recovery-v1/recovery'
ROOT = PROJECT/'runs/20261009-llama-e2-formal-eos-v2'
BENCHES = ('humaneval','gsm8k','livecodebench','mmlu')
MODULE = 'pals_validation.recovery.e2_formal'


def check(root):
    p=read(root/'manifest.json')
    require(p['version']==VERSION and p['protocol_id']==digest({k:v for k,v in p.items() if k!='protocol_id'}), 'manifest')
    require(p['implementation']==implementation_files(), 'implementation drift')
    for name,h in p['files'].items(): require(sha256(root/name)==h,'frozen file drift')
    require(sha256(RECOVERY/'audit.json')==p['source_audit_sha256'], 'recovery audit drift')
    return p


def corrected_source(label):
    """No inference: reuse every unchanged valid score; invalidate only v2 misses."""
    plan=read(RECOVERY/'plan.json');src=Path(plan['sources'][label]['path'])
    output=[];changed=[]
    for cell in read(RECOVERY/(label+'.json')):
        j=cell['job'];jid=j['job_id']
        gen=read(src/'generations'/(jid+'.json'))
        require(sha256(src/'generations'/(jid+'.json'))==plan['sources'][label]['files']['generations/'+jid+'.json'],'old raw drift')
        previous=read(RECOVERY/'merged'/(label+'-'+jid+'.json'))
        rows=[]
        for raw,old in zip(gen['rows'],previous['rows']):
            d=select_target(raw,gen['generation_contract'],'humaneval' if label.startswith('humaneval') else 'gpqa')
            require(all(old[k]==v for k,v in raw.items()), 'old raw mismatch')
            if d['kind']=='invalid':
                row={**raw,'status':'invalid_generation','recovery':d}
                if old['status']=='ok':changed.append({'job_id':jid,'repeat':raw['repeat']})
            else:
                require(old['status']=='ok' and old['evidence']['target_text']==d['target'],'unexpected new target')
                row={**old,'recovery':d};check_score(row)
            rows.append(row)
        output.append({'job':j,'rows':rows,'summary':repeats(rows,8)})
    return output,changed


def prepare(root):
    require(not (root/'manifest.json').exists(), 'already prepared')
    oldaudit=read(RECOVERY/'audit.json')
    require(oldaudit['status']=='PASS' and oldaudit['scored_new']==491 and oldaudit['generation_calls']==0,'old audit')
    verify(RECOVERY,oldaudit['files'])
    source_plan=read(RECOVERY/'plan.json')
    verify(Path(source_plan['config']['model_path']),source_plan['model_files'])
    for source in source_plan['sources'].values(): verify(Path(source['path']),source['files'])
    (root/'corrected-history').mkdir()
    changed={}
    for label in source_plan['sources']:
        results,delta=corrected_source(label);changed[label]=delta
        folder=root/'corrected-history'/label;folder.mkdir()
        for result in results:save(folder/(result['job']['job_id']+'.json'),result)
        save(folder/'analysis.json',e2_analysis(results,source_plan['sources'][label]['config']))
    require({k:len(v) for k,v in changed.items()}=={'gpqa-formal':2,'gpqa-canary':0,'humaneval-canary':0},'unexpected v2 impact')
    he=read(root/'corrected-history/humaneval-canary/analysis.json')
    canary_coverage(he,source_plan['sources']['humaneval-canary']['config'])
    spec=read(OLD/'legacy-spec.json'); datasets={};model_files=None
    for b in BENCHES:
        config=read(OLD/b/'e2/llama3-8b/config.json')
        require(config['temperatures']==[.3,.7,1.2] and config['repeats']==8 and config['max_new_tokens']==4096,'science config')
        shared={k:v for k,v in config.items() if k not in ('generation_prompt_version','max_new_tokens')}
        require(shared=={k:v for k,v in source_plan['config'].items() if k not in ('generation_prompt_version','max_new_tokens')},'model/seed/runtime drift')
        require(config['generation_prompt_version']==('humaneval-single-step-v2' if b=='humaneval' else 'v1'),'prompt version')
        require(not (OLD/b/'e2/llama3-8b/formal-e2').exists(),'formal already attempted')
        slot=root/b;slot.mkdir();save(slot/'config.json',config)
        phases=('formal',) if b=='humaneval' else ('canary','formal')
        for phase in phases:
            prepared=OLD/'prepared'/b/('full' if phase=='formal' else 'canary-e2')
            verify(prepared,spec['benchmarks'][b]['inputs']['full' if phase=='formal' else 'canary-e2']['files'])
            init_run(prepared,slot/'config.json',slot/phase,'e2',8)
            protocol,cfg=validate_run(slot/phase)
            if model_files is None:model_files=protocol['model_files']
            require(protocol['model_files']==model_files==source_plan['model_files'],'model drift')
            (slot/phase/'eos-results').mkdir();(slot/phase/'generation-attempts').mkdir()
        jobs=read(slot/'formal/jobs.json')
        require(len(jobs)==spec['benchmarks'][b]['e2_anchors']*3,'formal denominator')
        datasets[b]={'anchors':len(jobs)//3,'formal_draws':len(jobs)*8,'canary_draws_new':0 if b=='humaneval' else len(read(slot/'canary/jobs.json'))*8}
        (slot/'worker-logs').mkdir()
    require(sum(x['anchors'] for x in datasets.values())==5005,'remaining cohort')
    frozen=[p for p in root.rglob('*.json') if 'scratch' not in p.parts and 'submissions' not in p.parts and p.name!='deployment.json']
    manifest={'version':VERSION,'created':now(),'datasets':datasets,'model_files':model_files,
              'model_stats':model_stats(Path(source_plan['config']['model_path']),model_files),
              'source_audit_sha256':sha256(RECOVERY/'audit.json'),'implementation':implementation_files(),
              'files':{str(p.relative_to(root)):sha256(p) for p in frozen},'corrected_history_changes':changed}
    save(root/'manifest.json',{**manifest,'protocol_id':digest(manifest)})
    save(root/'ready.json',{'status':'PASS','job_id':os.environ['SLURM_JOB_ID'],'manifest_sha256':sha256(root/'manifest.json')})


def dual_rows(generation, case, job, backend):
    strict=[];extended=[]
    for raw in generation['rows']:
        decision=select_target(raw,generation['generation_contract'],case.get('task_type'))
        good=raw['parse']['valid'] and raw['finish_reason']=='boundary'
        row={**raw,'status':'ok' if good else 'invalid_generation'}
        scored=None
        if decision['target'] is not None:
            scored=backend.score(case,job['prefix_ids'],job['deleted_id'],decision['target']);check_score(scored)
        if good:
            require(decision['kind']=='strict' and decision['target']==raw['parse']['body'],'strict target drift')
            row.update(scored)
        strict.append(row)
        ext={**row,'recovery':decision}
        if decision['kind']=='eos_recovered':ext.update(status='ok',**scored)
        extended.append(ext)
    return strict,extended


def process_phase(root,b,phase,index,backend):
    run=root/b/phase;protocol,config=validate_run(run)
    jobs=[j for i,j in enumerate(read(run/'jobs.json')) if i%8==index]
    cases={c['item_id']:c for c in read(run/'inputs/cases.json')}
    folder=run/'workers'/str(index);folder.mkdir()
    for job in jobs:
        jid=job['job_id'];case=cases[job['item_id']]
        require(not (run/'generations'/(jid+'.json')).exists(),'uncertain generation: no retry')
        seed=int(digest([config['seed'],jid])[:8],16)
        save(run/'generation-attempts'/(jid+'.json'),{'job_id':jid,'seed':seed,'worker':index,'slurm_job_id':os.environ['SLURM_JOB_ID']})
        generated=backend.generate(case,job['prefix_ids'],job['temperature'],config['repeats'],seed)
        save(run/'generations'/(jid+'.json'),{'job_id':jid,'protocol_id':protocol['protocol_id'],**generated})
        strict,eos=dual_rows(generated,case,job,backend)
        base={'job':job,'protocol_id':protocol['protocol_id'],'scientific_evidence':protocol['scientific_evidence'],'status':'ok',
              'generation_sha256':sha256(run/'generations'/(jid+'.json'))}
        save(run/'results'/(jid+'.json'),{**base,'rows':strict,'summary':repeats(strict,8)})
        save(run/'eos-results'/(jid+'.json'),{**base,'parser_version':VERSION,'rows':eos,'summary':repeats(eos,8)})
        print(b,phase,jid,'completed',flush=True)
    save(folder/'completion.json',{'status':'complete','jobs':len(jobs),
         'result_hashes':{j['job_id']:sha256(run/'results'/(j['job_id']+'.json')) for j in jobs},
         'eos_result_hashes':{j['job_id']:sha256(run/'eos-results'/(j['job_id']+'.json')) for j in jobs},'ended':now()})


def worker(root,b,index):
    p=check(root);slot=root/b;cfg=read(slot/'config.json')
    save(slot/'worker-logs'/f'{index}-attempt.json',{'job_id':os.environ['SLURM_JOB_ID'],'worker':index})
    require(model_stats(Path(cfg['model_path']),p['model_files'])==p['model_stats'],'model changed')
    backend=HFBackend(cfg['model_path'],cfg)
    jobs=read(slot/'formal/jobs.json');j=jobs[index%len(jobs)]
    cases={c['item_id']:c for c in read(slot/'formal/inputs/cases.json')}
    probe=backend.score(cases[j['item_id']],j['prefix_ids'],j['deleted_id'],'A deterministic scoring check.')
    save(slot/'worker-logs'/f'{index}-reference.json',numeric_reference(backend,probe['evidence']))
    if b!='humaneval':process_phase(root,b,'canary',index,backend)
    while not (slot/'canary-gate.json').exists():time.sleep(.5)
    require(read(slot/'canary-gate.json')['status']=='PASS','canary gate')
    process_phase(root,b,'formal',index,backend)
    save(slot/'worker-logs'/f'{index}-complete.json',{'status':'PASS','worker':index,'job_id':os.environ['SLURM_JOB_ID']})


def launch(root,b):
    p=check(root);slot=root/b
    require(read(root/'ready.json')['manifest_sha256']==sha256(root/'manifest.json'),'CPU ready identity')
    visible=os.environ.get('CUDA_VISIBLE_DEVICES','').split(',')
    require(len(visible)==len(set(visible))==8 and all(visible),'8 allocated GPUs required')
    require(not list((slot/'worker-logs').glob('*-attempt.json')),'worker attempt exists')
    children=[]
    try:
        for i,gpu in enumerate(visible):
            log=(slot/'worker-logs'/f'{i}.log').open('x')
            proc=subprocess.Popen([sys.executable,'-m',MODULE,'worker','--root',str(root),'--benchmark',b,'--worker',str(i)],
                                  stdout=log,stderr=subprocess.STDOUT,env={**os.environ,'CUDA_VISIBLE_DEVICES':gpu})
            children.append((proc,log))
        while True:
            require(all(proc.poll() in (None,0) for proc,_ in children),'worker failed')
            refs=list((slot/'worker-logs').glob('*-reference.json'))
            done=b=='humaneval' or len(list((slot/'canary/workers').glob('*/completion.json')))==8
            if len(refs)==8 and done:break
            time.sleep(1)
        if b=='humaneval':stats=read(root/'corrected-history/humaneval-canary/analysis.json')
        else:
            records=[read(f) for f in sorted((slot/'canary/eos-results').glob('*.json'))]
            cells=[r['summary'] for r in records]
            require(len(cells)==len(read(slot/'canary/jobs.json')),'canary result set')
            groups={}
            for r in records:groups.setdefault(r['job']['item_id'],[]).append(r['summary']['complete'])
            stats={'cells':cells,'complete_questions':sum(len(v)==3 and all(v) for v in groups.values()),'planned_questions':len(groups)}
        gate=canary_coverage(stats,read(slot/'config.json'))
        save(slot/'canary-gate.json',{'status':'PASS','coverage':gate,'reused':b=='humaneval'})
        while any(proc.poll() is None for proc,_ in children):
            require(all(proc.poll() in (None,0) for proc,_ in children),'worker failed')
            time.sleep(1)
        require(all(proc.returncode==0 for proc,_ in children),'worker failure')
    finally:stop_children(children)
    save(slot/'gpu-complete.json',{'status':'PASS','job_id':os.environ['SLURM_JOB_ID'],'version':VERSION,'protocol_id':p['protocol_id']})


def audit(root,b):
    from transformers import AutoTokenizer
    p=check(root);slot=root/b
    require(read(slot/'gpu-complete.json')['status']=='PASS','GPU completion missing')
    cfg=read(slot/'config.json');verify(Path(cfg['model_path']),p['model_files'])
    tok=AutoTokenizer.from_pretrained(cfg['model_path'],local_files_only=True,trust_remote_code=False)
    context=object.__new__(HFBackend);context.tokenizer=tok;context.config=cfg
    encode=lambda text:tok.encode(text,add_special_tokens=False)
    outputs={}
    for phase in (('formal',) if b=='humaneval' else ('canary','formal')):
        run=slot/phase;strict=analyze(run,slot/(phase+'-strict-analysis'))
        jobs=read(run/'jobs.json');keys={j['job_id'] for j in jobs}
        require({f.stem for f in (run/'generation-attempts').glob('*.json')}==keys,'generation attempts')
        require({f.stem for f in (run/'eos-results').glob('*.json')}==keys,'extra/missing recovered cells')
        cases={c['item_id']:c for c in read(run/'inputs/cases.json')};results=[]
        for i,j in enumerate(jobs):
            jid=j['job_id'];r=read(run/'eos-results'/(jid+'.json'));g=read(run/'generations'/(jid+'.json'));old=read(run/'results'/(jid+'.json'))
            done=read(run/'workers'/str(i%8)/'completion.json')
            require(done['eos_result_hashes'][jid]==sha256(run/'eos-results'/(jid+'.json')),'worker hash')
            require(r['job']==j and r['generation_sha256']==sha256(run/'generations'/(jid+'.json')) and r['parser_version']==VERSION,'result identity')
            require(len(r['rows'])==len(g['rows'])==8,'repeat count')
            case=cases[j['item_id']]
            require(g['prompt']==context.context(case,j['prefix_ids'],for_generation=True) and g['prompt_token_ids']==encode(g['prompt']),'generation prompt')
            require(j['prefix_ids'].count(j['deleted_id'])==1,'delete exactly once')
            expected_contexts={'full_context_ids':encode(context.context(case,j['prefix_ids'])),
                'deleted_context_ids':encode(context.context(case,[n for n in j['prefix_ids'] if n!=j['deleted_id']]))}
            contract=g['generation_contract']
            require(contract['max_new_tokens']==cfg['max_new_tokens'] and contract['max_context']==8192 and
                    contract['prompt_version']==cfg.get('generation_prompt_version','v1') and contract['boundary']=='</step>' and
                    contract['constrained_decoding'] is False,'generation contract')
            expected_seed=int(digest([cfg['seed'],jid])[:8],16)
            attempt=read(run/'generation-attempts'/(jid+'.json'))
            require(attempt['job_id']==jid and attempt['seed']==expected_seed and attempt['worker']==i%8,'generation attempt')
            for raw,row,prior in zip(g['rows'],r['rows'],old['rows']):
                require(all(row[k]==v for k,v in raw.items()),'raw modified')
                require(raw['seed']==expected_seed and len(raw['generated_token_ids'])<=cfg['max_new_tokens'] and
                        tok.decode(raw['generated_token_ids'],skip_special_tokens=True)==raw['raw_text'],'raw tokens/seed')
                d=select_target(raw,g['generation_contract'],cases[j['item_id']].get('task_type'))
                require(row['recovery']==d and (row['status']=='ok')==(d['target'] is not None),'decision mismatch')
                if row['status']=='ok':
                    check_score(row);require(row['evidence']['target_text']==d['target'],'target mismatch')
                    require(row['evidence']['target_ids']==encode(d['target']) and
                            all(row['evidence'][k]==v for k,v in expected_contexts.items()),'score context or target ids')
                if d['kind']=='strict':require({k:v for k,v in row.items() if k!='recovery'}==prior,'strict score drift')
            results.append(r)
        out=e2_analysis(results,cfg);save(slot/(phase+'-eos-analysis.json'),out)
        outputs[phase]={'strict_complete':strict['complete_questions'],'eos_complete':out['complete_questions'],
                        'planned_questions':out['planned_questions'],'coverage':out['generation_coverage_by_temperature']}
    for i in range(8):
        ref=read(slot/'worker-logs'/f'{i}-reference.json')
        require(ref['status']=='PASS' and ref['repeat_max_abs']<=1e-5 and all(e<=.005 for e in ref['masked_loss_errors'].values()),'numerical reference')
        check_score(ref['probe']);check_score(ref['repeat_probe'])
        actual=max(abs(x-y) for k in ('full_logprobs','deleted_logprobs')
                   for x,y in zip(ref['probe']['evidence'][k],ref['repeat_probe']['evidence'][k]))
        require(actual==ref['repeat_max_abs'] and all(abs(ref['native_losses'][k]-ref['probe']['score'][k+'_nll'])==ref['masked_loss_errors'][k] for k in ('full','deleted')),'numerical arithmetic')
        require(read(slot/'worker-logs'/f'{i}-complete.json')['status']=='PASS','worker completion')
    save(slot/'audit.json',{'status':'PASS','protocol_id':p['protocol_id'],'version':VERSION,'benchmark':b,'results':outputs,
         'files':{str(f.relative_to(slot)):sha256(f) for f in sorted(slot.rglob('*.json'))},
         'note':'Strict historical parser and Llama-only EOS adaptation are separate; invalid draws preserved, no regeneration.'})


def main():
    ap=argparse.ArgumentParser();ap.add_argument('action',choices=('prepare','launch','worker','audit'))
    ap.add_argument('--root',type=Path,required=True);ap.add_argument('--benchmark',choices=BENCHES);ap.add_argument('--worker',type=int)
    a=ap.parse_args();os.umask(0o077)
    require(a.root==ROOT and ROOT.resolve()==ROOT and PROJECT.resolve()==PROJECT and os.environ.get('SLURM_JOB_ID'),'allocated project boundary')
    with terminate_as_exception():
        if a.action=='prepare':prepare(a.root)
        elif a.action=='worker':
            require(a.worker is not None and 0<=a.worker<8,'worker id');worker(a.root,a.benchmark,a.worker)
        else:globals()[a.action](a.root,a.benchmark)

if __name__=='__main__':main()
