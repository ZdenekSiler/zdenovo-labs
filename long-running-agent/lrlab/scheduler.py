"""How the agent gets woken up on a schedule.

The scheduler is a producer like any channel: it turns "it is 01:00" into an Inbound with an
idempotency key of `schedule_id@fire_time`. So:
  * two cron hosts firing the same job      -> gateway dedupes, one run
  * cron calling `tick` every 5 minutes      -> only due fire times produce messages
  * laptop asleep for three nights           -> the catch-up policy decides: once, all, or skip
  * a hundred agents all scheduled at 01:00  -> deterministic jitter spreads them out

A thread key like "nightly-deps/{date}" gives every night its own conversation. Memory between
nights lives in workspace files (notes/last_seen.json), not in an ever-growing transcript.

`tick(now)` takes the clock as an argument, so it is testable and the demo can time-travel.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from .channels import Inbound
from .gateway import Gateway
from .state import StateStore


class Scheduler:
    def __init__(self, store: StateStore, gateway: Gateway):
        self.store = store
        self.gateway = gateway

    def add_daily(self, schedule_id: str, at_utc: str, thread_key: str, prompt: str,
                  catchup: str = "once", jitter_s: int = 0, created: datetime | None = None) -> None:
        assert catchup in ("once", "all", "skip")
        start = (created or datetime.now(timezone.utc)).isoformat()
        self.store.db.execute(
            "INSERT OR IGNORE INTO schedules (id, at_utc, thread_key, prompt, catchup, jitter_s, last_fired)"
            " VALUES (?,?,?,?,?,?,?)",
            (schedule_id, at_utc, thread_key, prompt, catchup, jitter_s, start),
        )

    @staticmethod
    def _jitter(schedule_id: str, day: str, jitter_s: int) -> timedelta:
        if not jitter_s:
            return timedelta(0)
        h = int(hashlib.sha256(f"{schedule_id}|{day}".encode()).hexdigest(), 16)
        return timedelta(seconds=h % jitter_s)

    def _due_times(self, row, now: datetime) -> list[datetime]:
        hh, mm = map(int, row["at_utc"].split(":"))
        last = datetime.fromisoformat(row["last_fired"])
        day = last.date()
        out = []
        while day <= now.date():
            t = datetime(day.year, day.month, day.day, hh, mm, tzinfo=timezone.utc)
            t += self._jitter(row["id"], day.isoformat(), row["jitter_s"])
            if last < t <= now:
                out.append(t)
            day += timedelta(days=1)
        return out

    def tick(self, now: datetime) -> list[dict]:
        fired = []
        for row in self.store.db.execute("SELECT * FROM schedules").fetchall():
            due = self._due_times(row, now)
            if not due:
                continue
            if row["catchup"] == "once":
                chosen = due[-1:]                 # missed three nights? run once, for the latest
            elif row["catchup"] == "skip":
                chosen = [t for t in due if now - t < timedelta(hours=1)]  # only if still fresh
            else:
                chosen = due                      # "all": every missed night, in order
            for t in chosen:
                msg = Inbound(
                    channel="schedule",
                    sender=f"schedule:{row['id']}",
                    thread_key=row["thread_key"].replace("{date}", t.date().isoformat()),
                    text=row["prompt"].replace("{fire_time}", t.isoformat()),
                    idem_key=f"{row['id']}@{t.isoformat()}",
                    verified=True,
                )
                res = self.gateway.submit(msg)
                fired.append({"schedule": row["id"], "fire_time": t.isoformat(), "accepted": res.accepted,
                              "reason": res.reason, "skipped_missed": len(due) - len(chosen)})
            self.store.db.execute("UPDATE schedules SET last_fired=? WHERE id=?", (due[-1].isoformat(), row["id"]))
        return fired
