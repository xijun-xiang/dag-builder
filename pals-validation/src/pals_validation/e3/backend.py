"""Model-backed full-trace generation and explicit-boundary likelihood scoring."""

from __future__ import annotations

import math

from ..backend import HFBackend
from ..metrics import pair
from .protocol import messages, parse_trace
from .schema import require
from .greedy_extension import generation_budget


def generation_options(cfg: dict) -> dict:
    """Separate greedy decoding from sampling; never feed temperature=0 to a sampler."""
    common = {"do_sample": cfg["do_sample"], "num_beams": cfg["num_beams"],
              "repetition_penalty": cfg["repetition_penalty"]}
    if cfg["do_sample"]:
        require(cfg["temperature"] > 0, "sampling requires positive temperature")
        common.update(temperature=cfg["temperature"], top_p=cfg["top_p"], top_k=cfg["top_k"])
    else:
        require(cfg["temperature"] == 0 and cfg["num_beams"] == 1,
                "E3 greedy decoding requires T=0 and one beam")
    return common


class AnswerBoundaryTracker:
    """Track each row's first closing answer tag without repeatedly decoding its full history."""

    def __init__(self, tokenizer, prompt_width: int, size: int, eos_ids: list[int]):
        self.tokenizer, self.prompt_width, self.eos_ids = tokenizer, prompt_width, set(eos_ids)
        self.lengths: list[int | None] = [None] * size
        self.reasons: list[str | None] = [None] * size

    def update(self, rows: list[list[int]]) -> list[bool]:
        require(len(rows) == len(self.lengths), "generation batch changed shape")
        for index, row in enumerate(rows):
            if self.lengths[index] is not None:
                continue
            generated = row[self.prompt_width:]
            # A tag is at most nine characters; 64 tokens leave room for tokenization differences.
            tail = self.tokenizer.decode(generated[-64:], skip_special_tokens=True)
            if "</answer>" in tail:
                self.lengths[index], self.reasons[index] = len(generated), "boundary"
            elif generated and generated[-1] in self.eos_ids:
                self.lengths[index], self.reasons[index] = len(generated), "eos"
        return [length is not None for length in self.lengths]

    def __call__(self, input_ids, scores, **kwargs):
        import torch
        result = self.update(input_ids.tolist())
        return torch.tensor(result, device=input_ids.device, dtype=torch.bool)


class E3HFBackend(HFBackend):
    """Use the validated E1/E2 logits kernel, but never their prompt or generator."""

    def __init__(self, model_path: str, config: dict):
        runtime = {**config["hf_runtime"], "model_revision": config["model"]["revision"],
                   "device": config["device"]}
        super().__init__(model_path, runtime)
        self.e3_config = config
        self.tokenizer.padding_side = "left"

    def base_prompt(self, problem: dict) -> str:
        return self.tokenizer.apply_chat_template(
            messages(problem, self.e3_config["prompt_version"]),
            tokenize=False, add_generation_prompt=True,
            **self.config["chat_template_kwargs"])

    def score_pair(self, full_context: str, deleted_context: str, target: str) -> dict:
        encode = lambda s: self.tokenizer.encode(s, add_special_tokens=False)
        target_ids, full_ids, deleted_ids = encode(target), encode(full_context), encode(deleted_context)
        require(bool(target_ids) and bool(full_ids) and bool(deleted_ids), "empty score input")
        full, deleted = self._score(full_ids, target_ids), self._score(deleted_ids, target_ids)
        return {"score": pair(full, deleted),
                "evidence": {"target_text": target, "target_ids": target_ids,
                             "full_context_ids": full_ids, "deleted_context_ids": deleted_ids,
                             "full_logprobs": full, "deleted_logprobs": deleted}}

    def generate_batch(self, problems: list[dict], seed: int) -> dict:
        from transformers import GenerationConfig, StoppingCriteriaList
        torch = self.torch
        require(1 <= len(problems) <= self.e3_config["generation"]["batch_size"],
                "invalid generation batch size")
        require(len({problem["problem_id"] for problem in problems}) == len(problems),
                "generation batch repeats a question")
        prompts = [self.base_prompt(problem) for problem in problems]
        ids = [self.tokenizer.encode(prompt, add_special_tokens=False) for prompt in prompts]
        budget_info = generation_budget(self.e3_config, problems[0]["benchmark"], list(map(len, ids)))
        budget = budget_info["effective_max_new_tokens"]
        require(all((problem["benchmark"] in ("humaneval", "livecodebench")) ==
                    (problems[0]["benchmark"] in ("humaneval", "livecodebench"))
                    for problem in problems), "batch mixes output budgets")
        require(all(len(item) + budget <= self.config["max_context"] for item in ids),
                "prompt plus reserved generation exceeds model context")
        eos = self.model.generation_config.eos_token_id
        eos_ids = [eos] if isinstance(eos, int) else list(eos or [])
        require(bool(eos_ids), "model has no EOS token")
        pad = self.tokenizer.pad_token_id
        if pad is None:
            pad = eos_ids[0]
        width = max(map(len, ids))
        padded = [[pad] * (width - len(row)) + row for row in ids]
        masks = [[0] * (width - len(row)) + [1] * len(row) for row in ids]
        device = self.config["device"]
        input_ids = torch.tensor(padded, dtype=torch.long, device=device)
        attention = torch.tensor(masks, dtype=torch.long, device=device)
        tracker = AnswerBoundaryTracker(self.tokenizer, width, len(problems), eos_ids)
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
        cfg = self.e3_config["generation"]
        generation_config = GenerationConfig(
            **generation_options(cfg), max_new_tokens=budget,
            eos_token_id=eos_ids, pad_token_id=pad, use_cache=True)
        with torch.inference_mode():
            result = self.model.generate(input_ids=input_ids, attention_mask=attention,
                generation_config=generation_config, stopping_criteria=StoppingCriteriaList([tracker]))
        rows = []
        for index, (problem, sequence) in enumerate(zip(problems, result)):
            produced = sequence[width:].tolist()
            if tracker.lengths[index] is None:
                cut, reason = len(produced), "length"
            else:
                cut, reason = tracker.lengths[index], tracker.reasons[index]
            actual = produced[:cut]
            text = self.tokenizer.decode(actual, skip_special_tokens=True)
            rows.append({"problem_id": problem["problem_id"], "raw_text": text,
                         "generated_token_ids": actual, "finish_reason": reason,
                         "parse": parse_trace(text, reason), "prompt": prompts[index],
                         "prompt_token_ids": ids[index], "prompt_width": width,
                         "left_pad_tokens": width - len(ids[index]),
                         "stop_token_length": cut})
        output = {"seed": seed, "batch_size": len(rows), "rows": rows,
                "generation_contract": {"boundary": "</answer>", "max_new_tokens": budget,
                    "max_context": self.config["max_context"], "eos_token_ids": eos_ids,
                    "pad_token_id": pad, "constrained_decoding": False,
                    "decoding": generation_options(cfg)}}
        if "budget_policy" in self.e3_config:
            output["generation_contract"]["budget"] = budget_info
        return output


class E3MockBackend:
    """Deterministic synthetic backend. Its output is never scientific evidence."""

    versions = {"backend": "synthetic_mock", "scientific_evidence": False}

    def __init__(self, model_path: str, config: dict):
        self.config = config

    def base_prompt(self, problem: dict) -> str:
        return "SYNTHETIC_E3_PROMPT:" + problem["problem_id"]

    def score_pair(self, full_context: str, deleted_context: str, target: str) -> dict:
        ids = list(target.encode()) or [0]
        g = -0.2 if "second" in target else 0.3
        full = [-1.0] * len(ids)
        deleted = [v - g for v in full]
        return {"score": pair(full, deleted),
                "evidence": {"target_text": target, "target_ids": ids,
                             "full_context_ids": list(full_context.encode()),
                             "deleted_context_ids": list(deleted_context.encode()),
                             "full_logprobs": full, "deleted_logprobs": deleted}}

    def generate_batch(self, problems: list[dict], seed: int) -> dict:
        require(len({p["problem_id"] for p in problems}) == len(problems), "duplicate mock question")
        rows = []
        for problem in problems:
            answer = "A" if problem["choices"] else "1"
            if problem["benchmark"] in ("humaneval", "livecodebench"):
                answer = "def solution():\n    return 1"
            raw = f"<step>first reasoning</step>\n<step>second reasoning</step>\n<answer>{answer}</answer>"
            rows.append({"problem_id": problem["problem_id"], "raw_text": raw,
                         "generated_token_ids": list(raw.encode()), "finish_reason": "boundary",
                         "parse": parse_trace(raw, "boundary"),
                         "prompt": self.base_prompt(problem), "prompt_token_ids": [],
                         "prompt_width": 0, "left_pad_tokens": 0,
                         "stop_token_length": len(raw.encode())})
        output = {"seed": seed, "batch_size": len(rows), "rows": rows,
                "generation_contract": {"boundary": "</answer>",
                                        "constrained_decoding": False, "synthetic": True}}
        if "budget_policy" in self.config:
            for row in rows:
                row["prompt_token_ids"] = list(row["prompt"].encode())
            budget = generation_budget(self.config, problems[0]["benchmark"],
                                       [len(r["prompt_token_ids"]) for r in rows])
            for row in rows:
                row.update(prompt_width=budget["prompt_width"],
                           left_pad_tokens=budget["prompt_width"] - len(row["prompt_token_ids"]))
            output["generation_contract"].update(budget=budget,
                max_new_tokens=budget["effective_max_new_tokens"], max_context=budget["max_context"])
        return output
