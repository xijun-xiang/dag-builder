"""Validated non-secret run configuration, frozen before any paid request."""

from dataclasses import asdict, dataclass
from urllib.parse import urlparse

from .storage import read_json


@dataclass(frozen=True)
class Config:
    base_url: str = "https://proxy.infix-ai.xyz/v1/"
    model: str = "deepseek-v4-flash"
    key_env: str = "JUDGE_API_KEY"
    temperature: float = 0.0
    max_tokens: int = 8192
    timeout_seconds: int = 180
    workers: int = 2
    max_calls: int = 220
    max_reserved_tokens: int = 2500000
    rate_limit_retries: int = 2
    prompt_version: str = "v1"
    response_format: str | None = None
    task_type: str = "mmlu"
    thinking: str | None = None
    reasoning_effort: str | None = None
    solution_source: str = "independent_generation"
    strict_response_contract: bool = False
    tls_max_version: str | None = None
    transport: str = "urllib"
    content_gated_response: bool = False

    def __post_init__(self):
        url = urlparse(self.base_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "base_url must be an HTTPS URL without credentials or query"
            )
        for name in (
            "max_tokens",
            "timeout_seconds",
            "workers",
            "max_calls",
            "max_reserved_tokens",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError("positive integer configuration required")
        if (
            type(self.rate_limit_retries) is not int
            or not 0 <= self.rate_limit_retries <= 2
        ):
            raise ValueError("rate_limit_retries must be 0..2")
        worker_limit = 32 if self.task_type in ("gpqa", "humaneval", "livecodebench") else 6
        if self.workers > worker_limit:
            raise ValueError(
                f"at most {worker_limit} workers are supported for this task"
            )
        if self.task_type not in ("mmlu", "gsm8k", "gpqa", "humaneval", "livecodebench"):
            raise ValueError("unknown task_type")
        versions = {
            "mmlu": ("v1", "mmlu-thinking-v1"),
            "gsm8k": ("gsm8k-v1",),
            "gpqa": ("gpqa-reference-v1", "gpqa-repair-v1", "gpqa-revision-v1"),
            "humaneval": ("humaneval-reference-v1", "humaneval-reference-v2", "humaneval-reference-v3", "humaneval-reference-v4", "humaneval-reference-v5"),
            "livecodebench": ("livecodebench-reference-v1", "livecodebench-dag-v1", "livecodebench-editorial-pilot-v1", "calibri-lcb-normalize-v1", "calibri-lcb-normalize-v2", "calibri-lcb-normalize-v3", "calibri-lcb-repair-v1", "calibri-lcb-repair-v2", "t2ance-lcb-normalize-v1", "t2ance-lcb-normalize-v2", "t2ance-lcb-normalize-v3", "t2ance-lcb-normalize-v4", "lcb-dag-revision-v1", "lcb-dag-revision-v2", "lcb-dag-revision-v3", "lcb-answer-backward-review-v1"),
        }[self.task_type]
        if self.prompt_version not in versions:
            raise ValueError("prompt version does not match task_type")
        if self.thinking not in (None, "enabled", "disabled"):
            raise ValueError("unsupported thinking mode")
        if type(self.strict_response_contract) is not bool:
            raise ValueError("strict_response_contract must be boolean")
        if type(self.content_gated_response) is not bool:
            raise ValueError("content_gated_response must be boolean")
        if self.tls_max_version not in (None, "TLSv1.2"):
            raise ValueError("unsupported TLS compatibility setting")
        if self.transport not in ("urllib", "curl"):
            raise ValueError("unsupported HTTPS transport")
        if self.reasoning_effort not in (None, "none", "low", "high", "max"):
            raise ValueError("unsupported reasoning effort")
        if self.reasoning_effort == "none" and self.thinking != "disabled":
            raise ValueError("reasoning effort none requires thinking disabled")
        if self.reasoning_effort in ("low", "high", "max") and self.thinking != "enabled":
            raise ValueError("reasoning effort requires explicit thinking mode")
        if self.prompt_version == "mmlu-thinking-v1" and self.thinking != "enabled":
            raise ValueError("native reasoning protocol requires thinking enabled")
        if self.solution_source not in (
            "independent_generation",
            "answer_conditioned_generation",
            "official_rationale",
            "canonical_official_rationale",
            "diagnostic_repair_generation",
            "reference_code_explanation",
            "editorial_grounded_pilot",
            "calibri_reference_normalization",
            "t2ance_reference_normalization",
            "reference_dag_revision",
        ):
            raise ValueError("unknown solution source")
        if self.prompt_version in ("lcb-dag-revision-v1", "lcb-dag-revision-v2", "lcb-dag-revision-v3", "lcb-answer-backward-review-v1"):
            if self.solution_source != "reference_dag_revision":
                raise ValueError("DAG revision requires its dedicated source label")
        elif self.solution_source == "reference_dag_revision":
            raise ValueError("DAG revision source requires its dedicated protocol")
        elif self.prompt_version in ("t2ance-lcb-normalize-v1", "t2ance-lcb-normalize-v2", "t2ance-lcb-normalize-v3", "t2ance-lcb-normalize-v4"):
            if self.solution_source != "t2ance_reference_normalization":
                raise ValueError("t2ance requires explicit source-bound normalization")
        elif self.solution_source == "t2ance_reference_normalization":
            raise ValueError("t2ance source requires its dedicated protocol")
        elif self.prompt_version in ("calibri-lcb-normalize-v1", "calibri-lcb-normalize-v2", "calibri-lcb-normalize-v3", "calibri-lcb-repair-v1", "calibri-lcb-repair-v2"):
            if self.solution_source != "calibri_reference_normalization":
                raise ValueError("CALIBRI requires explicit source-bound normalization")
        elif self.solution_source == "calibri_reference_normalization":
            raise ValueError("CALIBRI source requires its dedicated protocol")
        elif self.prompt_version == "livecodebench-editorial-pilot-v1":
            if self.solution_source != "editorial_grounded_pilot":
                raise ValueError("editorial pilot requires explicit editorial source")
        elif self.solution_source == "editorial_grounded_pilot":
            raise ValueError("editorial source requires the dedicated pilot protocol")
        elif self.task_type == "humaneval" or self.prompt_version == "livecodebench-dag-v1":
            if self.solution_source != "reference_code_explanation":
                raise ValueError("reference DAG construction requires reference_code_explanation")
        elif self.solution_source == "reference_code_explanation":
            raise ValueError("reference_code_explanation requires humaneval")
        elif (
            self.task_type != "gsm8k"
            and self.solution_source != "independent_generation"
        ):
            raise ValueError("non-default solution sources require gsm8k")
        if type(self.temperature) not in (int, float) or not 0 <= self.temperature <= 2:
            raise ValueError("invalid temperature")
        if not self.model or not self.key_env:
            raise ValueError("model and key_env are required")
        if self.response_format not in (None, "json_object"):
            raise ValueError("response_format must be absent or json_object")

    def to_dict(self):
        value = asdict(self)
        # Preserve the serialized contract of existing MMLU/GSM8K runs.
        if self.solution_source == "independent_generation":
            value.pop("solution_source")
        if not self.strict_response_contract:
            value.pop("strict_response_contract")
        if self.tls_max_version is None:
            value.pop("tls_max_version")
        if self.transport == "urllib":
            value.pop("transport")
        if not self.content_gated_response:
            value.pop("content_gated_response")
        return value

    @classmethod
    def load(cls, path):
        return cls(**read_json(path))
