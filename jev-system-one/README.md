# Jev in Python: the lab

Companion code for the 6-part series **"Jev in Python: A Field Guide to TypeSafe's System One
Model"** on [zdenovo.com](https://zdenovo.com/series/jev-system-one-deep-dive). Pinned to
`typesafe-sdk` 0.7.4, written against `jev-1.13.0`.

```bash
uv run python demo.py           # every pattern, offline (replays cassettes/series.json)
uv run python demo.py wire      # captures the real SDK request serialisation, no network
```

**Read this before trusting any number:** without `TYPESAFE_API_KEY` the lab replays
`cassettes/series.json`, which `make_fixtures.py` **hand-wrote** in the documented response
shape. Those answers are not Jev's outputs; they exist to exercise the routing, threshold and
confidence code. Set `TYPESAFE_API_KEY` and the same commands call Jev and re-record the cassette.

| File | Part | What |
|---|---|---|
| `cassette.py` | all | live-or-replay client: records with a key, replays without |
| `make_fixtures.py` | all | writes the hand-written fixture cassette |
| `demo.py` | 1–6 | every pattern in the series |
| `confidence.py`, `check_confidence.py` | 3 | the confidence formulas, checked against TypeSafe's examples |
| `calibration.py` | 3 | reliability-table harness |
| `triage.py`, `batch.py` | 4 | the triage component and async batching |
| `jagged.py` | 5 | workarounds for the documented failure modes |
| `guard.py`, `hooks/jev_pretooluse.py`, `check_hook.py` | 6 | Jev as a guardrail, incl. a Claude Code PreToolUse hook |
| `jevai_community.py` | 6 | client for jevai.org's community endpoints |
| `*_output.txt` | | the captured runs the articles quote |
