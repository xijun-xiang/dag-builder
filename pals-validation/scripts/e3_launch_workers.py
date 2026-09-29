"""Launch eight fixed E3 shards inside one audited Slurm container allocation.

For GPU stages, the enclosing allocation owns eight visible devices. Each child
sees exactly one of them as cuda:0. No dynamic resharding or retry is allowed.
"""

import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    if not os.environ.get("SLURM_JOB_ID"):
        raise RuntimeError("E3 workers require a Slurm job")
    stage = os.environ["PALS_STAGE"]
    if stage not in ("generate", "score", "evaluate", "common-score"):
        raise ValueError("Unrecognized E3 stage")
    run = Path(os.environ["PALS_RUN"]).resolve(strict=True)
    allowed = Path("/work/projects/polyullm/xxj/PALS").resolve(strict=True)
    if allowed not in run.parents:
        raise ValueError("E3 run lies outside the approved PALS project")
    visible = os.environ.get("CUDA_VISIBLE_DEVICES", "")
    devices = [part.strip() for part in visible.split(",") if part.strip()]
    if stage != "evaluate" and len(devices) != 8:
        raise RuntimeError("GPU stages require exactly eight allocated visible GPUs")
    if stage == "evaluate" and devices:
        raise RuntimeError("CPU evaluation must not own GPUs")
    label = stage + ("-canary" if os.environ.get("PALS_CANARY") == "1" else "")
    logs = run / "logs"
    logs.mkdir(mode=0o700, exist_ok=True)
    processes = []
    handles = []
    try:
        for shard in range(8):
            env = dict(os.environ)
            env["PALS_SHARD"] = str(shard)
            if devices:
                env["CUDA_VISIBLE_DEVICES"] = devices[shard]
            path = logs / f"{os.environ['SLURM_JOB_ID']}-{label}-{shard}.log"
            stream = path.open("x", encoding="utf-8")
            handles.append(stream)
            processes.append(subprocess.Popen(
                [sys.executable, str(Path(__file__).with_name("e3_slurm_worker.py"))],
                env=env, cwd=os.environ["PALS_REPO"], stdout=stream,
                stderr=subprocess.STDOUT))
        codes = [process.wait() for process in processes]
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait()
        for stream in handles:
            stream.close()
    if any(code != 0 for code in codes):
        raise RuntimeError(f"E3 {label} shards failed: {codes}; inspect per-shard logs")


if __name__ == "__main__":
    main()
