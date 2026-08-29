"""A component's own class name must not also be a Tailwind utility.

THE BUG THIS EXISTS FOR, exactly as it happened. `Section.svelte` styled its
fold control with `class="collapse"`. `collapse` is also a Tailwind utility —
`visibility: collapse` — and Tailwind v4 generates a utility for any candidate
string it finds while scanning the source, with no idea that this one was a
class of ours.

Utilities live in `@layer utilities`; Svelte's scoped styles are unlayered, and
unlayered wins. So our rule won every property it SET. It never set
`visibility`, and nothing said it should, so the utility applied unopposed and
every section's collapse control was `visibility: collapse`: in the DOM,
correctly positioned, the right size, and impossible to see or click. Collapse
appeared to have been removed from the dashboard.

Nothing catches this without building. There is no error and no warning; the
component is right, the utility is right, and only the combination is wrong.
Both files read correctly in review — this was found by measuring a computed
style in a browser, which is not a thing anyone does routinely.

So this builds the CSS and compares the two sets of names. It is the same shape
as the other guards here: cheap, boring, and pointed at a failure that is silent
rather than loud.

SKIPPED when the frontend toolchain is absent, so a checkout that has not run
`npm install` still gets a green suite.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"

#: Collisions that are deliberate, with the reason. Empty, and it should stay
#: that way: a class name is cheap to change and a silent override is not.
ALLOWED: dict[str, str] = {}


def build_css(out: Path) -> str:
    """Build the frontend and return its stylesheet."""
    run = subprocess.run(
        ["npx", "vite", "build", "--logLevel", "error", "--outDir", str(out), "--emptyOutDir"],
        cwd=FRONTEND,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, f"vite build failed:\n{run.stdout}\n{run.stderr}"
    sheets = list((out / "assets").glob("*.css"))
    assert len(sheets) == 1, f"expected one stylesheet, found {[s.name for s in sheets]}"
    return sheets[0].read_text()


def scoped_names(css: str) -> set[str]:
    """Class names the components style themselves.

    Svelte compiles a scoped `.foo` into `.foo.svelte-<hash>`, which is what
    makes them identifiable in the built sheet at all.
    """
    return set(re.findall(r"\.([a-zA-Z][\w-]*)\.svelte-[\w-]+", css))


def utility_names(css: str) -> set[str]:
    """Single-class rules Tailwind emitted into `@layer utilities`.

    Scoped to that layer deliberately. This project has its own bare global
    helpers — `.num`, `.panel`, `.dim` — which are unlayered and are SUPPOSED to
    apply to elements that ask for them by name. Those are not shadowing; they
    are the point.
    """
    start = css.find("@layer utilities{")
    if start == -1:
        return set()
    body = css[start:]
    return {
        m.group(1)
        for m in re.finditer(r"(?<![\w.\-\)])\.([a-zA-Z][\w-]*)\s*\{[^{}]*\}", body)
        if ".svelte-" not in m.group(0)
    }


@pytest.mark.skipif(shutil.which("npx") is None, reason="node is not installed")
@pytest.mark.skipif(
    not (FRONTEND / "node_modules" / "vite").is_dir(), reason="frontend deps are not installed"
)
def test_no_scoped_class_shadows_a_tailwind_utility(tmp_path: Path):
    css = build_css(tmp_path / "dist")
    ours = scoped_names(css)
    theirs = utility_names(css)

    # The parser has to actually find both sets, or this passes by finding
    # nothing — which is the way a test like this usually rots.
    assert len(ours) > 50, f"only {len(ours)} scoped class names found; the parser has drifted"
    assert theirs, "no @layer utilities rules found; the parser has drifted"

    clash = sorted((ours & theirs) - set(ALLOWED))
    assert not clash, (
        "these class names are also Tailwind utilities, which will silently "
        "apply every property the component's own rule does not set: "
        f"{clash}. Rename the component's class."
    )


@pytest.mark.skipif(shutil.which("npx") is None, reason="node is not installed")
@pytest.mark.skipif(
    not (FRONTEND / "node_modules" / "vite").is_dir(), reason="frontend deps are not installed"
)
def test_the_close_control_is_visible(tmp_path: Path):
    """The specific regression, named.

    Belt and braces over the general check above: that one compares sets of
    names, and this one asserts the thing a reader actually cares about — that
    nothing in the built sheet hides the close control (the fold control it
    replaced was the original victim, and the hazard is the same).
    """
    css = build_css(tmp_path / "dist")
    assert re.search(r"\.close\.svelte-[\w-]+", css), "the close control lost its scoped styles"
    hidden = re.findall(r"\.(close)[^{}]*\{[^{}]*visibility\s*:\s*(collapse|hidden)", css)
    assert not hidden, f"something sets the close control invisible: {hidden}"
