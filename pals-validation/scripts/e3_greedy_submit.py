"""Submit the three full GPU/CPU chains once, after a successful CPU init.

At most one 8-GPU job can be active: the next model depends on the preceding
model's CPU audit. Failed or uncertain submission never triggers an automatic retry.
"""
import argparse
import os
from pathlib import Path
import subprocess

from pals_validation.e3.greedy import SLOTS, load
from pals_validation.e3.deployment import profile
from pals_validation.e3.schema import require
from pals_validation.io import read, save, sha256
from pals_validation.locking import exclusive_lock

def completed(job):
    raw = subprocess.check_output(["sacct", "-j", job, "--format=JobID,State,ExitCode", "-Pn"], text=True)
    expected = {job, job + ".batch", job + ".0"}
    rows = {parts[0]: parts[1:] for line in raw.splitlines() if (parts := line.split("|"))[0] in expected}
    require(set(rows) == expected and all(r == ["COMPLETED", "0:0"] for r in rows.values()), "CPU init not fully successful")


def submit(root: Path, repo: Path, prepared: Path, init_job: str):
    cluster = profile()
    fence = Path(cluster.root)
    for path in (fence, root, repo, prepared):
        require(path.resolve(strict=True) == path and (path == fence or fence in path.parents), "outside PALS/symlink")
    require(init_job.isdigit(), "bad init job")
    completed(init_job)
    require(not subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"], text=True).strip(), "dirty frozen code")
    run = root / "experiment"
    manifest, _, _, _ = load(run, "qwen25")
    require(read(root / "preflight/selftest.json")["status"] == "PASS", "safety preflight missing")
    receipt = read(root / "preflight/init.json")
    require(receipt == {"status": "PASS", "job_id": init_job, "run": str(run),
        "protocol_id": manifest["protocol_id"], "selftest_sha256": sha256(root / "preflight/selftest.json"),
        "context_checks_sha256": sha256(root / "preflight/context-checks.json")}, "init provenance differs")
    ledger = root / "submissions"
    ledger.mkdir(mode=0o700, exist_ok=True)
    with exclusive_lock(ledger / "submit.lock"):
        require(not list(ledger.glob("*-attempt.json")), "submission already attempted; inspect, never retry blindly")
        active = subprocess.check_output(["squeue", "-h", "-u", "xijun", "-o", "%j"], text=True).splitlines()
        require(not any(n.startswith(("e3g-", "e3c-", "e3x-")) for n in active), "another E3 job is active")
        previous, jobs = None, []
        for slot in SLOTS:
            for stage in ("gpu", "evaluate"):
                name = f"e3g-{slot}-{stage}"
                command = ["sbatch", "--parsable", "--job-name=" + name,
                    "--output=" + str(root / "logs" / ("%j-" + name + ".out")),
                    "--error=" + str(root / "logs" / ("%j-" + name + ".err"))]
                if cluster.reservation:
                    command += ["--reservation=" + cluster.reservation]
                if stage == "gpu":
                    command += ["--gpus-per-node=8", "--cpus-per-task=32", "--mem=256G", "--time=24:00:00"]
                else:
                    command += ["--gres=none", "--cpus-per-task=16", "--mem=64G", "--time=04:00:00"]
                if previous:
                    command += ["--dependency=afterok:" + previous, "--kill-on-invalid-dep=yes"]
                command.append(str(repo / "scripts" / cluster.launcher))
                env = {**os.environ, "PALS_REPO": str(repo), "PALS_JOB_ROOT": str(root),
                       "PALS_PREPARED": str(prepared), "PALS_STAGE": stage, "PALS_SLOT": slot,
                       "PALS_CLUSTER": cluster.name}
                save(ledger / (name + "-attempt.json"), {"command": command, "protocol_id": manifest["protocol_id"]})
                reply = subprocess.check_output(command, env=env, cwd=root, text=True).strip()
                previous = reply.split(";")[0]
                require(previous.isdigit(), "uncertain sbatch reply; stop and inspect")
                record = {"job_id": previous, "slot": slot, "stage": stage, "command": command,
                          "protocol_id": manifest["protocol_id"]}
                save(ledger / (name + "-submitted.json"), record)
                jobs.append(record)
        save(ledger / "full-chain.json", jobs)
        return jobs


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--prepared", type=Path, required=True)
    p.add_argument("--init-job", required=True)
    args = p.parse_args()
    os.umask(0o077)
    cluster = profile()
    os.environ["SLURM_CONF"] = cluster.slurm_conf
    os.environ["PATH"] = cluster.slurm_bin + ":" + os.environ["PATH"]
    import json
    print(json.dumps(submit(args.root, Path(__file__).resolve().parents[1], args.prepared, args.init_job)), flush=True)
