"""Optional cards (roadmap AM6a), guarded at source level.

`layout.svelte.ts` uses runes and cannot run under node, so the rules are
checked where they are written. Each is a way the Clients card could appear
when the feature is off, or lose its place when it comes back on.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAYOUT = ROOT / "frontend" / "src" / "lib" / "layout.svelte.ts"
APP = ROOT / "frontend" / "src" / "App.svelte"


def test_clients_is_declared_optional():
    src = LAYOUT.read_text()
    assert "{ id: 'clients', label: 'Clients', optional: true }" in src


def test_optional_kinds_start_unavailable():
    """Otherwise the card flashes onto the page while its status loads."""
    src = LAYOUT.read_text()
    assert re.search(
        r"unavailable = \$state<string\[\]>\(SECTIONS\.filter\(\(s\) => s\.optional\)", src
    )


def test_unavailable_is_filtered_from_the_page_not_removed_from_the_order():
    """Filtered in `visible`, which bands, zones and drag positions all read,
    so the saved place survives switching the feature off and on."""
    src = LAYOUT.read_text()
    getter = src[src.index("get visible(): string[] {") :]
    getter = getter[: getter.index("\n  }")]
    assert "this.isAvailable(kindOf(id))" in getter
    set_available = src[src.index("setAvailable(kind: string, on: boolean)") :]
    set_available = set_available[: set_available.index("\n  }")]
    assert "this.order" not in set_available, "availability must not rewrite the saved order"


def test_the_add_menu_offers_only_available_kinds():
    app = APP.read_text()
    assert "SECTIONS.filter((s) => layout.isAvailable(s.id))" in app


def test_the_card_follows_the_feature():
    app = APP.read_text()
    assert "layout.setAvailable('clients', clientsFeed.configured)" in app
    assert "{:else if kind === 'clients'}" in app
