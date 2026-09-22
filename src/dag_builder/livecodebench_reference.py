"""One score-blind reference candidate per frozen task; no code execution here."""

import ast

from .client import CallFailure
from .pipeline import Pipeline
from .schemas import InvalidOutput, require, text
from .stages import prompt, request_controls
from .storage import digest, read_json, write_once


def public_input(item):
    # Allowlist, never copy an entire source record into an API request.
    require(item.get("task_type") == "livecodebench", "wrong reference task type")
    return {key: item[key] for key in (
        "question", "starter_code", "io_type", "entry_point", "public_examples")}


def validate_reference(value, item):
    require(isinstance(value, dict) and set(value) == {"language", "code", "rationale"},
            "reference must contain exactly language, code, rationale")
    require(value["language"] == "python3", "Python 3 reference required")
    require(text(value["code"]) and text(value["rationale"]), "empty reference field")
    require(len(value["code"].encode()) <= 200000, "reference code too large")
    require("```" not in value["code"], "fenced code is not a reference program")
    try:
        tree = ast.parse(value["code"])
    except (SyntaxError, ValueError, RecursionError) as error:
        raise InvalidOutput("invalid reference Python syntax") from error
    if item["io_type"] == "functional":
        solutions = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Solution"]
        require(len(solutions) == 1, "exactly one Solution class required")
        methods = [n for n in solutions[0].body if isinstance(n, ast.FunctionDef)
                   and n.name == item["entry_point"]]
        require(len(methods) == 1, "reference method does not match frozen entry point")


class LiveCodeBenchReferencePipeline(Pipeline):
    def run(self, limit=None, progress=None, through="reference_code"):
        require(through == "reference_code", "execution verification required before DAG stages")
        manifest = read_json(self.root / "prepared-manifest.json")
        require(digest(read_json(self.root / "items.json")) == manifest["items_sha256"],
                "prepared items changed")
        require(digest(read_json(self.root / "selection.json")) == manifest["selection_sha256"],
                "prepared selection changed")
        return super().run(limit, progress, through)

    def process(self, item, through="reference_code"):
        import json
        stage = "reference_code"
        data = public_input(item)
        request = request_controls(self.config)
        request["messages"] = [
            {"role": "system", "content": prompt(stage, self.config.prompt_version, self.config.task_type)},
            {"role": "user", "content": json.dumps(data, ensure_ascii=False, sort_keys=True)}]
        try:
            output = self.request_stage(stage, item, data, request,
                                        lambda value: validate_reference(value, item))
            write_once(self.root / "items" / item["item_id"] / "reference.json", {
                "item_id": item["item_id"], "origin": "model_generated_candidate",
                "output_sha256": digest(output), "tests_sha256": item["tests_sha256"],
                "source_content_sha256": item["source_content_sha256"],
                "code_sha256": digest(output["code"]),
                "reference_execution": "not_executed", "not_official_gold": True})
            return self._finish(item, "reference_candidate", stage,
                                "syntax passed; isolated official tests and DAG review still required")
        except InvalidOutput as error:
            return self._finish(item, "needs_review", stage, str(error))
        except CallFailure as error:
            if not (self.resilient and error.category == "transient_retries_exhausted"):
                self._stop.set()
            return {"item_id": item["item_id"], "status": "paused", "stage": stage, "reason": error.category}
