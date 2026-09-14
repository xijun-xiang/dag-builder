# GSM8K DAG pilot

## Data contract

The pilot uses `openai/gsm8k`, configuration `main`, split `test`. Each record
has `question` and `answer`. The final answer is the text after the canonical
`####` marker. The full answer, including the worked rationale and marker, is
preserved in the private artifact for human review.

The first local snapshot was downloaded through `hf-mirror.com` at revision
`740312add88f781978c0658806c59bc2815b9866`. It contains 1,319 test records.

## Pipeline adaptation

GSM8K reuses the API transport, immutable attempts, global budgets, stage
barriers, reports, and human-gated release from the MMLU builder. It uses a
separate `gsm8k-v1` prompt set and `task_type=gsm8k` contract:

```text
solve -> review_solution -> atomize -> dependencies -> justify -> review_dag
```

Unlike MMLU, GSM8K has no choices or answer labels. The solve request excludes
the dataset answer. The answer field is constrained to one numeric literal;
comma separators, decimals, fractions, and equivalent boxed/currency forms are
normalized conservatively for the answer gate. Prose with units is rejected.

## API smoke test

The first smoke test used one selected test item and explicit JSON mode from
`configs/gsm8k-smoke.json`. The endpoint returned the requested model
`deepseek-v4-flash`, `finish_reason=stop`, and a valid JSON solution whose answer
was `14`, matching the source answer.

The request did not contain `gold_answer`, the `####` marker, or the raw answer.
Reported usage was 309 prompt tokens, 95 completion tokens, and 404 total tokens.
This is connectivity and contract evidence for one item, not a benchmark score
or independent validation.
