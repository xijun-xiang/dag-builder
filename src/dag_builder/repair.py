"""One semantic repair, fresh justifications, then a fresh-context final audit.

Uses Pipeline's immutable request store, budget and transport retry machinery.
No failed semantic response is retried. Original runs are never changed.
"""

from .client import CallFailure
from .pipeline import Pipeline
from .repair_graph import (
    topological_repair,
    validate_candidate,
    validate_proposal,
    validate_repair_review,
)
from .schemas import InvalidOutput
from .stages import payload, reference_solution, stage_input
from .storage import digest, read_json, write_once
from .validation import assemble, calculation_status, validate_justifications


class RepairPipeline(Pipeline):
    def __init__(self, root, config, client, **kwargs):
        if config.task_type != "gpqa" or config.prompt_version != "gpqa-repair-v1":
            raise ValueError("RepairPipeline requires gpqa-repair-v1")
        super().__init__(root, config, client, **kwargs)

    def run(self, limit=None, progress=None, through="review_repair"):
        if through != "review_repair":
            raise ValueError("repair must include its final audit")
        return super().run(limit=limit, progress=progress, through=through)

    def call_stage(self, stage, item, data, validator):
        return self.request_stage(
            stage, item, data, payload(stage, data, self.config), validator
        )

    def _candidate(self, item, reference, baseline):
        directory = self.root / "items" / item["item_id"]
        nodes = baseline["stage_outputs"].get("atomize", {}).get("nodes")
        invalid = (
            baseline["failed_completions"].get("dependencies", {}).get("parsed", {})
        )
        # Automatic changes are restricted to the exact numbering failure case.
        if (
            baseline["result"].get("stage") == "dependencies"
            and baseline["result"].get("reason") == "unknown, self or future dependency"
            and nodes is not None
        ):
            try:
                candidate, renumber = topological_repair(
                    nodes, invalid.get("parents"), reference
                )
            except InvalidOutput as error:
                write_once(
                    directory / "topology.json",
                    {"status": "not_applicable", "reason": str(error)},
                )
            else:
                audit = {
                    "status": "reordered_only",
                    "renumber": renumber,
                    "candidate": candidate,
                }
                write_once(directory / "topology.json", audit)
                return {
                    "decision": "candidate",
                    "candidate": candidate,
                    "route": "deterministic_topology",
                    "changes": [
                        "Only node numbering/order changed; all statements and graph edges preserved."
                    ],
                }
        data = dict(reference, original=baseline)
        proposal = self.call_stage(
            "repair", item, data, lambda value: validate_proposal(value, reference)
        )
        return dict(proposal, route="model_adjudication")

    def process(self, item, through="review_repair"):
        directory = self.root / "items" / item["item_id"]
        baseline = read_json(directory / "baseline.json")
        selection = read_json(self.root / "selection.json")
        if (
            selection.get("protocol") != "gpqa-repair-v1"
            or selection.get("semantic_repair_round_limit") != 1
        ):
            raise ValueError("missing one-round repair manifest")
        if digest(baseline) != selection["baseline_sha256"][item["item_id"]]:
            raise ValueError("baseline changed after repair selection")
        if digest(item) != selection["selected_item_sha256"][item["item_id"]]:
            raise ValueError("official source changed after repair selection")
        if baseline["result"]["status"] not in ("needs_review", "rejected"):
            raise ValueError("only first-pass semantic failures are repairable")
        current = "repair"
        try:
            reference = stage_input("review_solution", item, {})
            proposal = self._candidate(item, reference, baseline)
            if proposal["decision"] != "candidate":
                return self._finish(
                    item, "repair_" + proposal["decision"], current, proposal["reason"]
                )
            candidate = proposal["candidate"]
            validate_candidate(candidate, reference)
            write_once(directory / "candidate.json", candidate)
            write_once(
                directory / "repair_audit.json",
                {
                    "round": 1,
                    "route": proposal["route"],
                    "changes": proposal["changes"],
                    "baseline_sha256": digest(baseline),
                    "candidate_sha256": digest(candidate),
                    "before": baseline["stage_outputs"],
                    "failed_before": baseline["failed_completions"],
                    "after": candidate,
                },
            )
            # Never reuse old justifications after changing a graph.
            current = "justify"
            data = dict(reference, **candidate)
            justifications = self.call_stage(
                current,
                item,
                data,
                lambda value: validate_justifications(value, candidate["nodes"]),
            )
            # No repair rationale, original rejection or previous verdict enters this call.
            current = "review_repair"
            audit_input = dict(data, justifications=justifications["justifications"])
            review = self.call_stage(current, item, audit_input, validate_repair_review)
            if review["decision"] != "accept":
                status = (
                    "repair_rejected"
                    if review["decision"] == "reject"
                    else "repair_needs_review"
                )
                return self._finish(item, status, current, review["reason"])
            dag = {
                "schema_version": "reference_dag_v1",
                "item_id": item["item_id"],
                "source": item,
                "reference_solution": reference_solution(item, {}),
                "nodes": assemble(
                    candidate["nodes"],
                    {"parents": candidate["parents"]},
                    justifications,
                ),
                "construction_protocol": "gpqa-repair-v1",
                "repair_round": 1,
                "original_status": baseline["result"]["status"],
                "baseline_sha256": digest(baseline),
                "repair_audit_sha256": digest(
                    read_json(directory / "repair_audit.json")
                ),
                "dag_review": review,
                "calculation_check": calculation_status(),
                "quality_status": "repaired_model_reviewed_pending_human_review",
                "limitation": "fresh-context review by the same model; not independent verification, not official gold; source explanation unchanged",
            }
            write_once(directory / "dag.json", dag)
            return self._finish(
                item,
                "repaired_model_accepted",
                current,
                "one-round repair accepted; awaiting human review",
                digest(dag),
            )
        except InvalidOutput as error:
            return self._finish(item, "repair_needs_review", current, str(error))
        except CallFailure as error:
            if error.category != "transient_retries_exhausted":
                self._stop.set()
            return {
                "item_id": item["item_id"],
                "status": "paused",
                "stage": current,
                "reason": error.category,
            }
