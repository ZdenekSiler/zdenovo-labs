# Long-running agent lab

The code for the article *The Harness Is the Product: Building a Long-Running Agent in Python*.
A nightly agent that checks a real FastAPI backend's dependencies against PyPI, asks one
question when something looks breaking, waits for the answer (hours, if need be), and writes a
report. It survives crashes, outages, rate limits and its own premature "done".

Python 3.12, standard library only for everything offline. No `pip install` to run the demo.

```bash
python3 demo.py        # the overnight run: schedule, question, morning reply, quiet night, catch-up
python3 chaos.py       # 8 failure drills: kill -9, zombie worker, 429s, outage, hung tool, budget, early "done", secrets
python3 compare.py     # same job across models / approaches, graded against ground truth
```

## Map: box on the diagram → file

| Box | File | What it does |
|---|---|---|
| Apps and channels | `lrlab/channels.py` | CLI, HMAC-signed webhooks, deliveries (terminal, mail, chat) tailing one event log |
| Triggers | `lrlab/scheduler.py`, `triggers/` | daily schedules with catch-up (`once`/`all`/`skip`), jitter, idempotent fire keys; crontab, systemd timer, GitHub Actions |
| Entry point | `lrlab/gateway.py` | auth, size cap, rate limit, routing, secret capture, provenance stamp, dedupe |
| Queue, one per conversation | `lrlab/conv_queue.py` | lease + fencing token: one worker per conversation, many conversations in parallel |
| Agent loop and tools | `lrlab/harness.py`, `lrlab/tools.py`, `lrlab/hooks.py` | **the harness**: budgets, tool registry, hooks, `send_to_user`, done/stop checks, journal, checkpoints, fresh-context restart |
| Model | `lrlab/models/` | `fake` (scripted, offline), `ollama`, `openai_compat` (Groq, OpenRouter, Gemini, OpenAI), `claude` (Anthropic SDK) |
| Agent's computer | `lrlab/runner_server.py`, `lrlab/runner.py` | separate process, scrubbed env, workspace jail, host allowlist, robots.txt, per-night cache, timeouts |
| Saved state | `lrlab/state.py` | SQLite: conversations, inbound, events, checkpoints, tool journal, vault, schedules |
| Live updates | `lrlab/live.py` | Server-Sent Events over the public event log |
| The real case | `lrlab/task.py` | prompt, version classification, done check, quiet check |

`approaches/agent_sdk.py` and `approaches/claude_code.py` swap the model+loop box for the Claude
Agent SDK and for Claude Code headless (`claude -p`), and keep everything else.

## Models

```bash
python3 compare.py --model fake --model ollama:qwen3:4b --model ollama:llama3.2
GROQ_API_KEY=... python3 compare.py --model groq:<model-id>
OPENROUTER_API_KEY=... python3 compare.py --model openrouter:<model-id>:free
GEMINI_API_KEY=... python3 compare.py --model gemini:<model-id>
uv run --with anthropic python compare.py --model claude:claude-haiku-5-5 --model claude:claude-sonnet-5-5
uv run --with claude-agent-sdk python compare.py --approach sdk --model claude-haiku-5-5
python3 compare.py --approach claude-code --model claude-haiku-5-5
```

Free-tier limits change often, so check your provider's console. The paid runs in the article
cost cents.

## Running it for real

```bash
python3 -m lrlab.cli init  --home /var/lib/lrlab --live-pypi      # real pypi.org, robots.txt respected
python3 -m lrlab.cli fire  --home /var/lib/lrlab --model ollama:qwen3:4b
python3 -m lrlab.cli reply --home /var/lib/lrlab --thread <thread> pin
python3 -m lrlab.cli serve --home /var/lib/lrlab                  # POST /hook (signed) + live view
```

Copy `lockfile.txt` with your own `name==version` lines. `triggers/` has the cron line, the
systemd timer and the GitHub Actions workflow.

## Fixtures

`fixtures/pypi/` holds real PyPI release feeds (`/rss/project/<name>/releases.xml`) for the
16 packages, recorded on the date in `fixtures/pypi/RECORDED_AT`, plus PyPI's real
`robots.txt`. Maintainer e-mail addresses are replaced with `maintainer@example.org`, which the
redaction hook then strips anyway. PyPI's robots.txt disallows `/pypi/*/json` (the JSON API), so
the agent uses the RSS feeds, which robots.txt allows.

## What this is not

The runner is a separate process with a scrubbed environment and a path jail. That is not a
sandbox. In production, run it in a container or microVM with no credentials and an egress proxy.
