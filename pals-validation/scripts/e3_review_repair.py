"""Review an existing local model folder; produces a new private report only."""
import argparse
from pathlib import Path
from pals_validation.e3.repair_review import inventory, replay
from pals_validation.io import save, sha256


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--source',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--inventory',action='store_true')
    a=ap.parse_args()
    assert not a.output.exists()
    result={'replay':replay(a.source)}
    if a.inventory:result['inventory']=inventory(a.source)
    from pals_validation.e3 import greedy_parse,greedy_parse_v3,repair_review
    result['implementation_hashes']={Path(m.__file__).name:sha256(Path(m.__file__))
                                    for m in (greedy_parse,greedy_parse_v3,repair_review)}
    save(a.output,result)
    print(result['replay']['summary'])
    if a.inventory:print(result['inventory']['counts'])


if __name__=='__main__':main()
