"""Collector base class.

Collectors are independent on purpose: a node that can't read PSI should still
report GPU and memory. `Collector.safe_collect()` converts any failure into a
recorded error rather than letting it take down the whole snapshot.
"""

from __future__ import annotations

import abc
import logging

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
