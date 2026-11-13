"""Queue: one per conversation.

Not one global FIFO: a lane per conversation. Two rules:
  1. order inside a conversation is preserved (inbound seq order),
  2. at most one worker runs a conversation at a time (a lease with a fencing token).

Different conversations run in parallel; the same conversation never does. That is what makes
"the user replied while the agent was mid-run" boring instead of a race.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from .state import StateStore


@dataclass(frozen=True)
class Lease:
    conv_id: str
    owner: str
    fence: int


class ConversationQueue:
    def __init__(self, store: StateStore, lease_s: float, owner: str | None = None):
        self.store = store
        self.lease_s = lease_s
        self.owner = owner or f"worker-{uuid.uuid4().hex[:6]}"

    def claim(self) -> Lease | None:
        """Atomically pick the conversation with the oldest pending work whose lease is free."""
        db = self.store.db
        now = time.time()
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute(
                """
                SELECT c.id, c.fence FROM conversations c
                WHERE (c.lease_until IS NULL OR c.lease_until < :now)
                  AND (
                    EXISTS (SELECT 1 FROM inbound i WHERE i.conv_id = c.id AND i.consumed_at IS NULL)
                    OR (c.status IN ('parked_retry', 'running') AND COALESCE(c.retry_at, 0) <= :now)
                  )
                ORDER BY COALESCE(
                    (SELECT MIN(seq) FROM inbound i WHERE i.conv_id = c.id AND i.consumed_at IS NULL),
                    1e18)
                LIMIT 1
                """,
                {"now": now},
            ).fetchone()
            if row is None:
                db.execute("COMMIT")
                return None
            fence = row["fence"] + 1
            db.execute(
                "UPDATE conversations SET status='running', lease_owner=?, lease_until=?, fence=?, updated_at=?"
                " WHERE id=?",
                (self.owner, now + self.lease_s, fence, now, row["id"]),
            )
            db.execute("COMMIT")
            return Lease(row["id"], self.owner, fence)
        except BaseException:
            db.execute("ROLLBACK")
            raise

    def renew(self, lease: Lease) -> bool:
        cur = self.store.db.execute(
            "UPDATE conversations SET lease_until=? WHERE id=? AND lease_owner=? AND fence=?",
            (time.time() + self.lease_s, lease.conv_id, lease.owner, lease.fence),
        )
        return cur.rowcount == 1

    def release(self, lease: Lease, status: str, retry_at: float | None = None) -> None:
        self.store.db.execute(
            "UPDATE conversations SET status=?, lease_owner=NULL, lease_until=NULL, retry_at=?, updated_at=?"
            " WHERE id=? AND fence=?",
            (status, retry_at, time.time(), lease.conv_id, lease.fence),
        )
