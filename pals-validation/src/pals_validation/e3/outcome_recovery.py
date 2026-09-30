"""One approved outcome-only recovery overlay; original expansion is read-only.

No generation or model forward pass is reachable here. Finished outcomes are
copied exactly; only missing/infrastructure-error outcomes are evaluated once.
This overlay does not authorize continuing the original frozen expansion.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import shutil

from ..io import digest, read, save, sha256, verify
from .audit import _score_matches, token_encoder
from . import code_harness
from .decoupled import PARSER_VERSION, parse_decoupled
from .evaluate import evaluate_answer
from .expanded import ROOT, check_raw, implementation_hashes
from .schema import require

CHANGED = {'e3/code_harness.py', 'scripts/e3_harness_selftest.py'}
ADDED = {'e3/outcome_recovery.py'}


def gate(source, output, slot, wave, selftest):
    require(source.resolve(strict=True) == source and output.parent.resolve(strict=True) == output.parent,
            'noncanonical recovery paths')
    require(source != output and source not in output.parents and output not in source.parents,
            'recovery must be a separate sibling run')
    original = read(source / 'manifest.json')
    require(original['expansion_id'] == digest({k: v for k, v in original.items() if k != 'expansion_id'}),
            'source manifest changed')
    before, now = original['implementation_hashes'], implementation_hashes()
    require(set(now) - set(before) == ADDED and not set(before) - set(now), 'unexpected code file set')
    require({k for k in before if before[k] != now[k]} <= CHANGED, 'unapproved kernel change')
    origin = original['sources'][slot]
    require(0 <= wave < len(origin['waves']), 'unknown wave')
    source_run = Path(origin['source'])
    verify(source_run, origin['source_hashes'])
    config = origin['config']
    test = read(selftest)
    require(test['status'] == 'PASS' and test['harness_sha256'] == sha256(code_harness.__file__)
            and test['cpu_timeout_control']['reason'] == 'per_test_cpu_limit'
            and test['isolation_probes_passed'] is True, 'repaired harness selftest not passed')
    policy = None
    if config['backend'] == 'hf':
        require(os.environ.get('SLURM_JOB_ID') and not os.environ.get('CUDA_VISIBLE_DEVICES')
                and ROOT.resolve(strict=True) in output.parents, 'GPU-free PALS CPU allocation required')
        protocol = read(source_run / 'protocol.json')
        require(sha256(config['evaluation_manifest']) == protocol['evaluation_manifest_sha256'],
                'original evaluation policy changed')
        policy = read(config['evaluation_manifest'])
        require(policy['harness_sha256'] == before['e3/code_harness.py'], 'old harness identity differs')
        policy = {**policy, 'harness_sha256': sha256(code_harness.__file__)}
        reference = read(source_run / 'reference.json')
        require(reference['status'] == 'PASS' and reference['protocol_id'] == origin['protocol_id'],
                'numerical gate identity differs')
    by_id = {p['problem_id']: p for p in read(source_run / 'inputs/problems.json')}
    return original, origin, config, policy, by_id


def prepare(source: Path, output: Path, slot: str, wave: int, selftest: Path):
    require(not output.exists(), 'recovery already exists; no implicit retry')
    original, origin, config, policy, by_id = gate(source, output, slot, wave, selftest)
    folder, files, items = source / slot, {'manifest.json': sha256(source / 'manifest.json')}, []
    batches = origin['waves'][wave]
    for stage in ('generate', 'score'):
        for shard in range(8):
            rel = f'{slot}/workers/{stage}-{wave:03d}-{shard}.json'
            status = read(source / rel)
            expected = dict(expansion_id=original['expansion_id'], slot=slot, wave=wave,
                            stage=stage, shard=shard,
                            problems=sum(len(b['problem_ids']) for b in batches[shard::8]))
            require(all(status[k] == v for k, v in expected.items()), 'source worker incomplete')
            files[rel] = sha256(source / rel)
    for batch in batches:
        raw, raw_hash = check_raw(source, slot, origin, batch, by_id)
        for kind in ('generation_batches', 'attempts'):
            rel = f'{slot}/{kind}/{batch["batch_id"]}.json'
            files[rel] = sha256(source / rel)
        for row in raw['rows']:
            item = row['problem_id']
            rel = f'{slot}/scores/{digest(item)}.json'
            files[rel] = sha256(source / rel)
            old = folder / 'outcomes' / (digest(item) + '.json')
            action = 'evaluate'
            if old.exists():
                files[str(old.relative_to(source))] = sha256(old)
                action = 'evaluate' if read(old)['status'] == 'infrastructure_error' else 'reuse'
            items.append(dict(problem_id=item, action=action, raw_sha256=raw_hash))
    manifest = dict(source=str(source), slot=slot, wave=wave, expansion_id=original['expansion_id'],
                    implementation_hashes=implementation_hashes(), selftest=str(selftest),
                    selftest_sha256=sha256(selftest), source_files=files, policy=policy, items=items)
    manifest['recovery_id'] = digest(manifest)
    output.mkdir(mode=0o700)
    for name in ('outcomes', 'attempts', 'scratch'):
        (output / name).mkdir(mode=0o700)
    save(output / 'manifest.json', manifest)
    return manifest


def validate(output):
    m = read(output / 'manifest.json')
    require(m['recovery_id'] == digest({k: v for k, v in m.items() if k != 'recovery_id'}), 'recovery changed')
    require(m['implementation_hashes'] == implementation_hashes(), 'recovery code changed')
    require(sha256(m['selftest']) == m['selftest_sha256'], 'selftest changed')
    verify(Path(m['source']), m['source_files'])
    details = gate(Path(m['source']), output, m['slot'], m['wave'], Path(m['selftest']))
    require(m['policy'] == details[3], 'recovery policy changed')
    return m, details


def evaluate(output):
    m, (_, origin, _, policy, by_id) = validate(output)
    require(not (output / 'evaluated.json').exists(), 'recovery evaluation already completed')
    source, slot = Path(m['source']), m['slot']
    grading = read(Path(origin['source']) / 'inputs/grading.json')
    rows = {}
    for batch in origin['waves'][m['wave']]:
        raw, _ = check_raw(source, slot, origin, batch, by_id)
        rows.update((r['problem_id'], r) for r in raw['rows'])
    for spec in m['items']:
        item = spec['problem_id']
        dest = output / 'outcomes' / (digest(item) + '.json')
        require(not dest.exists(), 'no implicit outcome retry')
        if spec['action'] == 'reuse':
            with (source / slot / 'outcomes' / dest.name).open('rb') as src, dest.open('xb') as dst:
                shutil.copyfileobj(src, dst)
        else:
            save(output / 'attempts' / dest.name, dict(problem_id=item, recovery_id=m['recovery_id']))
            parsed = parse_decoupled(rows[item]['raw_text'], rows[item]['finish_reason'], by_id[item]['benchmark'])
            result = evaluate_answer(by_id[item], grading[item], parsed['answer'], code_policy=policy,
                                     scratch=output / 'scratch')
            save(dest, dict(expansion_id=m['expansion_id'], source_protocol_id=origin['protocol_id'],
                            problem_id=item, raw_sha256=spec['raw_sha256'], parser_version=PARSER_VERSION,
                            answer=parsed['answer'], recovery_id=m['recovery_id'], **result))
            require(result['status'] != 'infrastructure_error', 'recovery infrastructure failure; stop')
    save(output / 'evaluated.json', dict(status='PASS', actions=dict(Counter(s['action'] for s in m['items'])),
                                       recovery_id=m['recovery_id']))


def audit(output):
    m, (_, origin, config, _, by_id) = validate(output)
    require(read(output / 'evaluated.json')['recovery_id'] == m['recovery_id'], 'evaluation incomplete')
    source, slot = Path(m['source']), m['slot']
    expected = {digest(s['problem_id']) + '.json' for s in m['items']}
    require({p.name for p in (output / 'outcomes').glob('*.json')} == expected, 'outcome coverage mismatch')
    require({p.name for p in (output / 'attempts').glob('*.json')} ==
            {digest(s['problem_id']) + '.json' for s in m['items'] if s['action'] == 'evaluate'}, 'attempt mismatch')
    encode, rows, files = token_encoder(config), [], {}
    actions = {s['problem_id']: s for s in m['items']}
    for batch in origin['waves'][m['wave']]:
        raw, raw_hash = check_raw(source, slot, origin, batch, by_id)
        for generated in raw['rows']:
            item = generated['problem_id']
            parsed = parse_decoupled(generated['raw_text'], generated['finish_reason'], by_id[item]['benchmark'])
            require(generated['stop_token_length'] == len(generated['generated_token_ids']), 'completion length differs')
            if config['backend'] == 'hf':
                require(generated['prompt_token_ids'] == encode(generated['prompt']) and
                        generated['prompt_width'] - len(generated['prompt_token_ids']) == generated['left_pad_tokens'],
                        'prompt token evidence differs')
            saved = read(source / slot / 'scores' / (digest(item) + '.json'))
            path = output / 'outcomes' / (digest(item) + '.json')
            outcome = read(path)
            for record in (saved, outcome):
                require(record['expansion_id'] == m['expansion_id'] and record['problem_id'] == item and
                        record['raw_sha256'] == raw_hash and record['source_protocol_id'] == origin['protocol_id'] and
                        record['parser_version'] == PARSER_VERSION, 'evidence identity differs')
            _score_matches(saved, parsed, generated['prompt'], encode)
            require(outcome['answer'] == parsed['answer'] and outcome['status'] != 'infrastructure_error',
                    'answer mismatch or unresolved infrastructure failure')
            if actions[item]['action'] == 'reuse':
                require(sha256(path) == sha256(source / slot / 'outcomes' / path.name), 'reused outcome changed')
            else:
                require(outcome['recovery_id'] == m['recovery_id'], 'recovery provenance missing')
            files[str(path.relative_to(output))] = sha256(path)
            rows.append(dict(problem_id=item, benchmark=by_id[item]['benchmark'],
                             process_valid=parsed['process_valid'], step_count=len(parsed['steps']),
                             finish_reason=generated['finish_reason'], summary=saved['summary'],
                             correct=outcome['correct'], outcome=outcome['status'], action=actions[item]['action']))
    groups = defaultdict(list)
    for r in rows:
        groups[r['benchmark']].append(r)
    coverage = {k: dict(planned=len(v), process_valid=sum(r['process_valid'] for r in v)) for k, v in groups.items()}
    result = dict(status='PASS', recovery_id=m['recovery_id'], expansion_id=m['expansion_id'],
                  source=str(source), slot=slot, wave=m['wave'], files=files, items=rows,
                  counts=dict(planned=len(rows), process_valid=sum(r['process_valid'] for r in rows),
                              G_defined=sum(r['summary'] is not None and r['summary']['G'] is not None for r in rows),
                              W_defined=sum(r['summary'] is not None and r['summary']['W'] is not None for r in rows)),
                  coverage_by_benchmark=coverage,
                  coverage_gate=sum(r['process_valid'] for r in rows) >= .9 * len(rows) and
                      all(v['process_valid'] >= .75 * v['planned'] for v in coverage.values()))
    save(output / 'audit.json', result)
    return {k: v for k, v in result.items() if k not in ('files', 'items')}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--selftest', type=Path, required=True)
    p.add_argument('--slot', required=True)
    p.add_argument('--wave', type=int, required=True)
    a = p.parse_args()
    os.umask(0o077)
    prepare(a.source, a.output, a.slot, a.wave, a.selftest)
    evaluate(a.output)
    print(json.dumps(audit(a.output), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
