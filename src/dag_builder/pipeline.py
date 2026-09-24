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
        runtime_workers=None,
    ):
        if type(self) is Pipeline and config.prompt_version in (
            "gpqa-repair-v1",
            "gpqa-revision-v1",
        ):
            raise ValueError("repair protocol requires RepairPipeline")
        self.root = private_dir(root)
        self.config, self.client = config, client
        self.retry_safe_failures = retry_safe_failures
        self.isolate_uncertain_failures = isolate_uncertain_failures
        self.resilient = resilient
        # Runtime pressure testing may exceed the conservative frozen config
        # worker count; the caller still controls the actual safe ceiling.
        worker_limit = 32
        if runtime_workers is not None and (
            type(runtime_workers) is not int
            or not 1 <= runtime_workers <= worker_limit
        ):
            raise ValueError("invalid runtime worker count")
        self.runtime_workers = runtime_workers or config.workers
        self._new_uncertain_failures = 0
        self._budget_lock = threading.Lock()
        self._stop = threading.Event()
        self.calls = 0
        self.reserved_tokens = 0
        self._accounted_attempt_tokens = {}

    @staticmethod
    def _reported_tokens(response):
        total = response.get("usage", {}).get("total_tokens")
        return total if type(total) is int and total >= 0 else None

    def _attempt_tokens(self, attempt, request=None, response=None):
        request = request or read_json(attempt / "request.json")
        accounted = request["reserved_tokens"]
        response_file = attempt / "response.json"
        if response is None and response_file.exists():
            response = read_json(response_file)["body"]
        if response is not None:
            reported = self._reported_tokens(response)
            if reported is not None:
                accounted = max(accounted, reported)
        return accounted

    def _account_response(self, attempt, response):
        """Reconcile a returned or cached response with the global token budget."""
        accounted = self._attempt_tokens(attempt, response=response)
        with self._budget_lock:
            previous = self._accounted_attempt_tokens.get(attempt)
            if previous is None:
                self.calls += 1
                previous = 0
            if accounted > previous:
                self.reserved_tokens += accounted - previous
                self._accounted_attempt_tokens[attempt] = accounted
            if self.reserved_tokens > self.config.max_reserved_tokens:
                self._stop.set()

    def _cached_response(self, attempt, request_payload):
        request = read_json(attempt / "request.json")
        if request["payload"] != request_payload:
            raise ValueError("cached request does not match current payload")
        response = read_json(attempt / "response.json")["body"]
        self._account_response(attempt, response)
        return response

    def _restore_budget(self):
        self.calls = 0
        self.reserved_tokens = 0
        self._accounted_attempt_tokens = {}
        for path in self.root.glob("items/*/*/attempt-*/request.json"):
            request = read_json(path)
            attempt = path.parent
            accounted = self._attempt_tokens(attempt, request=request)
            self.calls += 1
            self.reserved_tokens += accounted
            self._accounted_attempt_tokens[attempt] = accounted
        if self.reserved_tokens > self.config.max_reserved_tokens:
            self._stop.set()

    def _reserve(self, path, request_payload):
        # Conservative byte-based input allowance, not a tokenizer measurement.
        # Output cap semantics must be checked against the selected API.
        allowance = (
            len(json.dumps(request_payload, ensure_ascii=False).encode())
            + 256
            + self.config.max_tokens
        )
        with self._budget_lock:
            if self._stop.is_set():
                raise CallFailure("paused")
            if (
                self.calls + 1 > self.config.max_calls
                or self.reserved_tokens + allowance > self.config.max_reserved_tokens
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
            self._accounted_attempt_tokens[path] = allowance

    def _call(self, directory, request_payload):
        if self.resilient:
            return self._call_resilient(directory, request_payload)
        for index in range(self.config.rate_limit_retries + 1):
            attempt = directory / f"attempt-{index:02d}"
            if (attempt / "response.json").exists():
                return self._cached_response(attempt, request_payload)
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
            self._account_response(attempt, response)
            return response
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
                    return self._cached_response(attempt, request_payload)
                if (attempt / "error.json").exists():
                    saved = read_json(attempt / "error.json")
                    if (
                        self.retry_safe_failures
                        and saved["category"] == "authentication"
                    ):
                        continue
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
            self._account_response(attempt, response)
            return response
        raise CallFailure("transient_retries_exhausted")

    def stage(self, stage, item, results):
        data = stage_input(
            stage,
            item,
            results,
            self.config.solution_source,
            self.config.prompt_version,
        )
        request_payload = payload(stage, data, self.config)
        return self.request_stage(
            stage,
            item,
            data,
            request_payload,
            lambda output: validate(stage, output, data, self.config.solution_source),
            native=stage == "solve"
            and self.config.prompt_version
            in ("mmlu-thinking-v1", "mmlu-thinking-v2"),
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
            if native:
                output = parse_native_solution(message)
            else:
                output = parse_object(message.get("content"))
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
            self._restore_budget()
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
            policy = {
                "resilient": self.resilient,
                "configured_workers": self.config.workers,
                "runtime_workers": self.runtime_workers,
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
            with ThreadPoolExecutor(max_workers=self.runtime_workers) as pool:
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
                "paused": any(r["status"] == "paused" for r in results)
                or self._stop.is_set(),
                "global_stop": self._stop.is_set(),
                "isolate_uncertain_failures": self.isolate_uncertain_failures,
                "new_uncertain_failures": self._new_uncertain_failures,
                "resilient": self.resilient,
                "runtime_workers": self.runtime_workers,
                "execution_policy": policy,
            }
            write_once(
                self.root / "invocations" / (digest(snapshot) + ".json"), snapshot
            )
            return snapshot
