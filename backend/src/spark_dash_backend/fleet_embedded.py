"""The fleet updater running inside this process (AL3c).

The other implementation of `fleet_api.FleetBackend`. Where `ProxyFleet` turns
an intent into an HTTP request to a second container, this turns it into a
call on `fleet.service.Service`, which is now an object this backend owns.

TWO THINGS EVERY METHOD HERE DOES.

**It never blocks the event loop.** `collect_node` is thirty to sixty seconds
of SSH; `sweep` joins a thread per Spark. Called straight from an `async def`,
either one freezes the live WebSocket for every viewer of the dashboard -- not
the fleet panel, the whole dashboard. So everything that reaches a Spark goes
through `asyncio.to_thread`, and `test_fleet_embedded.py` holds a test that
fails if that is ever undone.

**It applies the password rule itself.** The proxy could forward the channel
and let the fleet service refuse; there is nothing behind this one to refuse
on its behalf. `_secure` is that service's own check, ported with its own
sentence, and the default is refusal.

WHAT IT DOES NOT DO: log a body, or put a password anywhere a body could be
reconstructed from. `password` is passed positionally into the run and held
for that run only.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from spark_dash_backend.fleet import ssh
from spark_dash_backend.fleet.service import Service
from spark_dash_backend.fleet_api import FleetError

log = logging.getLogger(__name__)

PASSWORD_NEEDS_TLS = (
    "a password is only accepted over HTTPS — open the dashboard at https://, "
    "or reach it through the tunnel"
)


class EmbeddedFleet:
    """`FleetBackend` over a local `Service`."""

    def __init__(
        self,
        *,
        state_dir: Path,
        ssh_user: str,
        ssh_key: Path | None,
        interval_min: float = 60.0,
        allow_plain_password: bool = False,
        resolve=None,
    ) -> None:
        self._state_dir = Path(state_dir)
        self._ssh_user = ssh_user
        self._ssh_key = Path(ssh_key) if ssh_key else None
        self._interval_min = interval_min
        self._allow_plain_password = allow_plain_password
        # AL3d: node id -> {name, host, cluster}, the dashboard's own
        # inventory. One list of Sparks, so a host cannot differ from itself.
        self._resolve = resolve
        self.svc: Service | None = None

    # ------------------------------------------------------------ readiness
    #
    # The feature is on when it CAN work, and the requirements are checked one
    # by one rather than as a single boolean, because AL5 promised the Settings
    # warning would name what is missing rather than say "not configured".

    def requirements(self) -> dict[str, bool]:
        key = self._ssh_key
        return {
            "ssh_key_mounted": bool(key and key.is_file()),
            "ssh_key_readable": bool(key and key.is_file() and self._readable(key)),
            "ssh_user_set": bool(self._ssh_user),
            "state_dir_writable": self._writable(self._state_dir),
        }

    @staticmethod
    def _readable(p: Path) -> bool:
        try:
            with p.open("rb") as f:
                f.read(1)
            return True
        except OSError:
            return False

    @staticmethod
    def _writable(p: Path) -> bool:
        try:
            p.mkdir(parents=True, exist_ok=True)
            probe = p / ".writable"
            probe.write_text("")
            probe.unlink()
            return True
        except OSError:
            return False

    @property
    def capability(self) -> bool:
        """Can it work? The compose overlay's question (AL5)."""
        return all(self.requirements().values())

    @property
    def enabled(self) -> bool:
        """Should it? The Settings toggle's question.

        Defaults to on: somebody who mounted a key and named a login has said
        what they want. The switch exists to turn it OFF -- quiet while a bad
        kernel sits in the repos, say -- not to make them ask twice.
        """
        return self.svc.inv.enabled if self.svc else True

    @property
    def configured(self) -> bool:
        return self.capability and self.enabled

    def status(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "enabled": self.enabled,
            "requirements": self.requirements(),
            "embedded": True,
        }

    async def set_enabled(self, on: bool, *, secure: bool) -> dict[str, Any]:
        if not self.capability:
            missing = [k for k, ok in self.requirements().items() if not ok]
            raise FleetError(
                409,
                "fleet updates need a change to your compose file first: " + ", ".join(missing),
            )
        if on and not self.svc:
            self.start()
        svc = self._require()
        await asyncio.to_thread(svc.inv.set_enabled, on)
        if not on:
            # Stop checking at once rather than at the next hour. A run already
            # under way is left alone -- it is a unit on the Spark (AL3b).
            await asyncio.to_thread(svc.stop)
        else:
            await asyncio.to_thread(svc.start)
        return {"ok": True, "enabled": on}

    @property
    def embedded(self) -> bool:
        return True

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Build the service and begin checking. Called from the app's
        lifespan; a no-op when the requirements are not met, so a dashboard
        with no key mounted starts normally and simply offers nothing."""
        if self.svc or not self.capability:
            return
        known_hosts = self._state_dir / "known_hosts"
        ssh.configure(
            user=self._ssh_user,
            key=str(self._ssh_key) if self._ssh_key else "",
            known_hosts=str(known_hosts),
        )
        self.svc = Service(
            self._state_dir, interval_min=self._interval_min, resolve=self._resolve
        )
        # The reconcile runs either way: a Spark this tool left paused must be
        # unpaused even if someone has since switched the feature off (AL4.1).
        if self.svc.inv.enabled:
            self.svc.start()
        else:
            self.svc.resume_paused_dashboards()
            log.info("fleet updates are configured but switched off in Settings")

    def stop(self) -> None:
        if self.svc:
            self.svc.stop()

    async def reachable(self) -> bool:
        return self.svc is not None

    # ------------------------------------------------------------- the seam
    def _require(self) -> Service:
        if not self.svc:
            missing = [k for k, ok in self.requirements().items() if not ok]
            raise FleetError(
                404,
                "fleet updates are not set up: " + ", ".join(missing)
                if missing
                else "fleet updates are not running",
            )
        return self.svc

    def _check_password(self, password: str | None, secure: bool) -> None:
        """The one rule that must not soften when the network hop disappears."""
        if password and not (secure or self._allow_plain_password):
            raise FleetError(400, PASSWORD_NEEDS_TLS)

    async def envelope(self, *, secure: bool) -> dict[str, Any]:
        svc = self._require()
        # Reads files the sweep wrote; no node is touched. Still off-thread:
        # it stats every run directory, and the panel polls it at 3s while a
        # run is on.
        return await asyncio.to_thread(svc.fleet)

    async def log(self, run_id: str, node: str, *, secure: bool) -> dict[str, Any]:
        svc = self._require()

        def read() -> dict[str, Any]:
            f = svc.data / "runs" / run_id / f"{node}.log"
            text = f.read_text() if f.exists() else ""
            return {"lines": text.splitlines()[-400:]}

        return await asyncio.to_thread(read)

    async def check_all(self, *, secure: bool) -> dict[str, Any]:
        svc = self._require()
        # Returns at once and sweeps behind it, as the button expects: the
        # panel switches to its 3s cadence and watches `checking` go by.
        await asyncio.to_thread(lambda: svc.sweep_in_background("manual"))
        return {"ok": True}

    async def enrol(self, name: str, host: str, *, secure: bool) -> dict[str, Any]:
        svc = self._require()

        def add() -> dict[str, Any]:
            node = svc.inv.add(name, host)
            svc.collect_in_background(node, "added")
            return node

        return await asyncio.to_thread(add)

    async def node_action(
        self, name: str, action: str, *, password: str | None, secure: bool
    ) -> dict[str, Any]:
        svc = self._require()
        self._check_password(password, secure)
        node = svc.inv.get(name)
        if not node:
            raise FleetError(404, f"{name} is not on the fleet's list")

        if action == "remove":
            await asyncio.to_thread(svc.inv.remove, name)
            return {"ok": True}
        if action == "check":
            await asyncio.to_thread(lambda: svc.collect_in_background(node, "manual"))
            return {"ok": True}
        if action in ("update", "rehearse"):
            try:
                return await asyncio.to_thread(
                    svc.start_update, name, action == "rehearse", password
                )
            except ValueError as exc:  # "already being updated" -- its wording
                raise FleetError(409, str(exc)) from exc
            except KeyError as exc:
                raise FleetError(404, f"{name} is not on the fleet's list") from exc
        raise FleetError(404, f"no such action: {action}")

    async def run_action(self, run_id: str, action: str, *, secure: bool) -> dict[str, Any]:
        svc = self._require()
        if action == "stop":
            run = svc.runs.get(run_id)
            if not run:
                raise FleetError(404, "no such active run")
            run.stop_requested = True
            return {"ok": True}
        if action == "verify":
            try:
                return await asyncio.to_thread(svc.verify_run, run_id)
            except KeyError as exc:
                raise FleetError(404, "no such run") from exc
        raise FleetError(404, f"no such action: {action}")
