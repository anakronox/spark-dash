"""Validate every theme's categorical chart palette against its own surface.

WHY THIS EXISTS. `theme.svelte.ts` records that two candidate themes were cut
for failing exactly these checks — a green-phosphor look where green/teal/amber
fell below the separation floor even for full colour vision, and a muted slate
that read as grey. The tool that produced those rejections lived on one machine
and was never in this repo, so the practice was documented but not repeatable:
anyone adding a theme had no way to run the check, and the rejections survived
only as prose.

Run it for a report while designing a theme:

    uv run python scripts/palette_check.py

`tests/test_palettes.py` asserts the same thing, so a failing theme cannot land.

THE CHECKS, and what each one is protecting.

* Lightness band — every slot within the mode's OKLCH L window. Slots outside
  it either vanish into the surface or glare off it.
* Chroma floor — a slot below it is grey, and greys are not distinguishable
  from each other by hue at all.
* CVD separation — adjacent pairs, simulated for protanopia and deuteranopia,
  Euclidean distance in OKLab ×100. Adjacent pairs because a palette is
  assigned in fixed order, so pairs that land next to each other are the ones a
  reader must actually tell apart.
* Normal-vision floor — the same pairs unsimulated. A pair that is hard to
  separate with full colour vision is a defect regardless of CVD.
* Contrast vs surface — WCAG, against that theme's OWN surface. This is the
  check a new theme is most likely to break on its own, because a new surface
  can fail a palette that was fine everywhere else.

Colour science: OKLab (Björn Ottosson) for perceptual distance, and the
Machado, Oliveira & Fernandes (2009) CVD transforms at severity 1.0. Thresholds
match the tool that validated the existing themes, so this agrees with the
numbers already recorded in the roadmap rather than establishing new ones.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

APP_CSS = Path(__file__).resolve().parent.parent / "frontend" / "src" / "app.css"

# --- thresholds -------------------------------------------------------------
BAND = {"light": (0.43, 0.77), "dark": (0.48, 0.67)}  # OKLCH L

#: Themes whose lightness band is deliberately different, and why.
#:
#: The band exists so no slot vanishes into the surface or glares off it, and
#: it doubles as a UNIFORMITY rule: slots at one lightness carry equal visual
#: weight, so none reads as more important than another.
#:
#: `contrast` trades exactly that property away on purpose. Protanopia and
#: deuteranopia collapse red-green hue difference almost completely, and
#: lightness difference is what survives — so a theme whose entire job is
#: separation has to step lightness, and a uniform band would forbid the one
#: technique that works. The result is that its slots are NOT equally weighted,
#: which is a real cost accepted for a real gain: adjacent CVD separation goes
#: from the dark theme's 8.4 to 20.6.
#:
#: Every other check still applies, including the contrast floor the widened
#: band could otherwise hide behind.
BAND_OVERRIDE = {"contrast": (0.55, 0.90)}
CHROMA_FLOOR = 0.10

#: Minimum ΔE between any node slot and any status colour.
#:
#: 12 rather than the 8.4 the CVD check tolerates between adjacent slots. Two
#: node colours being close costs you a moment working out which node; a node
#: colour close to `critical` costs you the difference between "node 8" and
#: "something is wrong", which is a category error rather than a slower read.
STATUS_FLOOR = 12.0
CVD_TARGET = 8.0  # OKLab ΔE×100, min(protan, deutan), adjacent pairs
NORMAL_FLOOR = 15.0
CONTRAST_MIN = 3.0

#: Contrast failures that are accepted, and what discharges them.
#:
#: A contrast WARN is not dismissable on its own — it obligates visible labels
#: or a table view. The light theme's amber and pink sit under 3:1 against its
#: near-white surface, and what discharges that is the chart legend, which is
#: always present and names every series. Recorded here rather than left as an
#: unexplained pass, so that if the legend ever became optional this allowance
#: is the thing that has to be revisited.
CONTRAST_ALLOWANCES: dict[str, dict[str, str]] = {
    "paper": {
        "--chart-4": "amber on warm near-white; discharged by the always-present chart legend",
        "--chart-5": "pink on warm near-white; discharged by the always-present chart legend",
    },
    "light": {
        "--chart-4": "amber on near-white; discharged by the always-present chart legend",
        "--chart-5": "pink on near-white; discharged by the always-present chart legend",
    }
}

MACHADO = {
    "protan": [
        [0.152286, 1.052583, -0.204868],
        [0.114503, 0.786281, 0.099216],
        [-0.003882, -0.048116, 1.051998],
    ],
    "deutan": [
        [0.367322, 0.860646, -0.227968],
        [0.280085, 0.672501, 0.047413],
        [-0.011820, 0.042940, 0.968881],
    ],
}


# --- colour ------------------------------------------------------------------
def _srgb(hex_: str) -> tuple[float, float, float]:
    h = hex_.strip().lstrip("#")
    return tuple(int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def linear(hex_: str) -> tuple[float, float, float]:
    return tuple(_to_linear(c) for c in _srgb(hex_))  # type: ignore[return-value]


def luminance(hex_: str) -> float:
    r, g, b = linear(hex_)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def oklab(r: float, g: float, b: float) -> tuple[float, float, float]:
    """Linear sRGB -> OKLab. `lc`/`mc`/`sc` are the LMS cone responses, named
    `l`/`m`/`s` in every published form of this transform."""
    lc = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    mc = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    sc = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    lc, mc, sc = lc ** (1 / 3), mc ** (1 / 3), sc ** (1 / 3)
    return (
        0.2104542553 * lc + 0.7936177850 * mc - 0.0040720468 * sc,
        1.9779984951 * lc - 2.4285922050 * mc + 0.4505937099 * sc,
        0.0259040371 * lc + 0.7827717662 * mc - 0.8086757660 * sc,
    )


def oklch(hex_: str) -> tuple[float, float]:
    L, a, b = oklab(*linear(hex_))
    return L, math.hypot(a, b)


def simulate(hex_: str, kind: str) -> tuple[float, float, float]:
    r, g, b = linear(hex_)
    m = MACHADO[kind]
    return tuple(
        max(0.0, min(1.0, m[i][0] * r + m[i][1] * g + m[i][2] * b)) for i in range(3)
    )  # type: ignore[return-value]


def delta_e(a: str, b: str, kind: str | None = None) -> float:
    pa = oklab(*(simulate(a, kind) if kind else linear(a)))
    pb = oklab(*(simulate(b, kind) if kind else linear(b)))
    return 100 * math.dist(pa, pb)


# --- css ---------------------------------------------------------------------
@dataclass
class ThemeBlock:
    name: str
    mode: str
    surface: str
    slots: list[str]
    #: The four status colours, which node slots must NOT be confusable with.
    #: Read per theme because several themes override them. Defaulted so a
    #: synthetic palette can be checked without them; the check then skips
    #: rather than passing vacuously.
    status: dict[str, str] = field(default_factory=dict)


def parse_themes(css: str | None = None) -> list[ThemeBlock]:
    """Pull every theme out of app.css: its mode, surface and chart slots.

    Reads the CSS rather than a duplicate list, so a theme cannot be added to
    the stylesheet and quietly skipped by the check.
    """
    text = css if css is not None else APP_CSS.read_text()
    blocks = re.findall(
        r":root(?:\[data-theme='([a-z]+)'\])?\s*\{(.*?)\n\}", text, re.S
    )

    base: dict[str, str] = {}
    themes: list[ThemeBlock] = []
    for name, body in blocks:
        tokens = dict(re.findall(r"^\s*(--[a-z0-9-]+)\s*:\s*([^;]+);", body, re.M))
        tokens = {k: v.strip() for k, v in tokens.items()}
        if not name:
            base = tokens  # `:root` is the default theme AND the fallback
        resolved = {**base, **tokens}

        mode_match = re.search(r"color-scheme:\s*(light|dark)", body)
        # Mode comes from the block's own `color-scheme`, which is what the
        # browser is told; inferring it from surface luminance would be a second
        # source of truth that could disagree with the first.
        mode = mode_match.group(1) if mode_match else "dark"

        slots = [resolved.get(f"--chart-{i}", "") for i in range(1, 9)]
        status = {
            k: resolved.get(f"--{k}", "")
            for k in ("good", "warning", "serious", "critical")
            if resolved.get(f"--{k}", "").startswith("#")
        }
        themes.append(
            ThemeBlock(
                name=name or "dark",
                mode=mode,
                surface=resolved.get("--panel", ""),
                slots=slots,
                status=status,
            )
        )
    return themes


# --- checks ------------------------------------------------------------------
@dataclass
class Finding:
    check: str
    ok: bool
    detail: str


def check(theme: ThemeBlock) -> list[Finding]:
    lo, hi = BAND_OVERRIDE.get(theme.name, BAND[theme.mode])
    out: list[Finding] = []

    lch = [oklch(c) for c in theme.slots]
    outside = [
        (c, round(L, 3))
        for c, (L, _) in zip(theme.slots, lch, strict=True)
        if not lo <= L <= hi
    ]
    out.append(
        Finding(
            "lightness band",
            not outside,
            f"all {len(theme.slots)} inside L {lo}–{hi}" if not outside else f"outside: {outside}",
        )
    )

    grey = [
        (c, round(C, 3))
        for c, (_, C) in zip(theme.slots, lch, strict=True)
        if C < CHROMA_FLOOR
    ]
    out.append(
        Finding("chroma floor", not grey, f"all >= {CHROMA_FLOOR}" if not grey else f"grey: {grey}")
    )

    # STATUS SEPARATION. Node colours say WHICH machine; status colours say
    # HOW IT IS. A chart line the same red as `critical` makes the reader do
    # arithmetic on the legend to find out whether they are looking at an
    # identity or an alarm, and the answer differs per panel.
    #
    # Measured before this check existed: slot 8 sat ΔE 3.9 from `critical`,
    # slot 2 ΔE 5.1, slot 5 ΔE 6.4 — indistinguishable rather than merely
    # close. Six of eight were inside ΔE 10.
    if theme.status:
        near = [
            (slot, name, round(d, 1))
            for slot in theme.slots
            for name, colour in theme.status.items()
            if (d := delta_e(slot, colour)) < STATUS_FLOOR
        ]
        worst = min((d for _, _, d in near), default=None)
        out.append(
            Finding(
                "status separation",
                not near,
                f"all >= {STATUS_FLOOR} from good/warning/serious/critical"
                if not near
                else f"{len(near)} too close, worst ΔE {worst}: "
                + ", ".join(f"{s}<->{n} {d}" for s, n, d in sorted(near, key=lambda t: t[2])[:3]),
            )
        )

    pairs = list(zip(theme.slots, theme.slots[1:], strict=False))
    worst_cvd, worst_pair = min(
        ((min(delta_e(a, b, "protan"), delta_e(a, b, "deutan")), (a, b)) for a, b in pairs),
        key=lambda t: t[0],
    )
    out.append(
        Finding(
            "cvd separation",
            worst_cvd >= CVD_TARGET,
            f"worst adjacent {worst_pair[0]}<->{worst_pair[1]} ΔE {worst_cvd:.1f}",
        )
    )

    worst_norm, norm_pair = min(
        ((delta_e(a, b), (a, b)) for a, b in pairs), key=lambda t: t[0]
    )
    out.append(
        Finding(
            "normal-vision floor",
            worst_norm >= NORMAL_FLOOR,
            f"worst adjacent {norm_pair[0]}<->{norm_pair[1]} ΔE {worst_norm:.1f}",
        )
    )

    allowed = CONTRAST_ALLOWANCES.get(theme.name, {})
    low = []
    for i, c in enumerate(theme.slots, start=1):
        ratio = contrast(c, theme.surface)
        if ratio < CONTRAST_MIN and f"--chart-{i}" not in allowed:
            low.append((f"--chart-{i}", c, round(ratio, 2)))
    out.append(
        Finding(
            "contrast vs surface",
            not low,
            f"all >= {CONTRAST_MIN}:1 against {theme.surface}"
            + (f" ({len(allowed)} allowed)" if allowed else "")
            if not low
            else f"below {CONTRAST_MIN}:1: {low}",
        )
    )
    return out


def main() -> int:
    failed = 0
    for theme in parse_themes():
        findings = check(theme)
        bad = [f for f in findings if not f.ok]
        failed += bool(bad)
        print(f"\n{theme.name}  ({theme.mode}, surface {theme.surface})")
        for f in findings:
            print(f"  [{'PASS' if f.ok else 'FAIL'}] {f.check:22} {f.detail}")
    print("\nALL THEMES PASS" if not failed else f"\n{failed} theme(s) FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
