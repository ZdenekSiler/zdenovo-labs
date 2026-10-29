"""Two kinds of failure, because they need two different responses.

Retryable: the world was flaky (network drop, model timeout, a worker got killed). Try again
later and it will probably work.
Fatal: retrying will fail the same way every time (bad config, a 404, a schema the code
doesn't understand). Dead-letter it now and stop wasting attempts.
"""
import errno
import socket


class Retryable(Exception):
    pass


class Fatal(Exception):
    pass


class Offline(Retryable):
    """A dependency (a website, the government file server, Ollama) can't be reached at all:
    DNS fails, the connection is refused, the network is unreachable. That says nothing about
    the task, so it must not cost the task an attempt. The stage pauses (a circuit breaker)
    and probes until the dependency answers again.

    Timeouts are deliberately NOT Offline: a slow answer might be this task's fault."""

    def __init__(self, dependency: str, detail: str):
        super().__init__(f"{dependency} offline: {detail}")
        self.dependency = dependency


def is_unreachable(exc: BaseException) -> bool:
    """True for 'nothing is there' errors, False for 'something answered badly'."""
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, (socket.gaierror, ConnectionRefusedError)):
        return True
    return isinstance(reason, OSError) and reason.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN)
