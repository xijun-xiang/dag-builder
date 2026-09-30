"""Eight fixed score-only shards, then token-evidence audit. Never generate code."""

import os
from pathlib import Path
import subprocess
import sys


def main():
    if not os.environ.get("SLURM_JOB_ID"):
        raise RuntimeError("Derived scoring requires Slurm")
    devices = os.environ.get("CUDA_VISIBLE_DEVICES", "").split(",")
    if len(devices) != 8 or any(not device.strip() for device in devices):
        raise RuntimeError("Exactly eight allocated GPUs required")
    run = Path(os.environ["PALS_RUN"]).resolve(strict=True)
    source = Path(os.environ["PALS_SOURCE_RUN"]).resolve(strict=True)
    replay = Path(os.environ["PALS_REPLAY"]).resolve(strict=True)
    allowed = Path("/work/projects/polyullm/xxj/PALS").resolve(strict=True)
    if any(allowed not in path.parents for path in (run, source, replay)):
        raise ValueError("Path outside PALS")
    label = os.environ["PALS_RUN_LABEL"]
    common = ["--replay", str(replay), "--source-run", str(source),
              "--run-label", label, "--out", str(run / "scores")]
    prefix = [sys.executable, "-m", "pals_validation.e3.derived_score"]
    processes, streams = [], []
    try:
        for shard, device in enumerate(devices):
            env = {**os.environ, "CUDA_VISIBLE_DEVICES": device}
            stream = (run / "logs" / f"{os.environ['SLURM_JOB_ID']}-score-{shard}.log").open("x")
            streams.append(stream)
            processes.append(subprocess.Popen(prefix + ["score", *common, "--shard", str(shard)],
                                               env=env, stdout=stream, stderr=subprocess.STDOUT))
        codes = [p.wait() for p in processes]
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait()
        for stream in streams:
            stream.close()
    if any(code != 0 for code in codes):
        raise RuntimeError(f"Derived scoring failure: {codes}; no retry")
    subprocess.run(prefix + ["audit", *common], check=True)


if __name__ == "__main__":
    main()
