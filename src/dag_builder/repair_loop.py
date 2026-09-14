"""Two additional semantic revisions maximum; no accept-until-success loop."""

from .client import CallFailure
from .output_normalization import diagnose, normalize
from .pipeline import Pipeline
from .review_issues import (
    AUDIT_CONTRACT,
    issue_packet,
    validate_adjudication,
    validate_audit,
)
from .revision import candidate_diff, validate_revision
from .schemas import InvalidOutput, require
from .stages import payload, reference_solution, stage_input
from .storage import digest, read_json, write_once
from .validation import assemble, calculation_status


class RevisionPipeline(Pipeline):
    def __init__(self, root, config, client, **kwargs):
        if config.prompt_version != "gpqa-revision-v1":
            raise ValueError("revision loop requires gpqa-revision-v1")
        super().__init__(root, config, client, **kwargs)

    def run(self, limit=None, progress=None, through="audit"):
        if through != "audit":
            raise ValueError("revision run must include audit")
        return super().run(limit, progress, through)

    def _stage(self, item, round_no, stage, data, validator):
        name = f"round-{round_no:02d}-{stage}"
        location = self.root / "items" / item["item_id"] / name

        def checked(value):
            normalized, operations = (
                normalize(value) if stage == "revise" else (value, [])
            )
            write_once(
                location / "normalization.json",
                {
                    "operations": operations,
                    "raw_parsed_sha256": digest(value),
                    "normalized_sha256": digest(normalized),
                },
            )
            validator(normalized)
            if operations:
                value.clear()
                value.update(normalized)

        return self.request_stage(
            name, item, data, payload(stage, data, self.config), checked
        )

    def _audit(self, item, round_no, reference, candidate):
        # Deliberately omit previous verdicts, edit rationales and producer appeals.
        return self._stage(
            item,
            round_no,
            "audit",
            dict(reference, candidate=candidate),
            lambda v: validate_audit(v, reference, candidate),
        )

    def _adjudicate(self, item, round_no, reference, candidate, claim):
        return self._stage(
            item,
            round_no,
            "adjudicate",
            dict(reference, candidate=candidate, disputed_claim=claim),
            lambda v: validate_adjudication(v, reference, candidate),
        )

    def _accepted(self, item, candidate, audit, round_no, baseline):
        directory = self.root / "items" / item["item_id"]
        dag = {
            "schema_version": "reference_dag_v1",
            "item_id": item["item_id"],
            "source": item,
            "reference_solution": reference_solution(item, {}),
            "nodes": assemble(
                candidate["nodes"],
                {"parents": candidate["parents"]},
                {"justifications": candidate["justifications"]},
            ),
            "construction_protocol": "gpqa-revision-v1",
            "audit_contract": AUDIT_CONTRACT,
            "additional_revision_round": round_no,
            "dag_review": audit,
            "calculation_check": calculation_status(),
            "baseline_sha256": digest(baseline),
            "quality_status": "repaired_model_reviewed_pending_human_review",
            "requested_model": self.config.model,
            "limitation": "Same-model fresh-context audit; backend version may be unknown; not human verified or official gold. Original reference unchanged.",
        }
        write_once(directory / "dag.json", dag)
        return self._finish(
            item,
            "repaired_model_accepted",
            f"round-{round_no:02d}-audit",
            "candidate accepted; pending human review",
            digest(dag),
        )

    def process(self, item, through="audit"):
        directory = self.root / "items" / item["item_id"]
        baseline = read_json(directory / "baseline.json")
        selection = read_json(self.root / "selection.json")
        require(
            selection.get("protocol") == "gpqa-revision-v1"
            and selection.get("max_additional_rounds") == 2,
            "invalid bounded revision manifest",
        )
        require(
            digest(item) == selection["selected_item_sha256"][item["item_id"]],
            "source changed",
        )
        require(
            digest(baseline) == selection["baseline_sha256"][item["item_id"]],
            "baseline changed",
        )
        if (directory / "result.json").exists():
            return read_json(directory / "result.json")
        reference = stage_input("review_solution", item, {})
        candidate = baseline["candidate"]
        current = "round-00-audit"
        try:
            faults = diagnose(candidate, reference)
            write_once(
                directory / "initial_diagnostics.json",
                {"faults": faults, "normalization": baseline.get("normalization", [])},
            )
            issues, adjudication = [], None
            if not faults:
                audit = self._audit(item, 0, reference, candidate)
                if audit["decision"] == "accept":
                    return self._accepted(item, candidate, audit, 0, baseline)
                issues = issue_packet(audit)
                if baseline.get("role") == "control":
                    return self._finish(
                        item,
                        "revision_control_disagreement",
                        current,
                        "previously accepted control did not pass; no control edits allowed",
                    )
            elif baseline.get("role") == "control":
                return self._finish(
                    item,
                    "revision_control_disagreement",
                    "initial_diagnostics",
                    "control failed structural validation; not edited",
                )
            elif candidate is None and baseline.get("source_dispute"):
                current = "round-00-adjudicate"
                adjudication = self._adjudicate(
                    item,
                    0,
                    reference,
                    None,
                    {"legacy_claim_untrusted": baseline["prior_reason"]},
                )
                if adjudication["decision"] != "revisable":
                    return self._finish(
                        item,
                        "revision_source_disputed",
                        current,
                        adjudication["reason"],
                    )

            seen = {digest(candidate)} if candidate else set()
            previous_issue_signature = None
            for round_no in (1, 2):
                current = f"round-{round_no:02d}-revise"
                data = dict(
                    reference,
                    candidate=candidate,
                    issues=issues,
                    structural_faults=faults,
                    source_reassessment=adjudication,
                )
                proposal = self._stage(
                    item,
                    round_no,
                    "revise",
                    data,
                    lambda v, previous=candidate, packet=issues: validate_revision(
                        v, reference, previous, packet
                    ),
                )
                write_once(
                    directory / f"round-{round_no:02d}" / "revision.json", proposal
                )
                if proposal["action"] != "revise":
                    current = f"round-{round_no:02d}-adjudicate"
                    verdict = self._adjudicate(
                        item,
                        round_no,
                        reference,
                        candidate,
                        {"issues": issues, "producer_reply_untrusted": proposal},
                    )
                    status = (
                        "revision_source_disputed"
                        if verdict["decision"] == "source_disputed"
                        else "revision_review_disputed"
                    )
                    return self._finish(
                        item,
                        status,
                        current,
                        verdict["reason"]
                        + "; adjudication is not candidate acceptance",
                    )
                updated = proposal["candidate"]
                write_once(
                    directory / f"round-{round_no:02d}" / "diff.json",
                    candidate_diff(candidate, updated),
                )
                write_once(
                    directory / f"round-{round_no:02d}" / "candidate.json", updated
                )
                if digest(updated) in seen:
                    return self._finish(
                        item,
                        "revision_no_progress",
                        current,
                        "unchanged or cyclic candidate; no repeat audit",
                    )
                seen.add(digest(updated))
                candidate, faults = updated, []
                current = f"round-{round_no:02d}-audit"
                audit = self._audit(item, round_no, reference, candidate)
                write_once(directory / f"round-{round_no:02d}" / "review.json", audit)
                if audit["decision"] == "accept":
                    return self._accepted(item, candidate, audit, round_no, baseline)
                issues = issue_packet(audit)
                signature = digest(
                    sorted(
                        (
                            i["code"],
                            sorted((e["field"], e["quote"]) for e in i["evidence"]),
                        )
                        for i in issues
                    )
                )
                if signature == previous_issue_signature:
                    return self._finish(
                        item,
                        "revision_no_progress",
                        current,
                        "same evidenced issues persist across two revised candidates",
                    )
                previous_issue_signature = signature
            return self._finish(
                item,
                "revision_round_limit",
                current,
                "two additional revision rounds exhausted; retain unresolved evidence",
            )
        except InvalidOutput as error:
            return self._finish(item, "revision_protocol_error", current, str(error))
        except CallFailure as error:
            if error.category != "transient_retries_exhausted":
                self._stop.set()
            return {
                "item_id": item["item_id"],
                "status": "paused",
                "stage": current,
                "reason": error.category,
            }
