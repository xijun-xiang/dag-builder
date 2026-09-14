"""Operational pauses are evidence, not immutable scientific verdicts.

New runs record per-item pause events before publishing progress. Reports can
also read completed invocations from older runs without rewriting their files.
Neither a stale pause nor the absence of a pause proves a worker is running.
"""

from datetime import datetime

from .storage import digest, read_json, write_once


def _work_files(directory):
    return sorted(
        set(directory.glob("*/attempt-*/*.json"))
        | set(directory.glob("*/output.json"))
        | set(directory.glob("*/validation.json"))
    )


def _work_digest(directory):
    # Stage artifacts are immutable. Hash content as well as names so any new
    # request, returned response or parsed output invalidates an earlier pause.
    return digest(
        {
            str(p.relative_to(directory)): digest(read_json(p))
            for p in _work_files(directory)
        }
    )


def record_pause(root, result, recorded_at):
    if result.get("status") != "paused":
        return
    item_id = result["item_id"]
    event = {
        "recorded_at": recorded_at,
        "result": result,
        "work_digest": _work_digest(root / "items" / item_id),
    }
    write_once(root / "status_events" / item_id / (digest(event) + ".json"), event)


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def paused_items(root):
    """Return current, evidenced pauses; terminal result.json still wins."""
    latest = {}
    for path in root.glob("invocations/*.json"):
        invocation = read_json(path)
        stamp = _timestamp(invocation.get("ended_at"))
        if stamp is None:
            continue
        for row in invocation.get("results", []):
            item_id = row["item_id"]
            if item_id not in latest or stamp > latest[item_id][0]:
                latest[item_id] = (stamp, row)
    pauses = {}
    # Manifests, not event contents, determine which directories can be read.
    for item in read_json(root / "items.json"):
        item_id = item["item_id"]
        directory = root / "items" / item_id
        events = [
            read_json(p) for p in (root / "status_events" / item_id).glob("*.json")
        ]
        events = [e for e in events if _timestamp(e.get("recorded_at")) is not None]
        if events:
            event = max(events, key=lambda e: _timestamp(e["recorded_at"]))
            row = event["result"]
            if (
                row.get("item_id") == item_id
                and row.get("status") == "paused"
                and event.get("work_digest") == _work_digest(directory)
            ):
                pauses[item_id] = dict(row, status_evidence="pause_event")
            continue  # Never resurrect an older invocation after new work.
        if item_id not in latest:
            continue
        ended_at, row = latest[item_id]
        if row.get("status") != "paused":
            continue
        # Legacy invocations have no per-item fingerprint. Use stored timestamps,
        # not mtime (archives may be copied). Missing timestamps are inconclusive.
        attempts = list(directory.glob("*/attempt-*/*.json"))
        stamps = [
            _timestamp(value.get("ended_at") or value.get("started_at"))
            for value in (read_json(p) for p in attempts)
        ]
        if all(stamp is not None and stamp <= ended_at for stamp in stamps):
            pauses[item_id] = dict(row, status_evidence="completed_invocation")
    return pauses
