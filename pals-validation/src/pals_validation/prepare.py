"""Freeze score-blind E1 interventions and one-ancestor-prefix E2 anchor."""
from pathlib import Path
from .data import normalize
from .e1_overrides import BreakOverrides
from .graph import breaking, e2_anchor, forest, parent_control
from .io import digest, read, read_jsonl, save, sha256

VERSION = "gpqa-validation-v1"
KNOWN_E2_EXCLUSIONS = {"9220d9a8a6f7295fa73b": "historically reviewed incomplete question; no content repair"}


def prepare(source, output, seed=20260915, parent_probe=False, expected_sha=None,
            benchmark="gpqa", e1_break_overrides=None):
    source, output = Path(source), Path(output)
    source_hash = sha256(source)
    if expected_sha and source_hash != expected_sha:
        raise ValueError("Source SHA256 mismatch")
    overrides = (BreakOverrides(e1_break_overrides, benchmark, seed)
                 if e1_break_overrides is not None else None)
    frozen_release_sha = None
    frozen_compatible_experiments = None
    release_path = source.parent / "manifest.json"
    if release_path.is_file():
        release = read(release_path)
        frozen_protocols = {
            "coworker-pals-frozen-synthetic-cohort-v2",
            "coworker-pals-frozen-synthetic-cohort-v3-psych-e1-overrides",
        }
        if isinstance(release, dict) and release.get("protocol") in frozen_protocols:
            files = release.get("files")
            file_entry = files.get(source.name) if isinstance(files, dict) else None
            if not isinstance(file_entry, dict) or file_entry.get("sha256") != source_hash:
                raise ValueError("Frozen cohort source file hash mismatch")
            if release.get("selection_seed") != seed:
                raise ValueError("Frozen cohort selection seed mismatch")
            frozen_experiment = source.name.split("-", 1)[0]
            if frozen_experiment not in ("e1", "e2"):
                raise ValueError("Frozen source is not an E1 or E2 cohort")
            if (source.name == "e1-mmlu_psych_social.jsonl"
                    and release["protocol"] ==
                    "coworker-pals-frozen-synthetic-cohort-v3-psych-e1-overrides"):
                if overrides is None:
                    raise ValueError("Frozen psychology E1 requires its break override manifest")
                if release["e1_break_overrides"]["manifest_sha256"] != overrides.file_sha256:
                    raise ValueError("Frozen psychology E1 override manifest hash mismatch")
            frozen_release_sha = sha256(release_path)
        elif (isinstance(release, dict)
              and release.get("protocol") == "coworker-pals-full-delivery-v1"):
            files = release.get("files")
            file_entry = files.get(source.name) if isinstance(files, dict) else None
            if (not isinstance(file_entry, dict)
                    or file_entry.get("sha256") != source_hash
                    or file_entry.get("benchmark") != benchmark):
                raise ValueError("Full-delivery source file hash or benchmark mismatch")
            if (release.get("selection_seed") != seed
                    or release.get("compatible_experiments") != ["e1", "e2"]):
                raise ValueError("Full-delivery seed or experiment compatibility mismatch")
            frozen_release_sha = sha256(release_path)
            frozen_compatible_experiments = release["compatible_experiments"]
    cases, jobs, inventory, selection = [], [], [], []
    seen = set()
    for record in read_jsonl(source):
        if overrides:
            overrides.check_record(record)
        case = normalize(record, benchmark)
        item = case["item_id"]
        if item in seen:
            raise ValueError("Duplicate question ID")
        seen.add(item)
        cases.append(case)
        steps = case["steps"]
        by_id = {n["node_id"]: n for n in steps}
        original = [n["node_id"] for n in steps]
        f = forest(steps, seed, item)
        ob = breaking(steps, original, seed, item, "original_break")
        fb = breaking(steps, f["baseline"], seed, item, "forest_break")
        unmodified_fb = fb
        if overrides:
            fb = overrides.select(item, steps, f["baseline"], fb)
        orders = {"original": original, "forest_baseline": f["baseline"]}
        for label, order in (("original_break", ob["order"] if ob else None),
                             ("forest_break", fb["order"] if fb else None), ("legal", f["legal"])):
            if order:
                orders[label] = order
        for label, order in orders.items():
            for pos in range(1, len(order)):
                target = order[pos]
                jobs.append({"kind": "e1", "item_id": item, "variant": label,
                             "target_id": target, "prefix_ids": order[:pos],
                             "deleted_id": order[pos - 1], "target": by_id[target]["statement"]})
        probe = parent_control(steps, item, seed) if parent_probe else None
        if probe:
            for label, deleted in (("parent", probe["parent_id"]), ("control", probe["control_id"])):
                jobs.append({"kind": "e1", "item_id": item, "variant": label,
                             "target_id": probe["target_id"], "prefix_ids": probe["prefix_ids"],
                             "deleted_id": deleted, "target": by_id[probe["target_id"]]["statement"]})
        excluded = KNOWN_E2_EXCLUSIONS if benchmark == "gpqa" else {}
        anchor = None if item in excluded else e2_anchor(steps, item, seed)
        if anchor:
            # Target text, gold answer and edge metadata never enter the generation job.
            jobs.append({"kind": "e2", "item_id": item, "prefix_ids": anchor["prefix_ids"],
                         "deleted_id": anchor["deleted_id"]})
        selected = {"item_id": item, "orders": orders, "forest": f,
                    "original_break": ob, "forest_break": fb, "anchor": anchor,
                    "parent_probe": probe}
        if overrides and item in overrides.edges:
            selected["forest_break_before_override"] = unmodified_fb
            selected["forest_break_override_edge"] = list(overrides.edges[item])
        selection.append(selected)
        inventory.append({"item_id": item, "source_row": case["source_row"], "domain": case["domain"],
                          "legal": f["legal"] is not None, "original_break": ob is not None,
                          "forest_break": fb is not None, "fair_pair": bool(f["legal"] and fb),
                          "e2": anchor is not None, "legal_reason": f["reason"],
                          "e2_reason": None if anchor else excluded.get(item, "no_nonanswer_node_with_parent")})
    for job in jobs:
        job["job_id"] = digest(job)
    if overrides:
        overrides.check_complete()
    if not cases:
        raise ValueError("Empty accepted cohort")
    payloads = {"cases.json": cases, "jobs.json": jobs, "selection.json": selection, "inventory.json": inventory}
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name, payload in payloads.items():
        save(output / name, payload)
    protocols = {"gpqa": VERSION, "humaneval": "humaneval-validation-v1",
                 "livecodebench": "livecodebench-validation-v1",
                 "gsm8k": "gsm8k-unified-validation-v1", "mmlu": "mmlu-unified-validation-v1"}
    choices_policy = ("always_included_in_v1" if benchmark in ("gpqa", "mmlu") else
                      "none_open_answer" if benchmark == "gsm8k" else
                      "not_applicable_original_code_prompt")
    manifest = {"protocol": protocols[benchmark],
                "source_sha256": source_hash, "selection_seed": seed,
                "parent_probe": parent_probe, "e2_policy": "one_hash_selected_target_ancestor_prefix",
                "question_choices": choices_policy,
                "questions": len(cases),
                "counts": {k: sum(bool(r[k]) for r in inventory)
                           for k in ("legal", "original_break", "forest_break", "fair_pair", "e2")},
                "files": {name: sha256(output / name) for name in payloads}}
    if frozen_release_sha:
        manifest["frozen_cohort"] = {"release_manifest_sha256": frozen_release_sha,
                                     "source_file": source.name,
                                     "release_protocol": release["protocol"]}
        if frozen_compatible_experiments is None:
            manifest["frozen_source_experiment"] = frozen_experiment
        else:
            # A single, identical full-delivery source serves both arms.  Do not
            # mislabel "scoreable" as an experiment in run._init_run's arm gate.
            manifest["compatible_experiments"] = frozen_compatible_experiments
    if overrides:
        from . import data, e1_overrides, graph, io, unified

        manifest["protocol"] += "+mmlu-psych-e1-break-overrides-v1"
        manifest["e1_break_overrides"] = overrides.provenance((
            Path(__file__), Path(data.__file__), Path(graph.__file__),
            Path(io.__file__), Path(unified.__file__),
            Path(e1_overrides.__file__),
        ))
        if frozen_release_sha:
            manifest["e1_break_overrides"]["frozen_release_manifest_sha256"] = frozen_release_sha
    save(output / "manifest.json", manifest)
    return manifest
