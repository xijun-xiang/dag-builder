#!/usr/bin/env python3
"""Freeze the unresolved GSM8K comparison rows for auditable repair experiments."""

import argparse
import json
from pathlib import Path

from dag_builder.source import canonicalize_gsm8k_rationale
from dag_builder.storage import digest, private_dir, read_json, write_once


def result(root, item_id):
    path = root / "items" / item_id / "result.json"
    return read_json(path)


def draft_rationale(root, item_id):
    output = root / "items" / item_id / "solve" / "output.json"
    if output.exists():
        value = read_json(output).get("rationale")
        if isinstance(value, str) and value.strip():
            return value
    response = next(
        (root / "items" / item_id / "solve").glob("attempt-*/response.json"),
        None,
    )
    if response is not None:
        choices = read_json(response).get("body", {}).get("choices", [])
        if len(choices) == 1:
            content = choices[0].get("message", {}).get("content")
            if isinstance(content, str) and content.strip():
                return content
    return "[No usable conditioned draft was recorded.]"


def write_root(root, items, source, mode):
    root = private_dir(root)
    selection = {
        "selected_ids": [item["item_id"] for item in items],
        "selected_count": len(items),
        "selection_method": "all rows without model_accepted in three frozen baseline modes",
        "baseline": source,
        "mode": mode,
    }
    write_once(root / "items.json", items)
    write_once(root / "selection.json", selection)
    write_once(
        root / "supplement_manifest.json",
        {
            "schema_version": "gsm8k_supplement_v1",
            "mode": mode,
            "source": source,
            "item_count": len(items),
            "items_sha256": digest(items),
            "normalization": (
                "replace each GSM8K <<expression>>immediately-adjacent-numeric-result with expression; raw_answer remains unchanged"
                if mode == "canonical_official_rationale"
                else None
            ),
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", required=True, type=Path)
    parser.add_argument("--independent", required=True, type=Path)
    parser.add_argument("--conditioned", required=True, type=Path)
    parser.add_argument("--official", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    original_items = read_json(args.items)
    roots = {
        "independent_generation": args.independent.resolve(),
        "answer_conditioned_generation": args.conditioned.resolve(),
        "official_rationale": args.official.resolve(),
    }
    unresolved, diagnostic_items, canonical_items = [], [], []
    for original in original_items:
        statuses = {name: result(root, original["item_id"])["status"] for name, root in roots.items()}
        if "model_accepted" in statuses.values():
            continue
        item_id = original["item_id"]
        conditioned_result = result(roots["answer_conditioned_generation"], item_id)
        diagnostic = dict(original)
        diagnostic.update(
            repair_draft_rationale=draft_rationale(
                roots["answer_conditioned_generation"], item_id
            ),
            repair_diagnosis=(
                "Baseline statuses: "
                + "; ".join(f"{key}={value}" for key, value in statuses.items())
                + ". Conditioned failure: "
                + conditioned_result.get("reason", "unspecified")
            ),
            supplement_baseline_statuses=statuses,
        )
        canonical = dict(original)
        canonical.update(
            canonical_rationale=canonicalize_gsm8k_rationale(original["raw_answer"]),
            canonicalization_method="gsm8k_calculator_markup_v1",
            supplement_baseline_statuses=statuses,
        )
        unresolved.append({"item_id": item_id, "statuses": statuses})
        diagnostic_items.append(diagnostic)
        canonical_items.append(canonical)

    if not unresolved:
        raise ValueError("no unresolved rows found")
    output = args.output.resolve()
    source = {
        "items": str(args.items.resolve()),
        **{name: str(path) for name, path in roots.items()},
    }
    write_root(output / "diagnostic-repair", diagnostic_items, source, "diagnostic_repair_generation")
    write_root(output / "canonical-official", canonical_items, source, "canonical_official_rationale")
    write_once(
        output / "supplement_selection.json",
        {
            "unresolved_count": len(unresolved),
            "unresolved": unresolved,
            "source": source,
        },
    )
    print(json.dumps({"output": str(output), "unresolved_count": len(unresolved)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
