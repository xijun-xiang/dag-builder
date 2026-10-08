"""One-shot B1 Llama campaign; unchanged historical E1/E2 engine and inputs.

prepare is CPU/file-only. submit/release queue behind the frozen B1 E3 chain;
admit-runtime requires completed E3 before the unchanged canary/formal engine.
No retry/resume, A1 dependency, candidate-code execution, or new DAG selection.
"""
import argparse
import copy
import os
from pathlib import Path
import shutil
import subprocess
import sys

from pals_validation.io import read, save, sha256, verify
from pals_validation.locking import exclusive_lock
from pals_validation.run import code_hashes, validate_config

PROJECT = Path('/work/projects/polyullm/xxj/PALS')
OLD = PROJECT / 'runs/20261003-internlm-fivebench-e1e2-v1'
ROOT = PROJECT / 'runs/20261009-llama-fivebench-e1e2-v2'
PREVIOUS_ROOT = PROJECT / 'runs/20261008-llama-fivebench-e1e2-v1'
WEIGHTS = PROJECT / 'runs/20261008-llama-e1e2-weights-v1'
CONTEXT = PROJECT / 'runs/20261008-llama-e1e2-context-v1'
E3 = PROJECT / 'runs/20261009-e3-full-internlm-b1-v1'
E3_JOBS = ('115000', '115001')
E3_PROTOCOL = '42d005583186ea38bd57141a3a2b4493e18a843bacb1ac37c7ae047099c8626a'
REPO = Path(__file__).resolve().parents[1]
SPEC_SHA = '54419278650427638ce329c502c4de40d5736d17cfbfd6d4ef0224cdbe63d01f'
PLAN_SHA = '53f2de52a2cd8cfd0f891c8917797e77792a81b20204db1e51a38a2ccbf89586'
REVISION = 'e9f7e7d3fa08b550ea228e38bb5501d35be92c0d'
MODEL = PROJECT / 'models/hf/meta-llama/Meta-Llama-3-8B-Instruct'
BENCHES = ('gpqa', 'humaneval', 'gsm8k', 'livecodebench', 'mmlu')
SLOTS = tuple((b, e) for e in ('e1', 'e2') for b in BENCHES)
EXCLUDE = 'tko-b1-nv-dgx06'
LAUNCHER = REPO / 'scripts/b1-llama-e1e2.sbatch'


def require(ok, message):
    if not ok:
        raise ValueError(message)


def safe(path, existing=True):
    path = Path(path)
    require(PROJECT.resolve(strict=True) == PROJECT, 'project symlink')
    require(path.is_absolute() and PROJECT in path.parents, 'outside PALS')
    require(path.resolve(strict=existing) == path, 'symlink/path escape')
    return path


def scheduler_complete(job, step=True):
    rows = subprocess.check_output(['sacct', '-j', str(job), '-Pn',
                                   '--format=JobID,State,ExitCode'], text=True)
    expected = {str(job), str(job) + '.batch'} | ({str(job) + '.0'} if step else set())
    found = {r[0]: r[1:] for line in rows.splitlines() if (r := line.split('|'))[0] in expected}
    require(set(found) == expected and all(v == ['COMPLETED', '0:0'] for v in found.values()),
            'scheduler gate failed: ' + str(job))


def specification():
    require(sha256(safe(OLD / 'spec.json')) == SPEC_SHA, 'legacy spec changed')
    spec = read(OLD / 'spec.json')
    require(spec['slots'] == [{'benchmark': b, 'experiment': e} for b, e in SLOTS], 'slot order changed')
    require(set(spec['benchmarks']) == set(BENCHES), 'benchmark set changed')
    hashes = code_hashes()
    require(all(hashes.get(n) == h for n, h in spec['source_hashes'].items()), 'legacy engine changed')
    return spec


def config(spec, benchmark, experiment, runtime):
    require((benchmark, experiment) in SLOTS, 'unknown slot')
    require(runtime.get('transformers') == '5.6.0' and runtime.get('torch'), 'CPU runtime missing')
    result = copy.deepcopy(spec['base_config'])
    result.update(model_path=str(MODEL), model_revision=REVISION, max_context=8192,
                  reviewed_local_code=None, chat_template_kwargs={}, runtime_versions=dict(runtime),
                  campaign_experiment=experiment, canary_coverage_policy='report_invalid',
                  canary_min_valid_repeats=2,
                  max_new_tokens=spec['benchmarks'][benchmark]['max_new_tokens'],
                  generation_prompt_version=spec['benchmarks'][benchmark]['generation_prompt_version'])
    require(result['temperatures'] == [.3, .7, 1.2] and result['repeats'] == 8
            and result['seed'] == 2026091507, 'sampling protocol changed')
    require(result['max_new_tokens'] == (2048 if benchmark == 'gpqa' else 4096), 'budget changed')
    validate_config(result)
    return result


def prerequisites():
    scheduler_complete(114951)
    scheduler_complete(114954, step=False)
    context = read(safe(CONTEXT / 'completion.json'))
    require(context['status'] == 'PASS' and context['job_id'] == '114951'
            and context['frozen_spec_sha256'] == SPEC_SHA, 'context gate failed')
    require(set(context['benchmarks']) == set(BENCHES)
            and all(x['status'] == 'PASS' for x in context['benchmarks'].values()), 'context coverage')
    require(sha256(safe(WEIGHTS / 'plan.json')) == PLAN_SHA, 'download plan changed')
    plan = read(WEIGHTS / 'plan.json')['models']['llama3']
    receipt = read(safe(WEIGHTS / 'completion.json'))
    require(receipt['status'] == 'PASS' and receipt['job_id'] == '114954'
            and receipt['revision'] == REVISION and receipt['plan_sha256'] == PLAN_SHA
            and receipt['path'] == str(MODEL), 'weight receipt failed')
    files = read(safe(WEIGHTS / 'model-files.json'))
    require(set(files) == set(plan['files']) == set(receipt['files']) and len(files) == 14,
            'model file set changed')
    for name, h in files.items():
        require(h == plan['files'][name]['sha256'] == receipt['files'][name]['sha256'], 'weight SHA changed')
        require(safe(MODEL / name).stat().st_size == plan['files'][name]['size'], 'weight size changed')
    require(not (WEIGHTS / 'failure.json').exists(), 'failed weight preparation')
    return context, files


def slot_root(benchmark, experiment):
    return ROOT / benchmark / experiment / 'llama3-8b'


def prepare():
    spec = specification()
    context, model_files = prerequisites()
    require(not subprocess.check_output(['git', '-C', str(REPO), 'status', '--porcelain'], text=True).strip(),
            'freeze/commit source before deployment')
    safe(ROOT, existing=False)
    require(not ROOT.exists(), 'existing/partial deployment; never overwrite')
    require(not list(safe(PREVIOUS_ROOT / 'submissions').iterdir()), 'old campaign has submission evidence')
    for b in BENCHES:
        for role, item in spec['benchmarks'][b]['inputs'].items():
            verify(safe(OLD / 'prepared' / b / role), item['files'])
    ROOT.mkdir(mode=0o700)
    for name in ('logs', 'submissions'):
        (ROOT / name).mkdir(mode=0o700)
    save(ROOT / 'deployment-attempt.json', {'legacy_spec_sha256': SPEC_SHA})
    shutil.copyfile(OLD / 'spec.json', ROOT / 'legacy-spec.json')
    denominators = {}
    for b in BENCHES:
        entry = spec['benchmarks'][b]
        for role, inp in entry['inputs'].items():
            dest = ROOT / 'prepared' / b / role
            dest.mkdir(parents=True, mode=0o700)
            for name in inp['files']:
                with (OLD / 'prepared' / b / role / name).open('rb') as src, (dest / name).open('xb') as dst:
                    shutil.copyfileobj(src, dst)
            verify(dest, inp['files'])
        manifest = read(ROOT / 'prepared' / b / 'full/manifest.json')
        jobs = read(ROOT / 'prepared' / b / 'full/jobs.json')
        require(manifest['questions'] == entry['questions'] and manifest['counts']['e2'] == entry['e2_anchors']
                and manifest['counts']['fair_pair'] == entry['fair_pair'], 'denominator changed')
        denominators[b] = {'questions': entry['questions'], 'fair_pair': entry['fair_pair'],
                           'e1_jobs': sum(j['kind'] == 'e1' for j in jobs), 'e2_anchors': entry['e2_anchors']}
        for e in ('e1', 'e2'):
            slot = slot_root(b, e)
            (slot / 'canary').mkdir(parents=True, mode=0o700)
            # Campaign's established layout expects these two internal links.
            (slot.parent / 'prepared').symlink_to(ROOT / 'prepared' / b / 'full')
            (slot / 'canary/prepared').symlink_to(ROOT / 'prepared' / b / ('canary-' + e))
            cfg = config(spec, b, e, context['runtime'])
            for location in (slot, slot / 'canary'):
                save(location / 'config.json', cfg)
            for name in ('enroot-cache', 'enroot-data', 'enroot-runtime', 'tmp', 'hf', 'hf-modules', 'cache', 'cuda'):
                (slot / 'scratch' / name).mkdir(parents=True, mode=0o700)
    require(sum(d['questions'] for d in denominators.values()) == 5500
            and sum(d['e1_jobs'] for d in denominators.values()) == 78823
            and sum(d['e2_anchors'] for d in denominators.values()) == 5109, 'full denominator changed')
    save(ROOT / 'deployment.json', {'status': 'PREPARED_NOT_SUBMITTED', 'legacy_spec_sha256': SPEC_SHA,
         'code_hashes': code_hashes(), 'repo': str(REPO), 'controller_sha256': sha256(Path(__file__)),
         'launcher_sha256': sha256(LAUNCHER), 'model_files': model_files, 'runtime': context['runtime'],
         'weight_receipt_sha256': sha256(WEIGHTS / 'completion.json'),
         'context_receipt_sha256': sha256(CONTEXT / 'completion.json'), 'denominators': denominators})
    # The prior prepared campaign has never run. Prove all ten scientific
    # configurations and all input hashes are identical rather than infer it.
    previous = read(safe(PREVIOUS_ROOT / 'deployment.json'))
    require(previous['denominators'] == denominators and previous['model_files'] == model_files
            and previous['code_hashes'] == code_hashes(), 'previous science identity differs')
    for b, e in SLOTS:
        require(read(safe(PREVIOUS_ROOT / b / e / 'llama3-8b/config.json')) ==
                read(slot_root(b, e) / 'config.json'), 'previous configuration differs')
    for b in BENCHES:
        for role, inp in spec['benchmarks'][b]['inputs'].items():
            verify(safe(PREVIOUS_ROOT / 'prepared' / b / role), inp['files'])
    save(ROOT / 'previous-preparation.json', {'status':'IDENTICAL_SCIENCE',
         'path':str(PREVIOUS_ROOT), 'deployment_sha256':sha256(PREVIOUS_ROOT / 'deployment.json')})


def check():
    spec, declared = specification(), read(safe(ROOT / 'deployment.json'))
    require(declared['repo'] == str(REPO) and declared['code_hashes'] == code_hashes()
            and declared['controller_sha256'] == sha256(Path(__file__))
            and declared['launcher_sha256'] == sha256(LAUNCHER), 'deployed source changed')
    require(sha256(ROOT / 'legacy-spec.json') == SPEC_SHA, 'copied spec changed')
    previous = read(safe(ROOT / 'previous-preparation.json'))
    require(previous['status'] == 'IDENTICAL_SCIENCE' and previous['path'] == str(PREVIOUS_ROOT)
            and sha256(safe(PREVIOUS_ROOT / 'deployment.json')) == previous['deployment_sha256'],
            'previous preparation identity changed')
    require(not list(safe(PREVIOUS_ROOT / 'submissions').iterdir()), 'old campaign has submission evidence')
    for b in BENCHES:
        for role, inp in spec['benchmarks'][b]['inputs'].items():
            verify(safe(ROOT / 'prepared' / b / role), inp['files'])
        for e in ('e1', 'e2'):
            slot = safe(slot_root(b, e))
            require((slot.parent / 'prepared').resolve(strict=True) == ROOT / 'prepared' / b / 'full', 'full input link')
            require((slot / 'canary/prepared').resolve(strict=True) == ROOT / 'prepared' / b / ('canary-' + e), 'canary input link')
            cfg = config(spec, b, e, declared['runtime'])
            require(read(slot / 'config.json') == read(slot / 'canary/config.json') == cfg, 'configuration changed')
    return declared


def e3_gate(completed=False):
    """Pending is admissible only for queueing, never for scientific completion."""
    chain_path = safe(E3 / 'submissions/full-chain.json')
    chain = read(chain_path)
    require([(x['job_id'], x['stage'], x['slot'], x['protocol_id']) for x in chain] ==
            [(E3_JOBS[0], 'gpu', 'internlm3', E3_PROTOCOL),
             (E3_JOBS[1], 'evaluate', 'internlm3', E3_PROTOCOL)], 'E3 chain identity differs')
    require(read(safe(E3 / 'submissions/released.json')) ==
            {'status':'RELEASED', 'job_ids':list(E3_JOBS)}, 'E3 chain not released')
    manifest_path = safe(E3 / 'experiment/manifest.json')
    manifest = read(manifest_path)
    require(manifest['protocol_id'] == E3_PROTOCOL and manifest['counts'] ==
            {'gpqa':198, 'gsm8k':1319, 'humaneval':164, 'livecodebench':175, 'mmlu':14042},
            'E3 manifest identity differs')
    require(manifest['full_continuation']['execution']['coverage_policy'] == 'report_invalid',
            'E3 coverage policy changed')
    rows = subprocess.check_output(['sacct', '-j', ','.join(E3_JOBS), '-Pn',
                                   '--format=JobID,State,ExitCode'], text=True)
    states = {r[0]:r[1:] for line in rows.splitlines() if (r := line.split('|'))[0] in E3_JOBS}
    require(set(states) == set(E3_JOBS) and all(v[0] in ('PENDING','RUNNING','COMPLETED')
            and v[1] == '0:0' for v in states.values()), 'E3 predecessor failed or unknown')
    identity = {'jobs':list(E3_JOBS), 'protocol_id':E3_PROTOCOL,
                'chain_sha256':sha256(chain_path), 'manifest_sha256':sha256(manifest_path)}
    if not completed:
        return identity
    for job in E3_JOBS:
        # Additional bounded monitoring steps are recorded separately. The
        # scientific executable is .0; a no-GPU telemetry probe failed with 6.
        scheduler_complete(job)
    audit_path = safe(E3 / 'experiment/internlm3/audit.json')
    audit = read(audit_path)
    require(audit['status'] == 'PASS' and audit['include_outcomes'] is True
            and audit['scientific_evidence'] is True and audit['coverage_policy'] == 'report_invalid'
            and len(audit['items']) == 15898 and audit['slot'] == 'internlm3'
            and audit['protocol_id'] == E3_PROTOCOL,
            'B1 E3 application gate failed')
    return {**identity, 'audit_sha256':sha256(audit_path)}


def command(index, previous):
    b, e = SLOTS[index]
    name = 'pals-llama-' + b + '-' + e
    cmd = ['sbatch', '--parsable', '--hold', '--partition=defq', '--reservation=code-agent',
           '--exclude=' + EXCLUDE, '--nodes=1', '--ntasks=1', '--gpus-per-node=8',
           '--cpus-per-task=32', '--mem=256G', '--time=08:00:00', '--no-requeue', '--job-name=' + name,
           '--output=' + str(ROOT / 'logs' / ('%j-' + name + '.out')),
           '--error=' + str(ROOT / 'logs' / ('%j-' + name + '.err'))]
    if previous:
        cmd += ['--dependency=afterok:' + previous, '--kill-on-invalid-dep=yes']
    return cmd + [str(LAUNCHER)]


def active_guard(allowed=()):
    rows = subprocess.check_output(['squeue', '-h', '-u', 'xijun', '-o', '%i %j'], text=True)
    for line in rows.splitlines():
        job, name = line.split(maxsplit=1)
        require(not name.startswith(('e3g-', 'e3c-', 'e3x-', 'pals-llama-')) or job in allowed,
                'another B1 PALS chain is active')


def submit():
    check()
    prerequisite = e3_gate()
    ledger = safe(ROOT / 'submissions')
    with exclusive_lock(ledger / 'submit.lock'):
        require(not list(ledger.glob('*attempt.json')), 'existing/uncertain submission; no retry')
        active_guard(E3_JOBS)
        save(ledger / 'prerequisite.json', prerequisite)
        previous, records = E3_JOBS[-1], []
        for index, (b, e) in enumerate(SLOTS):
            cmd = command(index, previous)
            env = {k: v for k, v in os.environ.items() if not k.startswith(('SBATCH_', 'PALS_'))}
            env.update(PALS_REPO=str(REPO), PALS_CAMPAIGN_ROOT=str(ROOT), PALS_BENCHMARK=b, PALS_EXPERIMENT=e)
            save(ledger / f'{index}-attempt.json', {'command': cmd})
            previous = subprocess.check_output(cmd, env=env, cwd=ROOT, text=True).strip().split(';')[0]
            require(previous.isdigit(), 'uncertain sbatch receipt')
            record = {'job_id': previous, 'index': index, 'benchmark': b, 'experiment': e, 'command': cmd}
            save(ledger / f'{index}-submitted.json', record)
            records.append(record)
        save(ledger / 'full-chain.json', records)
    return records


def verify_held(record, previous):
    index, job = record['index'], record['job_id']
    require(job.isdigit() and record['command'] == command(index, previous), 'job command changed')
    b, e = SLOTS[index]
    name = 'pals-llama-' + b + '-' + e
    require(record['benchmark'] == b and record['experiment'] == e, 'slot changed')
    raw = subprocess.check_output(['scontrol', 'show', 'job', job, '-o'], text=True).strip()
    require(len(raw.splitlines()) == 1, 'ambiguous job state')
    fields = dict(s.split('=', 1) for s in raw.split() if '=' in s)
    expected = {'JobId': job, 'JobName': name, 'JobState': 'PENDING', 'Priority': '0',
                'Partition': 'defq', 'Reservation': 'code-agent', 'ExcNodeList': EXCLUDE,
                'NumTasks': '1', 'NumCPUs': '32', 'CPUs/Task': '32', 'TimeLimit': '08:00:00',
                'Requeue': '0', 'Restarts': '0', 'Command': str(LAUNCHER), 'WorkDir': str(ROOT),
                'StdOut': str(ROOT / 'logs' / f'{job}-{name}.out'),
                'StdErr': str(ROOT / 'logs' / f'{job}-{name}.err')}
    require(all(fields.get(k) == v for k, v in expected.items()), 'effective scheduler fields differ')
    require(fields.get('UserId', '').startswith('xijun(') and fields.get('NumNodes') in ('1', '1-1'), 'owner/node mismatch')
    tres = dict(s.split('=', 1) for s in fields.get('ReqTRES', '').split(',') if '=' in s)
    require(all(tres.get(k) == v for k, v in {'cpu':'32', 'mem':'256G', 'node':'1', 'gres/gpu':'8'}.items()), 'resource mismatch')
    if previous:
        require(fields.get('Dependency') in ('afterok:' + previous, 'afterok:' + previous + '(unfulfilled)')
                and fields.get('KillOInInvalidDependent') == 'Yes', 'dependency mismatch')
    else:
        require(fields.get('Dependency') == '(null)', 'unexpected dependency')
    return fields


def release():
    check()
    ledger = safe(ROOT / 'submissions')
    require(e3_gate() == read(ledger / 'prerequisite.json'), 'E3 prerequisite changed')
    with exclusive_lock(ledger / 'submit.lock'):
        require(not list(ledger.glob('release-*-attempt.json')), 'release already attempted; no retry')
        jobs = read(ledger / 'full-chain.json')
        require([j['index'] for j in jobs] == list(range(10)) and len({j['job_id'] for j in jobs}) == 10,
                'incomplete/duplicate chain')
        active_guard({j['job_id'] for j in jobs} | set(E3_JOBS))
        states, previous = {}, E3_JOBS[-1]
        for record in jobs:
            require(record == read(ledger / f"{record['index']}-submitted.json"), 'receipt mismatch')
            states[record['job_id']] = verify_held(record, previous)
            previous = record['job_id']
        save(ledger / 'held-audit.json', {'status':'PASS', 'jobs':jobs, 'scheduler':states})
        for record in reversed(jobs):
            job = record['job_id']
            save(ledger / f'release-{job}-attempt.json', {'job_id':job})
            subprocess.run(['scontrol', 'release', job], check=True)
            save(ledger / f'release-{job}-complete.json', {'job_id':job})
        save(ledger / 'released.json', {'status':'RELEASED', 'job_ids':[j['job_id'] for j in jobs]})


def admit_runtime(benchmark, experiment):
    require(os.environ.get('SLURM_JOB_ID') and (benchmark, experiment) in SLOTS, 'allocated known slot required')
    check()
    complete = e3_gate(completed=True)
    require({k:v for k,v in complete.items() if k != 'audit_sha256'} ==
            read(ROOT / 'submissions/prerequisite.json'), 'E3 prerequisite changed')
    record = read(ROOT / 'submissions/full-chain.json')[SLOTS.index((benchmark, experiment))]
    require(record['job_id'] == os.environ['SLURM_JOB_ID'], 'wrong allocated job')
    target = slot_root(benchmark, experiment) / 'runtime-admission.json'
    require(not target.exists(), 'runtime admission already attempted')
    save(target, {'status':'PASS', 'job_id':record['job_id'], 'e3':complete})


def runtime(benchmark, experiment):
    require(os.environ.get('SLURM_JOB_ID') and (benchmark, experiment) in SLOTS, 'allocated known slot required')
    declared = check()
    admission = read(safe(slot_root(benchmark, experiment) / 'runtime-admission.json'))
    require(admission['status'] == 'PASS' and admission['job_id'] == os.environ['SLURM_JOB_ID']
            and admission['e3']['protocol_id'] == E3_PROTOCOL, 'runtime not admitted')
    verify(MODEL, declared['model_files'])  # compute node only
    slot = slot_root(benchmark, experiment)
    require(not (slot / 'campaign.json').exists(), 'existing attempt; no resume')
    subprocess.run([sys.executable, '-m', 'pals_validation.campaign', '--root', str(slot),
                    '--experiment', experiment], check=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('prepare', 'check', 'submit', 'release', 'admit-runtime', 'runtime'))
    p.add_argument('--benchmark', choices=BENCHES)
    p.add_argument('--experiment', choices=('e1', 'e2'))
    a = p.parse_args()
    os.umask(0o077)
    os.environ['PATH'] = '/cm/local/apps/slurm/current/bin:' + os.environ['PATH']
    os.environ['SLURM_CONF'] = '/cm/shared/apps/slurm/etc/slurm/slurm.conf'
    safe(REPO)
    if a.action == 'admit-runtime':
        admit_runtime(a.benchmark, a.experiment)
    elif a.action == 'runtime':
        runtime(a.benchmark, a.experiment)
    else:
        result = globals()[a.action]()
        print({'status':'PASS', 'action':a.action, 'result':result}, flush=True)
