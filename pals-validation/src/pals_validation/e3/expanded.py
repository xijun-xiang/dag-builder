"""Bounded E3 V2 expansion with frozen selection and byte-identical raw reuse.

The original runs remain read-only. Generation/likelihood/grading kernels are
unchanged; an outer manifest records this new cohort, parser and orchestration.
Each wave contains at most eight existing-size batches and has four explicit
stages. No stage submits jobs or retries an uncertain generation.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import os
from pathlib import Path
import shutil

from ..io import digest, read, save, sha256, verify
from ..locking import exclusive_lock
from .audit import _score_matches, token_encoder
from .data import common_subset
from .decoupled import PARSER_VERSION, parse_decoupled
from .evaluate import evaluate_answer
from .metrics import summarize_trace
from .protocol import parse_trace, score_text_pair
from .run import _backend, _batch_seed, canary_batches, code_hashes, validate_config
from .schema import require

SLOTS = ('qwen25', 'phi4mini', 'qwen3')
MODEL_IDS = dict(zip(SLOTS, ('Qwen2.5-7B-Instruct', 'Phi-4-mini-instruct', 'Qwen3-14B')))
KERNELS = ('backend.py', 'metrics.py', 'model_policy.py', 'e3/backend.py',
           'e3/protocol.py', 'e3/metrics.py', 'e3/evaluate.py',
           'e3/code_harness.py', 'e3/code_tests.py')
ROOT = Path('/work/projects/polyullm/xxj/PALS')


def implementation_hashes():
    result = code_hashes()
    scripts = Path(__file__).resolve().parents[3] / 'scripts'
    for name in ('e3_expanded_launch.py', 'b1-e3-expanded.sbatch', 'b1-e3-expanded-prepare.sbatch'):
        result['scripts/' + name] = sha256(scripts / name)
    return result


def select_cohort(problems, pinned_ids, mmlu_count=100):
    """Subject-stratified hash sampling; pinned V2 development items explicit."""
    require(type(mmlu_count) is int and mmlu_count > 0, 'invalid MMLU count')
    ids = {p['problem_id'] for p in problems}
    require(len(ids) == len(problems) and set(pinned_ids) <= ids, 'unknown/duplicate input')
    groups = defaultdict(list)
    for problem in problems:
        if problem['benchmark'] == 'mmlu':
            groups[problem['subset']].append(problem)
    total = sum(map(len, groups.values()))
    require(len(groups) <= mmlu_count <= total, 'MMLU sample outside subject coverage')
    pinned = {s: [p for p in rows if p['problem_id'] in pinned_ids] for s, rows in groups.items()}
    quotas = {s: max(1, len(pinned[s])) for s in groups}
    require(sum(quotas.values()) <= mmlu_count, 'pinned sample exceeds budget')
    while sum(quotas.values()) < mmlu_count:
        candidates = [s for s in groups if quotas[s] < len(groups[s])]
        subject = min(candidates, key=lambda s: (-(mmlu_count * len(groups[s]) / total - quotas[s]), s))
        quotas[subject] += 1
    chosen = {p['problem_id'] for p in problems if p['benchmark'] != 'mmlu'}
    for subject, rows in sorted(groups.items()):
        ordered = sorted((p for p in rows if p['problem_id'] not in pinned_ids),
                         key=lambda p: (digest(['e3-expanded-mmlu-v1', 2026093001,
                                               subject, p['problem_id']]), p['problem_id']))
        chosen.update(p['problem_id'] for p in pinned[subject])
        chosen.update(p['problem_id'] for p in ordered[:quotas[subject] - len(pinned[subject])])
    return sorted(chosen), {'method': 'subject_stratified_hash_with_declared_v2_development_stratum',
        'seed': 2026093001, 'mmlu_count': mmlu_count, 'subject_quotas': quotas,
        'pinned_mmlu_ids': sorted(p['problem_id'] for rows in pinned.values() for p in rows)}


def batch_plan(problems, source_batches, config, selected_ids):
    selected = set(selected_ids)
    canaries = canary_batches(source_batches, 1)
    first = [b for b in source_batches if b['batch_id'] in canaries]
    require(all(set(b['problem_ids']) <= selected for b in first), 'pinned canary missing')
    remainder = [b for b in source_batches if b['benchmark'] != 'mmlu' and b['batch_id'] not in canaries]
    pinned_mmlu = {i for b in first if b['benchmark'] == 'mmlu' for i in b['problem_ids']}
    remaining_mmlu = sorted(p['problem_id'] for p in problems
                            if p['benchmark'] == 'mmlu' and p['problem_id'] in selected - pinned_mmlu)
    for start in range(0, len(remaining_mmlu), 8):
        ids = remaining_mmlu[start:start + 8]
        batch = {'benchmark': 'mmlu', 'problem_ids': ids,
                 'seed': _batch_seed(config['generation']['master_seed'], config['model']['id'], ids)}
        batch['batch_id'] = digest(batch)
        remainder.append(batch)
    waves = [first] + [remainder[i:i + 8] for i in range(0, len(remainder), 8)]
    flat = [i for wave in waves for batch in wave for i in batch['problem_ids']]
    require(len(flat) == len(set(flat)) and set(flat) == selected, 'batch coverage differs')
    return waves


def source_identity(source):
    protocol, config = read(source / 'protocol.json'), read(source / 'config.json')
    require(protocol['protocol_id'] == digest({k: v for k, v in protocol.items() if k != 'protocol_id'}),
            'source protocol changed')
    validate_config(config)
    require(config['protocol_version'] == 'native-trace-v2', 'only V2 source is supported')
    verify(source, protocol['input_files'])
    require(protocol['batches_sha256'] == digest(read(source / 'batches.json')), 'source batches changed')
    current = code_hashes()
    require(all(current[k] == protocol['code_hashes'][k] for k in KERNELS), 'scientific kernel changed')
    reference = read(source / 'reference.json') if config['backend'] == 'hf' else None
    if reference:
        require(reference['protocol_id'] == protocol['protocol_id'] and reference['status'] == 'PASS'
                and reference['repeat_max_abs'] <= 1e-5
                and all(reference['masked_loss_errors'][s] <= .005 for s in ('full', 'deleted')),
                'V2 numerical reference failed')
        require(sha256(config['evaluation_manifest']) == protocol['evaluation_manifest_sha256'],
                'evaluation policy changed')
    return protocol, config


def prepare(source_root: Path, output: Path, mmlu_count=100):
    require(not output.exists(), 'expansion already exists')
    current = implementation_hashes()
    sources, shared_problems, chosen, selection = {}, None, None, None
    for slot in SLOTS:
        source = (source_root / slot).resolve(strict=True)
        require(source not in output.resolve().parents and source != output.resolve(), 'source is read-only')
        protocol, config = source_identity(source)
        require(config['model']['id'] == MODEL_IDS[slot], 'model slot mismatch')
        if config['backend'] == 'hf':
            require(os.environ.get('SLURM_JOB_ID') and ROOT.resolve(strict=True) in output.resolve().parents,
                    'real prepare requires a PALS Slurm allocation')
            require(output.parent.resolve(strict=True) == output.parent, 'output parent is a symlink')
        problems, batches = read(source / 'inputs/problems.json'), read(source / 'batches.json')
        if shared_problems is None:
            shared_problems = problems
            pinned = {i for b in batches if b['batch_id'] in canary_batches(batches, 1) for i in b['problem_ids']}
            chosen, selection = select_cohort(problems, pinned, mmlu_count)
        require(problems == shared_problems, 'models have different source questions')
        expected_canary = canary_batches(batches, 1)
        raw_ids = {p.stem for p in (source / 'generation_batches').glob('*.json')}
        attempt_ids = {p.stem for p in (source / 'attempts').glob('*.json')}
        require(raw_ids == attempt_ids and raw_ids in (set(), expected_canary),
                'source uncertain/partial/expanded; manual decision required')
        reused = {}
        for batch in batches:
            if batch['batch_id'] in raw_ids:
                raw_file = source / 'generation_batches' / (batch['batch_id'] + '.json')
                attempt_file = source / 'attempts' / (batch['batch_id'] + '.json')
                raw, attempt = read(raw_file), read(attempt_file)
                require(raw['protocol_id'] == protocol['protocol_id'] and raw['batch'] == batch and
                        attempt['protocol_id'] == protocol['protocol_id'] and
                        attempt['batch_id'] == batch['batch_id'], 'reuse source differs')
                reused[batch['batch_id']] = {'raw_sha256': sha256(raw_file), 'attempt_sha256': sha256(attempt_file)}
        hashes = {name: sha256(source / name) for name in
                  ('protocol.json', 'config.json', 'batches.json', 'inputs/problems.json', 'inputs/grading.json')}
        if (source / 'reference.json').is_file():
            hashes['reference.json'] = sha256(source / 'reference.json')
        sources[slot] = {'source': str(source), 'protocol_id': protocol['protocol_id'],
                         'config': config, 'source_hashes': hashes, 'reuse': reused,
                         'waves': batch_plan(problems, batches, config, chosen),
                         'scientific_evidence': protocol['scientific_evidence']}
    manifest = {'schema_version': 'pals_e3_expansion_v1', 'parser_version': PARSER_VERSION,
                'selection': selection, 'selected_ids': chosen, 'sources': sources,
                'implementation_hashes': current,
                'counts': dict(Counter(p['benchmark'] for p in shared_problems if p['problem_id'] in chosen)),
                'common_subset': common_subset([p for p in shared_problems if p['problem_id'] in chosen], 2026092903)}
    # Keep development exposure explicit; never call all rows held-out confirmation.
    manifest['development_ids'] = sorted({i for index in (0, 1) for b in batches
        if b['batch_id'] in canary_batches(batches, index) for i in b['problem_ids']} & set(chosen))
    manifest['expansion_id'] = digest(manifest)
    if any(s['scientific_evidence'] for s in sources.values()):
        require(all(s['scientific_evidence'] for s in sources.values()) and
                manifest['counts'] == {'gpqa': 198, 'gsm8k': 1319, 'humaneval': 164,
                                       'livecodebench': 175, 'mmlu': 100},
                'real cohort differs from the approved 1956-question scope')
    output.mkdir(parents=True, mode=0o700)
    save(output / 'manifest.json', manifest)
    for slot, origin in sources.items():
        folder = output / slot
        for name in ('generation_batches', 'attempts', 'scores', 'outcomes', 'workers', 'audits', 'locks', 'logs', 'scratch'):
            (folder / name).mkdir(parents=True, mode=0o700)
        for batch_id, hashes in origin['reuse'].items():
            for name, field in (('generation_batches', 'raw_sha256'), ('attempts', 'attempt_sha256')):
                old = Path(origin['source']) / name / (batch_id + '.json')
                require(sha256(old) == hashes[field], 'reuse changed during prepare')
                dest = folder / name / old.name
                with old.open('rb') as src, dest.open('xb') as dst:
                    shutil.copyfileobj(src, dst)
                require(sha256(dest) == hashes[field], 'raw copy mismatch')
    return {'expansion_id': manifest['expansion_id'], 'counts': manifest['counts'],
            'waves': {s: len(o['waves']) for s, o in sources.items()}}


def load(root: Path, slot: str):
    manifest = read(root / 'manifest.json')
    require(manifest['expansion_id'] == digest({k: v for k, v in manifest.items() if k != 'expansion_id'}),
            'expansion manifest changed')
    require(manifest['implementation_hashes'] == implementation_hashes(), 'expansion code changed')
    require(slot in SLOTS, 'unknown model slot')
    origin = manifest['sources'][slot]
    source = Path(origin['source'])
    # Grading content is only opened by CPU evaluate. Generation never consumes it.
    verify(source, {k: v for k, v in origin['source_hashes'].items() if k != 'inputs/grading.json'})
    config = origin['config']
    require(read(source / 'config.json') == config, 'config changed')
    if config['backend'] == 'hf':
        require(os.environ.get('SLURM_JOB_ID') and ROOT.resolve(strict=True) in root.resolve().parents,
                'real execution requires a PALS Slurm allocation')
        reference = read(source / 'reference.json')
        require(reference['status'] == 'PASS' and reference['protocol_id'] == origin['protocol_id'],
                'numerical reference identity changed')
        protocol = read(source / 'protocol.json')
        require(sha256(config['evaluation_manifest']) == protocol['evaluation_manifest_sha256'],
                'evaluation policy changed')
    by_id = {p['problem_id']: p for p in read(source / 'inputs/problems.json')}
    return manifest, origin, config, by_id


def check_raw(root, slot, origin, batch, by_id):
    path = root / slot / 'generation_batches' / (batch['batch_id'] + '.json')
    raw = read(path)
    attempt_path = root / slot / 'attempts' / path.name
    attempt = read(attempt_path)
    require(raw['batch'] == batch and raw['protocol_id'] == origin['protocol_id'] and
            raw['seed'] == batch['seed'] and raw['batch_size'] == len(batch['problem_ids']) and
            raw['scientific_evidence'] == origin['scientific_evidence'] and
            [r['problem_id'] for r in raw['rows']] == batch['problem_ids'] and
            attempt['batch_id'] == batch['batch_id'] and attempt['protocol_id'] == origin['protocol_id'],
            'raw/attempt identity mismatch')
    if batch['batch_id'] in origin['reuse']:
        require(sha256(path) == origin['reuse'][batch['batch_id']]['raw_sha256'] and
                sha256(attempt_path) == origin['reuse'][batch['batch_id']]['attempt_sha256'],
                'reused raw was changed')
    for row in raw['rows']:
        require(row['parse'] == parse_trace(row['raw_text'], row['finish_reason']), 'strict raw parse changed')
        require(row['problem_id'] in by_id, 'unknown raw question')
    return raw, sha256(path)


def worker(root: Path, slot: str, wave: int, stage: str, shard: int):
    require(stage in ('generate', 'score', 'evaluate') and 0 <= shard < 8, 'invalid worker')
    manifest, origin, config, by_id = load(root, slot)
    require(0 <= wave < len(origin['waves']), 'unknown wave')
    folder = root / slot
    if wave > 0:
        prior = read(folder / 'audits' / f'wave-{wave - 1:03d}.json')
        require(prior['status'] == 'PASS' and prior['expansion_id'] == manifest['expansion_id'],
                'prior wave must be audited before advancing')
        require(prior.get('coverage_gate') is True, 'prior wave coverage needs a user decision')
        verify(folder, prior['files'])
    # One common wave/shard lock prevents overlap between stage invocations.
    with exclusive_lock(folder / 'locks' / f'wave-{wave:03d}-{shard}.lock'):
        done = folder / 'workers' / f'{stage}-{wave:03d}-{shard}.json'
        require(not done.exists(), 'completed stage must not be resubmitted')
        selected = origin['waves'][wave][shard::8]
        backend, rows_done = None, 0
        grading, policy = None, None
        if stage == 'evaluate':
            require(config['backend'] == 'mock' or not os.environ.get('CUDA_VISIBLE_DEVICES'),
                    'code evaluation requires GPU-free allocation')
            source = Path(origin['source'])
            verify(source, {'inputs/grading.json': origin['source_hashes']['inputs/grading.json']})
            grading = read(source / 'inputs/grading.json')
            policy = read(config['evaluation_manifest']) if config['backend'] == 'hf' else None
            (folder / 'scratch/code-eval').mkdir(parents=True, exist_ok=True, mode=0o700)
        for batch in selected:
            raw_path = folder / 'generation_batches' / (batch['batch_id'] + '.json')
            if stage == 'generate' and not raw_path.exists():
                attempt = folder / 'attempts' / raw_path.name
                require(not attempt.exists(), 'UNCERTAIN_GENERATION: no automatic retry')
                if backend is None:
                    backend = _backend(config)
                save(attempt, {'batch_id': batch['batch_id'], 'protocol_id': origin['protocol_id'],
                               'expansion_id': manifest['expansion_id'], 'state': 'attempt_started',
                               'slurm_job_id': os.environ.get('SLURM_JOB_ID')})
                raw = backend.generate_batch([by_id[i] for i in batch['problem_ids']], batch['seed'])
                save(raw_path, {'batch': batch, 'protocol_id': origin['protocol_id'],
                                'scientific_evidence': origin['scientific_evidence'], **raw})
            raw, raw_hash = check_raw(root, slot, origin, batch, by_id)
            for row in raw['rows']:
                item = row['problem_id']
                parsed = parse_decoupled(row['raw_text'], row['finish_reason'], by_id[item]['benchmark'])
                identity = {'expansion_id': manifest['expansion_id'], 'source_protocol_id': origin['protocol_id'],
                            'problem_id': item, 'raw_sha256': raw_hash, 'parser_version': PARSER_VERSION}
                if stage == 'score':
                    out = folder / 'scores' / (digest(item) + '.json')
                    require(not out.exists(), 'score already exists; no automatic rerun')
                    steps = []
                    if parsed['process_valid']:
                        for index in range(1, len(parsed['steps'])):
                            if backend is None:
                                backend = _backend(config)
                            require(backend.base_prompt(by_id[item]) == row['prompt'], 'original prompt differs')
                            full, deleted, target = score_text_pair(row['prompt'], parsed, index)
                            steps.append({'index': index, 'full_context_text': full, 'deleted_context_text': deleted,
                                          **backend.score_pair(full, deleted, target)})
                    save(out, {**identity, 'status': 'ok' if parsed['process_valid'] else 'invalid_process',
                               'reason': parsed['reason'], 'steps': steps,
                               'summary': summarize_trace([s['score'] for s in steps]) if parsed['process_valid'] else None})
                elif stage == 'evaluate':
                    out = folder / 'outcomes' / (digest(item) + '.json')
                    require(not out.exists(), 'outcome already exists; no automatic rerun')
                    result = evaluate_answer(by_id[item], grading[item], parsed['answer'], code_policy=policy,
                                             scratch=folder / 'scratch/code-eval')
                    save(out, {**identity, 'answer': parsed['answer'], **result})
                    require(result['status'] != 'infrastructure_error', 'code isolation/infrastructure failure; stop')
                rows_done += 1
        result = {'expansion_id': manifest['expansion_id'], 'slot': slot, 'wave': wave,
                  'stage': stage, 'shard': shard, 'problems': rows_done,
                  'slurm_job_id': os.environ.get('SLURM_JOB_ID')}
        save(done, result)
        return result


def audit(root: Path, slot: str, wave: int):
    manifest, origin, config, by_id = load(root, slot)
    require(0 <= wave < len(origin['waves']), 'unknown wave')
    folder = root / slot
    out = folder / 'audits' / f'wave-{wave:03d}.json'
    require(not out.exists(), 'audit exists')
    selected = origin['waves'][wave]
    files, rows = {}, []
    for stage in ('generate', 'score', 'evaluate'):
        for shard in range(8):
            path = folder / 'workers' / f'{stage}-{wave:03d}-{shard}.json'
            status = read(path)
            expected = {'expansion_id': manifest['expansion_id'], 'slot': slot, 'wave': wave,
                        'stage': stage, 'shard': shard,
                        'problems': sum(len(b['problem_ids']) for b in selected[shard::8])}
            require(all(status[k] == v for k, v in expected.items()), 'worker coverage changed')
            files[str(path.relative_to(folder))] = sha256(path)
    encode = token_encoder(config)
    for batch in selected:
        raw, raw_hash = check_raw(root, slot, origin, batch, by_id)
        files['generation_batches/' + batch['batch_id'] + '.json'] = raw_hash
        files['attempts/' + batch['batch_id'] + '.json'] = sha256(folder / 'attempts' / (batch['batch_id'] + '.json'))
        for generated in raw['rows']:
            item = generated['problem_id']
            parsed = parse_decoupled(generated['raw_text'], generated['finish_reason'], by_id[item]['benchmark'])
            require(generated['stop_token_length'] == len(generated['generated_token_ids']),
                    'raw completion length differs')
            if config['backend'] == 'hf':
                require(generated['prompt_token_ids'] == encode(generated['prompt']) and
                        generated['prompt_width'] - len(generated['prompt_token_ids']) == generated['left_pad_tokens'],
                        'raw prompt token evidence differs')
            saved = read(folder / 'scores' / (digest(item) + '.json'))
            outcome = read(folder / 'outcomes' / (digest(item) + '.json'))
            for record in (saved, outcome):
                require(record['expansion_id'] == manifest['expansion_id'] and
                        record['source_protocol_id'] == origin['protocol_id'] and
                        record['problem_id'] == item and record['raw_sha256'] == raw_hash and
                        record['parser_version'] == PARSER_VERSION, 'item evidence identity changed')
            _score_matches(saved, parsed, generated['prompt'], encode)
            require(outcome['answer'] == parsed['answer'], 'graded answer changed')
            require(outcome['status'] != 'infrastructure_error', 'unsafe/incomplete evaluation')
            for name in ('scores', 'outcomes'):
                relative = name + '/' + digest(item) + '.json'
                files[relative] = sha256(folder / relative)
            rows.append({'problem_id': item, 'benchmark': by_id[item]['benchmark'],
                         'subset': by_id[item]['subset'], 'process_valid': parsed['process_valid'],
                         'process_reason': parsed['reason'], 'answer_extractable': parsed['answer']['valid'],
                         'finish_reason': generated['finish_reason'], 'summary': saved['summary'],
                         'step_count': len(parsed['steps']), 'generated_tokens': len(generated['generated_token_ids']),
                         'correct': outcome['correct'], 'outcome': outcome['status'],
                         'reused': batch['batch_id'] in origin['reuse']})
    result = {'status': 'PASS', 'expansion_id': manifest['expansion_id'], 'slot': slot, 'wave': wave,
              'scientific_evidence': origin['scientific_evidence'], 'items': rows, 'files': files,
              'counts': {'planned': len(rows), 'process_valid': sum(r['process_valid'] for r in rows),
                         'G_defined': sum(r['summary'] is not None and r['summary']['G'] is not None for r in rows),
                         'W_defined': sum(r['summary'] is not None and r['summary']['W'] is not None for r in rows)}}
    grouped = defaultdict(list)
    for row in rows:
        grouped[row['benchmark']].append(row)
    result['coverage_by_benchmark'] = {name: {'planned': len(group),
        'process_valid': sum(r['process_valid'] for r in group),
        'length_ended': sum(r['finish_reason'] == 'length' for r in group)} for name, group in grouped.items()}
    # This is a continuation signal, not a scientific significance test.
    # Keep all rows even when the engineering coverage signal is false.
    result['coverage_gate'] = (sum(r['process_valid'] for r in rows) >= .9 * len(rows) and
        all(sum(r['process_valid'] for r in group) >= .75 * len(group) for group in grouped.values()))
    save(out, result)
    return {k: v for k, v in result.items() if k not in ('items', 'files')}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('prepare', 'generate', 'score', 'evaluate', 'audit'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--source-root', type=Path)
    parser.add_argument('--slot', choices=SLOTS)
    parser.add_argument('--wave', type=int)
    parser.add_argument('--shard', type=int)
    parser.add_argument('--mmlu-count', type=int, default=100)
    args = parser.parse_args(argv)
    os.umask(0o077)
    if args.stage == 'prepare':
        require(args.source_root is not None, 'prepare source required')
        result = prepare(args.source_root, args.root, args.mmlu_count)
    else:
        require(args.slot is not None and args.wave is not None, 'slot/wave required')
        result = audit(args.root, args.slot, args.wave) if args.stage == 'audit' else worker(
            args.root, args.slot, args.wave, args.stage, args.shard)
    import json
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
