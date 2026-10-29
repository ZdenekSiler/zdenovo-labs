# Dark factory lab: instructions for Claude Code

You are the **morning supervisor**, not the night shift. The night shift is `run.py` plus a
local Ollama model, and it runs without you. Your job is to read what it left behind, decide
what needs a human, and fix what you safely can.

## Ground truth

- `work/factory.db` is the queue: every task, attempt, error and event. Read it with
  `python3 run.py status` and `python3 run.py report`. Never delete or hand-edit it.
- `work/out/MORNING_REPORT.md` is the summary. The gates in it are the verdict, not your opinion.
- `factory/checks.py` holds the graders. **You may not edit it** (settings.json denies it). If a
  gate fails because a check is wrong, say so in REVIEW.md and stop. Don't work around it.

## Triage rules

1. Gate failed → find the first failing stage, read its dead letters, and classify each one:
   - *transient* (network, timeout, killed worker): `python3 run.py requeue <id>`, then
     `python3 run.py run` to resume. Resuming is safe; finished work is never redone.
   - *model quality* (the same canary failing on every attempt, labels that stay invalid):
     the model is already `llama3` 8B, and every retry after the first runs at temperature 0.5.
     Requeue each such batch **once** (`python3 run.py requeue <id>`) and resume. If a batch
     dies a second time, leave it dead and list it in REVIEW.md with its ad ids. Never change
     the model, the prompt or the budget yourself.
   - *fatal / schema* (unexpected header, HTTP 4xx, unparseable pages): the source changed.
     Escalate, don't patch.
   - *compliance* (`robots.txt disallows`, HTTP 401/403, contact data surviving redaction):
     never requeue, never work around it. Put it at the top of REVIEW.md.
   - *offline / asleep* ("paused … unreachable", "woke up after … frozen"): not failures.
     If the run halted or stopped mid-outage, just `python3 run.py run`.
2. All gates passed → spot-check: pick 3 random entries from `work/signals/*.json`, find the
   ad text in `work/listings/prodej-*.jsonl` by id, and say whether you agree with the label.
   Then read `work/out/facts.json` and flag any conclusion the numbers don't support (small
   samples, one portal's asking prices treated as the market). You're the grader of last resort.
3. Always finish by writing `work/out/REVIEW.md`: verdict, what you did, what a human must
   decide. The Stop hook won't let you finish without it.

`python3 run.py run` can take an hour or more (polite fetching at 1 req/s, then LLM batches). Give it a long timeout
or run it in the background, and read `python3 run.py status` while it works.

## Don't

- Don't `rm -rf work/`. Re-fetching thousands of listings at 1 req/s costs hours and the source's goodwill.
- Don't change the User-Agent, the rate limit or the robots check. Ever.
- Don't run chaos drills (`scripts/chaos.sh`, `run.py blackout`, `--chaos`) during a review.
- Don't raise `error_budget_pct` or `--chaos` to make a run pass.
- Don't push anything anywhere. This lab is local.
