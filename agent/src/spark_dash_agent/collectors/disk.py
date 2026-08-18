"""Root filesystem capacity.

Deliberately the narrowest possible collector: ONE `statvfs`, on ONE path,
cached. Everything about its shape is a reaction to something that has already
gone wrong on these nodes.

WHY ROOT ONLY. The obvious version enumerates mounts and reports them all.
These nodes mount a NAS (`/Volumes/AI`, ~59 TB), and `statvfs` on a stale NFS
mount does not fail — it blocks, uninterruptibly, until the server answers.
Walking every filesystem would reintroduce exactly the unbounded hang in
snapshot collection that Q existed to remove, and it would do it for data
nobody asked for: model weights live on the local root, so root is the disk
whose filling stops inference.

WHY CACHED. Disk usage moves in minutes, not seconds, while the snapshot is
built every couple of seconds for the live view. A TTL keeps a slow or wedged
filesystem from being touched on every poll, and makes the cost of this
collector effectively zero regardless of poll rate.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from spark_dash_common.models import DiskMetrics

from spark_dash_agent.collectors.base import Collector

log = logging.getLogger(__name__)

#: How long a reading stays good. Disk fills over hours; there is nothing to
#: gain from asking more often, and something to lose if the answer is slow.
DEFAULT_TTL_S = 60.0


class DiskCollector(Collector[DiskMetrics]):
    """Capacity of the host's root filesystem.

    `root` is where the HOST's `/` is visible from inside the container. It is
    not `/`: that would measure the container's own overlay filesystem, which
    is a different disk with a different size and would look plausible while
    being entirely wrong.
    """

    name = "disk"

    def __init__(self, root: Path, *, ttl_s: float = DEFAULT_TTL_S) -> None:
        self._root = root
        self._ttl_s = ttl_s
        self._cached: DiskMetrics | None = None
        self._read_at = 0.0

    def collect(self) -> DiskMetrics | None:
        now = time.monotonic()
        if self._cached is not None and (now - self._read_at) < self._ttl_s:
            return self._cached

        if not self._root.exists():
            # The bind mount is missing — almost always a node stack deployed
            # from a compose file predating this collector. Say which mount,
            # because the symptom otherwise is a silently absent number.
            log.warning(
                "no host root at %s; disk capacity unavailable. Add "
                "'- /:%s:ro' to the agent service in node/compose.yaml.",
                self._root,
                self._root,
            )
            return None

        st = os.statvfs(self._root)
        total = st.f_blocks * st.f_frsize
        available = st.f_bavail * st.f_frsize
        # total - available, not total - free: the gap is the filesystem's
        # reserved blocks, and `available` is what the disk alerts measure.
        # Matching them means the card and the alert cannot disagree.
        used = max(0, total - available)

        self._cached = DiskMetrics(
            total_bytes=total, available_bytes=available, used_bytes=used
        )
        self._read_at = now
        return self._cached
