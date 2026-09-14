"""Pinned full-Diamond coverage, with separate construction and bounded revision."""

import csv
import hashlib
import io
import zipfile
from pathlib import Path

from .campaign_export import export_campaign, outcome
from .config import Config
from .gpqa_source import ARCHIVE_SHA256, normalize_gpqa
from .pipeline import Pipeline, implementation, now
from .repair_loop import RevisionPipeline
from .review_issues import AUDIT_CONTRACT
from .revision_source import prepare_revision
from .source import select
from .storage import digest, read_json, run_lock, write_bytes_once, write_once

HISTORY = (
    "pilot-reference-30-v1",
    "extension-reference-50-v1",
    "recover-reference-3-transport-v1",
    "repair-reference-30-round1-v1",
    "repair-recovered-3-round1-v1",
    "repair-extension-50-round1-v1",
    "repair-extension-50-round1-c6-v1",
    "revision-pilot-10-v4-flash-v1",
    "verify-evidence-2-v1",
    "verify-diagnosis-1-retry2",
    "revision-remaining-31-diagnosis-v1",
    "verify-structure-options-2-v1",
)


def prepare_campaign(root, base):
    root, base = Path(root).absolute(), Path(base).absolute()
    if root.exists():
        raise ValueError("campaign destination exists; do not reselect")
    raw = (base / "pilot-reference-30-v1/source/official-dataset.zip").read_bytes()
    if hashlib.sha256(raw).hexdigest() != ARCHIVE_SHA256:
        raise ValueError("official archive hash mismatch")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        csv_text = archive.read(
            "dataset/gpqa_diamond.csv", pwd=b"deserted-untie-orchid"
        ).decode("utf-8-sig")
    raw_rows = list(csv.DictReader(io.StringIO(csv_text)))
    items = normalize_gpqa(raw_rows)
    if len(items) != 198:
        raise ValueError("not full pinned Diamond")
    prior = read_json(base / "pilot-reference-30-v1/items.json") + read_json(
        base / "extension-reference-50-v1/items.json"
    )
    by_id = {i["item_id"]: i for i in items}
    pilot_ids = [i["item_id"] for i in prior]
    if len(set(pilot_ids)) != 80 or any(by_id.get(i["item_id"]) != i for i in prior):
        raise ValueError("pilot source mismatch")
    _, eligibility = select(
        [i for i in items if not i.get("source_eligibility_issue")], 1, 20260910
    )
    exclusions = {
        i["item_id"]: i["source_eligibility_issue"]
        for i in items
        if i.get("source_eligibility_issue")
    }
    exclusions.update(
        {row["item_id"]: row["reason"] for row in eligibility["excluded"]}
    )
    pending = [i for i in items if i["item_id"] not in set(pilot_ids) | set(exclusions)]
    history = {
        i["item_id"]: [
            event
            for name in HISTORY
            if (event := outcome(base / name, i["item_id"])) is not None
        ]
        for i in prior
    }
    if any(not events for events in history.values()):
        raise ValueError("pilot item lacks preserved outcomes")
    write_bytes_once(root / "source/official-dataset.zip", raw)
    write_once(root / "source/raw_rows.json", raw_rows)
    write_once(root / "items.json", items)
    write_once(root / "pilot_history.json", history)
    write_once(
        root / "campaign.json",
        {
            "source_count": 198,
            "source_archive_sha256": ARCHIVE_SHA256,
            "pilot_ids": pilot_ids,
            "new_ids": [i["item_id"] for i in pending],
            "source_entry_reviews": exclusions,
            "audit_contract": AUDIT_CONTRACT,
            "pilot_history_sha256": digest(history),
            "protocol": "Reuse all pilot outcomes with provenance. New eligible items: five-stage official-reference construction, then final current-contract audit and at most two revision rounds. No restarting exhausted pilot repairs. All outcomes exported.",
            "human_approved": 0,
            "mixed_protocols": True,
        },
    )
    write_once(root / "construction/items.json", pending)
    write_once(
        root / "construction/selection.json",
        {
            "selected_ids": [i["item_id"] for i in pending],
            "scope": "all previously unprocessed eligible pinned Diamond rows; no outcome replacement",
        },
    )
    common = {
        "task_type": "gpqa",
        "model": "deepseek-v4-flash",
        "workers": 4,
        "max_tokens": 32768,
        "timeout_seconds": 600,
        "thinking": "enabled",
        "reasoning_effort": "high",
        "response_format": "json_object",
    }
    write_once(
        root / "construction/config.json",
        Config(
            **common,
            prompt_version="gpqa-reference-v1",
            max_calls=2400,
            max_reserved_tokens=180000000,
        ).to_dict(),
    )
    write_once(
        root / "revision-config.json",
        Config(
            **common,
            prompt_version="gpqa-revision-v1",
            max_calls=3000,
            max_reserved_tokens=180000000,
        ).to_dict(),
    )
    return export_campaign(root, "initial")


def run_campaign(root, key_file):
    from .client import APIClient, load_key

    root = Path(root).absolute()
    with run_lock(root):
        if (root / "completion.json").exists():
            return read_json(root / "completion.json")
        manifest = read_json(root / "campaign.json")
        if (
            digest(read_json(root / "pilot_history.json"))
            != manifest["pilot_history_sha256"]
        ):
            raise ValueError("frozen history changed")
        write_once(root / "implementation.json", implementation())

        def progress(row):
            print(str(dict(row, time=now())), flush=True)

        first_root = root / "construction"
        config = Config.load(first_root / "config.json")
        first = Pipeline(
            first_root,
            config,
            APIClient(config, load_key(config.key_env, key_file)),
            resilient=True,
        ).run(progress=progress)
        write_once(first_root / "phase-result.json", first)
        export_campaign(root, "after-construction")
        second = None
        if not first["global_stop"]:
            entries = [
                {
                    "item_id": row["item_id"],
                    "source_root": str(first_root),
                    "role": "case",
                }
                for row in first["results"]
                if row["status"] != "paused"
            ]
            if entries:
                # Even first-pass acceptances receive the current final audit.
                prepare_revision(
                    root / "revision",
                    entries,
                    "All transport-complete new cases, including first-pass acceptances. Zero prior revision rounds. Apply current final audit uniformly; at most two content revisions.",
                )
                config = Config.load(root / "revision-config.json")
                second = RevisionPipeline(
                    root / "revision",
                    config,
                    APIClient(config, load_key(config.key_env, key_file)),
                    resilient=True,
                ).run(progress=progress)
                write_once(root / "revision/phase-result.json", second)
        export_path, summary = export_campaign(root, "final")
        completion = {
            "ended_at": now(),
            "status": "paused"
            if first["paused"] or (second and second["paused"])
            else "processed",
            "export_path": export_path,
            "summary": summary,
            "first_attempts": first["attempt_count"],
            "revision_attempts": second["attempt_count"] if second else 0,
            "human_approved": 0,
        }
        write_once(root / "completion.json", completion)
        return completion
