# Dark factory lab: Czech flat-market radar

A small, unattended, crash-tolerant agent pipeline you can leave running overnight. It joins
four sources that were never meant to meet:

| Source | What | How | Volume |
|---|---|---|---|
| [bezrealitky.cz](https://www.bezrealitky.cz) | flats for sale and for rent: price, m², condition, energy class, price cuts, ad text | detail pages from the site's own sitemap, 1 request/s, robots.txt enforced | ~7k Czech flat listings |
| [ČÚZK RÚIAN](https://cuzk.gov.cz/ruian/) | every address point in Czechia with coordinates | national CSV, CC-BY 4.0 | ~3.0M rows, 6,258 files |
| [ČSÚ population](https://csu.gov.cz/produkty/obyvatelstvo-k-3112-podle-pohlavi-v-obcich) | population per municipality, 2000–2024 | open data CSV | ~2.0M rows |
| [ČSÚ completed dwellings](https://csu.gov.cz/produkty/dokoncene-byty-v-obcich) | new flats and houses per municipality, 1997–2024 | open data CSV | ~0.5M rows |

It then answers questions none of them answers alone:

- **Gross rental yield by city**: median asking rent per m² × 12 ÷ median asking sale price per m².
- **Housing pressure**: population change 2014–2024 vs dwellings completed, as new residents
  per new dwelling.
- **What the ad text is worth**: do sellers who sound urgent, or who admit defects, ask less
  than their own city's median? A local Ollama model reads the ads; regexes and canaries
  grade it.

Companion to the zdenovo.com post *Show Me Your Dark Factory*.

- Pure Python 3.11+ stdlib. No `pip install`.
- LLM work runs on Ollama (`llama3`, 8B). No API bill. See below for why not the 3B.
- Everything lands in `work/`, which is git-ignored. Nothing is uploaded or republished.

## Run it

```bash
ollama pull llama3
python3 run.py run --max-per-offer 300      # quick: ~10 min of polite fetching
python3 run.py run                          # the whole sitemap: ~8k pages, a night's work
python3 run.py run --chaos 0.1              # with crashes, hangs and silent corruption injected
python3 run.py status                       # queue counts per stage
python3 run.py report                       # rewrite + print work/out/MORNING_REPORT.md
python3 run.py stop                         # kill switch: finish in-flight tasks, then halt
python3 run.py requeue signals:0007         # retry a dead letter after fixing its cause
python3 run.py chaos                        # list the chaos faults
python3 run.py run --chaos 0.1 --chaos-faults crash,hang   # only some of them
python3 run.py blackout 120                 # drill: the network is "gone" for 120 s
scripts/chaos.sh freeze 90                  # drill: SIGSTOP everything, like a sleeping laptop
scripts/chaos.sh kill                       # drill: kill -9 everything, then resume
```

Kill it at any point (Ctrl-C, `kill -9`, reboot) and run `python3 run.py run` again. It resumes
from `work/factory.db`. Finished work is never redone, and nothing is downloaded twice.
`--offers`, `--max-per-offer`, `--model` and the RÚIAN snapshot date are pinned in
`work/config.json` on the first run. To change them, use a new `--workdir`.

**Keep the host awake.** A laptop that sleeps freezes the run: the first test of this lab sat
silent for six hours while WSL was suspended. It resumed correctly, but a dark factory is only
as dark as its power settings. On Linux, wrap the run in
`systemd-inhibit --what=sleep python3 run.py run`.

## Which model

Measured on the canaries (3 runs × 6 known-answer ads):

| Model | Canaries right | Time per call |
|---|---|---|
| llama3.2 3B | 14/18 | ~10 s |
| llama3 8B | 17/18 | ~21 s |

With two canaries per batch, the 3B passes about 60% of batches and the 8B about 89%. Once
retries and dead letters are counted, the cheap model is the expensive one. `--escalate-model`
(a cascade: small model first, big one from attempt 3) is still there, but it keeps both models
in memory, and on a 14 GB laptop that crashed Ollama.

## Data, law and manners

This lab scrapes, so here is exactly where the line is and why.

- **Sreality.cz and ceskereality.cz are excluded.** Their robots.txt is `User-agent: *` /
  `Disallow: /`. Under the Czech text-and-data-mining licence (amendment 429/2022 Sb.), a
  machine-readable reservation like that is an opt-out, so the lab doesn't touch them.
- **Bezrealitky** disallows only its search pages (`/vyhledat*`, `/search*`) and account pages,
  and publishes a sitemap of listing pages. The lab reads only URLs from that sitemap. Its terms
  (checked 2026-10-27) don't prohibit automated access. They limit the use of **personal data**
  to dealing over the property (3.13), so the lab keeps a whitelist of listing fields, never
  names, phones, emails or photos, and redacts contact details out of ad text. A grader fails
  the batch if any survive.
- **robots.txt is enforced in code** (`factory/checks.py`, `robots_allows`), including `*`
  wildcards, which Python's own `urllib.robotparser` silently ignores. A disallowed URL is a
  `Fatal` error: the task dead-letters and a human reads why. An unreachable robots.txt means
  "don't crawl yet", never "anything goes".
- **One request per second per host, across all worker processes** (a lock file in
  `work/.rate/`), with an honest User-Agent that links to this repo. No browser disguise.
- RÚIAN is CC-BY 4.0 (© ČÚZK). The ČSÚ data is open data (© Český statistický úřad).

This is a lab, not legal advice. If you point it at another site, read that site's robots.txt
and terms first. The code will enforce the first; only you can read the second.

## What's in it

| File | Job |
|---|---|
| `factory/queue.py` | SQLite task queue: leases, fencing, retry with backoff, dead letters, event log |
| `factory/stages.py` | download → listings → RÚIAN shards (parallel, 3M rows) → census → seller signals (LLM) → reduce |
| `factory/checks.py` | the graders: robots matcher, contact redaction, price bounds, signal schema + canaries, GPS-join holdout, "every number must be in the facts" |
| `factory/fetch.py` | `PoliteFetcher`: robots gate, global per-host rate limit, resumable downloads |
| `factory/geo.py` | WGS84 → S-JTSK (Křovák) so GPS-only listings can join RÚIAN |
| `factory/supervisor.py` | runs stages in order, keeps worker processes alive, enforces gates, the deadline and a single-supervisor lock |
| `factory/workspace.py` | config pinning, atomic writes, the seeded chaos monkey |
| `factory/report.py` | the morning report |
| `overnight.sh`, `scripts/morning.sh` | the cron entry: night shift on Ollama, then a headless Claude Code review |
| `CLAUDE.md`, `.claude/` | Claude Code scaffold: triage rules, permissions (can't edit the graders), Stop hook, `/morning-review` |
| `demo_output.txt` | the captured run the post quotes |

## Failure modes it handles

| What breaks | What happens |
|---|---|
| Network gone, DNS failing, Ollama down | `Offline`: attempt refunded, stage paused by a circuit breaker, one probe every 15 s → 5 min, resumes on its own |
| Laptop sleeps / VM suspended | supervisor sees a >60 s gap between ticks, extends every running lease by the gap; workers don't reclaim while the heartbeat is stale |
| Download dies halfway (ČÚZK dropped 63 MB at 56 MB once) | `.part` kept, next attempt resumes with an HTTP Range request |
| robots.txt disallows a URL | `Fatal`, dead letter on the first attempt, no workaround |
| robots.txt unreachable | `Retryable`: don't crawl until it answers |
| 429 / 503 | `Retryable` with backoff; 401/403 is `Fatal`: access refused means no |
| Listing removed (404) or inactive | counted, skipped, not an error |
| Listing abroad (bezrealitky also lists Germany) | GPS outside Czechia: counted, skipped |
| Page layout changes | more than 5% unparseable pages fails the listings gate |
| Contact data survives redaction | listings batch fails verification |
| Government CSV changes its header | `Fatal` with the new header in the message |
| Government CSV contains duplicate records (ČSÚ dwellings: 448) | deduped on `idhod`, count reported |
| Worker killed / hangs past its lease | reclaimed after the lease; a late result is fenced |
| Shard silently loses rows | reconciled against an independent line count of the same files |
| LLM wrong shape / invalid label | grader feedback, up to 3 self-correction rounds |
| LLM fails a canary | attempt discarded without revealing the answer; retried at temperature 0.5 |
| GPS conversion broken (mine was once 7 km off) | holdout check: median error > 250 m or < 95% municipality agreement fails the run |
| Narrator invents a number, including "2.3 million" for 3,022,397 | rejected; after 3 rounds a template takes over and the report says so |
| Not enough listings for any city | `Fatal` at once: retrying can't conjure data |
| Two supervisors on one workdir | `flock`: the second refuses to start |
| The run overruns the night | `--deadline-min` touches `STOP`; workers finish in-flight tasks and exit |

## Chaos monkey

Every handler above is a claim until you've watched it work. `--chaos 0.1` makes each fault
point fire on ~10% of attempts, seeded by task id + attempt + fault so a run reproduces.
Every injection is logged, and the morning report puts *injected* next to *ended done*.

| Fault | Mimics | Tests |
|---|---|---|
| `net_drop` | ČÚZK dropping a 63 MB download at 56 MB | `Retryable`, resumable download |
| `crash` | worker killed with exit 137 (OOM killer) | lease expiry, reclaim, supervisor restart |
| `hang` | worker stalled past its lease, then finishing anyway | reclaim + fencing |
| `corrupt` | a shard silently losing a municipality | independent recount |
| `poison` | the U+2028 that once dead-lettered a batch | regression test for the JSONL fix |
| `pii_leak` | a phone number surviving redaction | the PII grader |
| `llm_garbage` | the model inventing a label | self-correction feedback |
| `llm_down` | Ollama OOM-crashing | `Offline` → circuit breaker |

Drills against a live run: `scripts/chaos.sh freeze N` (laptop sleep), `scripts/chaos.sh kill`,
`python3 run.py blackout N`.

## Morning runbook

| In the report | Means | Do |
|---|---|---|
| "woke up after N min frozen" | the host slept; leases were extended | nothing (fix the power settings if it was long) |
| "paused, X unreachable" … "resumed" | an outage the factory rode out | nothing; if it never resumed, check the network, then `python3 run.py run` |
| halted: stop requested | deadline or `STOP` | `python3 run.py run` |
| dead letter, transient error each time | infrastructure, attempts exhausted | `python3 run.py requeue <id>`, then resume |
| dead letter, same error every attempt | poison pill | fix the code, then requeue |
| `fatal: robots.txt disallows`, 401/403 | compliance stop | never requeue; read why |
| `fatal: … header changed`, layout gate | the source changed | update the parser; escalate |
| gate failed on a budget | quality below threshold | look at the dead batches before touching the budget |

## The Claude Code part

```bash
cd dark-factory && claude          # then: /morning-review
scripts/morning.sh                 # headless, as overnight.sh runs it
```

**Trust gotcha.** Claude Code ignores a project's `permissions.allow` until someone has accepted
the trust dialog in that folder interactively, so a cron job relying on `.claude/settings.json`
gets a reviewer that can't run anything. `scripts/morning.sh` passes the allow and deny lists on
the command line. Keep `work/` a real directory: a symlink pointing outside the project is out of
the reviewer's reach. The review calls Claude, so it costs money. Everything before it is local.

## v1

The first version of this lab processed 1.2M Chicago crime records instead. The factory
(queue, leases, canaries, gates, morning review) is the same, and several of the lessons in the
post came from that run.
