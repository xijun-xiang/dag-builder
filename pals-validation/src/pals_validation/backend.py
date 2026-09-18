"""Optional local Hugging Face backend; no evalscope dependency or remote code."""
import math
from .metrics import pair
from .protocol import SYSTEM, BoundaryTracker, parse_step, question, step_prefix


def target_positions(context_length, target_length):
    if context_length < 1 or target_length < 1:
        raise ValueError("Nonempty context and target required")
    return slice(context_length - 1, context_length + target_length - 1)


class HFBackend:
    def __init__(self, model_path, config):
        import torch
        import transformers
        from transformers import AutoModelForCausalLM, AutoTokenizer
        self.torch, self.config = torch, config
        torch.set_num_threads(config.get("cpu_threads", 4))
        if torch.cuda.is_available():
            torch.backends.cuda.matmul.allow_tf32 = False
            torch.backends.cudnn.allow_tf32 = False
        self.tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True, trust_remote_code=False)
        dtype = {"float32": torch.float32, "bfloat16": torch.bfloat16, "float16": torch.float16}[config["dtype"]]
        self.model = AutoModelForCausalLM.from_pretrained(
            model_path, local_files_only=True, trust_remote_code=False,
            torch_dtype=dtype, attn_implementation=config["attention"])
        self.model.to(config["device"]).eval()
        limit = getattr(self.model.config, "max_position_embeddings", None)
        if limit is None or config["max_context"] > limit:
            raise ValueError("max_context exceeds or cannot verify model context limit")
        self.versions = {"torch": torch.__version__, "transformers": transformers.__version__,
                         "device": config["device"], "dtype": config["dtype"]}
        if str(config["device"]).startswith("cuda"):
            self.versions["gpu"] = torch.cuda.get_device_name(config["device"])

    def context(self, case, ids):
        by_id = {n["node_id"]: n for n in case["steps"]}
        base = self.tokenizer.apply_chat_template(
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question(case)}],
            tokenize=False, add_generation_prompt=True, **self.config["chat_template_kwargs"])
        return step_prefix(base, [by_id[i]["statement"] for i in ids])

    def _score(self, context_ids, target_ids):
        torch = self.torch
        if len(context_ids) + len(target_ids) > self.config["max_context"]:
            raise ValueError("Context overflow: no truncation allowed")
        tokens = torch.tensor([context_ids + target_ids], dtype=torch.long, device=self.config["device"])
        with torch.inference_mode():
            logits = self.model(input_ids=tokens, attention_mask=torch.ones_like(tokens), use_cache=False).logits
            selected = logits[0, target_positions(len(context_ids), len(target_ids)), :].float()
            logprobs = torch.log_softmax(selected, dim=-1).gather(
                -1, torch.tensor(target_ids, device=selected.device).unsqueeze(-1)).squeeze(-1)
        values = logprobs.cpu().tolist()
        if not all(math.isfinite(v) for v in values):
            raise ValueError("Non-finite model scores")
        return values

    def score(self, case, prefix_ids, deleted_id, target):
        full = self.context(case, prefix_ids)
        deleted = self.context(case, [i for i in prefix_ids if i != deleted_id])
        if prefix_ids.count(deleted_id) != 1:
            raise ValueError("Deletion must remove exactly one prefix step")
        encode = lambda s: self.tokenizer.encode(s, add_special_tokens=False)
        # Explicit context/target token boundary, identical target IDs on both sides.
        target_ids, full_ids, deleted_ids = encode(target), encode(full), encode(deleted)
        a, b = self._score(full_ids, target_ids), self._score(deleted_ids, target_ids)
        return {"score": pair(a, b), "evidence": {"target_text": target, "target_ids": target_ids,
                "full_context_ids": full_ids, "deleted_context_ids": deleted_ids,
                "full_logprobs": a, "deleted_logprobs": b}}

    def generate(self, case, prefix_ids, temperature, repeats, seed):
        torch = self.torch
        from transformers import GenerationConfig, StoppingCriteriaList
        tok = self.tokenizer
        prompt = self.context(case, prefix_ids)
        ids = tok.encode(prompt, add_special_tokens=False)
        if len(ids) + self.config["max_new_tokens"] > self.config["max_context"]:
            raise ValueError("Prompt plus reserved generation budget exceeds max_context")
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
        eos = self.model.generation_config.eos_token_id
        eos_ids = [eos] if isinstance(eos, int) else list(eos or [])
        if not eos_ids:
            raise ValueError("No model EOS IDs")
        pad = tok.pad_token_id if tok.pad_token_id is not None else eos_ids[0]
        tracker = BoundaryTracker(tok, len(ids), repeats)
        x = torch.tensor([ids] * repeats, dtype=torch.long, device=self.config["device"])
        generation = GenerationConfig(do_sample=True, temperature=temperature, top_p=1., top_k=0,
                                      repetition_penalty=1., num_beams=1, max_new_tokens=self.config["max_new_tokens"],
                                      eos_token_id=eos_ids, pad_token_id=pad, use_cache=True)
        with torch.inference_mode():
            result = self.model.generate(input_ids=x, attention_mask=torch.ones_like(x),
                                         generation_config=generation, stopping_criteria=StoppingCriteriaList([tracker]))
        rows = []
        for index, sequence in enumerate(result):
            tokens = sequence[len(ids):].tolist()
            eos_pos = next((i for i, token in enumerate(tokens) if token in eos_ids), None)
            boundary = tracker.lengths[index]
            if boundary is not None and (eos_pos is None or boundary <= eos_pos + 1):
                tokens, reason = tokens[:boundary], "boundary"
            elif eos_pos is not None:
                tokens, reason = tokens[:eos_pos + 1], "eos"
            else:
                reason = "length"
            text = tok.decode(tokens, skip_special_tokens=True)
            rows.append({"repeat": index, "seed": seed, "raw_text": text,
                         "generated_token_ids": tokens, "finish_reason": reason, "parse": parse_step(text)})
        return {"prompt": prompt, "prompt_token_ids": ids, "rows": rows}


class MockBackend:
    """Synthetic end-to-end contract fixture. NEVER scientific evidence."""
    versions = {"backend": "synthetic_mock", "scientific_evidence": False}

    def __init__(self, model_path, config):
        pass

    def score(self, case, prefix_ids, deleted_id, target):
        ids = list(target.encode()) or [0]
        a, b = [-1.] * len(ids), [-1.1] * len(ids)
        return {"score": pair(a, b), "evidence": {"target_text": target, "target_ids": ids,
                "full_logprobs": a, "deleted_logprobs": b}}

    def generate(self, case, prefix_ids, temperature, repeats, seed):
        return {"prompt": "SYNTHETIC_NO_MODEL", "rows": [
            {"repeat": i, "seed": seed, "raw_text": "Synthetic fixture.</step>",
             "generated_token_ids": [], "finish_reason": "boundary",
             "parse": parse_step("Synthetic fixture.</step>")} for i in range(repeats)]}
