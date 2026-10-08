"""Freeze and receive the two approved public snapshots. Never execute model code.

Prepare runs on the client; download/publish runs only in a bounded CPU Slurm job.
No credentials, upload, automatic retries, input data, or pretrained pickle weights.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import urlopen

ROOT = Path("/work/projects/polyullm/xxj/pals")
SOURCES = {
    "llama3": ("modelscope", "LLM-Research/Meta-Llama-3-8B-Instruct", "e9f7e7d3fa08b550ea228e38bb5501d35be92c0d"),
    "internlm3": ("huggingface", "internlm/internlm3-8b-instruct", "28c99415adaf61767bd1c619f4f99f308fdfd223"),
}
TARGETS = {"llama3": "meta-llama/Meta-Llama-3-8B-Instruct", "internlm3": "internlm/internlm3-8b-instruct"}


def save(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)


def read(path):
    return json.loads(path.read_text())


def hashes(path):
    size = path.stat().st_size
    sha, blob = hashlib.sha256(), hashlib.sha1(("blob " + str(size) + "\0").encode())
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            sha.update(block)
            blob.update(block)
    return {"size": size, "sha256": sha.hexdigest(), "git_blob_sha1": blob.hexdigest()}


def validate_plan(plan):
    assert plan["version"] == "pals-two-public-models-v1" and set(plan["models"]) == set(SOURCES)
    for slot, item in plan["models"].items():
        provider, repo, revision = SOURCES[slot]
        assert (item["provider"], item["repository"], item["revision"]) == SOURCES[slot]
        assert item["relative_target"] == TARGETS[slot]
        names = set(item["files"])
        assert {"config.json", "tokenizer_config.json", "generation_config.json", "model.safetensors.index.json"} <= names
        assert any(n.endswith(".safetensors") for n in names)
        for name, record in item["files"].items():
            assert Path(name).name == name and name not in (".", "..") and not name.startswith(".")
            assert record["size"] > 0 and not name.endswith((".pth", ".bin", ".pt", ".pkl"))
            assert record["url"] == file_url(provider, repo, revision, name)
            assert any(record.get(k) for k in ("sha256", "git_blob_sha1"))


def file_url(provider, repo, revision, name):
    if provider == "modelscope":
        return f"https://modelscope.cn/api/v1/models/{repo}/repo?" + urlencode({"Revision": revision, "FilePath": name})
    return f"https://huggingface.co/{repo}/resolve/{revision}/{quote(name)}"


def prepare(output):
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    models = {}
    for slot, (provider, repo, revision) in SOURCES.items():
        url = (f"https://modelscope.cn/api/v1/models/{repo}/repo/files?" + urlencode({"Revision": revision, "Recursive": "true"})
               if provider == "modelscope" else f"https://huggingface.co/api/models/{repo}/revision/{revision}?blobs=true")
        with urlopen(url, timeout=30) as response:
            metadata = json.load(response)
        save(output / (slot + "-source.json"), metadata)
        files = {}
        if provider == "modelscope":
            assert metadata["Success"] and metadata["Code"] == 200
            records = metadata["Data"]["Files"]
            for row in records:
                name = row["Path"]
                if row["Type"] != "blob" or "/" in name or name.startswith("."):
                    continue
                files[name] = {"size": row["Size"], "sha256": row["Sha256"],
                               "url": file_url(provider, repo, revision, name)}
        else:
            assert metadata["sha"] == revision and not metadata.get("gated")
            for row in metadata["siblings"]:
                name = row["rfilename"]
                if "/" in name or name.startswith("."):
                    continue
                record = {"size": row["size"], "url": file_url(provider, repo, revision, name)}
                if row.get("lfs"):
                    record["sha256"] = row["lfs"]["sha256"]
                else:
                    record["git_blob_sha1"] = row["blobId"]
                files[name] = record
        models[slot] = {"provider": provider, "repository": repo, "revision": revision,
                        "relative_target": TARGETS[slot], "metadata_sha256": hashes(output / (slot + "-source.json"))["sha256"],
                        "files": files}
    plan = {"version": "pals-two-public-models-v1", "models": models}
    validate_plan(plan)
    save(output / "plan.json", plan)
    print(json.dumps({s: {"files": len(m["files"]), "bytes": sum(f["size"] for f in m["files"].values())}
                      for s, m in models.items()}), flush=True)


def safe(path):
    assert ROOT.resolve(strict=True) == ROOT
    existing = path
    while not existing.exists():
        assert not existing.is_symlink()
        existing = existing.parent
    assert existing.resolve(strict=True) == existing and (existing == ROOT or ROOT in existing.parents)
    assert ROOT in path.parents


def transport(proxy):
    """Explicit per-job transport; do not inherit unrelated proxy settings."""
    env = {k: v for k, v in os.environ.items() if k.lower() not in
           ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
    options = []
    if proxy:
        parsed = urlsplit(proxy)
        assert parsed.scheme == "http" and parsed.hostname and parsed.port
        assert not parsed.username and not parsed.password and parsed.path in ("", "/")
        assert not parsed.query and not parsed.fragment
        options = ["--proxy", proxy, "--noproxy", ""]
    return options, env


def fetch(expected, path, log_path, proxy, timeout=7200):
    options, env = transport(proxy)
    assert not path.exists() and not path.is_symlink()
    with log_path.open("x") as log:
        subprocess.run(["curl", "--fail", "--silent", "--show-error", "--location", "--proto", "=https",
            "--proto-redir", "=https", "--connect-timeout", "15", "--max-time", str(timeout),
            "--speed-time", "120", "--speed-limit", "1024", "--retry", "0", *options,
            "--write-out", "http=%{http_code} bytes=%{size_download} speed=%{speed_download} seconds=%{time_total}\\n",
            "--output", str(path), expected["url"]], env=env, check=True, stdout=log, stderr=log)
    actual = hashes(path)
    assert all(actual[k] == v for k, v in expected.items() if k != "url"), "file digest/size mismatch: " + path.name
    return actual


def receive(run):
    assert os.environ.get("SLURM_JOB_ID") and not os.environ.get("CUDA_VISIBLE_DEVICES"), "CPU allocation required"
    safe(run)
    assert run.parent == ROOT / "runs" and run.resolve(strict=True) == run
    plan = read(run / "plan.json")
    validate_plan(plan)
    assert shutil.disk_usage(ROOT).free > 2 * sum(f["size"] for m in plan["models"].values() for f in m["files"].values()), "insufficient space"
    # An interrupted attempt requires inspection, not an implicit resume.
    proxy = os.environ.get("PALS_DOWNLOAD_PROXY", "")
    transport(proxy)
    save(run / "download-attempt.json", {"job_id": os.environ["SLURM_JOB_ID"],
        "plan_sha256": hashes(run / "plan.json")["sha256"], "proxy": proxy or None})
    probes = {}
    # Both providers must answer with the exact pinned small file before weights.
    for slot, item in plan["models"].items():
        expected = item["files"]["config.json"]
        assert expected["size"] < 65536
        probes[slot] = fetch(expected, run / (slot + "-network-probe.json"),
                            run / "logs" / (slot + "-network-probe.log"), proxy, timeout=60)
    save(run / "network-preflight.json", {"status": "PASS", "proxy": proxy or None, "files": probes})
    print("NETWORK_PREFLIGHT PASS: both pinned configs verified", flush=True)
    for slot, item in plan["models"].items():
        staging = ROOT / "transfer-staging" / (run.name + "-" + slot)
        target = ROOT / "models/hf" / item["relative_target"]
        safe(staging)
        safe(target)
        assert not staging.exists() and not staging.is_symlink() and not target.exists() and not target.is_symlink()
        staging.mkdir(mode=0o700)
        def download(entry):
            name, expected = entry
            path = staging / name
            print("DOWNLOADING " + slot + "/" + name, flush=True)
            actual = fetch(expected, path, run / "logs" / (slot + "-" + name + ".log"), proxy)
            print("VERIFIED " + slot + "/" + name, flush=True)
            return name, actual
        with ThreadPoolExecutor(max_workers=2) as pool:
            records = dict(pool.map(download, item["files"].items()))
        assert {p.name for p in staging.iterdir()} == set(item["files"])
        index = read(staging / "model.safetensors.index.json")
        assert set(index["weight_map"].values()) == {n for n in records if n.endswith(".safetensors")}
        safe(target.parent)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        safe(target)
        assert not target.exists()
        staging.rename(target)
        save(run / (slot + "-model-files.json"), {n: r["sha256"] for n, r in records.items()})
        save(run / (slot + "-downloaded.json"), {"status": "PASS", "path": str(target),
            "revision": item["revision"], "plan_sha256": hashes(run / "plan.json")["sha256"], "files": records})
        print("PUBLISHED " + slot, flush=True)
    save(run / "completion.json", {"status": "PASS", "job_id": os.environ["SLURM_JOB_ID"],
                                  "plan_sha256": hashes(run / "plan.json")["sha256"]})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare", "download"))
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    (prepare if args.stage == "prepare" else receive)(args.path)
