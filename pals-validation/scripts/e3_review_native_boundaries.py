"""Local CPU layout exploration. Sources JSON names folders, never model APIs."""
import argparse
from collections import Counter
from pathlib import Path

from pals_validation.e3.native_boundary_review import inspect, POLICY, VERSION
from pals_validation.io import read, save, sha256, digest
from pals_validation.e3.schema import require


def review(folder: Path, scope: str) -> dict:
    manifest=read(folder.parent/'manifest.json')
    require(manifest['protocol_id']==digest({k:v for k,v in manifest.items() if k!='protocol_id'}),'manifest identity')
    batches_path=folder/'batches.json'
    require(sha256(batches_path)==manifest['files'][f'{folder.name}/batches.json'],'schedule hash')
    batches=read(batches_path)
    if scope=='fixed40':
        require(manifest['initial_batches']==5,'initial schedule')
        batches=batches[:5]
        require(len(batches)==5 and all(len(b['problem_ids'])==8 for b in batches),'fixed40 count')
        require(len({b['benchmark'] for b in batches})==5,'fixed40 benchmarks')
    items=[];sources={str(folder.parent/'manifest.json'):sha256(folder.parent/'manifest.json'),
                     str(batches_path):sha256(batches_path)}
    for batch in batches:
        path=folder/'raw'/(batch['batch_id']+'.json');raw=read(path)
        receipt_path=folder/'receipts'/path.name
        require(read(receipt_path)=={'protocol_id':manifest['protocol_id'],'sha256':sha256(path)},'raw receipt')
        require(raw['protocol_id']==manifest['protocol_id'] and raw['batch']==batch,'raw identity')
        require(raw['output']['seed']==batch['seed'] and raw['output']['batch_size']==len(batch['problem_ids']), 'raw batch metadata')
        require([r['problem_id'] for r in raw['output']['rows']]==batch['problem_ids'],'raw order')
        sources[str(path)]=sha256(path);sources[str(receipt_path)]=sha256(receipt_path)
        for row in raw['output']['rows']:
            checked=inspect(row['raw_text'],row['finish_reason'],batch['benchmark'])
            items.append({'problem_id':row['problem_id'],'benchmark':batch['benchmark'],
                'raw_file':str(path),'raw_sha256':sha256(path),'finish_reason':row['finish_reason'],
                'raw_text':row['raw_text'],**checked})
    require(items and len(items)==len({x['problem_id'] for x in items}),'repeated/empty cohort')
    def count(rows):
        return {'total':len(rows),**{key:sum(x[key] for x in rows) for key in (
            'v2_valid','v2_scoreable','v3_valid','v3_scoreable','native_candidates',
            'candidate_with_existing_process_boundary','candidate_without_code_mixture')},
            'unscorable_reasons':dict(Counter(x['v3_reason'] or 'single_step' for x in rows if not x['v3_scoreable']))}
    return {'scope':scope,'source_protocol_id':manifest['protocol_id'],'summary':count(items),
        'by_benchmark':{b:count([x for x in items if x['benchmark']==b]) for b in sorted({x['benchmark'] for x in items})},
        'items':items,'source_hashes':sources}


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--sources',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--scope',choices=('fixed40','all'),default='fixed40')
    args=ap.parse_args();require(not args.output.exists(),'output already exists')
    sources=read(args.sources);require(len({s['slot'] for s in sources})==len(sources),'duplicate slot')
    reports={s['slot']:review(Path(s['folder']),args.scope) for s in sources}
    if args.scope=='fixed40':
        ids=[{r['problem_id'] for r in x['items']} for x in reports.values()]
        require(all(x==ids[0] for x in ids),'models did not use common questions')
    from pals_validation.e3 import native_boundary_review,greedy_parse,greedy_parse_v3
    result={'version':VERSION,'policy':POLICY,'policy_sha256':digest(POLICY),
        'sources_config_sha256':sha256(args.sources),'formal_eligible':False,
        'warning':'Exploratory on existing development outputs. No new probabilities; candidates are not accepted steps.',
        'implementation_hashes':{Path(m.__file__).name:sha256(m.__file__) for m in (
            native_boundary_review,greedy_parse,greedy_parse_v3)},
        'review_script_sha256':sha256(__file__),'models':reports}
    save(args.output,result)
    for name,r in reports.items():print(name,r['summary'])


if __name__=='__main__':main()
