# Prompt injection: the lab

Companion code for **"Prompt Injection Is Not a Bug You Can Patch"** on
[zdenovo.com](https://zdenovo.com/blog/prompt-injection-is-not-a-bug-you-can-patch).
Plain Python 3.12, no dependencies except `harness.py`, which needs `anthropic`.

```bash
python demo.py        # every defence in the post, offline, no API key
```

`demo_output.txt` is the captured run the post quotes.

| File | What |
|---|---|
| `corpus.py` | the injection carriers used by the demo and harness |
| `smuggling.py` | invisible Unicode tag smuggling, and a scanner for it |
| `spotlight.py` | spotlighting (delimiting / datamarking / encoding untrusted text) |
| `egress.py` | markdown/HTML egress filter against exfiltration links |
| `taint.py` | taint-tracking tool gate |
| `trifecta.py` | the "lethal trifecta" check for an agent's tool profile |
| `harness.py` | live attack-success harness. **Costs real money**: it calls a model for every carrier × mode × trial |

```bash
uv run --with anthropic python harness.py --trials 5   # needs ANTHROPIC_API_KEY
```
