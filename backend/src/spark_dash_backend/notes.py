"""Notes on the timeline (roadmap BB17).

An agent, through the MCP tool `add_note`, marks a moment: "benchmark
started", "restarted ComfyUI". A later `correlate`, or a reader looking at a
chart, then sees why something moved. Every other event on the timeline is
derived from Prometheus; a note is the one thing nothing else records, so it
is kept in a file of its own beside the cluster file, which is the
backend's persistent, writable directory.

THE FIRST WRITE AN AGENT CAN MAKE, so it is behind a switch of its own (off
by default) and every note says it came from an agent. Bounded: the newest
MAX_NOTES are kept, none older than Prometheus's retention.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path

from .annotations import Annotation

FILE = "notes.json"
MAX_NOTES = 2000
MAX_CHARS = 200
#: Prometheus keeps 180 days here; a note about a moment no chart can show is
#: clutter.
RETENTION_S = 180 * 86400


class NoteStore:
    def __init__(self, cluster_path: Path):
        self._path = cluster_path.parent / FILE
        self._lock = threading.Lock()

    def _read(self) -> list[dict]:
        try:
            data = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return []
        return [n for n in data if isinstance(n, dict) and "ts" in n] if isinstance(data, list) else []

    def _write(self, notes: list[dict]) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(notes, ensure_ascii=False))
        os.replace(tmp, self._path)

    def add(self, text: str, *, node: str | None, at: float | None, by: str) -> dict:
        text = " ".join(text.split())
        if not text:
            raise ValueError("a note needs some text")
        if len(text) > MAX_CHARS:
            raise ValueError(f"a note is at most {MAX_CHARS} characters")
        now = time.time()
        note = {"id": secrets.token_hex(6), "ts": min(at or now, now), "text": text,
                "node": node, "by": by, "added": now}
        with self._lock:
            keep = [n for n in self._read() if n["ts"] > now - RETENTION_S]
            keep.append(note)
            keep.sort(key=lambda n: n["ts"])
            self._write(keep[-MAX_NOTES:])
        return note

    def between(self, start: float, end: float) -> list[Annotation]:
        return [
            Annotation(ts=n["ts"], kind="note", label=f"{n['text']} ({n.get('by') or 'agent'})",
                       node=n.get("node"))
            for n in self._read() if start <= n["ts"] <= end
        ]

    def count(self) -> int:
        return len(self._read())

    def clear(self) -> int:
        with self._lock:
            n = len(self._read())
            self._path.unlink(missing_ok=True)
        return n
