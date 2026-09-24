"""Export local accepted GSM8K/MMLU DAGs as pals_dag_unified_v1."""

import hashlib
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path

from .storage import digest, private_dir, read_json, write_bytes_once, write_once
from .unified_viewer import render


VERSION = "pals_dag_unified_v1"
GRAPH_VERSION = "pals_step_dag_v1"
SOURCE_VERSION = "dag_builder_model_accepted_v1"
NODE_FIELDS = (
    "node_id",
    "kind",
    "statement",
    "parents",
    "source_field",
    "source_quote",
    "justification",
)
ROW_FIELDS = {
    "schema_version",
    "item_id",
    "benchmark",
    "problem",
    "answer",
    "dag",
    "review",
    "provenance",
}
KINDS = {"given", "knowledge", "derived", "answer"}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _jsonl(rows):
    return "".join(
        json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
        for row in rows
    ).encode("utf-8")


def accepted_records(roots):
    """Freeze model-accepted local records before adapting their public fields."""
    records = []
    seen = set()
    for root in map(Path, roots):
        items = read_json(root / "items.json")
        for item in items:
            item_id = item["item_id"]
            directory = root / "items" / item_id
            result_path = directory / "result.json"
            dag_path = directory / "dag.json"
            if not result_path.exists():
                continue
            result = read_json(result_path)
            if result.get("status") != "model_accepted":
                continue
            _require(dag_path.exists(), f"accepted item missing DAG: {item_id}")
            dag = read_json(dag_path)
            _require(
                result.get("dag_sha256") == digest(dag),
                f"accepted DAG hash mismatch: {item_id}",
            )
            _require(
                dag.get("item_id") == item_id and dag.get("source") == item,
                f"accepted DAG/source mismatch: {item_id}",
            )
            _require(item_id not in seen, f"duplicate accepted item: {item_id}")
            seen.add(item_id)
            records.append(
                {
                    "schema_version": SOURCE_VERSION,
                    "item_id": item_id,
                    "status": result["status"],
                    "result": result,
                    "dag": dag,
                }
            )
    records.sort(key=lambda record: record["item_id"])
    _require(bool(records), "no model-accepted records found")
    return records


def convert_record(record, benchmark, source_file_sha256):
    """Convert one frozen local accepted record without modifying its evidence."""
    _require(benchmark in ("gsm8k", "mmlu"), "unsupported benchmark")
    _require(_sha256(source_file_sha256), "source file SHA256 required")
    _require(
        isinstance(record, dict)
        and record.get("schema_version") == SOURCE_VERSION
        and record.get("status") == "model_accepted",
        "only model-accepted local source records can be exported",
    )
    dag = record["dag"]
    source = dag["source"]
    item_id = record["item_id"]
    _require(source.get("item_id") == item_id, "source item ID mismatch")
    _require(digest(dag) == record["result"].get("dag_sha256"), "DAG hash mismatch")
    task_type = source.get("task_type", "mmlu")
    _require(task_type == benchmark, "benchmark/source task mismatch")

    if benchmark == "mmlu":
        choices = source.get("choices")
        answer_value = source.get("gold_answer")
        _require(
            isinstance(choices, list)
            and len(choices) == 4
            and all(_text(choice) for choice in choices),
            "invalid MMLU choices",
        )
        _require(answer_value in "ABCD", "invalid MMLU answer")
        subset = source["subset"]
        problem = {
            "question": source["question"],
            "domain": subset,
            "choices": choices,
            "entry_point": None,
        }
        answer = {"kind": "choice", "value": answer_value}
    else:
        subset = source["config"]
        answer_value = source.get("gold_answer")
        _require(_text(answer_value), "invalid GSM8K answer")
        problem = {
            "question": source["question"],
            "domain": "grade_school_math",
            "choices": None,
            "entry_point": None,
        }
        answer = {"kind": "text", "value": answer_value}

    nodes = deepcopy(dag.get("nodes"))
    _require(isinstance(nodes, list) and len(nodes) >= 2, "incomplete DAG")
    canonical_nodes = [{key: node[key] for key in NODE_FIELDS} for node in nodes]
    source_id = f"{source['dataset']}:{subset}:{source['split']}:{source['row']}"
    row = {
        "schema_version": VERSION,
        "item_id": item_id,
        "benchmark": benchmark,
        "problem": problem,
        "answer": answer,
        "dag": {
            "schema_version": GRAPH_VERSION,
            "nodes": canonical_nodes,
            "nodes_sha256": digest(canonical_nodes),
        },
        "review": {
            "source_status": record["status"],
            "model_accepted": True,
            "human_approved": False,
            "quality_status": dag.get("quality_status"),
            "construction_protocol": dag.get("construction_protocol"),
        },
        "provenance": {
            "dataset": source["dataset"],
            "subset": subset,
            "split": source["split"],
            "revision": source["revision"],
            "source_row": source["row"],
            "source_id": source_id,
            "source_content_sha256": source["source_content_sha256"],
            "source_schema_version": SOURCE_VERSION,
            "source_file_sha256": source_file_sha256,
            "source_record_sha256": digest(record),
            "source_dag_sha256": digest(dag),
        },
    }
    validate_row(row)
    return row


def validate_row(row):
    """Check stable shape plus graph and cross-field invariants."""
    _require(isinstance(row, dict) and set(row) == ROW_FIELDS, "row fields mismatch")
    _require(row["schema_version"] == VERSION and _text(row["item_id"]), "version/ID mismatch")
    _require(row["benchmark"] in ("gsm8k", "mmlu"), "invalid benchmark")
    problem, answer, graph, review, provenance = (
        row[key] for key in ("problem", "answer", "dag", "review", "provenance")
    )
    _require(
        set(problem) == {"question", "domain", "choices", "entry_point"}
        and _text(problem["question"])
        and _text(problem["domain"]),
        "problem fields mismatch",
    )
    _require(
        set(answer) == {"kind", "value"}
        and answer["kind"] in ("choice", "code", "text")
        and _text(answer["value"]),
        "answer fields mismatch",
    )
    _require(
        problem["choices"] is None or isinstance(problem["choices"], list),
        "invalid choices",
    )
    if answer["kind"] == "choice":
        _require(
            isinstance(problem["choices"], list)
            and len(problem["choices"]) == 4
            and answer["value"] in "ABCD",
            "invalid choice answer",
        )
    _require(
        set(graph) == {"schema_version", "nodes", "nodes_sha256"}
        and graph["schema_version"] == GRAPH_VERSION,
        "DAG fields mismatch",
    )
    nodes = graph["nodes"]
    _require(
        isinstance(nodes, list)
        and len(nodes) >= 2
        and digest(nodes) == graph["nodes_sha256"],
        "DAG nodes/hash mismatch",
    )
    seen = set()
    for node in nodes:
        _require(
            isinstance(node, dict) and set(node) == set(NODE_FIELDS),
            "node fields mismatch",
        )
        node_id, parents = node["node_id"], node["parents"]
        _require(
            type(node_id) is int and node_id > 0 and node_id not in seen,
            "invalid or duplicate node ID",
        )
        _require(
            node["kind"] in KINDS
            and _text(node["statement"])
            and _text(node["source_field"])
            and _text(node["source_quote"])
            and _text(node["justification"]),
            "invalid node content",
        )
        _require(
            isinstance(parents, list)
            and all(type(parent) is int for parent in parents)
            and len(parents) == len(set(parents))
            and set(parents) <= seen,
            "parents must refer to distinct earlier nodes",
        )
        seen.add(node_id)
    _require(
        nodes[-1]["kind"] == "answer"
        and sum(node["kind"] == "answer" for node in nodes) == 1,
        "exactly one terminal answer required",
    )
    _require(
        set(review)
        == {
            "source_status",
            "model_accepted",
            "human_approved",
            "quality_status",
            "construction_protocol",
        }
        and review["source_status"] == "model_accepted"
        and review["model_accepted"] is True
        and type(review["human_approved"]) is bool
        and all(
            value is None or _text(value)
            for value in (review["quality_status"], review["construction_protocol"])
        ),
        "review fields mismatch",
    )
    _require(
        set(provenance)
        == {
            "dataset",
            "subset",
            "split",
            "revision",
            "source_row",
            "source_id",
            "source_content_sha256",
            "source_schema_version",
            "source_file_sha256",
            "source_record_sha256",
            "source_dag_sha256",
        }
        and type(provenance["source_row"]) is int
        and provenance["source_row"] >= 0
        and all(
            _text(provenance[key])
            for key in (
                "dataset",
                "subset",
                "split",
                "revision",
                "source_id",
                "source_schema_version",
            )
        )
        and all(
            _sha256(provenance[key])
            for key in (
                "source_content_sha256",
                "source_file_sha256",
                "source_record_sha256",
                "source_dag_sha256",
            )
        ),
        "provenance fields mismatch",
    )


def export_roots(roots, benchmark, output_dir):
    """Freeze accepted records and write an immutable unified delivery."""
    records = accepted_records(roots)
    source_bytes = _jsonl(records)
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    rows = [convert_record(record, benchmark, source_hash) for record in records]
    _require(len({row["item_id"] for row in rows}) == len(rows), "duplicate item ID")
    encoded = _jsonl(rows)
    html = render(rows, benchmark)
    subsets = Counter(row["provenance"]["subset"] for row in rows)
    manifest = {
        "schema_version": VERSION,
        "benchmark": benchmark,
        "records": len(rows),
        "records_by_subset": dict(sorted(subsets.items())),
        "source_schema_version": SOURCE_VERSION,
        "source_sha256": source_hash,
        "unified_sha256": hashlib.sha256(encoded).hexdigest(),
        "html_schema_version": "pals_dag_unified_view_v1",
        "html_sha256": hashlib.sha256(html).hexdigest(),
        "human_approved_records": 0,
        "scope": "model-accepted synthetic reference DAGs; not human-approved gold",
    }
    destination = Path(output_dir)
    _require(not destination.exists(), "output directory already exists")
    private_dir(destination)
    write_bytes_once(destination / "accepted_source.jsonl", source_bytes)
    write_bytes_once(destination / "pals_dag_unified_v1.jsonl", encoded)
    write_bytes_once(destination / "pals_dag_unified_v1.html", html)
    write_once(destination / "manifest.json", manifest)
    return manifest
