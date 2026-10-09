"""One-shot B1 CPU -> 8GPU score-only -> CPU audit chain. No generation."""
import argparse
import os
from pathlib import Path
import subprocess

from pals_validation.io import read, save, sha256, verify
from pals_validation.locking import exclusive_lock
from pals_validation.recovery.e2_eos import PROJECT, SOURCE_ROOT, SOURCES, require, VERSION

REPO = Path(__file__).resolve().parents[1]
ROOT = PROJECT / "runs/20261009-llama-e2-eos-recovery-v1"
LAUNCHER = REPO / "scripts/b1-e2-eos-recovery.sbatch"
STAGES = ("prepare", "launch", "audit")
EXCLUDE = "tko-b1-nv-dgx06"


def safe(path):
    require(path.is_absolute() and path.resolve() == path and PROJECT in path.parents, "unsafe project path")
    return path


def scheduler_rows(ids):
    text = subprocess.check_output(["sacct", "-n", "-P", "-j", ",".join(ids),
                                    "--format=JobIDRaw,State,ExitCode"], text=True)
    return {f[0]:f[1:3] for line in text.splitlines() if len(f:=line.split("|")) >= 3}


def successful(job):
    rows = scheduler_rows([job])
    require(all(rows.get(job+s)==["COMPLETED", "0:0"] for s in ("", ".batch", ".0")), "predecessor not scientifically complete")


def active_guard(allowed=()):
    text = subprocess.check_output(["squeue", "-h", "-u", "xijun", "-o", "%i %j"], text=True)
    for line in text.splitlines():
        job, name = line.split(maxsplit=1)
        require(not name.startswith(("pals-llama-", "pals-e2-eos-", "e3g-", "e3c-", "e3x-")) or job in allowed,
                "another B1 PALS chain active")


def prepare():
    safe(ROOT); safe(REPO)
    require(not ROOT.exists(), "existing/partial deployment; no repeat")
    active_guard()
    successful("115009")
    rows = scheduler_rows(["115010", "115011", "115012", "115013"])
    require(rows.get("115010") == ["FAILED", "1:0"], "expected old canary failure differs")
    require(all(rows.get(j, [""])[0].startswith("CANCELLED") for j in ("115011", "115012", "115013")), "old continuation active")
    require(all(p.is_dir() and p.resolve() == p for p in SOURCES.values()), "source paths")
    ROOT.mkdir(mode=0o700)
    for name in ("submissions", "logs"):
        (ROOT/name).mkdir(mode=0o700)
    for stage in STAGES:
        for sub in ("enroot-cache", "enroot-data", "enroot-runtime", "tmp", "hf", "hf-modules", "cache", "cuda"):
            (ROOT/"scratch"/stage/sub).mkdir(parents=True, mode=0o700)
    paths = [*sorted((REPO/"src").rglob("*.py")), LAUNCHER, Path(__file__).resolve()]
    save(ROOT/"deployment.json", {"version":VERSION, "repo":str(REPO), "sources":{k:str(v) for k,v in SOURCES.items()},
         "old_jobs":rows, "files":{str(p.relative_to(REPO)):sha256(p) for p in paths}})


def check():
    safe(ROOT); safe(REPO)
    d = read(ROOT/"deployment.json")
    require(d["version"]==VERSION and d["repo"]==str(REPO), "deployment identity")
    verify(REPO, d["files"])


def command(index, previous):
    stage = STAGES[index]; gpu = stage == "launch"
    name = "pals-e2-eos-"+stage
    cmd = ["sbatch", "--parsable", "--hold", "--no-requeue", "--partition=defq", "--reservation=code-agent",
           "--nodes=1", "--ntasks=1", "--cpus-per-task="+("32" if gpu else "8"),
           "--mem="+("256G" if gpu else "64G"), "--time=01:00:00", "--exclude="+EXCLUDE,
           "--job-name="+name, "--chdir="+str(ROOT), "--export=ALL",
           "--output="+str(ROOT/"logs"/("%j-"+name+".out")), "--error="+str(ROOT/"logs"/("%j-"+name+".err")),
           "--gpus-per-node=8" if gpu else "--gres=none"]
    if previous: cmd += ["--dependency=afterok:"+previous, "--kill-on-invalid-dep=yes"]
    return cmd + [str(LAUNCHER)]


def held(record, previous):
    i, job = record["index"], record["job_id"]
    require(record["command"]==command(i, previous) and record["stage"]==STAGES[i], "receipt changed")
    raw = subprocess.check_output(["scontrol", "show", "job", job, "-o"], text=True).strip()
    require(len(raw.splitlines())==1, "ambiguous state")
    fields = dict(s.split("=",1) for s in raw.split() if "=" in s)
    gpu = STAGES[i]=="launch"; name = "pals-e2-eos-"+STAGES[i]
    expected = {"JobId":job,"JobName":name,"JobState":"PENDING","Priority":"0","Partition":"defq",
        "Reservation":"code-agent","ExcNodeList":EXCLUDE,"NumTasks":"1","NumCPUs":"32" if gpu else "8",
        "CPUs/Task":"32" if gpu else "8","TimeLimit":"01:00:00","Requeue":"0","Restarts":"0",
        "Command":str(LAUNCHER),"WorkDir":str(ROOT),
        "StdOut":str(ROOT/"logs"/(job+"-"+name+".out")),"StdErr":str(ROOT/"logs"/(job+"-"+name+".err"))}
    require(all(fields.get(k)==v for k,v in expected.items()), "effective fields differ")
    require(fields.get("UserId","").startswith("xijun(") and fields.get("NumNodes") in ("1","1-1"), "owner/nodes")
    tres = dict(s.split("=",1) for s in fields.get("ReqTRES","").split(",") if "=" in s)
    require(tres.get("cpu")==expected["NumCPUs"] and tres.get("mem")==("256G" if gpu else "64G")
            and tres.get("node")=="1" and (tres.get("gres/gpu")=="8" if gpu else not any("gpu" in k and v!="0" for k,v in tres.items())), "resources")
    if previous:
        require(fields.get("Dependency") in ("afterok:"+previous,"afterok:"+previous+"(unfulfilled)") and
                fields.get("KillOInInvalidDependent")=="Yes", "dependency")
    else: require(fields.get("Dependency")=="(null)", "unexpected dependency")
    return fields


def submit():
    check(); ledger = ROOT/"submissions"
    with exclusive_lock(ledger/"submit.lock"):
        require(not list(ledger.glob("*attempt.json")), "existing/uncertain submission")
        active_guard(); previous = None; records = []
        for i,stage in enumerate(STAGES):
            cmd = command(i, previous)
            env = {k:v for k,v in os.environ.items() if not k.startswith(("PALS_","SBATCH_"))}
            env.update(PALS_REPO=str(REPO),PALS_RECOVERY_RUN=str(ROOT),PALS_RECOVERY_ACTION=stage)
            save(ledger/(str(i)+"-attempt.json"), {"command":cmd})
            job = subprocess.check_output(cmd,env=env,cwd=ROOT,text=True).strip().split(";")[0]
            require(job.isdigit(), "uncertain sbatch response")
            r = {"job_id":job,"stage":stage,"index":i,"command":cmd}
            save(ledger/(str(i)+"-submitted.json"), r); records.append(r); previous = job
        save(ledger/"chain.json", records)
    return records


def release():
    check(); ledger = ROOT/"submissions"
    with exclusive_lock(ledger/"submit.lock"):
        require(not list(ledger.glob("release-*-attempt.json")), "release already attempted")
        records = read(ledger/"chain.json")
        require([r["index"] for r in records]==list(range(3)) and len({r["job_id"] for r in records})==3, "chain receipt")
        active_guard({r["job_id"] for r in records})
        states, previous = {}, None
        for r in records:
            require(r==read(ledger/(str(r["index"])+"-submitted.json")), "saved receipt")
            states[r["job_id"]] = held(r,previous); previous=r["job_id"]
        save(ledger/"held-audit.json", {"status":"PASS","states":states})
        for r in reversed(records):
            job=r["job_id"]
            save(ledger/("release-"+job+"-attempt.json"), {"job_id":job})
            subprocess.run(["scontrol","release",job],check=True)
            save(ledger/("release-"+job+"-done.json"), {"job_id":job})
        save(ledger/"released.json", {"status":"RELEASED","jobs":[r["job_id"] for r in records]})


def admit(stage):
    check(); require(stage in STAGES, "stage")
    records=read(ROOT/"submissions/chain.json"); i=STAGES.index(stage)
    require(records[i]["job_id"]==os.environ.get("SLURM_JOB_ID"), "allocated job identity")
    if i: successful(records[i-1]["job_id"])
    if stage=="launch": require(read(ROOT/"recovery/ready.json")["status"]=="PASS", "prepare app")
    if stage=="audit": require(read(ROOT/"recovery/gpu-complete.json")["status"]=="PASS", "GPU app")
    save(ROOT/(stage+"-admitted.json"), {"job_id":records[i]["job_id"],"status":"PASS"})


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=("prepare","submit","release","admit")); parser.add_argument("--stage",choices=STAGES)
    args=parser.parse_args(); os.umask(0o077)
    os.environ["PATH"]="/cm/local/apps/slurm/current/bin:"+os.environ["PATH"]
    os.environ["SLURM_CONF"]="/cm/shared/apps/slurm/etc/slurm/slurm.conf"
    result=admit(args.stage) if args.action=="admit" else globals()[args.action]()
    print({"status":"PASS","action":args.action,"result":result},flush=True)
