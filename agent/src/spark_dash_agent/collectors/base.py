"""Collector base class.

Collectors are independent on purpose: a node that can't read PSI should still
report GPU and memory. `Collector.safe_collect()` converts any failure into a
recorded error rather than letting it take down the whole snapshot.
"""

from __future__ import annotations

import abc
import logging
import time

log = logging.getLogger(__name__)


class Collector[T](abc.ABC):
    """One source of metrics, collected on demand."""

    name: str

    @abc.abstractmethod
    def collect(self) -> T | None:
        """Gather metrics. May raise; callers should use `safe_collect`."""

    def safe_collect(self, errors: dict[str, str]) -> T | None:
        """Collect, recording any failure in `errors` instead of raising.

        Logged at debug rather than error: on a machine without a GPU (a dev
        laptop, CI) an unavailable collector is expected, not a fault. The
        error still lands in the snapshot for the UI to surface.
        """
        try:
            return self.collect()
        except Exception as exc:  # noqa: BLE001 — deliberately broad, see docstring
            log.debug("collector %s failed", self.name, exc_info=True)
            errors[self.name] = f"{type(exc).__name__}: {exc}"
            return None


class Budget:
    """A wall-clock allowance shared by every request in ONE collection.

    Per-request timeouts bound a single call. They do not bound a `collect()`
    that makes many, and that distinction is what took the agent off the
    dashboard on 2026-08-18: two routers at three requests each, 2s apiece,
    is a 12s worst case from a "2 second timeout" — and it grew with every
    router, model and vLLM endpoint the node gained.

    This converts "2s per request" into "at most N seconds for the whole
    collection", by shrinking each request's timeout to whatever is left.

    Cooperative on purpose. A hard cancel would mean abandoning threads that
    keep running, holding sockets and finishing into a snapshot nobody wants;
    Python cannot kill a thread, so the honest version is to stop *starting*
    work and to let what is in flight finish inside a shrinking window.
    """

    def __init__(self, seconds: float) -> None:
        self._deadline = time.monotonic() + seconds

    @property
    def remaining(self) -> float:
        return self._deadline - time.monotonic()

    @property
    def spent(self) -> bool:
        return self.remaining <= 0.0

    def timeout(self, ceiling: float) -> float:
        """Timeout for one request: the ceiling, or what is left, whichever is
        smaller. Never negative — callers check `spent` before requesting."""
        return max(0.0, min(ceiling, self.remaining))
