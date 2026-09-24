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
        worker_limit = 32 if self.task_type == "gpqa" else 6
        if self.workers > worker_limit:
            raise ValueError(
                f"at most {worker_limit} workers are supported for this task"
            )
        if self.task_type not in ("mmlu", "gsm8k", "gpqa"):
            raise ValueError("unknown task_type")
        versions = {
            "mmlu": ("v1", "mmlu-thinking-v1", "mmlu-thinking-v2"),
            "gsm8k": ("gsm8k-v1", "gsm8k-v2"),
            "gpqa": ("gpqa-reference-v1", "gpqa-repair-v1", "gpqa-revision-v1"),
        }[self.task_type]
        if self.prompt_version not in versions:
            raise ValueError("prompt version does not match task_type")
        if self.thinking not in (None, "enabled", "disabled"):
            raise ValueError("unsupported thinking mode")
        if self.reasoning_effort not in (None, "low", "high", "max"):
            raise ValueError("unsupported reasoning effort")
        if self.reasoning_effort is not None and self.thinking != "enabled":
            raise ValueError("reasoning effort requires explicit thinking mode")
        if self.prompt_version in ("mmlu-thinking-v1", "mmlu-thinking-v2") and self.thinking != "enabled":
            raise ValueError("native reasoning protocol requires thinking enabled")
        if self.solution_source not in (
            "independent_generation",
            "answer_conditioned_generation",
            "official_rationale",
            "canonical_official_rationale",
            "diagnostic_repair_generation",
        ):
            raise ValueError("unknown solution source")
        if (
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
        return value

    @classmethod
    def load(cls, path):
        return cls(**read_json(path))
