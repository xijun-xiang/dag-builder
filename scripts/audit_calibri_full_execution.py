"""Rebuild full CPU input from the pinned raw dataset; never execute candidates.

The full-run input manifest is generated on the compute node, unlike the small
canary's local pre-submission manifest. Reconstruct its data identity locally
before using that returned manifest as evidence. Do not describe it as a local
pre-submission manifest.
"""
import argparse
from collections import Counter
from pathlib import Path

from dag_builder.livecodebench_dag import sha256, verify_execution
from dag_builder.livecodebench_source import REVISION, SOURCE_SHA256, normalize_livecodebench
from dag_builder.livecodebench_tests import decode_tests
from dag_builder.schemas import require, parse_object
from dag_builder.storage import digest, read_json, write_once


def expected_rows(items, original_items, bundles):
    by_id = {i['item_id']: i for i in original_items}
    rows = []
    for item in items:
        original = by_id[item['item_id']]
        require(all(item[k] == original[k] for k in ('question', 'question_id', 'io_type',
            'entry_point', 'starter_code', 'source_content_sha256', 'tests_sha256')),
            'candidate differs from pinned v6 source')
        bundle = bundles[item['item_id']]
        require(digest(bundle) == item['tests_sha256'], 'original tests changed')
        rows.append({'item_id': item['item_id'], 'io_type': item['io_type'],
            'entry_point': item['entry_point'], 'code': item['reference_code'],
            'code_sha256': digest(item['reference_code']),
            'source_content_sha256': item['source_content_sha256'], 'tests_sha256': item['tests_sha256'],
            'tests': decode_tests(bundle, item['io_type'])})
    return rows


def audit(execution, source, raw_source, frozen_package):
    require(sha256(raw_source) == SOURCE_SHA256, 'raw v6 source hash changed')
    with raw_source.open(encoding='utf-8') as stream:
        originals, bundles = normalize_livecodebench([parse_object(s) for s in stream if s.strip()], REVISION)
    candidates = read_json(source / 'items.json')
    origin_manifest = read_json(source / 'calibri-manifest.json')
    require(origin_manifest['protocol'] == 'calibri-lcb-source-full-v1'
            and len(originals) == 175 and digest(originals) == origin_manifest['full_cohort_sha256']
            and digest(candidates) == origin_manifest['items_sha256'], 'frozen source selection changed')
    rows = expected_rows(candidates, originals, bundles)
    returned = read_json(execution / 'input-manifest.json')
    require(read_json(frozen_package / 'candidates.json') == candidates
            and read_json(frozen_package / 'calibri-manifest.json') == origin_manifest,
            'deployed candidate package changed')
    origin = read_json(frozen_package / 'code/snapshot_origin.json')
    require(returned['implementation']['source_files'] == origin['source_files']
            and returned['implementation']['git_commit'] == origin['git_commit']
            and returned['harness_sha256'] == sha256(frozen_package / 'scripts/verify_livecodebench_reference.py')
            and returned['prepare_script_sha256'] == sha256(frozen_package / 'scripts/prepare_calibri_full_execution.py'),
            'execution implementation differs from frozen package')
    require(returned['source_sha256'] == SOURCE_SHA256
            and returned['calibri_manifest_sha256'] == digest(origin_manifest)
            and returned['inputs_sha256'] == digest(rows)
            and read_json(execution / 'execution-input.json') == rows,
            'returned input differs from independent reconstruction')
    require(returned['selected'] == len(originals)
            and {r['item_id'] for r in returned['not_executed']}
            == {i['item_id'] for i in originals} - {i['item_id'] for i in candidates},
            'original cohort denominator changed')
    results, completion = verify_execution(execution, execution / 'input-manifest.json')
    by_id = {i['item_id']: i for i in candidates}
    failures = []
    for item_id, (_, result) in results.items():
        if result['status'] != 'passed':
            failed = [t for t in result['tests'] if t['status'] != 'passed']
            failures.append({'item_id': item_id, 'question_id': by_id[item_id]['question_id'],
                'counts': result['counts'],
                'diagnosis': ('SIGXCPU under fixed per-test CPU limit' if all(t.get('returncode') == -24 for t in failed)
                              else 'inspect retained per-test result; not necessarily wrong answer')})
    return {'protocol': 'calibri-full-execution-offline-audit-v1', 'mechanical_pass': True,
        'input_manifest_origin': 'compute-generated; independently rebuilt from pinned raw source and frozen candidate package',
        'job_id': completion['job_id'], 'hostname': completion['hostname'],
        'source_questions': len(originals), 'source_candidates': len(candidates),
        'reference_passed': completion['passed'], 'reference_not_passed': len(failures),
        'tests': sum(len(r['tests']) for r in rows),
        'test_statuses': dict(Counter(t['status'] for _, r in results.values() for t in r['tests'])),
        'failures': failures, 'input_manifest_sha256': sha256(execution / 'input-manifest.json'),
        'source_sha256': SOURCE_SHA256, 'dag_semantic_acceptance': False}


if __name__ == '__main__':
    import json
    import os
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('execution', 'source', 'raw-source', 'frozen-package'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.execution, args.source, args.raw_source, args.frozen_package)
    write_once(args.execution / 'offline-audit.json', report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
