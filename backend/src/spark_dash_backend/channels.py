"""Slack, Discord and Telegram notifications (roadmap BJ).

WHY THE DASHBOARD SENDS THEM, NOT ALERTMANAGER. Alertmanager speaks all
three, but each channel would be a receiver in `alertmanager.yml`, and that
file is tracked in git: the dashboard rewriting it would collide with every
`git pull`, and every change would need a reload. Instead Alertmanager has
one fixed receiver that forwards every alert here (`/api/alerts/notify`,
behind a token from `secrets/`), and the dashboard fans out to the channels
set in Settings: added, filtered and tested without touching Alertmanager.
ntfy stays a direct Alertmanager receiver, so alerts still reach a phone
when the dashboard is down.

Each channel chooses which alerts (all, critical only, none), whether
recoveries are sent, and whether reports are (BI6). Secrets (a webhook URL,
a bot token) live in their own file beside the cluster file, 0600, and the
browser only ever sees whether one is set and its last four characters.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml

from .cluster import (
    ClusterConfigError,
    NoClusterFileError,
    _atomic_write,
    _current_document,
    dump_cluster,
    parse_cluster,
)

TYPES = ("slack", "discord", "telegram")
ALERTS = ("all", "critical", "off")
SECRETS_FILE = "channels.secrets.json"
#: The token Alertmanager presents when it forwards alerts, written into
#: secrets/ (shared with Alertmanager by group, as the ntfy topic is).
WEBHOOK_TOKEN_FILE = "dashboard-webhook-token"
#: The receiver name the shipped alertmanager.yml uses.
RECEIVER = "spark-dash"
TELEGRAM_API = os.environ.get("TELEGRAM_API_URL", "https://api.telegram.org")

#: Status colours, the dashboard's own critical, warning and good.
COLOURS = {"critical": 0xD85B5B, "warning": 0xFAB219, "resolved": 0x0CA30C, "info": 0x3987E5}


@dataclass(frozen=True)
class Channel:
    id: str
    type: str
    name: str
    enabled: bool = True
    alerts: str = "all"
    resolved: bool = True
    reports: bool = True
    #: Telegram only: the chat to post to. Not a secret.
    chat_id: str | None = None

    def as_dict(self) -> dict:
        d = {"id": self.id, "type": self.type, "name": self.name, "enabled": self.enabled,
             "alerts": self.alerts, "resolved": self.resolved, "reports": self.reports}
        if self.type == "telegram":
            d["chat_id"] = self.chat_id
        return d


def parse_channel(raw: object) -> Channel:
    if not isinstance(raw, dict):
        raise ClusterConfigError("a channel must be a mapping")
    kind = raw.get("type")
    if kind not in TYPES:
        raise ClusterConfigError(f"a channel's type is one of {TYPES}")
    alerts = raw.get("alerts", "all")
    if alerts not in ALERTS:
        raise ClusterConfigError(f"a channel's alerts is one of {ALERTS}")
    chat = raw.get("chat_id")
    if kind == "telegram":
        chat = str(chat).strip() if chat is not None else ""
        if not chat:
            raise ClusterConfigError("a Telegram channel needs its chat_id")
    return Channel(id=str(raw.get("id") or "").strip() or secrets.token_hex(4), type=kind,
                   name=str(raw.get("name") or kind.title()).strip()[:60], enabled=bool(raw.get("enabled", True)),
                   alerts=alerts, resolved=bool(raw.get("resolved", True)), reports=bool(raw.get("reports", True)),
                   chat_id=chat if kind == "telegram" else None)


def parse_channels(payload: object) -> tuple[Channel, ...]:
    if not isinstance(payload, dict) or payload.get("channels") is None:
        return ()
    raw = payload["channels"]
    if not isinstance(raw, list):
        raise ClusterConfigError("`channels:` must be a list")
    out = tuple(parse_channel(c) for c in raw)
    if len({c.id for c in out}) != len(out):
        raise ClusterConfigError("channels must have different ids")
    return out


def load_channels(path: Path) -> tuple[Channel, ...]:
    try:
        payload = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        return ()
    except (OSError, yaml.YAMLError) as exc:
        raise ClusterConfigError(f"could not read {path}: {exc}") from exc
    return parse_channels(payload)


def write_channels(path: Path, channels: tuple[Channel, ...]) -> None:
    doc = _current_document(path)
    if doc is None or not isinstance(doc.get("nodes"), list):
        raise NoClusterFileError(
            f"notification channels are configured in the cluster file ({path.name}), and this deployment "
            "does not have one yet. Nodes still come from SPARK_NODES; move them into the cluster file first."
        )
    nodes = parse_cluster(path.read_text())
    updated = dict(doc)
    if channels:
        updated["channels"] = [c.as_dict() for c in channels]
    else:
        updated.pop("channels", None)
    text = dump_cluster(nodes, preserve=updated)
    parse_channels(yaml.safe_load(text))
    _atomic_write(path, text)


# --- secrets -------------------------------------------------------------------------------

def _secrets_path(cluster_path: Path) -> Path:
    return cluster_path.parent / SECRETS_FILE


def read_secrets(cluster_path: Path) -> dict[str, str]:
    try:
        data = json.loads(_secrets_path(cluster_path).read_text())
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_secrets(cluster_path: Path, values: dict[str, str]) -> None:
    p = _secrets_path(cluster_path)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(values, fh)
    os.replace(tmp, p)


def check_secret(kind: str, value: str) -> str:
    """The webhook URL or the bot token, checked for its shape."""
    v = value.strip()
    if kind in ("slack", "discord"):
        p = urlparse(v)
        if p.scheme != "https" or not p.hostname:
            raise ClusterConfigError(f"a {kind.title()} webhook is an https:// URL")
        return v
    if ":" not in v or len(v) < 20 or "/" in v:
        raise ClusterConfigError("a Telegram bot token looks like 123456789:AA... (from @BotFather)")
    return v


def hint(value: str | None) -> str | None:
    return f"…{value[-4:]}" if value and len(value) >= 8 else ("set" if value else None)


# --- messages --------------------------------------------------------------------------------

@dataclass
class Message:
    title: str
    lines: list[str] = field(default_factory=list)
    #: critical | warning | info: the filter, and the colour unless resolved.
    severity: str = "info"
    resolved: bool = False
    url: str | None = None
    #: An alert (filtered by each channel's `alerts`), or a report.
    kind: str = "alert"


def from_alertmanager(payload: dict, public_url: str | None) -> Message:
    """One message for one Alertmanager notification (a group of alerts)."""
    alerts = [a for a in payload.get("alerts") or [] if isinstance(a, dict)]
    resolved = payload.get("status") == "resolved"
    common = payload.get("commonLabels") or {}
    sev_rank = {"critical": 2, "warning": 1}
    severity = common.get("severity") or max(
        ((a.get("labels") or {}).get("severity", "info") for a in alerts),
        key=lambda s: sev_rank.get(s, 0), default="info")
    name = common.get("alertname") or ((alerts[0].get("labels") or {}).get("alertname") if alerts else None) or "Alert"
    nodes = sorted({(a.get("labels") or {}).get("node") for a in alerts} - {None})
    where = f" on {', '.join(nodes)}" if nodes else ""
    count = f" ({len(alerts)})" if len(alerts) > 1 else ""
    title = f"{'Resolved' if resolved else severity.title()}: {name}{where}{count}"
    lines = []
    for a in alerts[:5]:
        ann = a.get("annotations") or {}
        text = ann.get("summary") or ann.get("description")
        if text and text not in lines:
            lines.append(text)
    first = (alerts[0].get("annotations") or {}).get("description") if alerts else None
    if first and first not in lines:
        lines.append(first)
    if len(alerts) > 5:
        lines.append(f"and {len(alerts) - 5} more")
    return Message(title=title, lines=lines[:6], severity=severity, resolved=resolved, url=public_url,
                   kind="alert")


def wants(c: Channel, m: Message) -> bool:
    """Whether this channel takes this message. A recovery follows its
    alert's severity: a critical-only channel hears a critical one end."""
    if not c.enabled:
        return False
    if m.kind == "report":
        return c.reports
    if m.kind == "test":
        return True
    if c.alerts == "off" or (m.resolved and not c.resolved):
        return False
    return c.alerts == "all" or m.severity == "critical"


def _body(m: Message) -> str:
    return "\n".join(m.lines)


async def send(c: Channel, secret: str, m: Message, *, client: httpx.AsyncClient | None = None) -> dict:
    """Post one message to one channel, formatted for it."""
    colour = COLOURS["resolved"] if m.resolved else COLOURS.get(m.severity, COLOURS["info"])
    if c.type == "slack":
        url = secret
        body: dict[str, Any] = {
            "text": m.title,  # the notification preview
            "attachments": [{"color": f"#{colour:06x}", "title": m.title, "title_link": m.url,
                             "text": _body(m), "footer": "spark-dash"}],
        }
    elif c.type == "discord":
        url = secret
        embed: dict[str, Any] = {"title": m.title[:256], "description": _body(m)[:4000], "color": colour,
                                 "footer": {"text": "spark-dash"}}
        if m.url:
            embed["url"] = m.url
        body = {"username": "spark-dash", "embeds": [embed]}
    else:  # telegram
        url = f"{TELEGRAM_API}/bot{secret}/sendMessage"
        # Text escapes only <, > and &: Telegram's HTML needs no more, and
        # quotes stay quotes. The link's attribute is quoted in full.
        text = f"<b>{escape(m.title, quote=False)}</b>"
        if m.lines:
            text += "\n" + escape(_body(m), quote=False)
        if m.url:
            text += f'\n<a href="{escape(m.url, quote=True)}">Open the dashboard</a>'
        body = {"chat_id": c.chat_id, "text": text[:4000], "parse_mode": "HTML",
                "disable_web_page_preview": True}
    own = client is None
    client = client or httpx.AsyncClient(timeout=10.0)
    try:
        resp = await client.post(url, json=body, headers={"User-Agent": "spark-dash"})
    finally:
        if own:
            await client.aclose()
    ok = resp.status_code < 300
    detail = None
    if not ok:
        # Never echo the URL or the token: they are the secret.
        detail = resp.text[:200]
    return {"ok": ok, "status": resp.status_code, "detail": detail}


async def fan_out(channels: tuple[Channel, ...], secrets_: dict[str, str], m: Message) -> dict[str, dict]:
    """Send to every channel that wants it; one failing never stops the rest."""
    out: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=10.0) as client:
        for c in channels:
            if not wants(c, m):
                continue
            secret = secrets_.get(c.id)
            if not secret:
                out[c.id] = {"ok": False, "detail": "no webhook or token set"}
                continue
            try:
                out[c.id] = await send(c, secret, m, client=client)
            except httpx.HTTPError as exc:
                out[c.id] = {"ok": False, "detail": type(exc).__name__}
    return out


# --- the token Alertmanager presents --------------------------------------------------------

def ensure_webhook_token(secrets_dir: Path) -> str | None:
    """Read the token, making it once if the directory is writable. Group-
    readable, as the ntfy topic is, so Alertmanager can present it."""
    p = secrets_dir / WEBHOOK_TOKEN_FILE
    try:
        existing = p.read_text().strip()
        if existing:
            return existing
    except OSError:
        pass
    if not secrets_dir.is_dir() or not os.access(secrets_dir, os.W_OK):
        return None
    token = secrets.token_urlsafe(32)
    tmp = secrets_dir / f".{WEBHOOK_TOKEN_FILE}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "w") as fh:
        fh.write(token)
    os.chmod(tmp, 0o640)
    os.replace(tmp, p)
    return token
