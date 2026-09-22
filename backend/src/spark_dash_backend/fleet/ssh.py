"""SSH transport. One function, no library: the framework's model is plain
commands over ssh with stdout captured, and that is all this needs."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

SSH_USER = os.environ.get("SPARK_FLEET_SSH_USER", "")
SSH_KEY = os.environ.get("SPARK_FLEET_SSH_KEY", "")
CONNECT_TIMEOUT = int(os.environ.get("SPARK_FLEET_CONNECT_TIMEOUT", "8"))


@dataclass
class Result:
    rc: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


def _base(host: str, user: str | None) -> list[str]:
    target = f"{user}@{host}" if user else (f"{SSH_USER}@{host}" if SSH_USER else host)
    argv = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={CONNECT_TIMEOUT}",
            # In a container the known_hosts file starts empty; accept-new pins a host
            # on first contact and refuses a changed key afterwards.
            "-o", "StrictHostKeyChecking=accept-new", "-o", "LogLevel=ERROR"]
    if SSH_KEY:
        argv += ["-i", SSH_KEY, "-o", "IdentitiesOnly=yes"]
    argv.append(target)
    return argv


def run(host: str, command: str, *, user: str | None = None, stdin: str | None = None,
        timeout: int = 60) -> Result:
    """Run `command` on `host` through the remote login shell."""
    try:
        p = subprocess.run(_base(host, user) + [command], input=stdin, capture_output=True,
                           text=True, timeout=timeout)
        return Result(p.returncode, p.stdout, p.stderr)
    except subprocess.TimeoutExpired:
        return Result(124, "", f"timed out after {timeout}s")
    except OSError as e:
        return Result(127, "", str(e))


def run_python(host: str, script: str, *, user: str | None = None, timeout: int = 120) -> Result:
    """Pipe a Python program to the node's interpreter. Nothing is installed."""
    return run(host, "python3 -", user=user, stdin=script, timeout=timeout)


def reachable(host: str, *, user: str | None = None) -> Result:
    return run(host, "echo ok && cat /sys/class/dmi/id/board_vendor 2>/dev/null", user=user, timeout=20)
