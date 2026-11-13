# Comparison runs (2026-11-12)

6-package subset (fastapi, anthropic, httpx, jinja2, starlette, pygments), scripted human answers "pin".
Graded by `compare.py` against ground truth from the recorded feeds. One JSON line per run, as written by `compare.py --out`.

## Final runs (the post's table)
```json
{"approach": "harness", "model": "claude:claude-haiku-5-5", "status": "done", "turns": 5, "tokens": 22397, "cost_usd": 0.003, "wall_s": 9.5, "rows_correct": "6/6", "asked": 1, "delivered": true, "invalid_calls": 0, "denied": 0, "done_rejections": 0, "model_errors": 0}
{"approach": "harness", "model": "claude:claude-sonnet-5-5", "status": "done", "turns": 5, "tokens": 20942, "cost_usd": 0.0523, "wall_s": 9.8, "rows_correct": "6/6", "asked": 1, "delivered": true, "invalid_calls": 0, "denied": 0, "done_rejections": 0, "model_errors": 0}
{"approach": "harness", "model": "claude:claude-opus-5-5", "status": "done", "turns": 5, "tokens": 21019, "cost_usd": 0.1055, "wall_s": 16.5, "rows_correct": "6/6", "asked": 1, "delivered": true, "invalid_calls": 0, "denied": 0, "done_rejections": 0, "model_errors": 0}
{"approach": "sdk", "model": "claude-haiku-5-5", "status": "done", "turns": 14, "tokens": null, "cost_usd": 0.0042, "wall_s": 15.6, "rows_correct": "6/6", "asked": 1, "delivered": true, "invalid_calls": 0, "denied": 0, "done_rejections": 0, "model_errors": 0}
{"approach": "claude-code", "model": "claude-haiku-5-5", "status": "done", "turns": 14, "tokens": null, "cost_usd": 0.0146, "wall_s": 31.0, "rows_correct": "6/6", "asked": 1, "delivered": true, "invalid_calls": 0, "denied": 0, "done_rejections": 0, "model_errors": 0}
{"approach": "harness", "model": "ollama:granite4.1:3b", "status": "quiet", "turns": 12, "tokens": 25244, "cost_usd": 0.0, "wall_s": 106.6, "rows_correct": "4/6", "asked": 1, "delivered": true, "invalid_calls": 0, "denied": 0, "done_rejections": 0, "model_errors": 0}
{"approach": "harness", "model": "ollama:llama3.2", "status": "quiet", "turns": 6, "tokens": 10633, "cost_usd": 0.0, "wall_s": 76.3, "rows_correct": "0/6", "asked": 0, "delivered": false, "invalid_calls": 0, "denied": 0, "done_rejections": 0, "model_errors": 0}
{"approach": "harness", "model": "ollama:llama3", "status": "failed", "turns": 0, "tokens": 0, "cost_usd": 0.0, "wall_s": 0.3, "rows_correct": "0/6", "asked": 0, "delivered": false, "invalid_calls": 0, "denied": 0, "done_rejections": 0, "model_errors": 1}
```

qwen3:4b: no run finished within the 40-minute timeout on CPU (the first attempt ended with Ollama dying mid-request: "Remote end closed connection", then "connection refused").

## Earlier runs, kept because the post discusses them

Before the stop check existed (llama3.2 wrote tool calls as text; granite asked its question in private text):
```json
{"approach": "harness", "model": "ollama:llama3.2", "status": "quiet", "turns": 2, "tokens": 2665, "cost_usd": 0.0, "wall_s": 37.1, "rows_correct": "0/6", "asked": 0, "delivered": false, "invalid_calls": 0, "denied": 0, "done_rejections": 0, "model_errors": 0}
{"approach": "harness", "model": "ollama:granite4.1:3b", "status": "quiet", "turns": 9, "tokens": 16210, "cost_usd": 0.0, "wall_s": 61.2, "rows_correct": "0/6", "asked": 0, "delivered": false, "invalid_calls": 0, "denied": 0, "done_rejections": 0, "model_errors": 0}
{"approach": "harness", "model": "ollama:llama3", "status": "failed", "turns": 0, "tokens": 0, "cost_usd": 0.0, "wall_s": 0.2, "rows_correct": "0/6", "asked": 0, "delivered": false, "invalid_calls": 0, "denied": 0, "done_rejections": 0, "model_errors": 1}
```

Claude Code headless, first attempt, before the tool-rule fix (the model gave up when a `cat` was denied):
```json
{"approach": "claude-code", "model": "claude-haiku-5-5", "status": "stopped", "turns": 4, "tokens": null, "cost_usd": 0.0018, "wall_s": 10.0, "rows_correct": "0/6", "asked": 0, "delivered": false, "invalid_calls": 0, "denied": 0, "done_rejections": 0, "model_errors": 0}
```

granite4.1:3b after the stop check, before the decision-required done check (no question, empty final text):
```json
{"approach": "harness", "model": "ollama:granite4.1:3b", "status": "done", "turns": 14, "tokens": 32385, "cost_usd": 0.0, "wall_s": 192.7, "rows_correct": "4/6", "asked": 0, "delivered": true, "invalid_calls": 1, "denied": 0, "done_rejections": 1, "model_errors": 0}
```
