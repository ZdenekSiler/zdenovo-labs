---
description: Review last night's dark-factory run and write work/out/REVIEW.md
---
Run `python3 run.py report` and read `work/out/MORNING_REPORT.md`.

Follow the triage rules in CLAUDE.md. For every dead letter, state its class (transient /
model quality / fatal) and what you did about it. If you requeued anything, resume with
`python3 run.py run` and re-read the report afterwards.

Finish by writing `work/out/REVIEW.md` with exactly three sections:
`## Verdict`, `## What I did`, `## Needs a human`. Keep it under 30 lines.
