"""Versioned, benchmark-neutral export of accepted step DAGs.

The source record remains immutable. This module standardizes the graph and
problem fields needed by downstream research, while recording a hash of the
complete original row for the remaining historical metadata.
"""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

from .storage import digest, private_dir, write_bytes_once, write_once
from .unified_viewer import render


VERSION = "pals_dag_unified_v1"
GRAPH_VERSION = "pals_step_dag_v1"
NODE_FIELDS = ("node_id", "kind", "statement", "parents", "source_field", "source_quote", "justification")
ROW_FIELDS = {"schema_version", "item_id", "benchmark", "problem", "answer", "dag", "review", "provenance"}
KINDS = {"given", "knowledge", "derived", "answer"}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _sha256(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _candidate_nodes(candidate):
    nodes = deepcopy(candidate["nodes"])
    ids = [node["node_id"] for node in nodes]
    _require(len(ids) == len(set(ids)), "duplicate candidate node ID")
    for key in ("parents", "justifications"):
        entries = candidate[key]
        entry_ids = [entry["node_id"] for entry in entries]
        _require(len(entry_ids) == len(set(entry_ids)) and set(entry_ids) == set(ids),
                 f"candidate {key} must join one-to-one by node_id")
    parents = {entry["node_id"]: entry["parents"] for entry in candidate["parents"]}
    justifications = {entry["node_id"]: entry["text"] for entry in candidate["justifications"]}
    for node in nodes:
        node["parents"] = parents[node["node_id"]]
        node["justification"] = justifications[node["node_id"]]
    return nodes


def convert_record(record, benchmark, source_file_sha256):
    """Convert one frozen, model-accepted record without modifying its evidence."""
    _require(benchmark in ("gpqa_diamond", "humaneval", "livecodebench_v6"), "unsupported benchmark")
    _require(_sha256(source_file_sha256), "source file SHA256 required")
    _require(isinstance(record, dict) and record.get("model_accepted") is True,
             "only model-accepted source records can be exported")
    source = record["source"]
    item_id = record["item_id"]
    _require(_text(item_id) and source.get("item_id") == item_id, "source item ID mismatch")
    source_dag = record.get("dag")

    if benchmark == "gpqa_diamond":
        _require(record.get("schema_version") == "gpqa_all_outcomes_v1"
                 and source.get("subset") == "gpqa_diamond", "GPQA source contract mismatch")
        _require(record.get("status") in ("model_accepted", "repaired_model_accepted",
                                            "model_accepted_diagnostic"), "unaccepted GPQA status")
        choices = source["choices"]
        _require(isinstance(choices, list) and len(choices) == 4
                 and all(_text(choice) for choice in choices), "invalid GPQA choices")
        answer = source["gold_answer"]
        _require(answer in ("A", "B", "C", "D"), "invalid GPQA answer")
        if source_dag is None:
            _require(record["status"] == "model_accepted_diagnostic", "missing GPQA DAG")
            nodes = _candidate_nodes(record["candidate"])
        else:
            _require(source_dag.get("source") == source and source_dag.get("item_id") == item_id,
                     "GPQA DAG/source mismatch")
            nodes = deepcopy(source_dag["nodes"])
        source_id = source["record_id"]
        problem = {"question": source["question"], "domain": source["domain"],
                   "choices": choices, "entry_point": None}
        answer = {"kind": "choice", "value": answer}
    elif benchmark == "humaneval":
        _require(record.get("schema_version") == "humaneval_validation_export_v1"
                 and record.get("status") == "model_accepted"
                 and source.get("subset") == "humaneval"
                 and source.get("task_type") == "humaneval", "HumanEval source contract mismatch")
        _require(isinstance(source_dag, dict) and source_dag.get("source") == source
                 and source_dag.get("item_id") == item_id, "HumanEval DAG/source mismatch")
        _require(digest(source_dag) == record.get("dag_sha256"), "HumanEval DAG hash mismatch")
        nodes = deepcopy(source_dag["nodes"])
        source_id = source["task_id"]
        problem = {"question": source["question"], "domain": source["domain"], "choices": None,
                   "entry_point": source["entry_point"]}
        answer = {"kind": "code", "value": source["canonical_solution"]}
        _require(nodes[-1]["statement"] == answer["value"], "HumanEval answer/code mismatch")
    else:
        source_status = record.get("source_status")
        schema_version = record.get("schema_version")
        _require((schema_version == "calibri-lcb-v6-model-candidates-v1"
                  and source_status == "calibri_derived_tested_reference")
                 or (schema_version in ("lcb-v6-source-stratified-candidates-v1",
                                        "lcb-v6-source-stratified-candidates-split-repair-v1")
                     and source_status in ("calibri_derived_tested_reference",
                                           "t2ance_derived_tested_reference")),
                 "LCB source protocol mismatch")
        _require(record.get("status", "model_accepted") == "model_accepted"
                 and record.get("human_approved") is False
                 and record.get("formal_eligible") is False
                 and source.get("subset") == "v6"
                 and source.get("task_type") == "livecodebench", "LCB source contract mismatch")
        if source_status == "t2ance_derived_tested_reference":
            _require(isinstance(source_dag, dict)
                     and source.get("reference_origin") == "t2ance_model_output"
                     and source_dag.get("construction_protocol") in
                     ("t2ance-lcb-normalize-v1", "t2ance-lcb-normalize-v2")
                     and source_dag.get("normalization", {}).get("protocol") ==
                     source_dag.get("construction_protocol"), "t2ance protocol/source mismatch")
        _require(isinstance(source_dag, dict) and source_dag.get("source") == source
                 and source_dag.get("item_id") == item_id
                 and source_dag.get("formal_eligible") is False
                 and digest(source_dag) == record.get("dag_sha256")
                 and _sha256(record.get("cpu_result_sha256")), "LCB DAG/CPU provenance mismatch")
        _require(source.get("execution_evidence", {}).get("status") == "passed"
                 and source["execution_evidence"].get("result_sha256") == record["cpu_result_sha256"]
                 and source.get("io_type") in ("stdin", "functional")
                 and ((source.get("entry_point") is None) if source["io_type"] == "stdin"
                      else _text(source.get("entry_point"))), "LCB execution or I/O contract mismatch")
        starter = source.get("starter_code")
        _require(isinstance(starter, str)
                 and (not starter.strip() if source["io_type"] == "stdin" else bool(starter.strip())),
                 "LCB starter code/I/O mismatch")
        nodes = deepcopy(source_dag["nodes"])
        source_id = source["question_id"]
        # The construction model saw starter_code as a separate public field;
        # the unified PALS question must preserve that visible context.
        visible_question = source["question"] + ("\n\nStarter code:\n" + starter if starter else "")
        problem = {"question": visible_question, "domain": source["domain"],
                   "choices": None, "entry_point": source["entry_point"]}
        # This is a CPU-tested source-derived reference program, not official gold.
        answer = {"kind": "code", "value": source["reference_code"]}
        _require(nodes[-1]["statement"] == answer["value"], "LCB answer/code mismatch")

    _require(_text(source_id) and _text(problem["question"]), "missing source ID or question")
    _require(_sha256(source.get("source_content_sha256")), "invalid source content hash")
    _require(isinstance(nodes, list) and len(nodes) >= 2, "incomplete DAG")
    canonical_nodes = [{key: node[key] for key in NODE_FIELDS} for node in nodes]
    row = {
        "schema_version": VERSION,
        "item_id": item_id,
        "benchmark": benchmark,
        "problem": problem,
        "answer": answer,
        "dag": {"schema_version": GRAPH_VERSION, "nodes": canonical_nodes,
                "nodes_sha256": digest(canonical_nodes)},
        "review": {"source_status": record["source_status"] if benchmark == "livecodebench_v6"
                   else record["status"], "model_accepted": True,
                   "human_approved": record.get("human_approved") is True,
                   "quality_status": source_dag.get("quality_status") if source_dag else None,
                   "construction_protocol": source_dag.get("construction_protocol") if source_dag else None},
        "provenance": {"dataset": source["dataset"], "subset": source["subset"],
                       "split": source["split"], "revision": source["revision"],
                       "source_row": source["row"],
                       "source_id": source_id,
                       "source_content_sha256": source["source_content_sha256"],
                       "source_schema_version": record["schema_version"],
                       "source_file_sha256": source_file_sha256,
                       "source_record_sha256": digest(record),
                       "source_dag_sha256": digest(source_dag) if source_dag else None},
    }
    validate_row(row)
    return row


def validate_row(row):
    """Check the stable shape plus graph and cross-field invariants."""
    _require(isinstance(row, dict) and set(row) == ROW_FIELDS, "unified row fields mismatch")
    _require(row["schema_version"] == VERSION and _text(row["item_id"]), "unified version or ID mismatch")
    _require(_text(row["benchmark"]), "missing benchmark")
    problem, answer, graph, review, provenance = (row[key] for key in
                                                   ("problem", "answer", "dag", "review", "provenance"))
    _require(set(problem) == {"question", "domain", "choices", "entry_point"}
             and _text(problem["question"]) and _text(problem["domain"]), "problem fields mismatch")
    _require(set(answer) == {"kind", "value"} and answer["kind"] in ("choice", "code", "text")
             and _text(answer["value"]), "answer fields mismatch")
    _require((problem["choices"] is None or isinstance(problem["choices"], list))
             and (problem["entry_point"] is None or _text(problem["entry_point"])),
             "invalid problem fields")
    if answer["kind"] == "choice":
        _require(isinstance(problem["choices"], list) and len(problem["choices"]) == 4
                 and answer["value"] in ("A", "B", "C", "D"), "invalid choice answer")
    if answer["kind"] == "code":
        _require(problem["choices"] is None
                 and (problem["entry_point"] is None or _text(problem["entry_point"]))
                 and (row["benchmark"] == "livecodebench_v6" or _text(problem["entry_point"])),
                 "invalid code answer")
    _require(set(graph) == {"schema_version", "nodes", "nodes_sha256"}
             and graph["schema_version"] == GRAPH_VERSION, "DAG fields mismatch")
    nodes = graph["nodes"]
    _require(isinstance(nodes, list) and len(nodes) >= 2
             and digest(nodes) == graph["nodes_sha256"], "DAG nodes/hash mismatch")
    seen = set()
    for node in nodes:
        _require(isinstance(node, dict) and set(node) == set(NODE_FIELDS), "node fields mismatch")
        node_id, parents = node["node_id"], node["parents"]
        _require(type(node_id) is int and node_id > 0 and node_id not in seen,
                 "invalid or duplicate node ID")
        _require(node["kind"] in KINDS and _text(node["statement"])
                 and _text(node["source_field"]) and _text(node["source_quote"])
                 and _text(node["justification"]), "invalid node content")
        _require(isinstance(parents, list) and all(type(p) is int for p in parents)
                 and len(parents) == len(set(parents)) and set(parents) <= seen,
                 "parents must refer to distinct earlier nodes")
        seen.add(node_id)
    _require(nodes[-1]["kind"] == "answer"
             and sum(node["kind"] == "answer" for node in nodes) == 1,
             "exactly one terminal answer required")
    if answer["kind"] == "code":
        _require(nodes[-1]["statement"] == answer["value"], "code answer mismatch")
    _require(set(review) == {"source_status", "model_accepted", "human_approved",
                             "quality_status", "construction_protocol"}
             and _text(review["source_status"])
             and review["model_accepted"] is True
             and type(review["human_approved"]) is bool
             and all(value is None or _text(value) for value in
                     (review["quality_status"], review["construction_protocol"])),
             "review fields mismatch")
    _require(set(provenance) == {"dataset", "subset", "split", "revision", "source_row", "source_id",
                                 "source_content_sha256", "source_schema_version",
                                 "source_file_sha256", "source_record_sha256", "source_dag_sha256"}
             and type(provenance["source_row"]) is int and provenance["source_row"] >= 0
             and all(_text(provenance[key]) for key in
                     ("dataset", "subset", "split", "revision", "source_id", "source_schema_version"))
             and all(_sha256(provenance[key]) for key in
                     ("source_content_sha256", "source_file_sha256", "source_record_sha256"))
             and (provenance["source_dag_sha256"] is None
                  or _sha256(provenance["source_dag_sha256"])), "provenance fields mismatch")


def convert_file(source_path, benchmark, expected_sha256, output_dir=None):
    """Validate a frozen JSONL; optionally write a new, immutable unified export."""
    source_path = Path(source_path)
    raw = source_path.read_bytes()
    source_hash = hashlib.sha256(raw).hexdigest()
    _require(source_hash == expected_sha256, "source file SHA256 mismatch")
    # JSONL records are separated by LF bytes. str.splitlines() also splits
    # U+2028/U+2029 inside valid quoted question text.
    lines = raw.decode("utf-8").split("\n")
    if lines[-1] == "":
        lines.pop()
    _require(bool(lines) and all(line.strip() for line in lines), "empty or blank JSONL row")
    rows = []
    for index, line in enumerate(lines, 1):
        try:
            original = json.loads(line, parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"nonfinite JSON value {value}")))
            rows.append(convert_record(original, benchmark, source_hash))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"source line {index}: {error}") from error
    _require(len({row["item_id"] for row in rows}) == len(rows), "duplicate item ID")
    encoded = ("".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                  allow_nan=False) + "\n" for row in rows)).encode("utf-8")
    html = render(rows, benchmark)
    manifest = {"schema_version": VERSION, "benchmark": benchmark, "records": len(rows),
                "source_sha256": source_hash,
                "unified_sha256": hashlib.sha256(encoded).hexdigest(),
                "html_schema_version": "pals_dag_unified_view_v1",
                "html_sha256": hashlib.sha256(html).hexdigest(),
                "diagnostic_records": sum(row["review"]["source_status"] ==
                                          "model_accepted_diagnostic" for row in rows)}
    if output_dir is not None:
        root = Path(output_dir)
        _require(not root.exists(), "output directory already exists")
        private_dir(root)
        write_bytes_once(root / "pals_dag_unified_v1.jsonl", encoded)
        write_bytes_once(root / "pals_dag_unified_v1.html", html)
        write_once(root / "manifest.json", manifest)
    return manifest
