"""Explicit outcome-fix continuation, preserving raw bytes and score payloads."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import shutil

from ..io import digest, read, save, sha256, verify
from . import code_harness, code_tests
from .expanded import KERNELS, ROOT, SLOTS, audit, implementation_hashes
from .schema import require


def verify_imports(root, manifest):
    c = manifest['continuation']
    # Local copies are sufficient during GPU jobs; no grading source is opened.
    verify(root, c['import_files'])
    for dest, spec in c['imported_records'].items():
        old = read(root / spec['evidence'])
        actual = read(root / dest)
        expected = {**old, 'expansion_id': manifest['expansion_id'],
                    'imported_from': {'evidence': spec['evidence'], 'sha256': spec['sha256']}}
        require(actual == expected, 'imported record payload changed')
    for slot, policy in c['evaluation_policies'].items():
        if policy is not None:
            require(policy['harness_sha256'] == sha256(code_harness.__file__) and
                    policy['decoder_sha256'] == sha256(code_tests.__file__), 'continuation policy differs')


def prepare(parent: Path, recovery: Path, output: Path):
    require(not output.exists() and not output.is_symlink(), 'continuation already exists')
    require(parent.resolve(strict=True) == parent and recovery.resolve(strict=True) == recovery and
            output.parent.resolve(strict=True) == output.parent, 'noncanonical paths')
    require(parent not in output.parents and recovery not in output.parents, 'sources must remain read-only')
    old = read(parent / 'manifest.json')
    rm, ra = read(recovery / 'manifest.json'), read(recovery / 'audit.json')
    require(old['expansion_id'] == digest({k: v for k, v in old.items() if k != 'expansion_id'}), 'parent changed')
    require(rm['recovery_id'] == digest({k: v for k, v in rm.items() if k != 'recovery_id'}), 'recovery changed')
    require(rm['source'] == str(parent) and rm['expansion_id'] == old['expansion_id'] and
            rm['slot'] == 'phi4mini' and rm['wave'] == 0 and ra['recovery_id'] == rm['recovery_id'] and
            ra['expansion_id'] == old['expansion_id'] and ra['status'] == 'PASS' and ra['coverage_gate'] is True,
            'recovery audit not accepted')
    verify(parent, rm['source_files'])
    verify(recovery, ra['files'])
    current = implementation_hashes()
    # The patched harness is the only changed scientific kernel.
    require(all(old['implementation_hashes'][k] == current[k] for k in (*KERNELS, 'e3/decoupled.py') if k != 'e3/code_harness.py'),
            'generation/scoring/parser kernel changed')
    require(rm['implementation_hashes']['e3/code_harness.py'] == current['e3/code_harness.py'],
            'harness differs from accepted recovery')
    selftest = Path(rm['selftest'])
    require(sha256(selftest) == rm['selftest_sha256'], 'selftest changed')
    st = read(selftest)
    require(st['status'] == 'PASS' and st['harness_sha256'] == current['e3/code_harness.py'] and
            st['decoder_sha256'] == current['e3/code_tests.py'] and st['isolation_probes_passed'] is True and
            st['cpu_timeout_control']['reason'] == 'per_test_cpu_limit', 'selftest not accepted')
    files, imported, policies = {}, {}, {}
    def include(dest, source):
        require(not source.is_symlink(), 'import symlink')
        files[dest] = {'path': str(source), 'sha256': sha256(source)}
    include('imports/parent-manifest.json', parent / 'manifest.json')
    include('imports/recovery-manifest.json', recovery / 'manifest.json')
    include('imports/recovery-audit.json', recovery / 'audit.json')
    include('imports/selftest.json', selftest)
    for slot, origin in old['sources'].items():
        verify(Path(origin['source']), origin['source_hashes'])
        config = origin['config']
        if config['backend'] == 'hf':
            require(os.environ.get('SLURM_JOB_ID') and not os.environ.get('CUDA_VISIBLE_DEVICES') and
                    ROOT.resolve(strict=True) in output.parents, 'prepare needs GPU-free PALS allocation')
            protocol = read(Path(origin['source']) / 'protocol.json')
            require(sha256(config['evaluation_manifest']) == protocol['evaluation_manifest_sha256'], 'old policy changed')
            policy = read(config['evaluation_manifest'])
            policies[slot] = {**policy, 'harness_sha256': current['e3/code_harness.py']}
        else:
            policies[slot] = None
        expected_raw = {b['batch_id'] + '.json' for b in origin['waves'][0]} if slot == 'phi4mini' else set()
        expected_items = {digest(i) + '.json' for b in origin['waves'][0] for i in b['problem_ids']} if slot == 'phi4mini' else set()
        for kind in ('generation_batches', 'attempts', 'scores'):
            expected = expected_items if kind == 'scores' else expected_raw
            require({p.name for p in (parent / slot / kind).glob('*.json')} == expected,
                    'unexpected partial or extra work; no regeneration')
        for kind in ('generation_batches', 'attempts'):
            for name in sorted(expected_raw):
                include(f'{slot}/{kind}/{name}', parent / slot / kind / name)
        if slot == 'phi4mini':
            require({p.name for p in (recovery / 'outcomes').glob('*.json')} == expected_items, 'recovery coverage differs')
            for kind, base in (('scores', parent / slot), ('outcomes', recovery)):
                for name in sorted(expected_items):
                    evidence = f'imports/{kind}/{name}'
                    include(evidence, base / kind / name)
                    imported[f'{slot}/{kind}/{name}'] = {'evidence': evidence, 'sha256': files[evidence]['sha256']}
    m = copy.deepcopy(old)
    m['implementation_hashes'] = current
    m['continuation'] = dict(parent_expansion_id=old['expansion_id'], recovery_id=rm['recovery_id'],
                            import_files={k: v['sha256'] for k, v in files.items()}, imported_records=imported,
                            evaluation_policies=policies, completed_waves={'phi4mini': [0]})
    m['expansion_id'] = digest({k: v for k, v in m.items() if k != 'expansion_id'})
    output.mkdir(mode=0o700)
    save(output / 'manifest.json', m)
    for slot in SLOTS:
        for name in ('generation_batches','attempts','scores','outcomes','workers','audits','locks','logs','scratch'):
            (output / slot / name).mkdir(parents=True, mode=0o700)
    for dest, spec in files.items():
        path = output / dest
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with Path(spec['path']).open('rb') as src, path.open('xb') as dst:
            shutil.copyfileobj(src, dst)
        require(sha256(path) == spec['sha256'], 'copy hash differs')
    for dest, spec in imported.items():
        original = read(output / spec['evidence'])
        save(output / dest, {**original, 'expansion_id': m['expansion_id'],
                            'imported_from': {'evidence': spec['evidence'], 'sha256': spec['sha256']}})
    # Import receipts, explicitly not claims of executing new workers/jobs.
    for stage in ('generate','score','evaluate'):
        for shard in range(8):
            save(output / 'phi4mini/workers' / f'{stage}-000-{shard}.json',
                 dict(expansion_id=m['expansion_id'], slot='phi4mini', wave=0, stage=stage, shard=shard,
                      problems=sum(len(b['problem_ids']) for b in m['sources']['phi4mini']['waves'][0][shard::8]),
                      imported=True, evidence='imports/recovery-audit.json', recovery_id=rm['recovery_id']))
    result = audit(output, 'phi4mini', 0)
    require(result['coverage_gate'], 'import coverage failed')
    save(output / 'continuation-ready.json', dict(status='PASS', expansion_id=m['expansion_id'],
         imported_questions=len(ra['items']),
         parent_expansion_id=old['expansion_id'], recovery_id=rm['recovery_id']))
    return dict(status='PASS', expansion_id=m['expansion_id'], counts=m['counts'], imported_audit=result)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--parent', type=Path, required=True)
    p.add_argument('--recovery', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    os.umask(0o077)
    print(json.dumps(prepare(a.parent, a.recovery, a.output), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
