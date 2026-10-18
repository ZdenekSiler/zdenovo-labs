# Context rot: the lab

Companion code for **"Context Rot: Why Your Million-Token Window Is a Budget, Not a
Container"** on [zdenovo.com](https://zdenovo.com/blog/context-rot-why-your-million-token-window-is-a-budget-not-a-container).
Plain Python 3.12, standard library only (the live probe additionally needs `anthropic`).

| File | What |
|---|---|
| `haystack.py` | builds needle-in-a-haystack contexts at chosen lengths and depths |
| `distractors.py` | near-miss distractor needles |
| `counters.py` | token counters: a rough offline estimate, and the API's real count |
| `probe.py` | the sweep harness |
| `budget.py`, `demo_budget.py` | packing items into a context budget and reporting what was evicted |
| `filler.md` | deterministic filler text |

```bash
python probe.py --offline --lengths 4000,16000 --depths 0,0.5,1 --trials 1   # no model calls
python demo_budget.py /path/to/blog.db
```

`--offline` calls no model, so it measures the harness, not a model. Drop it (and set
`ANTHROPIC_API_KEY`) to run a real sweep, and price it first: long contexts are expensive.
`demo_budget.py` reads posts from a SQLite blog database (any `posts` table with a
`content` column). The article ran it against the blog's own database. Point it at
your own, or adapt the query.
