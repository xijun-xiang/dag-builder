"""Official GPQA-Diamond expert references, with pinned and aligned revisions.

The archive password is public in the upstream README, not an API credential.
Never extract arbitrary archive paths or publish the private benchmark text.
"""

import csv
import hashlib
import io
import zipfile
from collections import Counter
from pathlib import Path
from urllib.request import urlopen

from .schemas import Question, require, text
from .source import select
from .storage import digest, private_dir, read_json, write_bytes_once, write_once

REVISION = "56686c06f5e19865c153de0fdb11be3890014df7"
ARCHIVE_SHA256 = "461ae7329f15a3e35f8184d2dac24b990f34fdf12f366ca4062d8e6638cd08dc"
URL = f"https://raw.githubusercontent.com/idavidrein/gpqa/{REVISION}/dataset.zip"
FIELDS = (
    "Question",
    "Explanation",
    "Correct Answer",
    "Incorrect Answer 1",
    "Incorrect Answer 2",
    "Incorrect Answer 3",
)
DOMAINS = ("Biology", "Chemistry", "Physics")


def normalize_gpqa(rows, revision=REVISION, choice_seed=20260910):
    """Use a complete revised bundle or the complete base bundle, never a mix."""
    items, seen = [], set()
    for index, row in enumerate(rows):
        record_id = row.get("Record ID")
        require(
            text(record_id) and record_id not in seen,
            "missing/duplicate GPQA Record ID",
        )
        seen.add(record_id)
        revised = [text(row.get("Extra Revised " + field)) for field in FIELDS]
        require(not any(revised) or all(revised), "incomplete GPQA revision bundle")
        prefix = "Extra Revised " if all(revised) else ""
        fields = {field: prefix + field for field in FIELDS}
        require(
            all(text(row.get(name)) for name in fields.values()),
            "empty GPQA source field",
        )
        values = {field: row[name] for field, name in fields.items()}
        domain = row.get("High-level domain")
        require(domain in DOMAINS, "unknown GPQA domain")
        identity = {
            "dataset": "Idavidrein/gpqa",
            "revision": revision,
            "subset": "gpqa_diamond",
            "split": "train",
            "row": index,
            "record_id": record_id,
        }
        options = [
            "Correct Answer",
            "Incorrect Answer 1",
            "Incorrect Answer 2",
            "Incorrect Answer 3",
        ]
        options.sort(
            key=lambda field: digest(
                {"seed": choice_seed, "record_id": record_id, "field": field}
            )
        )
        choices = [values[field] for field in options]
        Question(values["Question"], tuple(choices))
        items.append(
            dict(
                identity,
                task_type="gpqa",
                item_id=digest(identity)[:20],
                question=values["Question"],
                choices=choices,
                gold_answer="ABCD"[options.index("Correct Answer")],
                official_explanation=values["Explanation"],
                domain=domain,
                subdomain=row.get("Subdomain"),
                official_difficulty=row.get("Writer's Difficulty Estimate"),
                source_fields=fields,
                choice_source_fields={
                    label: fields[field] for label, field in zip("ABCD", options)
                },
                source_content_sha256=digest(row),
                source_eligibility_issue=(
                    "duplicate_choice_text" if len(set(choices)) != 4 else None
                ),
            )
        )
    return items


def select_gpqa(items, count=30, seed=20260910):
    """Balanced development coverage, not a population accuracy estimate."""
    require(
        type(count) is int and count > 0 and count % 3 == 0,
        "count must be positive and divisible by 3",
    )
    chosen, strata = [], {}
    for domain in DOMAINS:
        subset = [
            item
            for item in items
            if item["domain"] == domain and not item.get("source_eligibility_issue")
        ]
        selected, manifest = select(subset, count // 3, seed)
        chosen.extend(selected)
        strata[domain] = manifest
    chosen.sort(key=lambda item: digest({"seed": seed, "item_id": item["item_id"]}))
    return chosen, {
        "seed": seed,
        "choice_seed": seed,
        "selected_ids": [item["item_id"] for item in chosen],
        "selected_count": len(chosen),
        "candidate_count": len(items),
        "domain_counts": dict(Counter(item["domain"] for item in chosen)),
        "strata": strata,
        "source_exclusions": [
            {
                "item_id": item["item_id"],
                "row": item["row"],
                "reason": item["source_eligibility_issue"],
            }
            for item in items
            if item.get("source_eligibility_issue")
        ],
        "sampling": "equal domain allocation; within-domain SHA256(seed,item_id), without replacement",
        "scope": "fixed development pilot; no score-based selection or replenishment; not representative of Diamond domain proportions",
        "revision_policy": "complete Extra Revised bundle when present; otherwise complete base bundle; incomplete bundles stop preparation",
    }


def select_gpqa_extension(items, prior_items, count=50, seed=20260910):
    """Exclude the entire prior cohort, regardless of its construction outcomes.

    Allocate proportionally to eligible remaining domain sizes using largest
    remainders. Neither selection nor allocation reads API outputs or decisions.
    """
    require(type(count) is int and count > 0, "count must be a positive integer")
    by_id = {item["item_id"]: item for item in items}
    prior_ids = [item["item_id"] for item in prior_items]
    require(
        bool(prior_ids) and len(set(prior_ids)) == len(prior_ids),
        "invalid prior cohort IDs",
    )
    require(
        all(by_id.get(item["item_id"]) == item for item in prior_items),
        "prior cohort differs from pinned source or option mapping",
    )
    excluded = set(prior_ids)
    pool = [
        item
        for item in items
        if item["item_id"] not in excluded and not item.get("source_eligibility_issue")
    ]
    # Reuse the exact visual/duplicate filters of the initial protocol.
    _, eligibility = select(pool, 1, seed)
    excluded_source_ids = {row["item_id"] for row in eligibility["excluded"]}
    eligible = [item for item in pool if item["item_id"] not in excluded_source_ids]
    total = len(eligible)
    require(count <= total, "insufficient eligible remaining items")
    sizes = Counter(item["domain"] for item in eligible)
    allocation = {domain: count * sizes[domain] // total for domain in DOMAINS}
    priority = sorted(
        DOMAINS, key=lambda domain: (-(count * sizes[domain] % total), domain)
    )
    for domain in priority[: count - sum(allocation.values())]:
        allocation[domain] += 1
    chosen = []
    for domain in DOMAINS:
        subset = sorted(
            (item for item in eligible if item["domain"] == domain),
            key=lambda item: digest({"seed": seed, "item_id": item["item_id"]}),
        )
        chosen.extend(subset[: allocation[domain]])
    chosen.sort(key=lambda item: digest({"seed": seed, "item_id": item["item_id"]}))
    require(
        len(chosen) == count
        and not excluded.intersection(item["item_id"] for item in chosen),
        "extension selection invariant failed",
    )
    return chosen, {
        "seed": seed,
        "choice_seed": seed,
        "selected_count": len(chosen),
        "selected_ids": [item["item_id"] for item in chosen],
        "excluded_prior_ids": prior_ids,
        "prior_items_sha256": digest(prior_items),
        "candidate_count": len(items),
        "remaining_eligible_count": total,
        "remaining_domain_counts": dict(sizes),
        "domain_counts": allocation,
        "source_exclusions": [
            {"item_id": item["item_id"], "reason": item["source_eligibility_issue"]}
            for item in items
            if item.get("source_eligibility_issue")
        ]
        + eligibility["excluded"],
        "sampling": "largest-remainder proportional allocation over eligible remaining domains; within-domain SHA256(seed,item_id), without replacement",
        "scope": "non-overlapping development extension, no outcome-based selection or replenishment; not an independent confirmatory test",
        "revision_policy": "complete Extra Revised bundle when present; otherwise complete base bundle; incomplete bundles stop preparation",
    }


def prepare_gpqa(root, count=30, seed=20260910, source_archive=None, exclude_root=None):
    root = private_dir(root)
    source = private_dir(root / "source")
    path = source / "official-dataset.zip"
    if path.exists():
        raw = path.read_bytes()
    elif source_archive is not None:
        raw = Path(source_archive).read_bytes()
    else:
        with urlopen(URL, timeout=90) as response:
            raw = response.read(20_000_001)
    require(len(raw) <= 20_000_000, "unexpected archive size")
    require(
        hashlib.sha256(raw).hexdigest() == ARCHIVE_SHA256,
        "official archive hash mismatch",
    )
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        name = "dataset/gpqa_diamond.csv"
        require(archive.getinfo(name).file_size < 10_000_000, "unexpected CSV size")
        csv_text = archive.read(name, pwd=b"deserted-untie-orchid").decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    require(len(rows) == 198, "pinned Diamond must contain 198 rows")
    write_bytes_once(path, raw)
    write_once(source / "raw_rows.json", rows)
    items = normalize_gpqa(rows, choice_seed=seed)
    if exclude_root is None:
        chosen, selection = select_gpqa(items, count, seed)
    else:
        prior_root = Path(exclude_root).resolve()
        require(prior_root != root.resolve(), "cannot exclude the destination run")
        prior_items = read_json(prior_root / "items.json")
        require(
            [item["item_id"] for item in prior_items]
            == read_json(prior_root / "selection.json")["selected_ids"],
            "prior cohort manifest mismatch",
        )
        chosen, selection = select_gpqa_extension(items, prior_items, count, seed)
        selection["excluded_prior_root"] = str(prior_root)
    write_once(source / "normalized.json", items)
    write_once(
        source / "provenance.json",
        {
            "dataset": "Idavidrein/gpqa",
            "url": URL,
            "revision": REVISION,
            "archive_sha256": ARCHIVE_SHA256,
            "csv_member": name,
            "csv_sha256": hashlib.sha256(csv_text.encode()).hexdigest(),
            "rows": len(rows),
            "nonempty_explanations": len(items),
            "revised_bundles": sum(
                i["source_fields"]["Explanation"].startswith("Extra Revised")
                for i in items
            ),
            "use": "private local development; no public benchmark-text upload",
        },
    )
    write_once(root / "items.json", chosen)
    write_once(root / "selection.json", selection)
    return selection
