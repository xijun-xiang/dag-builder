"""Launch bounded E3 shards inside one allocation; no scheduler submission."""
import os
from pathlib import Path
import subprocess
import sys


def main():
    root, slot, stage = os.environ['PALS_EXPANSION'], os.environ['PALS_SLOT'], os.environ['PALS_STAGE']
    wave = os.environ['PALS_WAVE']
    if stage not in ('generate', 'score', 'evaluate'):
        raise ValueError('Unsupported expansion stage')
    devices = os.environ.get('CUDA_VISIBLE_DEVICES', '').split(',')
    if stage in ('generate', 'score') and (len(devices) != 8 or not all(devices)):
        raise RuntimeError('Exactly eight assigned GPUs required')
    if stage == 'evaluate' and os.environ.get('CUDA_VISIBLE_DEVICES'):
        raise RuntimeError('Evaluation must have no GPU')
    procs, handles = [], []
    try:
        for shard in range(8):
            log = Path(root) / slot / 'logs' / f'{os.environ["SLURM_JOB_ID"]}-{stage}-{wave}-{shard}.log'
            handle = log.open('x')
            handles.append(handle)
            env = dict(os.environ)
            if stage != 'evaluate':
                env['CUDA_VISIBLE_DEVICES'] = devices[shard]
            command = [sys.executable, '-m', 'pals_validation.e3.expanded', stage,
                       '--root', root, '--slot', slot, '--wave', wave, '--shard', str(shard)]
            procs.append(subprocess.Popen(command, env=env, stdout=handle, stderr=subprocess.STDOUT))
        failures = [i for i, proc in enumerate(procs) if proc.wait() != 0]
        if failures:
            raise RuntimeError('Failed expansion shards: ' + str(failures))
        if stage == 'evaluate':
            subprocess.run([sys.executable, '-m', 'pals_validation.e3.expanded', 'audit',
                            '--root', root, '--slot', slot, '--wave', wave], check=True)
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        for handle in handles:
            handle.close()


if __name__ == '__main__':
    main()
