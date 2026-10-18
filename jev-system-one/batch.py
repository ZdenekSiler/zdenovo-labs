"""Part 4: classify many documents concurrently without tripping the rate limit.

One AsyncTypeSafeClient (connection reuse), a semaphore for in-flight requests, and the
SDK's own retry policy for 429/529. Failures come back as values, not exceptions, so one
bad document doesn't sink a batch of ten thousand.

  uv run python batch.py   # offline: mock server with simulated latency and 429s
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import httpx2
from typesafe_sdk import AsyncTypeSafeClient, Noul, RetryPolicy, SystemOneResponse, TypeSafeAPIError, TypeSafeError


@dataclass(frozen=True)
class Outcome:
  index: int
  response: SystemOneResponse | None
  error: str | None = None


class BatchRunner:
  def __init__(self, client: AsyncTypeSafeClient, max_in_flight: int = 32) -> None:
    self.client = client
    self.gate = asyncio.Semaphore(max_in_flight)
    self.peak = 0
    self._in_flight = 0

  async def _one(self, i: int, state, questions: Mapping) -> Outcome:
    async with self.gate:
      self._in_flight += 1
      self.peak = max(self.peak, self._in_flight)
      try:
        return Outcome(i, await self.client.system_one(state, questions))
      except TypeSafeAPIError as e:  # after the SDK's retries are exhausted
        return Outcome(i, None, f"{type(e).__name__} {e.status}")
      except TypeSafeError as e:  # connection/timeout/validation
        return Outcome(i, None, type(e).__name__)
      finally:
        self._in_flight -= 1

  async def run(self, states: Sequence, questions: Mapping) -> list[Outcome]:
    return await asyncio.gather(*(self._one(i, s, questions) for i, s in enumerate(states)))


def flaky_server(rate_limit_every: int = 7, seed: int = 1) -> httpx2.MockTransport:
  """Simulated API: 20-60 ms latency, a 429 on every Nth request it receives."""
  rng = random.Random(seed)
  counter = {"n": 0}

  async def handler(request: httpx2.Request) -> httpx2.Response:
    counter["n"] += 1
    n = counter["n"]  # read before awaiting: other requests bump the counter meanwhile
    await asyncio.sleep(rng.uniform(0.02, 0.06))
    if n % rate_limit_every == 0:
      return httpx2.Response(429, json={"error": "rate limited"}, headers={"retry-after-ms": "50"})
    body = json.loads(request.content)
    answers = {qid: {"type": "noul", "noul": 0.5} for qid in body["questions"]}
    return httpx2.Response(200, json={"model": "jev-1.13.0", "answers": answers,
                                      "usage": {"input_tokens": 300, "output_tokens": 20}},
                           headers={"x-typesafe-request-id": f"req_{n}"})

  handler.counter = counter
  return httpx2.MockTransport(handler)


async def main() -> None:
  transport = flaky_server()
  async with AsyncTypeSafeClient(api_key="mock", transport=transport,
                                 retry=RetryPolicy(max_retries=2, backoff_initial=0.05)) as client:
    runner = BatchRunner(client, max_in_flight=16)
    docs = [{"message": f"ticket {i}"} for i in range(200)]
    t0 = time.perf_counter()
    outcomes = await runner.run(docs, {"urgent": Noul(instructions="Is `message` urgent?")})
    dt = time.perf_counter() - t0
  ok = sum(o.response is not None for o in outcomes)
  print(f"{len(docs)} docs, {ok} ok, {len(docs) - ok} failed, peak in-flight {runner.peak}, "
        f"{transport.handler.counter['n']} HTTP requests (incl. retried 429s), {dt:.2f}s")


if __name__ == "__main__":
  asyncio.run(main())
