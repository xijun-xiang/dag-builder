"""Submit the frozen cohort's GPU/CPU chains once, after a successful CPU init.

At most one 8-GPU job can be active: the next model depends on the preceding
model's CPU audit. All jobs are submitted held and explicitly verified before release.
Failed or uncertain submission/release never triggers an automatic retry.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess

from pals_validation.e3.greedy import model_slots, load
from pals_validation.e3.deployment import profile
from pals_validation.e3.schema import require
from pals_validation.io import read, save, sha256
from pals_validation.locking import exclusive_lock

def completed(job):
    raw = subprocess.check_output(["sacct", "-j", job, "--format=JobID,State,ExitCode", "-Pn"], text=True)
    expected = {job, job + ".batch", job + ".0"}
    rows = {parts[0]: parts[1:] for line in raw.splitlines() if (parts := line.split("|"))[0] in expected}
    require(set(rows) == expected and all(r == ["COMPLETED", "0:0"] for r in rows.values()), "CPU init not fully successful")


def exclusions(values):
    values = tuple(sorted(set(values)))
    require(all(re.fullmatch(r"[a-z0-9][a-z0-9-]*", v) for v in values), "invalid excluded node")
    return values


def command_for(root, repo, slot, stage, previous, excluded):
    cluster = profile()
    name = f"e3g-{slot}-{stage}"
    command = ["sbatch", "--parsable", "--hold", "--partition=defq", "--nodes=1", "--ntasks=1",
        "--no-requeue", "--job-name=" + name,
        "--output=" + str(root / "logs" / ("%j-" + name + ".out")),
        "--error=" + str(root / "logs" / ("%j-" + name + ".err"))]
    if excluded:
        command += ["--exclude=" + ",".join(excluded)]
    if cluster.reservation:
        command += ["--reservation=" + cluster.reservation]
    if stage == "gpu":
        command += ["--gpus-per-node=8", "--cpus-per-task=32", "--mem=256G", "--time=24:00:00"]
    else:
        require(stage == "evaluate", "unknown stage")
        command += ["--gres=none", "--cpus-per-task=16", "--mem=64G", "--time=04:00:00"]
    if previous:
        command += ["--dependency=afterok:" + previous, "--kill-on-invalid-dep=yes"]
    return command + [str(repo / "scripts" / cluster.launcher)]


def submission_environment(repo, root, prepared, stage, slot):
    # Avoid inherited scheduler options silently changing the submitted request.
    env = {k: v for k, v in os.environ.items() if not k.startswith("SBATCH_")}
    return {**env, "PALS_REPO": str(repo), "PALS_JOB_ROOT": str(root),
            "PALS_PREPARED": str(prepared), "PALS_STAGE": stage, "PALS_SLOT": slot,
            "PALS_CLUSTER": profile().name}


def preflight(root, repo, prepared, init_job):
    cluster = profile()
    fence = Path(cluster.root)
    for path in (fence, root, repo, prepared):
        require(path.resolve(strict=True) == path and (path == fence or fence in path.parents), "outside PALS/symlink")
    require(init_job.isdigit(), "bad init job")
    completed(init_job)
    require(not subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"], text=True).strip(), "dirty frozen code")
    run = root / "experiment"
    slots = model_slots(read(run / "manifest.json"))
    manifest, _, _, _ = load(run, next(iter(slots)))
    for slot in slots:
        load(run, slot)
    require(read(root / "preflight/selftest.json")["status"] == "PASS", "safety preflight missing")
    receipt = read(root / "preflight/init.json")
    require(receipt == {"status": "PASS", "job_id": init_job, "run": str(run),
        "protocol_id": manifest["protocol_id"], "selftest_sha256": sha256(root / "preflight/selftest.json"),
        "context_checks_sha256": sha256(root / "preflight/context-checks.json")}, "init provenance differs")
    return manifest


def submit(root: Path, repo: Path, prepared: Path, init_job: str, exclude_nodes=()):
    manifest = preflight(root, repo, prepared, init_job)
    excluded = exclusions(exclude_nodes)
    ledger = root / "submissions"
    ledger.mkdir(mode=0o700, exist_ok=True)
    with exclusive_lock(ledger / "submit.lock"):
        require(not list(ledger.glob("*-attempt.json")), "submission already attempted; inspect, never retry blindly")
        active = subprocess.check_output(["squeue", "-h", "-u", "xijun", "-o", "%j"], text=True).splitlines()
        require(not any(n.startswith(("e3g-", "e3c-", "e3x-")) for n in active), "another E3 job is active")
        save(ledger / "scheduler-options.json", {"version": "explicit-held-v1", "excluded_nodes": list(excluded)})
        previous, jobs = None, []
        for slot in model_slots(manifest):
            for stage in ("gpu", "evaluate"):
                name = f"e3g-{slot}-{stage}"
                command = command_for(root, repo, slot, stage, previous, excluded)
                env = submission_environment(repo, root, prepared, stage, slot)
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


def scheduler_fields(job):
    require(job.isdigit(), "bad scheduler job ID")
    raw = subprocess.check_output(["scontrol", "show", "job", job, "-o"], text=True).strip()
    require(len(raw.splitlines()) == 1, "ambiguous scheduler response")
    fields = dict(part.split("=", 1) for part in raw.split() if "=" in part)
    require(fields.get("JobId") == job, "scheduler job differs")
    return fields


def verify_held(record, root, repo, previous, excluded):
    job, slot, stage = record["job_id"], record["slot"], record["stage"]
    require(record["command"] == command_for(root, repo, slot, stage, previous, excluded), "submission command changed")
    f = scheduler_fields(job)
    expected = {"JobName": f"e3g-{slot}-{stage}", "JobState": "PENDING", "Priority": "0",
        "ExcNodeList": ",".join(excluded) if excluded else "(null)", "Partition": "defq",
        "WorkDir": str(root), "Command": str(repo / "scripts" / profile().launcher),
        "NumTasks": "1", "Requeue": "0", "Restarts": "0",
        "StdOut": str(root / "logs" / f"{job}-e3g-{slot}-{stage}.out"),
        "StdErr": str(root / "logs" / f"{job}-e3g-{slot}-{stage}.err")}
    if profile().reservation:
        expected["Reservation"] = profile().reservation
    for key, value in expected.items():
        require(f.get(key) == value, f"scheduler {key} differs: {f.get(key)!r}")
    # Slurm 23.02 prints the exact one-node bound as "1-1" while held.
    require(f.get("NumNodes") in ("1", "1-1"), "scheduler node count differs")
    require(f.get("UserId", "").startswith("xijun("), "wrong job owner")
    cpus, memory, wall = (32, "256G", ("1-00:00:00", "24:00:00")) if stage == "gpu" else (16, "64G", ("04:00:00",))
    require(f.get("NumCPUs") == str(cpus) and f.get("CPUs/Task") == str(cpus), "CPU allocation differs")
    require(f.get("TimeLimit") in wall, "time limit differs")
    tres = dict(part.split("=", 1) for part in f.get("ReqTRES", "").split(",") if "=" in part)
    require(tres.get("cpu") == str(cpus) and tres.get("mem") == memory and tres.get("node") == "1", "requested resources differ")
    gpu_tres = {k: v for k, v in tres.items() if k.startswith("gres/gpu")}
    require(gpu_tres == ({"gres/gpu": "8"} if stage == "gpu" else {}), "GPU allocation differs")
    dependency = f.get("Dependency")
    if previous:
        require(dependency in ("afterok:" + previous, "afterok:" + previous + "(unfulfilled)"), "serial dependency differs")
        require(f.get("KillOInInvalidDependent") == "Yes", "invalid-dependency protection differs")
    else:
        require(dependency == "(null)", "unexpected first dependency")
    return f


def verify_chain(root, repo, prepared, init_job):
    manifest = preflight(root, repo, prepared, init_job)
    ledger = root / "submissions"
    options = read(ledger / "scheduler-options.json")
    excluded = exclusions(options["excluded_nodes"])
    require(options == {"version": "explicit-held-v1", "excluded_nodes": list(excluded)}, "scheduler options changed")
    jobs = read(ledger / "full-chain.json")
    slots = model_slots(manifest)
    require([(j["slot"], j["stage"]) for j in jobs] == [(s, t) for s in slots for t in ("gpu", "evaluate")], "incomplete job chain")
    ids = {j["job_id"] for j in jobs}
    require(len(ids) == 2 * len(slots), "duplicate job IDs")
    snapshots, previous = {}, None
    for record in jobs:
        require(record["protocol_id"] == manifest["protocol_id"], "job protocol differs")
        name = f"e3g-{record['slot']}-{record['stage']}"
        require(record == read(ledger / (name + "-submitted.json")), "submitted receipt changed")
        snapshots[record["job_id"]] = verify_held(record, root, repo, previous, excluded)
        previous = record["job_id"]
    active = subprocess.check_output(["squeue", "-h", "-u", "xijun", "-o", "%i %j"], text=True).splitlines()
    for line in active:
        job, name = line.split(maxsplit=1)
        require(not name.startswith(("e3g-", "e3c-", "e3x-")) or job in ids, "another E3 job is active")
    return {"status": "PASS", "protocol_id": manifest["protocol_id"], "jobs": jobs, "scheduler": snapshots,
            "controller_sha256": sha256(Path(__file__)), "runtime_repo": str(repo)}


def release(root, repo, prepared, init_job):
    ledger = root / "submissions"
    require(ledger.resolve(strict=True) == ledger and root in ledger.parents, "unsafe ledger")
    with exclusive_lock(ledger / "submit.lock"):
        require(not list(ledger.glob("release-*-attempt.json")), "release already attempted; inspect, never retry blindly")
        audit = verify_chain(root, repo, prepared, init_job)
        save(ledger / "held-audit.json", audit)
        # Release downstream first. The only dependency-free GPU job is released
        # last, so a partial release cannot start experimental computation.
        for record in reversed(audit["jobs"]):
            job = record["job_id"]
            save(ledger / f"release-{job}-attempt.json", {"job_id": job, "command": ["scontrol", "release", job]})
            subprocess.run(["scontrol", "release", job], check=True)
            save(ledger / f"release-{job}-complete.json", {"job_id": job, "status": "RELEASE_COMMAND_SUCCEEDED"})
        save(ledger / "released.json", {"status": "RELEASED", "job_ids": [j["job_id"] for j in audit["jobs"]]})
        return read(ledger / "released.json")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--prepared", type=Path, required=True)
    p.add_argument("--init-job", required=True)
    p.add_argument("--action", choices=("submit", "verify", "release"), default="submit")
    p.add_argument("--exclude-node", action="append", default=[])
    p.add_argument("--runtime-repo", type=Path, help="Verify/release an existing frozen runtime without modifying it")
    args = p.parse_args()
    os.umask(0o077)
    cluster = profile()
    os.environ["SLURM_CONF"] = cluster.slurm_conf
    os.environ["PATH"] = cluster.slurm_bin + ":" + os.environ["PATH"]
    import json
    repo = args.runtime_repo or Path(__file__).resolve().parents[1]
    if args.runtime_repo:
        require(args.action in ("verify", "release"), "runtime override cannot submit new jobs")
        require(Path(load.__globals__["__file__"]).resolve() == repo / "src/pals_validation/e3/greedy.py",
                "PYTHONPATH does not match the frozen runtime")
    if args.action == "submit":
        result = submit(args.root, repo, args.prepared, args.init_job, args.exclude_node)
    else:
        require(not args.exclude_node, "verification reads frozen scheduler-options, not new CLI overrides")
        result = (verify_chain if args.action == "verify" else release)(args.root, repo, args.prepared, args.init_job)
    print(json.dumps(result), flush=True)
