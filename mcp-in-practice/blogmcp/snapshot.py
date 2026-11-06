"""Sanitise a copy of the real blog database into a snapshot the lab may query.

    uv run python -m blogmcp.snapshot data/prod-raw.db data/snapshot.db

Kept: published posts, series, API cost log. Comments: only id, post, status and timestamp;
authors and bodies are blanked (other people's words, not ours to publish or analyse).
Dropped: drafts, settings, deploy log, topics. The snapshot never leaves this machine:
`data/` is gitignored and the public repo ships the synthetic fixture instead.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from .fixture import SCHEMA


def sanitise(raw: Path, out: Path) -> dict[str, int]:
    out.unlink(missing_ok=True)
    dst = sqlite3.connect(out)
    dst.executescript(SCHEMA.read_text())
    src = sqlite3.connect(f"file:{raw}?mode=ro", uri=True)
    counts = {}
    for table in ("series", "posts", "api_costs"):
        cols = [r[1] for r in dst.execute(f"PRAGMA table_info({table})")]
        src_cols = {r[1] for r in src.execute(f"PRAGMA table_info({table})")}
        use = [c for c in cols if c in src_cols]
        rows = src.execute(f"SELECT {','.join(use)} FROM {table}").fetchall()
        dst.executemany(f"INSERT INTO {table} ({','.join(use)}) VALUES ({','.join('?' * len(use))})", rows)
        counts[table] = len(rows)
    rows = src.execute("SELECT id, post_slug, created_at, status FROM comments").fetchall()
    dst.executemany("INSERT INTO comments (id, post_slug, author, body, created_at, status) "
                    "VALUES (?, ?, '', '', ?, ?)", rows)
    counts["comments (text blanked)"] = len(rows)
    dst.commit()
    dst.execute("VACUUM")
    return counts


if __name__ == "__main__":
    print(sanitise(Path(sys.argv[1]), Path(sys.argv[2])))
