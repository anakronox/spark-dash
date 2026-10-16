"""Settings' per-agent setup for the MCP tools (roadmap BB2), run under node
and each block parsed the way its agent reads it.

Same toolchain and skip rules as test_gauges_js.py.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
DRIVER = ROOT / "tests" / "js" / "mcp_clients.test.mjs"
ESBUILD = ROOT / "frontend" / "node_modules" / ".bin" / "esbuild"

URL = "http://monitor.example:8080/mcp"
BEARER = "Bearer sdk-abc_DEF-123"


def test_the_driver_exists():
    assert DRIVER.is_file(), f"missing {DRIVER.relative_to(ROOT)}"


@pytest.fixture(scope="module")
def out(tmp_path_factory) -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    if not ESBUILD.is_file():
        pytest.skip("frontend deps are not installed")
    bundle = tmp_path_factory.mktemp("mcp") / "bundle.mjs"
    build = subprocess.run(
        [str(ESBUILD), str(DRIVER), "--bundle", "--format=esm", "--platform=node",
         "--packages=external", f"--outfile={bundle}"],
        capture_output=True, text=True,
    )
    assert build.returncode == 0, f"esbuild failed:\n{build.stderr}"
    run = subprocess.run(["node", str(bundle)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_hermes_reads_a_url_and_an_authorization_header(out):
    doc = yaml.safe_load(out["setups"]["hermes"]["text"])
    assert doc == {"mcp_servers": {"spark-dash": {"url": URL, "headers": {"Authorization": BEARER}}}}
    assert "config.yaml" in out["setups"]["hermes"]["where"]


def test_codex_reads_a_toml_table(out):
    doc = tomllib.loads(out["setups"]["codex"]["text"])
    assert doc == {"mcp_servers": {"spark-dash": {"url": URL, "http_headers": {"Authorization": BEARER}}}}


@pytest.mark.parametrize("client, want", [
    ("cursor", {"mcpServers": {"spark-dash": {"url": URL, "headers": {"Authorization": BEARER}}}}),
    ("vscode", {"servers": {"spark-dash": {"type": "http", "url": URL, "headers": {"Authorization": BEARER}}}}),
    ("opencode", {"mcp": {"spark-dash": {"type": "remote", "url": URL, "oauth": False,
                                          "headers": {"Authorization": BEARER}}}}),
])
def test_json_configs(out, client, want):
    assert json.loads(out["setups"][client]["text"]) == want


@pytest.mark.parametrize("client, program", [("claude-code", "claude"), ("gemini", "gemini")])
def test_commands_split_into_the_right_arguments(out, client, program):
    argv = shlex.split(out["setups"][client]["text"].replace("\\\n", " "))
    assert argv == [program, "mcp", "add", "--transport", "http", "spark-dash", URL,
                    "--header", f"Authorization: {BEARER}"]


def test_other_gives_the_address_and_header_alone(out):
    text = out["setups"]["other"]["text"]
    assert URL in text and f"Authorization: {BEARER}" in text


def test_an_unknown_choice_falls_back_to_claude_code(out):
    assert out["fallback"] == ["claude-code", "claude-code", "hermes"]
