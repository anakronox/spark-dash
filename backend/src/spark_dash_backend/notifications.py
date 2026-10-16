"""Notifications through ntfy, as an integration (roadmap BF).

Alertmanager sends every alert to an ntfy topic by its generic webhook, and
reads the topic's URL from a file, `secrets/ntfy-url`, because on ntfy.sh the
topic name is the only access control: the URL is the whole secret, so it is
kept out of git and out of the config.

ALERTMANAGER READS THAT FILE ON EVERY SEND (notify/webhook: `os.ReadFile` in
`Notify`, whitespace trimmed), so writing it here changes where the next alert
goes with no restart. That is what makes this an integration rather than a
README step: Settings shows where alerts go, sends a test, and makes a new
topic.

CAPABILITY, THEN USE, as DGX OS updates (AL5). The backend sees the file only
when the stack mounts `secrets/` into it and lets it share Alertmanager's
group; until then this says which piece is missing. The directory is setgid,
so a file written here belongs to Alertmanager's group and it can still read
it.
"""

from __future__ import annotations

import os
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx

FILE = "ntfy-url"
DEFAULT_SERVER = "https://ntfy.sh"
TEMPLATE = "alertmanager"
SETUP_DOC = "https://github.com/anakronox/spark-dash/blob/main/central/README.md#notifications-ntfy"
#: Topics anyone would try first. The topic is the password on ntfy.sh.
GUESSABLE = {"spark-dash", "spark-dash-alerts", "sparkdash", "alerts", "gx10", "dgx-spark"}


def new_topic() -> str:
    """`spark-dash-` and 16 random base32 characters: about 80 bits."""
    import base64

    return "spark-dash-" + base64.b32encode(secrets.token_bytes(10)).decode().lower().rstrip("=")


def build_url(server: str, topic: str) -> str:
    """The URL Alertmanager posts to, with ntfy's Alertmanager template, so a
    notification reads as an alert and not as a wall of JSON."""
    server = server.strip().rstrip("/") or DEFAULT_SERVER
    if "://" not in server:
        server = f"https://{server}"
    p = urlparse(server)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError(f"not a server address: {server!r}")
    topic = topic.strip().strip("/")
    if not topic or "/" in topic or len(topic) > 64:
        raise ValueError("a topic is one path segment, at most 64 characters")
    return urlunparse((p.scheme, p.netloc, f"/{topic}", "", urlencode({"template": TEMPLATE}), ""))


def describe(url: str) -> dict:
    """Server, topic and whether the template is on, from a stored URL."""
    p = urlparse(url)
    topic = p.path.strip("/")
    q = parse_qs(p.query)
    return {
        "server": f"{p.scheme}://{p.hostname}" + (f":{p.port}" if p.port else ""),
        "topic": topic,
        "template": TEMPLATE in q.get("template", []),
        "guessable": topic.lower() in GUESSABLE or len(topic) < 12,
        "public_server": p.hostname == "ntfy.sh",
    }


def mask(topic: str) -> str:
    return topic[: min(len(topic), 13)] + "•" * max(0, min(8, len(topic) - 13))


class NtfyFile:
    def __init__(self, directory: Path):
        self.dir = directory
        self.path = directory / FILE

    def capability(self) -> dict:
        """Whether the dashboard can read and write the topic file, and if
        not, which piece is missing."""
        if not self.dir.is_dir():
            return {"ok": False, "missing": "mount",
                    "detail": "secrets/ is not mounted into the backend, so the topic can only be set by "
                              "hand. Add the two lines from the setup guide to the compose file."}
        if self.path.exists() and not os.access(self.path, os.R_OK | os.W_OK):
            return {"ok": False, "missing": "file_permissions",
                    "detail": "The topic file is there but the backend may not read and write it. On the "
                              "monitoring host: sudo chmod 660 secrets/ntfy-url"}
        if not os.access(self.dir, os.W_OK):
            return {"ok": False, "missing": "dir_permissions",
                    "detail": "The backend may not write in secrets/. On the monitoring host: "
                              "sudo chmod 2770 secrets"}
        return {"ok": True}

    def read(self) -> str | None:
        try:
            return self.path.read_text().strip() or None
        except OSError:
            return None

    def write(self, url: str) -> None:
        """Atomically, so Alertmanager never reads half a URL. Group-readable:
        the directory is setgid, so the file takes Alertmanager's group."""
        tmp = self.dir / f".{FILE}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        with os.fdopen(fd, "w") as fh:
            fh.write(url)
        os.chmod(tmp, 0o640)
        os.replace(tmp, self.path)


async def send_test(url: str) -> dict:
    """One notification shaped like Alertmanager's own, so it renders through
    the same template a real alert does and proves the whole path."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    body = {
        "version": "4", "status": "firing", "receiver": "ntfy", "groupKey": "spark-dash-test",
        "commonLabels": {"alertname": "SparkDashTest", "severity": "info"},
        "commonAnnotations": {"summary": "Test from spark-dash",
                              "description": "Sent from Settings → Notifications. Alerts will arrive here."},
        "alerts": [{
            "status": "firing", "startsAt": now,
            "labels": {"alertname": "SparkDashTest", "severity": "info"},
            "annotations": {"summary": "Test from spark-dash",
                            "description": "Sent from Settings → Notifications. Alerts will arrive here."},
        }],
    }
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, json=body, headers={"User-Agent": "spark-dash"})
    return {"ok": resp.status_code < 300, "status": resp.status_code,
            "seconds": round(time.monotonic() - started, 2),
            "detail": None if resp.status_code < 300 else resp.text[:200]}
