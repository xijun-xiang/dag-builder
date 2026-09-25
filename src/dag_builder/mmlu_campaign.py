"""Offline, resumable preparation of the 57 distinct MMLU subjects."""

from pathlib import Path

from .mmlu_catalog import MMLU_SUBJECTS
from .response_contract import reported_tokens
from .source import prepare
from .storage import private_dir, read_json, run_lock, write_once


def prepare_all(root, revision, split="test", count_per_subject=None,
                seed=20260909, source_dir=None):
    """Freeze each subject independently; never select by model outcome."""
    root = private_dir(root)
    if count_per_subject is not None and (type(count_per_subject) is not int or count_per_subject <= 0):
        raise ValueError("count_per_subject must be positive or omitted for all eligible")
    source_dir = Path(source_dir).resolve() if source_dir is not None else None
    if source_dir is not None and not source_dir.is_dir():
        raise ValueError("source_dir must exist")
    subjects = {}
    for subset in MMLU_SUBJECTS:
        local_source = (source_dir / subset / f"{split}-00000-of-00001.parquet"
                        if source_dir is not None else None)
        subjects[subset] = prepare(root / "subjects" / subset, revision, subset,
                                   split, count_per_subject, seed, "mmlu", local_source)
    manifest = {"protocol": "mmlu-57-source-selection-v1", "dataset": "cais/mmlu",
                "revision": revision, "split": split, "seed": seed,
                "count_per_subject": count_per_subject,
                "subject_count": len(MMLU_SUBJECTS),
                "candidate_count": sum(row["candidate_count"] for row in subjects.values()),
                "eligible_count": sum(row["eligible_count"] for row in subjects.values()),
                "selected_count": sum(row["selected_count"] for row in subjects.values()),
                "subjects": subjects}
    write_once(root / "campaign_manifest.json", manifest)
    return manifest


def run_all(root, config, client, subjects, max_total_calls,
            max_total_reserved_tokens, limit_per_subject=None, resilient=False,
            progress=None):
    """Sequential paid execution with an explicit worst-case campaign ceiling."""
    with run_lock(root):
        return _run_all_locked(root, config, client, subjects, max_total_calls,
                               max_total_reserved_tokens, limit_per_subject,
                               resilient, progress)


def _run_all_locked(root, config, client, subjects, max_total_calls,
                    max_total_reserved_tokens, limit_per_subject, resilient,
                    progress):
    from .pipeline import Pipeline

    root = Path(root)
    manifest = read_json(root / "campaign_manifest.json")
    if (manifest.get("protocol") != "mmlu-57-source-selection-v1"
            or set(manifest.get("subjects", {})) != set(MMLU_SUBJECTS)
            or config.task_type != "mmlu"
            or config.prompt_version != "mmlu-general-thinking-v1"):
        raise ValueError("MMLU campaign/config mismatch")
    if (not subjects or len(set(subjects)) != len(subjects)
            or any(subject not in MMLU_SUBJECTS for subject in subjects)):
        raise ValueError("subjects must be a nonempty, unique MMLU subject list")
    if (type(max_total_calls) is not int or max_total_calls <= 0
            or type(max_total_reserved_tokens) is not int
            or max_total_reserved_tokens <= 0):
        raise ValueError("explicit positive campaign ceilings are required")
    if limit_per_subject is not None and (type(limit_per_subject) is not int
                                          or limit_per_subject <= 0):
        raise ValueError("limit_per_subject must be positive")
    for subject in subjects:
        source_root = root / "subjects" / subject
        if read_json(source_root / "selection.json") != manifest["subjects"][subject]:
            raise ValueError("campaign and subject selections differ")
    # Count all prior attempts across the campaign, including previously chosen
    # subjects not requested in this invocation. A later call cannot reset the
    # campaign-wide ceiling by selecting a different slice.
    history = {}
    for subject in MMLU_SUBJECTS:
        paths = tuple((root / "subjects" / subject).glob("items/*/*/attempt-*/request.json"))
        reservations = [read_json(path).get("reserved_tokens") for path in paths]
        if any(type(value) is not int or value <= 0 for value in reservations):
            raise ValueError("invalid historical API reservation")
        for path, reservation in zip(paths, reservations):
            response_path = path.parent / "response.json"
            if (response_path.is_file()
                    and reported_tokens(read_json(response_path)["body"]) > reservation):
                raise ValueError("historical response exceeded its token reservation; audit required")
        history[subject] = (len(paths), sum(reservations))
    max_possible_calls = (sum(row[0] for row in history.values())
                          + sum(max(0, config.max_calls - history[subject][0])
                                for subject in subjects))
    max_possible_tokens = (sum(row[1] for row in history.values())
                           + sum(max(0, config.max_reserved_tokens - history[subject][1])
                                 for subject in subjects))
    if max_possible_calls > max_total_calls or max_possible_tokens > max_total_reserved_tokens:
        raise ValueError("explicit campaign ceilings must cover history and all subject-local caps")
    results = {}
    for subject in subjects:
        runner = Pipeline(root / "subjects" / subject, config, client,
                          resilient=resilient)
        snapshot = runner.run(limit_per_subject,
                              progress=(lambda row, name=subject: progress({"subject": name, **row}))
                              if progress else None)
        results[subject] = {"paused": snapshot["paused"],
                            "attempt_count": snapshot["attempt_count"],
                            "reserved_tokens": snapshot["reserved_tokens"],
                            "status_counts": {status: sum(row["status"] == status
                                                          for row in snapshot["results"])
                                              for status in {row["status"] for row in snapshot["results"]}}}
        if snapshot["paused"]:
            break
    return {"subjects_requested": subjects, "subjects_run": results,
            "paused": any(row["paused"] for row in results.values()),
            "historical_calls_before_run": sum(row[0] for row in history.values()),
            "historical_reserved_tokens_before_run": sum(row[1] for row in history.values()),
            "max_total_calls": max_total_calls,
            "max_total_reserved_tokens": max_total_reserved_tokens}
