"""Freeze official MMLU-57 test rows and the four existing original cohorts.

No DAG eligibility filter, content deduplication, reference explanation or
answer-dependent selection is applied. Public model input and grading stay apart.
"""
from collections import Counter
from pathlib import Path

from ..io import digest, read, save, sha256, verify
from .data import validate_prepared
from .schema import make_problem, require

MMLU_REVISION = "c30699e8356da336a370243923dbaf21066bb9fe"
SUBJECTS = tuple("""abstract_algebra anatomy astronomy business_ethics clinical_knowledge
college_biology college_chemistry college_computer_science college_mathematics
college_medicine college_physics computer_security conceptual_physics econometrics
electrical_engineering elementary_mathematics formal_logic global_facts high_school_biology
high_school_chemistry high_school_computer_science high_school_european_history
high_school_geography high_school_government_and_politics high_school_macroeconomics
high_school_mathematics high_school_microeconomics high_school_physics high_school_psychology
high_school_statistics high_school_us_history high_school_world_history human_aging
human_sexuality international_law jurisprudence logical_fallacies machine_learning
management marketing medical_genetics miscellaneous moral_disputes moral_scenarios nutrition
philosophy prehistory professional_accounting professional_law professional_medicine
professional_psychology public_relations security_studies sociology us_foreign_policy
virology world_religions""".split())
COUNTS = {"gpqa": 198, "gsm8k": 1319, "humaneval": 164, "livecodebench": 175, "mmlu": 14042}


def adapt_subject(rows: list[dict], subject: str, revision: str) -> tuple[list, dict]:
    require(subject in SUBJECTS and revision == MMLU_REVISION, "unfrozen MMLU source")
    problems, grading = [], {}
    for index, row in enumerate(rows):
        require(row.get("subject", subject) == subject, "source subject differs")
        choices, answer = row.get("choices"), row.get("answer")
        require(isinstance(choices, list) and len(choices) == 4 and
                type(answer) is int and 0 <= answer < 4, "invalid MMLU options/answer")
        identity = f"cais/mmlu:{revision}:{subject}:test:{index}"
        problem = make_problem(benchmark="mmlu", problem_id=f"mmlu:{subject}:test:{index}",
            subset=subject, source_id=identity, source_revision=revision,
            source_record_sha256=digest(row), question=row["question"], choices=choices)
        problems.append(problem)
        ambiguous = len(set(choices)) != 4
        grading[problem["problem_id"]] = {"kind": "choice", "value": chr(65 + answer),
            "outcome_eligible": not ambiguous,
            "ineligibility_reason": "duplicate_choice_text" if ambiguous else None}
    return problems, grading


def prepare(old_prepared: Path, source_manifest: Path, output: Path) -> dict:
    import pyarrow.parquet as pq
    old_manifest, old_problems = validate_prepared(old_prepared)
    sources = read(source_manifest)
    require(sources["dataset"] == "cais/mmlu" and sources["revision"] == MMLU_REVISION
            and sources["split"] == "test" and set(sources["subjects"]) == set(SUBJECTS),
            "all 57 official test subjects required")
    problems = [p for p in old_problems if p["benchmark"] != "mmlu"]
    old_grading = read(old_prepared / "grading/answers.json")
    grading = {p["problem_id"]: old_grading[p["problem_id"]] for p in problems}
    inventory = {}
    for subject in SUBJECTS:
        spec = sources["subjects"][subject]
        path = Path(spec["path"])
        require(path.is_file() and not path.is_symlink() and sha256(path) == spec["sha256"],
                "MMLU source hash mismatch: " + subject)
        rows = pq.read_table(path).to_pylist()
        require(len(rows) == spec["count"] and len(rows) > 0, "subject count mismatch")
        prepared, gold = adapt_subject(rows, subject, sources["revision"])
        problems.extend(prepared)
        grading.update(gold)
        inventory[subject] = {"count": len(rows), "sha256": spec["sha256"]}
    counts = dict(Counter(p["benchmark"] for p in problems))
    require(counts == COUNTS, "official full cohort count mismatch: " + str(counts))
    require(len(grading) == len(problems) == len({p["problem_id"] for p in problems}), "duplicate identity")
    require(not output.exists(), "prepared output already exists")
    output.mkdir(mode=0o700, parents=True)
    (output / "grading").mkdir(mode=0o700)
    save(output / "problems.json", sorted(problems, key=lambda p: (p["benchmark"], p["problem_id"])))
    save(output / "grading/answers.json", grading)
    manifest = {"schema_version": "pals_e3_greedy_prepared_v1", "scientific_evidence": True,
        "counts": counts, "subjects": inventory, "mmlu_revision": MMLU_REVISION,
        "source_manifest_sha256": sha256(source_manifest), "prior_prepared_sha256": sha256(old_prepared / "manifest.json"),
        "other_sources": {k: v for k, v in old_manifest["sources"].items() if k != "mmlu"},
        "files": {name: sha256(output / name) for name in ("problems.json", "grading/answers.json")}}
    save(output / "manifest.json", manifest)
    return manifest


def validate(folder: Path) -> tuple[dict, list]:
    manifest = read(folder / "manifest.json")
    require(manifest["schema_version"] in ("pals_e3_greedy_prepared_v1", "pals_e3_greedy_mock_v1"), "prepared version")
    verify(folder, manifest["files"])
    problems = read(folder / "problems.json")
    counts = dict(Counter(p["benchmark"] for p in problems))
    require(counts == manifest["counts"], "prepared counts differ")
    if manifest["scientific_evidence"]:
        require(counts == COUNTS and set(manifest["subjects"]) == set(SUBJECTS), "not full MMLU-57")
        actual = Counter(p["subset"] for p in problems if p["benchmark"] == "mmlu")
        require(dict(actual) == {s: r["count"] for s, r in manifest["subjects"].items()}, "subject counts differ")
    require(len(problems) == len({p["problem_id"] for p in problems}), "duplicate question")
    require(set(read(folder / "grading/answers.json")) == {p["problem_id"] for p in problems}, "grading coverage")
    return manifest, problems
