"""The fleet overlay adds a capability and changes nothing else (AL3e).

`compose.fleet.yaml` is how the fleet updater is switched on at deployment
level: two mounts and a login, merged over `compose.yaml` by `COMPOSE_FILE`.
An overlay rather than a `profiles:` key for J1's reason — a profile has to be
declared in the base file, so every existing install would silently gain or
lose something the first time it deployed without the flag set.

That only holds if the base file stays innocent of the feature, and if the
overlay adds rather than replaces. Both are checked here rather than hoped
about, because the failure is quiet in the worst way: a backend that starts
cleanly, reports "not configured", and offers nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

CENTRAL = Path(__file__).resolve().parent.parent / "central"
BASE = CENTRAL / "compose.yaml"
OVERLAY = CENTRAL / "compose.fleet.yaml"
SETUP_GUIDE = Path(__file__).resolve().parent.parent / "docs" / "fleet-updates-setup.md"
DOCKERFILE = Path(__file__).resolve().parent.parent / "backend" / "Dockerfile"


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def env_of(service: dict) -> dict[str, str]:
    """Compose accepts a list or a map; the repo uses lists."""
    raw = service.get("environment", [])
    if isinstance(raw, dict):
        return {k: str(v) for k, v in raw.items()}
    return dict(item.split("=", 1) for item in raw if "=" in item)


def test_the_overlay_touches_only_the_backend():
    """It grants the backend a capability. It must not start a container, open
    a port, or alter Prometheus -- anything else here would be a surprise in a
    file people are told to add with one line in .env."""
    services = load(OVERLAY)["services"]
    assert list(services) == ["backend"]
    backend = services["backend"]
    assert set(backend) <= {"environment", "volumes"}, f"unexpected keys: {set(backend)}"
    assert "ports" not in backend
    assert "image" not in backend, "the overlay must not pin an image the base file chooses"


def test_the_base_file_says_nothing_about_the_embedded_fleet():
    """AL5's promise: leave the overlay out and there is nothing to strip. A
    FLEET_SSH_* line in the base file would mean an install that never opted in
    still carried the feature's configuration.

    Scoped to the backend on purpose. The base file also still carries the old
    `spark-fleet-updates` container behind its `fleet` profile, which mounts a
    key of its own -- that is AK's proxy layout, kept until one real update has
    run embedded, and AL3g deletes it. It is off unless a profile is named, so
    it cannot affect an install that has not asked for it.
    """
    backend = load(BASE)["services"]["backend"]
    for key in env_of(backend):
        assert not key.startswith("FLEET_SSH"), f"the backend carries {key} in the base file"
        assert key != "FLEET_STATE_DIR", f"the backend carries {key} in the base file"
    for mount in backend.get("volumes", []):
        target = mount.split(":")[1] if isinstance(mount, str) and ":" in mount else ""
        assert target not in ("/ssh", "/data/fleet"), f"the backend mounts {target} in the base"


def test_the_overlay_supplies_everything_the_backend_needs_to_start_the_feature():
    """The four settings the readiness check looks for. Missing one is the
    silent failure this file exists to prevent."""
    env = env_of(load(OVERLAY)["services"]["backend"])
    assert "FLEET_SSH_USER" in env
    assert env["FLEET_SSH_KEY"] == "/ssh/id_ed25519"
    assert env["FLEET_STATE_DIR"] == "/data/fleet"
    assert "FLEET_INTERVAL_MIN" in env


def test_the_login_has_no_default():
    """`:?` rather than `:-`. A fleet updater that does not know who to log in
    as should refuse to start the feature, not guess at root -- and compose
    saying so at deploy time beats the dashboard saying it afterwards."""
    env = env_of(load(OVERLAY)["services"]["backend"])
    assert ":?" in env["FLEET_SSH_USER"], "a missing login must fail the deploy, not default"


@pytest.mark.parametrize(
    "target,read_only",
    [("/data/fleet", False), ("/ssh", True)],
)
def test_the_mounts_are_what_the_image_expects(target: str, read_only: bool):
    """State is writable because ssh pins host keys into it -- the image's user
    has no home, so known_hosts has nowhere else to go (AL4.3). The key is
    read-only because nothing in the dashboard ever writes it."""
    mounts = load(OVERLAY)["services"]["backend"]["volumes"]
    found = [m for m in mounts if m.split(":")[1] == target]
    assert found, f"no mount at {target}"
    assert (":ro" in found[0]) is read_only, found[0]
    assert found[0].startswith("./"), "relative, so a clone runs unedited from anywhere"


def test_the_paths_agree_with_the_setup_guide():
    """The guide tells people to make a key at a path and set a variable. If
    those drift from the overlay, the walkthrough produces a dashboard that
    starts and offers nothing."""
    guide = SETUP_GUIDE.read_text()
    assert "central/fleet-ssh" in guide
    assert "COMPOSE_FILE=compose.yaml:compose.fleet.yaml" in guide
    assert "SPARK_FLEET_SSH_USER" in guide
    mounts = load(OVERLAY)["services"]["backend"]["volumes"]
    assert any(m.startswith("./fleet-ssh:") for m in mounts)


def test_the_state_and_key_directories_are_ignored_by_git():
    """One holds a private key, the other every Spark's posture. Neither
    belongs in a public repo, and `.gitignore` is what protects host files from
    a pull rather than repo boundaries."""
    ignored = (CENTRAL / ".gitignore").read_text()
    assert "fleet-ssh/" in ignored
    assert "fleet-state/" in ignored


# ------------------------------------------------- the image the overlay needs


def test_the_image_can_actually_open_an_ssh_session():
    """The overlay mounts a key. Without the client in the image that is a
    directory nobody can use, and the failure arrives as "could not connect"
    on every Spark rather than as anything that names the cause."""
    assert "openssh-client" in DOCKERFILE.read_text()


def test_the_image_seeds_the_state_directory_with_the_right_owner():
    """`--no-create-home` means ssh has nowhere for known_hosts, so it is
    pointed at the state directory instead (AL4.3) -- which only works if the
    running user can write there. Docker copies an image directory's ownership
    onto a fresh named volume, and a bind mount needs the same chown on the
    host; the guide says so."""
    body = DOCKERFILE.read_text()
    assert "mkdir -p /data/fleet" in body
    assert "chown -R 10002:10002 /data/fleet" in body
    # and the guide has to tell people to do the same to the host directories,
    # because Docker will not do it for a bind mount
    assert "chown -R 10002:10002" in SETUP_GUIDE.read_text()


def test_the_state_path_is_the_same_in_the_image_the_overlay_and_the_default():
    """Three places name this path. Two agreeing and one not is a dashboard
    that starts, writes its fleet list somewhere nobody mounted, and loses it
    on the next deploy."""
    from spark_dash_backend.config import Settings

    env = env_of(load(OVERLAY)["services"]["backend"])
    assert env["FLEET_STATE_DIR"] == "/data/fleet"
    assert "mkdir -p /data/fleet" in DOCKERFILE.read_text()
    assert str(Settings(fleet_updates_url="").fleet_state_dir) == "/data/fleet"


# ----------------------------------------- the values, not only the keys


def compose_expand(value: str, env: dict[str, str]) -> str:
    """`${VAR}`, `${VAR:-default}` and `${VAR:?msg}`, as compose resolves them.

    `:-` substitutes when the variable is unset OR EMPTY, which is the part
    that matters here: it is how a compose file passes an optional setting
    through, and what it hands the container when nobody set one.
    """
    import re

    def sub(m: re.Match) -> str:
        name, op, arg = m.group(1), m.group(2), m.group(3)
        got = env.get(name, "")
        if op == ":-":
            return got or arg
        if op == ":?":
            return got
        return got

    return re.sub(r"\$\{([A-Z_][A-Z0-9_]*)(:-|:\?)?([^}]*)\}", sub, value)


def test_the_overlay_with_nothing_set_produces_settings_that_load():
    """THE BUG THIS EXISTS FOR, found in production on 2026-09-22.

    `FLEET_ALLOW_PLAIN_PASSWORD=${SPARK_FLEET_ALLOW_PLAIN_PASSWORD:-}` hands
    the container an EMPTY STRING, pydantic could not read "" as a bool, and
    the backend died at import — restart loop, the whole dashboard down, for
    an optional feature's opt-out that nobody had set.

    Checking that the keys are present was not enough. This resolves the
    overlay the way compose does, with the operator setting only what the
    setup guide tells them to, and builds the real Settings from the result.
    """
    from spark_dash_backend.config import Settings

    raw = env_of(load(OVERLAY)["services"]["backend"])
    # what someone who followed the guide has in .env, and nothing more
    operator = {"SPARK_FLEET_SSH_USER": "brian"}
    resolved = {k: compose_expand(v, operator) for k, v in raw.items()}

    settings = Settings(fleet_updates_url="", **{k.lower(): v for k, v in resolved.items()})
    assert settings.fleet_ssh_user == "brian"
    assert str(settings.fleet_state_dir) == "/data/fleet"
    assert settings.fleet_allow_plain_password is False
    assert settings.fleet_interval_min == 60.0


def test_every_overlay_value_survives_an_operator_who_set_nothing_at_all():
    """Harsher: not even the login. Compose would refuse a `:?` at deploy
    time, which is the point of one, but nothing else may explode."""
    from spark_dash_backend.config import Settings

    raw = env_of(load(OVERLAY)["services"]["backend"])
    resolved = {k: compose_expand(v, {}) for k, v in raw.items()}
    settings = Settings(fleet_updates_url="", **{k.lower(): v for k, v in resolved.items()})
    assert settings.fleet_allow_plain_password is False
    assert settings.fleet_ssh_user == "", "no login named: the feature reports itself unconfigured"
