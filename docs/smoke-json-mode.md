# Live smoke-test correction: explicit JSON mode

2026-09-09: the exact `deepseek-v4-flash` model ID was listed by the configured
proxy. The first two live requests returned HTTP success, matching model names,
`finish_reason=stop`, and token usage. Neither met the output contract:

- Source row 131: explanatory text preceded an otherwise plausible JSON answer.
- Source row 132: no difficulty object, and answer field B contradicted the
  rationale's D conclusion. This is retained as a real inconsistent response, not
  silently corrected to D.

The interface smoke gate failed; the other 28 items were not submitted. Original
requests/responses, 1,193 reported tokens and all failure records stay in `pilot-v1`.

The original requests only requested JSON in the prompt, without API-level JSON
mode. DeepSeek documents `response_format={"type":"json_object"}` for Chat
Completions: https://api-docs.deepseek.com/api/create-chat-completion/ . The proxy's
actual support still needs live verification; JSON mode guarantees neither schema
completeness nor mathematical correctness. No permissive response extraction is
added, no answer is rewritten, and semantic acceptance criteria are unchanged.

`pilot-json-v2.json` adds only JSON mode to the generation protocol. It retains
the exact same 30 selected questions, model, prompts, temperature and output cap.
The first two selected questions are generated anew to test the corrected API
request, under a new immutable `pilot-json-v2` run directory. They are engineering
retests, not independent samples and not retries until scientifically favorable.
If the second smoke gate still fails, do not expand to the remaining 28.

The two earlier calls and 20,280 reserved tokens are subtracted from v2's limits:
218 remaining calls and 2,479,720 remaining reserved tokens. Thus v1 + v2 keep the
original 220-call / 2,500,000-reserved-token budget. No content-based replacement
or additional candidate sampling is authorized by this correction.
