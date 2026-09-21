"""Explicit, human-gated release. Never auto-promote model-reviewed candidates."""

from .schemas import require, text
from .storage import digest, read_json, write_bytes_once, write_once


def trajectory(dag, view):
    require(view in ("node_only", "justification_plus_node"), "unknown rendering view")
    source = dag["source"]
    steps = []
    for node in dag["nodes"]:
        if node["kind"] == "answer":
            continue
        rendered = (
            node["statement"]
            if view == "node_only"
            else node["justification"] + "\n" + node["statement"]
        )
        steps.append(
            {"step_id": node["node_id"], "text": rendered, "parents": node["parents"]}
        )
    answer = dag["reference_solution"]["answer"]
    if source.get("task_type") in ("gsm8k", "humaneval"):
        question = source["question"]
    else:
        question = (
            source["question"]
            + "\n\n"
            + "\n".join(
                f"{letter}. {choice}"
                for letter, choice in zip("ABCD", source["choices"])
            )
        )
        answer = answer + ". " + source["choices"]["ABCD".index(answer)]
    return {
        "problem_id": dag["item_id"],
        "trajectory_id": dag["item_id"] + ":" + view + ":" + digest(dag)[:16],
        "question": question,
        "steps": steps,
        "answer": answer,
        "variant": (
            "repaired:"
            if dag.get("construction_protocol") == "gpqa-repair-v1"
            else "original:"
        )
        + view
        + ":answer_separated_v1",
    }


def release(root, review_path):
    reviews = read_json(review_path)
    require(isinstance(reviews, list), "human review must be a list")
    require(all(isinstance(r, dict) for r in reviews), "invalid human review row")
    require(
        len({r.get("item_id") for r in reviews}) == len(reviews),
        "duplicate human review",
    )
    selected = {i["item_id"] for i in read_json(root / "items.json")}
    accepted = []
    for review in reviews:
        require(
            review.get("item_id") in selected,
            "human review refers to an unselected item",
        )
        require(
            text(review.get("reviewer")) and text(review.get("reason")),
            "reviewer and reason required",
        )
        require(
            review.get("decision") in ("accept", "reject", "needs_review"),
            "invalid human decision",
        )
        if review["decision"] != "accept":
            continue
        directory = root / "items" / review["item_id"]
        result = read_json(directory / "result.json")
        require(
            result["status"] in ("model_accepted", "repaired_model_accepted"),
            "only model-accepted candidates can be released",
        )
        dag = read_json(directory / "dag.json")
        require(
            review.get("dag_sha256") == digest(dag) == result["dag_sha256"],
            "stale human review or changed DAG",
        )
        accepted.append(dag)
    require(bool(accepted), "no human-approved records to release")
    accepted.sort(key=lambda d: d["item_id"])
    manifest = {
        "schema_version": "reviewed_synthetic_reference_dag_v1",
        "selected_count": len(selected),
        "released_count": len(accepted),
        "repaired_count": sum(
            d.get("construction_protocol") == "gpqa-repair-v1" for d in accepted
        ),
        "dag_hashes": {d["item_id"]: digest(d) for d in accepted},
        "human_reviews_sha256": digest(reviews),
        "scope": "reviewed synthetic reference data; no claim of complete human-independent gold truth",
    }
    destination = root / "release" / digest(manifest)[:16]
    write_once(destination / "manifest.json", manifest)
    write_once(destination / "human_reviews.json", reviews)
    write_once(destination / "dags.json", accepted)
    import json

    for view in ("node_only", "justification_plus_node"):
        lines = "".join(
            json.dumps(trajectory(d, view), ensure_ascii=False, allow_nan=False) + "\n"
            for d in accepted
        )
        write_bytes_once(destination / (view + ".jsonl"), lines.encode())
    return destination
