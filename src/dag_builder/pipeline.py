"""Bounded API-only pipeline with immutable attempts and explicit review gates."""

import hashlib
import json
import platform
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from .client import CallFailure
from .response_contract import check_response
from .math_answers import numeric_answers_equivalent
from .run_status import record_pause
from .schemas import (
    InvalidOutput,
    parse_native_solution,
    parse_object,
    public_question,
    require,
    text,
)
from .stages import (
    payload,
    prompt,
    reference_solution,
    stage_input,
    stages_for,
    validate,
)
from .storage import digest, private_dir, read_json, run_lock, write_once
from .validation import assemble, calculation_status


def now():
    return datetime.now(timezone.utc).isoformat()


def implementation():
    package = Path(__file__).resolve().parent
    hashes = {
        str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(package.rglob("*"))
        if p.suffix in (".py", ".md")
    }
    origin_path = package.parent / "snapshot_origin.json"
    if origin_path.exists():
        origin = read_json(origin_path)
        if origin["source_files"] != hashes:
            raise ValueError("frozen source snapshot changed")
        commit = origin["git_commit"]
    else:
        commit = _git_commit(package)
    return {
        "code_sha256": digest(hashes),
        "source_files": hashes,
        "git_commit": commit,
        "python": platform.python_version(),
        "platform": platform.platform(),
    }


def _git_commit(package):
    try:
        commit = subprocess.check_output(
            ["git", "-C", str(package), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = None
    return commit


def answer_matches(item, solution):
    """Use the task's declared answer contract without extracting from prose."""
    if item.get("task_type") == "gsm8k":
        return numeric_answers_equivalent(solution.get("answer"), item["gold_answer"])
    return solution.get("answer") == item["gold_answer"]


class Pipeline:
    def __init__(
        self,
        root,
        config,
        client,
        retry_safe_failures=False,
        isolate_uncertain_failures=False,
        resilient=False,
    ):
        if type(self) is Pipeline and config.prompt_version in (
            "gpqa-repair-v1",
            "gpqa-revision-v1",
        ):
            raise ValueError("repair protocol requires RepairPipeline")
        self.root = private_dir(root)
        if type(self) is Pipeline and (self.root / "recovery_manifest.json").exists():
            raise ValueError("recovery cohort requires HumanEvalRecoveryPipeline")
        self.config, self.client = config, client
        self.retry_safe_failures = retry_safe_failures
        self.isolate_uncertain_failures = isolate_uncertain_failures
        if resilient and (isolate_uncertain_failures or retry_safe_failures):
            raise ValueError(
                "resilient mode cannot be combined with legacy retry flags"
            )
        self.resilient = resilient
        self._new_uncertain_failures = 0
        self._budget_lock = threading.Lock()
        self._stop = threading.Event()
        # Also cancel not-yet-sent calls waiting in the shared client's limiter.
        bind_stop = getattr(client, "bind_stop_event", None)
        if callable(bind_stop):
            bind_stop(self._stop)
        self.calls = 0
        self.reserved_tokens = 0
        self.accounted_tokens = 0
        self._accounted_responses = set()
        self._contract_violations = set()

    def _restore_budget(self):
        self.calls = 0
        self.reserved_tokens = 0
        self.accounted_tokens = 0
        self._accounted_responses.clear()
        for path in self.root.glob("items/*/*/attempt-*/request.json"):
            request = read_json(path)
            self.calls += 1
            self.reserved_tokens += request["reserved_tokens"]
            self.accounted_tokens += request["reserved_tokens"]
            if (path.parent / "response.json").exists():
                self._check_response(
                    path.parent,
                    read_json(path.parent / "response.json")["body"],
                    raise_failure=False,
                )

    def _check_response(self, attempt, response, *, raise_failure=True):
        """Persist raw responses first; reject infra failures before stage parsing.

        Cached responses and budget restoration use the same checks. A violation
        cannot be bypassed with --resilient, resume, or a previously saved output.
        """
        request = read_json(attempt / "request.json")
        check = check_response(
            request["payload"], response, request["reserved_tokens"],
            strict=self.config.strict_response_contract,
            content_gated=self.config.content_gated_response,
        )
        with self._budget_lock:
            if attempt not in self._accounted_responses:
                self.accounted_tokens += (
                    check["accounted_tokens"] - request["reserved_tokens"]
                )
                self._accounted_responses.add(attempt)
            if check["violations"]:
                self._contract_violations.add(str(attempt.relative_to(self.root)))
                self._stop.set()
        if check["violations"] or self.config.content_gated_response:
            filename = "contract_check-v2.json" if self.config.content_gated_response else "contract_check-v1.json"
            write_once(attempt / filename, check)
        if check["violations"]:
            if raise_failure:
                raise CallFailure("response_contract_violation")
        return response

    def _reserve(self, path, request_payload):
        # Conservative byte-based input allowance, not a tokenizer measurement.
        # Output cap semantics must be checked against the selected API.
        allowance = (
            len(json.dumps(request_payload, ensure_ascii=False).encode())
            + 256
            + request_payload["max_tokens"]
        )
        with self._budget_lock:
            if self._stop.is_set():
                raise CallFailure("paused")
            if (
                self.calls + 1 > self.config.max_calls
                or self.accounted_tokens + allowance > self.config.max_reserved_tokens
            ):
                self._stop.set()
                raise CallFailure("budget_exhausted")
            write_once(
                path / "request.json",
                {
                    "started_at": now(),
                    "payload": request_payload,
                    "reserved_tokens": allowance,
                },
            )
            self.calls += 1
            self.reserved_tokens += allowance
            self.accounted_tokens += allowance

    def _call(self, directory, request_payload):
        if self.resilient:
            return self._call_resilient(directory, request_payload)
        for index in range(self.config.rate_limit_retries + 1):
            attempt = directory / f"attempt-{index:02d}"
            if (attempt / "response.json").exists():
                cached = read_json(attempt / "request.json")
                if cached["payload"] != request_payload:
                    raise ValueError("cached request does not match current payload")
                return self._check_response(
                    attempt, read_json(attempt / "response.json")["body"]
                )
            if (attempt / "error.json").exists():
                if read_json(attempt / "request.json")["payload"] != request_payload:
                    raise ValueError("cached failure request mismatch")
                error = read_json(attempt / "error.json")
                if error["category"] == "rate_limit":
                    continue
                if self.retry_safe_failures and error["category"] == "authentication":
                    continue
                raise CallFailure(error["category"], error.get("http_status"))
            if (attempt / "request.json").exists():
                raise CallFailure("uncertain_remote_state")
            self._reserve(attempt, request_payload)
            try:
                response = self.client.complete(request_payload)
            except CallFailure as error:
                write_once(
                    attempt / "error.json",
                    {
                        "ended_at": now(),
                        "category": error.category,
                        "http_status": error.status,
                        "transport_kind": error.transport_kind,
                    },
                )
                if (
                    error.category == "rate_limit"
                    and index < self.config.rate_limit_retries
                ):
                    time.sleep(min(2 ** (index + 1), 8))
                    continue
                if (
                    error.category == "uncertain_remote_state"
                    and self.isolate_uncertain_failures
                ):
                    # No retries: isolate the item, but stop a persistent outage.
                    with self._budget_lock:
                        self._new_uncertain_failures += 1
                        if self._new_uncertain_failures >= 3:
                            self._stop.set()
                else:
                    self._stop.set()
                raise
            write_once(attempt / "response.json", {"ended_at": now(), "body": response})
            return self._check_response(attempt, response)
        raise CallFailure("retry_limit_reached")

    def _call_resilient(self, directory, request_payload):
        """At most four lifetime attempts per stage, including cached failures.

        Transport retries are independent of semantic quality: the first returned
        response is always used, even when it fails downstream validation. Unknown
        calls may have been billed and remain part of the restored global budget.
        """
        transient = {"uncertain_remote_state", "rate_limit"}
        delays = (10, 30, 60)
        for index in range(4):
            attempt = directory / f"attempt-{index:02d}"
            request_file = attempt / "request.json"
            if request_file.exists():
                if read_json(request_file)["payload"] != request_payload:
                    raise ValueError("cached request does not match current payload")
                if (attempt / "response.json").exists():
                    return self._check_response(
                        attempt, read_json(attempt / "response.json")["body"]
                    )
                if (attempt / "error.json").exists():
                    saved = read_json(attempt / "error.json")
                    if saved["category"] not in transient:
                        raise CallFailure(saved["category"], saved.get("http_status"))
                # A request without response/error is also unknown, not free.
                continue
            # Interruptible backoff; no mutex held while waiting.
            if index and self._stop.wait(delays[index - 1]):
                raise CallFailure("paused")
            self._reserve(attempt, request_payload)
            try:
                response = self.client.complete(request_payload)
            except CallFailure as error:
                write_once(
                    attempt / "error.json",
                    {
                        "ended_at": now(),
                        "category": error.category,
                        "http_status": error.status,
                        "transport_kind": error.transport_kind,
                    },
                )
                if error.category in transient:
                    if error.category == "uncertain_remote_state":
                        with self._budget_lock:
                            self._new_uncertain_failures += 1
                    continue
                raise
            write_once(attempt / "response.json", {"ended_at": now(), "body": response})
            return self._check_response(attempt, response)
        raise CallFailure("transient_retries_exhausted")

    def stage(self, stage, item, results):
        data = stage_input(stage, item, results, self.config.solution_source)
        request_payload = payload(stage, data, self.config)
        return self.request_stage(
            stage,
            item,
            data,
            request_payload,
            lambda output: validate(stage, output, data, self.config.solution_source,
                                    prompt_version=self.config.prompt_version),
            native=stage == "solve"
            and self.config.prompt_version == "mmlu-thinking-v1",
        )

    def request_stage(
        self, stage, item, data, request_payload, validator, native=False
    ):
        """Persist one semantic response; transport retries never resample content."""
        directory = self.root / "items" / item["item_id"] / stage
        expected = {"input": data, "payload_sha256": digest(request_payload)}
        write_once(directory / "input.json", expected)
        if (directory / "validation.json").exists():
            raise InvalidOutput(read_json(directory / "validation.json")["reason"])
        response = self._call(directory, request_payload)
        try:
            choices = response.get("choices", [])
            if len(choices) != 1 or choices[0].get("finish_reason") != "stop":
                raise InvalidOutput(
                    "missing, ambiguous or non-stop completion; no truncated text is accepted"
                )
            message = choices[0].get("message", {})
            require(isinstance(message, dict), "invalid assistant message")
            if native:
                output = parse_native_solution(message)
            else:
                output = parse_object(message.get("content"))
                if self.config.prompt_version in ("humaneval-reference-v4", "humaneval-reference-v5") and stage == "atomize":
                    from .humaneval_quality import normalize_sources
                    original = output
                    output, changes = normalize_sources(output)
                    write_once(directory / "normalization.json", {
                        "policy": "humaneval-source-aliases-v1", "changes": changes,
                        "raw_parsed_sha256": digest(original), "normalized_sha256": digest(output),
                    })
                validator(output)
            if stage == "solve" and self.config.solution_source in (
                "answer_conditioned_generation",
                "diagnostic_repair_generation",
            ):
                output = {
                    **output,
                    "answer": item["gold_answer"],
                    "answer_source": "dataset_reference",
                    "solution_source": self.config.solution_source,
                }
        except (InvalidOutput, TypeError, KeyError, AttributeError) as error:
            reason = (
                str(error)
                if isinstance(error, InvalidOutput)
                else "invalid response schema"
            )
            write_once(
                directory / "validation.json",
                {"status": "needs_review", "reason": reason},
            )
            raise InvalidOutput(reason) from None
        write_once(directory / "output.json", output)
        return output

    def official_solution(self, item):
        """Materialize GSM8K's source rationale without spending a solve API call."""
        require(item.get("task_type") == "gsm8k", "official rationale requires gsm8k")
        raw_answer = item.get("raw_answer")
        require(
            text(raw_answer) and "\n####" in raw_answer, "missing official rationale"
        )
        rationale = (
            item.get("canonical_rationale")
            if self.config.solution_source == "canonical_official_rationale"
            else raw_answer.rsplit("\n####", 1)[0].strip()
        )
        require(text(rationale), "empty official rationale")
        directory = self.root / "items" / item["item_id"] / "solve"
        write_once(
            directory / "input.json",
            {
                "input": {
                    "question": public_question(item),
                    "reference_answer": item["gold_answer"],
                    "raw_answer_sha256": digest(raw_answer),
                    "canonical_rationale_sha256": (
                        digest(rationale)
                        if self.config.solution_source == "canonical_official_rationale"
                        else None
                    ),
                },
                "source": self.config.solution_source,
            },
        )
        output = {
            "answer": item["gold_answer"],
            "answer_source": "dataset_reference",
            "rationale": rationale,
            "solution_source": self.config.solution_source,
        }
        write_once(directory / "output.json", output)
        return output

    def process(self, item, through="review_dag"):
        item_dir = self.root / "items" / item["item_id"]
        results = {}
        current = stages_for(self.config)[0]
        try:
            stages = stages_for(self.config)
            if self.config.solution_source in (
                "official_rationale",
                "canonical_official_rationale",
            ):
                results["solve"] = self.official_solution(item)
                if through == "solve":
                    return {
                        "item_id": item["item_id"],
                        "status": "stage_complete",
                        "stage": "solve",
                    }
                stages = stages[1:]
            for current in stages:
                if self._stop.is_set():
                    return {
                        "item_id": item["item_id"],
                        "status": "paused",
                        "stage": current,
                    }
                results[current] = self.stage(current, item, results)
                if (
                    current == "solve"
                    and self.config.solution_source == "independent_generation"
                    and not answer_matches(item, results[current])
                ):
                    return self._finish(
                        item,
                        "rejected",
                        current,
                        "answer_label_mismatch; no repair or regeneration",
                    )
                if current in ("review_solution", "review_dag"):
                    decision = results[current]["decision"]
                    if decision != "accept":
                        return self._finish(
                            item,
                            "rejected" if decision == "reject" else "needs_review",
                            current,
                            results[current]["reason"],
                        )
                if current == through and through != "review_dag":
                    return {
                        "item_id": item["item_id"],
                        "status": "stage_complete",
                        "stage": current,
                    }
            nodes = assemble(
                results["atomize"]["nodes"], results["dependencies"], results["justify"]
            )
            dag = {
                "schema_version": "reference_dag_v1",
                "item_id": item["item_id"],
                "source": item,
                "reference_solution": reference_solution(item, results),
                "nodes": nodes,
                "solution_review": results["review_solution"],
                "dag_review": results["review_dag"],
                "calculation_check": calculation_status(),
                "quality_status": "model_reviewed_pending_human_review",
                "limitation": (
                    "the dataset answer conditions rationale generation and same-model review is not independent validation"
                    if self.config.solution_source
                    in (
                        "answer_conditioned_generation",
                        "diagnostic_repair_generation",
                    )
                    else "the DAG is derived from the dataset rationale and same-model review is not independent validation"
                    if self.config.solution_source
                    in (
                        "official_rationale",
                        "canonical_official_rationale",
                    )
                    else "generation and semantic review use the same model; not independent gold validation"
                ),
            }
            if "structure_solution" in results:
                dag["native_solution"] = results["solve"]
                dag["construction_protocol"] = self.config.prompt_version
            if self.config.task_type == "gsm8k":
                dag["solution_source"] = self.config.solution_source
            if self.config.task_type == "gpqa":
                dag["construction_protocol"] = self.config.prompt_version
                dag["limitation"] = (
                    "official expert explanation transformed into a synthetic DAG; "
                    "DAG construction and semantic review use the same model; "
                    "dependencies are not official gold annotations"
                )
            if self.config.task_type == "humaneval":
                dag["construction_protocol"] = self.config.prompt_version
                dag["solution_source"] = self.config.solution_source
                dag["limitation"] = (
                    "model-generated algorithm explanation conditioned on official reference code; "
                    "same-model semantic review, not official gold CoT; code/tests not executed"
                )
                dag["calculation_check"] = {"status": "not_checked", "reason": "no code execution in DAG builder"}
            provenance = self.recovery_provenance(item)
            if provenance is not None:
                dag["recovery_provenance"] = provenance
            write_once(item_dir / "dag.json", dag)
            return self._finish(
                item,
                "model_accepted",
                "review_dag",
                "awaiting explicit human review",
                digest(dag),
            )
        except InvalidOutput as error:
            return self._finish(item, "needs_review", current, str(error))
        except CallFailure as error:
            isolated = (
                self.resilient and error.category == "transient_retries_exhausted"
            )
            if not isolated and not (
                self.isolate_uncertain_failures
                and error.category == "uncertain_remote_state"
            ):
                self._stop.set()
            # Do not publish a scientific rejection for an infrastructure error.
            return {
                "item_id": item["item_id"],
                "status": "paused",
                "stage": current,
                "reason": error.category,
            }

    def recovery_provenance(self, item):
        return None

    def _finish(self, item, status, stage, reason, dag_sha256=None):
        result = {
            "item_id": item["item_id"],
            "status": status,
            "stage": stage,
            "reason": reason,
            "dag_sha256": dag_sha256,
        }
        write_once(self.root / "items" / item["item_id"] / "result.json", result)
        return result

    def run(self, limit=None, progress=None, through="review_dag"):
        if through not in stages_for(self.config):
            raise ValueError("unknown stopping stage")
        with run_lock(self.root):
            items = read_json(self.root / "items.json")
            selection = read_json(self.root / "selection.json")
            if not items or any(
                not re.fullmatch(r"[0-9a-f]{20}", i["item_id"]) for i in items
            ):
                raise ValueError("invalid immutable item IDs")
            if len({i["item_id"] for i in items}) != len(items):
                raise ValueError("duplicate item IDs")
            if [i["item_id"] for i in items] != selection["selected_ids"]:
                raise ValueError("selection and item manifest disagree")
            if any(i.get("task_type", "mmlu") != self.config.task_type for i in items):
                raise ValueError("run config task_type does not match selected items")
            write_once(self.root / "run_config.json", self.config.to_dict())
            write_once(self.root / "implementation.json", implementation())
            self._restore_budget()
            policy = {
                "resilient": self.resilient,
                "isolate_uncertain_failures": self.isolate_uncertain_failures,
                "retry_safe_failures": self.retry_safe_failures,
                "max_lifetime_stage_attempts": 4 if self.resilient else None,
                "transient_backoff_seconds": [10, 30, 60] if self.resilient else [],
            }
            write_once(self.root / "policies" / (digest(policy) + ".json"), policy)
            write_once(
                self.root / "inputs_manifest.json",
                {
                    "items_sha256": digest(items),
                    "selection_sha256": digest(selection),
                    "prompts": {
                        s: digest(
                            prompt(
                                s,
                                self.config.prompt_version,
                                self.config.task_type,
                                self.config.solution_source,
                            )
                        )
                        for s in stages_for(self.config)
                    },
                },
            )
            selected = items if limit is None else items[:limit]
            if limit is not None and (type(limit) is not int or limit <= 0):
                raise ValueError("limit must be positive")
            started, results = now(), []
            with ThreadPoolExecutor(max_workers=self.config.workers) as pool:
                futures = [
                    pool.submit(self.process, item, through) for item in selected
                ]
                for future in as_completed(futures):
                    result = future.result()
                    results.append(result)
                    record_pause(self.root, result, now())
                    if progress:
                        progress({k: result[k] for k in ("item_id", "status", "stage")})
            snapshot = {
                "started_at": started,
                "ended_at": now(),
                "limit": limit,
                "through": through,
                "results": sorted(results, key=lambda r: r["item_id"]),
                "attempt_count": self.calls,
                "reserved_tokens": self.reserved_tokens,
                "accounted_tokens": self.accounted_tokens,
                "response_contract_violations": sorted(self._contract_violations),
                "paused": any(r["status"] == "paused" for r in results)
                or self._stop.is_set(),
                "global_stop": self._stop.is_set(),
                "isolate_uncertain_failures": self.isolate_uncertain_failures,
                "new_uncertain_failures": self._new_uncertain_failures,
                "resilient": self.resilient,
                "execution_policy": policy,
            }
            write_once(
                self.root / "invocations" / (digest(snapshot) + ".json"), snapshot
            )
            return snapshot
