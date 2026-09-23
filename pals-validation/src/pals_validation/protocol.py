"""Versioned prompts and exact visible-step boundary contract."""
import ast
import textwrap

SYSTEM = (
    "Continue the supplied reasoning prefix by producing only the next reasoning step. "
    "Use the supplied question and prefix. Do not repeat the prefix. "
    "The response already starts with <step>; finish this one concise step with </step> and stop. "
    "Do not give a separate RESULT label or final answer block. Do not invent missing facts. "
    "If the prefix is insufficient, explain that within the step."
)

HUMANEVAL_SYSTEM = SYSTEM + (
    " The question is an original Python function specification. "
    "Continue the algorithm explanation in natural language, not Python code. "
    "Do not output a function implementation, code fence or unit tests."
)

LIVECODEBENCH_SYSTEM = SYSTEM + (
    " The question is a programming problem. Continue the algorithm explanation "
    "in natural language, not source code. Do not output a program, code fence or tests."
)

HUMANEVAL_GENERATION_V2 = HUMANEVAL_SYSTEM + (
    " Complete exactly ONE next reasoning step, not the remaining solution. "
    "Normally one to four sentences are enough; finish the thought before stopping. "
    "The opening <step> has already been supplied: do not emit another opening tag. "
    "End your text with the exact seven characters </step>, including the final >. "
    "Do not use [/step>, do not leave </step unfinished, and output nothing after </step>."
)


def system_prompt(case, generation_prompt_version="v1"):
    if generation_prompt_version not in ("v1", "humaneval-single-step-v2"):
        raise ValueError("Unknown generation prompt version")
    if generation_prompt_version == "humaneval-single-step-v2":
        if case.get("task_type") != "humaneval":
            raise ValueError("HumanEval generation prompt used for another benchmark")
        return HUMANEVAL_GENERATION_V2
    if case.get("task_type") == "humaneval":
        return HUMANEVAL_SYSTEM
    return LIVECODEBENCH_SYSTEM if case.get("task_type") == "livecodebench" else SYSTEM


def question(case):
    if case.get("task_type") in ("humaneval", "livecodebench"):
        return case["question"]  # Preserve code-task formatting, including visible starter code.
    return case["question"] + "\n\n" + "\n".join(
        f"{letter}. {text}" for letter, text in zip("ABCD", case["choices"]))


def step_prefix(base, statements):
    return base + "".join(f"<step>\n{s}\n</step>\n" for s in statements) + "<step>\n"


def is_code_step(body):
    """A syntax guard, not a semantic judge; ordinary 'return the result' is prose."""
    if "```" in body:
        return True
    try:
        tree = ast.parse(textwrap.dedent(body.strip()))
    except (SyntaxError, ValueError):
        return False
    # Bare nouns/numbers can be prose; statements and executable expressions are code.
    return any(not isinstance(node, ast.Expr) or not isinstance(node.value, (ast.Name, ast.Constant))
               for node in tree.body)


def parse_step(text, task_type=None):
    body, boundary, spill = text.partition("</step>")
    invalid = (not boundary or not body.strip() or
               any(tag in body for tag in ("<step>", "<answer>", "</answer>", "RESULT:", "<think>", "</think>")))
    if not invalid and task_type in ("humaneval", "livecodebench") and is_code_step(body):
        return {"valid": False, "body": None, "spill": spill, "reason": "code_instead_of_algorithm_step"}
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
