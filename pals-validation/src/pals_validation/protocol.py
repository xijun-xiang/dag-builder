"""Versioned prompts and exact visible-step boundary contract."""

SYSTEM = (
    "Continue the supplied reasoning prefix by producing only the next reasoning step. "
    "Use the supplied question and prefix. Do not repeat the prefix. "
    "The response already starts with <step>; finish this one concise step with </step> and stop. "
    "Do not give a separate RESULT label or final answer block. Do not invent missing facts. "
    "If the prefix is insufficient, explain that within the step."
)


def question(case):
    return case["question"] + "\n\n" + "\n".join(
        f"{letter}. {text}" for letter, text in zip("ABCD", case["choices"]))


def step_prefix(base, statements):
    return base + "".join(f"<step>\n{s}\n</step>\n" for s in statements) + "<step>\n"


def parse_step(text):
    body, boundary, spill = text.partition("</step>")
    invalid = (not boundary or not body.strip() or
               any(tag in body for tag in ("<step>", "<answer>", "</answer>", "RESULT:", "<think>", "</think>")))
    return {"valid": not invalid, "body": None if invalid else body.strip(),
            "spill": spill, "reason": "missing_boundary_empty_or_nested_structure" if invalid else None}


class BoundaryTracker:
    """Track first </step> separately per batch row, including multi-token tags."""
    def __init__(self, tokenizer, prompt_length, size):
        self.tokenizer = tokenizer
        self.prompt_length = prompt_length
        self.lengths = [None] * size

    def update(self, rows):
        if len(rows) != len(self.lengths):
            raise ValueError("Batch shape changed")
        for i, row in enumerate(rows):
            if self.lengths[i] is None:
                tokens = row[self.prompt_length:]
                if "</step>" in self.tokenizer.decode(tokens, skip_special_tokens=True):
                    self.lengths[i] = len(tokens)
        return [n is not None for n in self.lengths]

    def __call__(self, input_ids, scores, **kwargs):
        import torch
        return torch.tensor(self.update(input_ids.tolist()), device=input_ids.device, dtype=torch.bool)
