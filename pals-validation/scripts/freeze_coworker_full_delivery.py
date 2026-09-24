#!/usr/bin/env python3
"""Freeze every delivered coworker DAG without modifying its content.

The original ZIP, its three embedded manifests, and audit-v4 are independent
inputs.  A graph with fewer than two scored steps is retained in the flow but
cannot enter the existing E1/E2 scorer.  No semantic flag is treated as an
automatic exclusion.  This is a full *delivered* cohort, not a claim about the
entire underlying benchmark or human-reviewed gold.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import zipfile


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pals_validation.data import normalize, validated_steps  # noqa: E402
from pals_validation.graph import e2_anchor  # noqa: E402


PACKAGES = {
    "gsm8k": "gsm8k-final-20260923.tar.gz",
    "mmlu_math": "mmlu-math-5-subsets-final-20260923.tar.gz",
    "mmlu_psych_social": "mmlu-psych-social-4-subsets-final-20260923.tar.gz",
}
EXPECTED_DELIVERED = {"gsm8k": 530, "mmlu_math": 415, "mmlu_psych_social": 594}
EXPECTED_SCOREABLE = {"gsm8k": 530, "mmlu_math": 415, "mmlu_psych_social": 586}
EXPECTED_E2_ANCHORS = {"gsm8k": 530, "mmlu_math": 413, "mmlu_psych_social": 520}
PROTOCOL = "coworker-pals-full-delivery-v1"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def jsonl(rows: list[dict]) -> bytes:
    return b"".join(json.dumps(row, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
                    for row in rows)


def parse_jsonl(data: bytes, label: str) -> list[dict]:
    lines = data.splitlines()
    if not lines or any(not line.strip() for line in lines):
        raise ValueError(f"Blank or empty JSONL: {label}")
    rows = [json.loads(line) for line in lines]
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"Non-object JSONL row: {label}")
    return rows


def member(tar: tarfile.TarFile, name: str) -> bytes:
    matches = [entry for entry in tar.getmembers() if entry.name == name]
    if len(matches) != 1 or not matches[0].isfile():
        raise ValueError(f"Missing, duplicate or non-file tar member: {name}")
    stream = tar.extractfile(matches[0])
    if stream is None:
        raise ValueError(f"Cannot open tar member: {name}")
    return stream.read()


def load_package(zf: zipfile.ZipFile, key: str, tar_name: str,
                 audited: dict) -> list[dict]:
    if zf.namelist().count(tar_name) != 1:
        raise ValueError(f"Missing or duplicate ZIP member: {tar_name}")
    tar_bytes = zf.read(tar_name)
    if sha(tar_bytes) != audited["tar_sha256"]:
        raise ValueError(f"Embedded archive changed: {key}")
    root = tar_name.removesuffix(".tar.gz")
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        manifest_raw = member(tar, f"{root}/manifest.json")
        unified = member(tar, f"{root}/pals_dag_unified_v1.jsonl")
        accepted = member(tar, f"{root}/accepted_source.jsonl")
        html = member(tar, f"{root}/pals_dag_unified_v1.html")
    inner = json.loads(manifest_raw)
    hashes = {"unified_sha256": sha(unified), "source_sha256": sha(accepted),
              "html_sha256": sha(html)}
    if inner != audited["manifest"] or any(
            inner.get(name) != digest or audited[name] != digest
            for name, digest in hashes.items()):
        raise ValueError(f"Embedded manifest or source content changed: {key}")
    if inner.get("schema_version") != "pals_dag_unified_v1":
        raise ValueError(f"Wrong embedded schema: {key}")
    rows = parse_jsonl(unified, key)
    if len(rows) != inner["records"]:
        raise ValueError(f"Embedded record count changed: {key}")
    return rows


def verified_audit(audit_dir: Path) -> tuple[dict, bytes, list[dict]]:
    raw = (audit_dir / "manifest.json").read_bytes()
    audit = json.loads(raw)
    if (audit.get("protocol") != "coworker-dag-conservative-audit-v1"
            or audit.get("delivered_total") != 1539
            or audit.get("selection_seed") != 20260915):
        raise ValueError("Unexpected audit protocol, denominator, or seed")
    code_root = Path(__file__).resolve().parents[1] / "src/pals_validation"
    for name in ("data", "graph"):
        if sha((code_root / f"{name}.py").read_bytes()) != audit[f"validation_{name}_sha256"]:
            raise ValueError(f"Current {name} code differs from frozen audit")
    flow_raw = (audit_dir / "flow_1539.jsonl").read_bytes()
    if sha(flow_raw) != audit["outputs_sha256"]["flow_1539.jsonl"]:
        raise ValueError("Audit flow hash mismatch")
    flow = parse_jsonl(flow_raw, "audit flow")
    if len(flow) != 1539:
        raise ValueError("Audit flow denominator mismatch")
    return audit, raw, flow


def write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)


def freeze(source_zip: Path, audit_dir: Path, output_dir: Path) -> dict:
    audit, audit_raw, audit_flow = verified_audit(audit_dir)
    zip_raw = source_zip.read_bytes()
    if sha(zip_raw) != audit["source_zip_sha256"]:
        raise ValueError("Original ZIP hash mismatch")
    if set(audit["packages"]) != set(PACKAGES):
        raise ValueError("Unexpected package inventory")
    by_package = {}
    with zipfile.ZipFile(io.BytesIO(zip_raw)) as zf:
        for key, name in PACKAGES.items():
            by_package[key] = load_package(zf, key, name, audit["packages"][key])
    combined = [(key, row) for key, rows in by_package.items() for row in rows]
    if len(combined) != len(audit_flow):
        raise ValueError("ZIP and audit flow have different denominators")
    if any(len(by_package[key]) != EXPECTED_DELIVERED[key] for key in PACKAGES):
        raise ValueError("Delivered package denominator changed")

    seen_items, seen_sources = set(), set()
    outputs = {}
    flow = []
    package_counts = {}
    for key in PACKAGES:
        package_counts[key] = Counter(delivered=0, scoreable=0, e2_anchor=0,
                                      no_e2_anchor=0, single_step_not_applicable=0)
        outputs[f"scoreable-{key}.jsonl"] = []
    for (key, record), original in zip(combined, audit_flow):
        item = record.get("item_id")
        source_id = record.get("provenance", {}).get("source_id")
        if (item in seen_items or source_id in seen_sources or not item or not source_id):
            raise ValueError("Duplicate or absent item/source ID")
        seen_items.add(item)
        seen_sources.add(source_id)
        if (original["package"] != key or original["item_id"] != item
                or original["source_id"] != source_id
                or original["record_sha256"] != sha(canonical(record))
                or not original["structural_valid"] or original["errors"]):
            raise ValueError(f"ZIP record differs from audited flow: {item}")
        if record.get("schema_version") != "pals_dag_unified_v1":
            raise ValueError(f"Wrong record schema: {item}")
        nodes = record["dag"]["nodes"]
        steps = validated_steps(nodes, minimum=1)
        scoreable = len(steps) >= 2
        if scoreable != original["protocol_minimum_steps"]:
            raise ValueError(f"Scoreability changed since audit: {item}")
        benchmark = "gsm8k" if key == "gsm8k" else "mmlu"
        if scoreable:
            case = normalize(record, benchmark)
            anchor = e2_anchor(case["steps"], item, audit["selection_seed"])
            anchored = anchor is not None
            if anchored != original["e2_anchor_mechanical"]:
                raise ValueError(f"E2 anchor changed since audit: {item}")
            outputs[f"scoreable-{key}.jsonl"].append(record)
            package_counts[key]["scoreable"] += 1
            package_counts[key]["e2_anchor" if anchored else "no_e2_anchor"] += 1
            e1_status = "scoreable"
            e2_status = "anchored" if anchored else "not_applicable_no_dependency_anchor"
        else:
            if original["e2_anchor_mechanical"]:
                raise ValueError(f"One-step graph cannot have E2 anchor: {item}")
            package_counts[key]["single_step_not_applicable"] += 1
            e1_status = e2_status = "not_applicable_single_scored_step"
        package_counts[key]["delivered"] += 1
        # Copy every audit-v4 field, including all adverse flags and formal_eligible=false.
        flow.append({**original, "full_delivery_scoreable": scoreable,
                     "e1_status": e1_status, "e2_status": e2_status})
    if any(package_counts[key]["scoreable"] != EXPECTED_SCOREABLE[key]
           or package_counts[key]["e2_anchor"] != EXPECTED_E2_ANCHORS[key]
           for key in PACKAGES):
        raise ValueError("Expected full-delivery scoreability/anchor counts changed")

    payloads = {name: jsonl(records) for name, records in outputs.items()}
    payloads["flow_1539.jsonl"] = jsonl(flow)
    files = {name: {"sha256": sha(data), "records": len(parse_jsonl(data, name)),
                    **({"benchmark": "gsm8k" if name == "scoreable-gsm8k.jsonl" else "mmlu"}
                       if name.startswith("scoreable-") else {})}
             for name, data in payloads.items()}
    result = {
        "protocol": PROTOCOL,
        "scope": "all 1539 delivered model-accepted DAGs; scoreable records unchanged; not human gold",
        "compatible_experiments": ["e1", "e2"],
        "selection_seed": audit["selection_seed"],
        "source_zip_sha256": audit["source_zip_sha256"],
        "audit_manifest_sha256": sha(audit_raw),
        "audit_flow_sha256": audit["outputs_sha256"]["flow_1539.jsonl"],
        "source_delivered": len(flow),
        "scoreable_total": sum(v["scoreable"] for v in package_counts.values()),
        "e2_anchor_total": sum(v["e2_anchor"] for v in package_counts.values()),
        "per_package": {key: dict(package_counts[key]) for key in PACKAGES},
        "files": files,
        "limitations": [
            "Audit flags are preserved but do not silently exclude full-delivery rows.",
            "Eight one-step graphs cannot be scored by the current two-step-minimum PALS protocol.",
            "Graphs without a dependency anchor can have E1 scoring but E2 is undefined.",
            "Original benchmark-to-delivery sampling flow was not provided.",
        ],
    }
    if not output_dir.parent.is_dir():
        raise ValueError("Create private output parent before publishing")
    output_dir.mkdir(mode=0o700, exist_ok=False)
    for name, data in payloads.items():
        write_private(output_dir / name, data)
    write_private(output_dir / "manifest.json", canonical(result))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", type=Path, required=True)
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(freeze(args.zip, args.audit_dir, args.output),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
