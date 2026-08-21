# Where styling reasoning lives, once styling is utilities

Established for roadmap AB phase 3, before converting the tables — because the
tables are where 26–36% of the CSS is explanation, and discovering there is
nowhere to put it *during* the refactor is the bad version of finding out.

## The problem

Hand-written CSS puts the reason directly above the rule it explains:

```css
/* These columns swing between an em dash and a live reading every time a
   model wakes, which resized the whole row on every transition. */
.toks { min-width: calc(7ch + 24px); }
```

A utility class string has nowhere to put that. `class="w-[9ch] text-right"`
inside markup is not a place a paragraph can go, and a comment above the
element describes the *element*, not the styling decision.

## The convention

**Name the class string, and comment the name.** Utilities live in a `const` in
the component's script; the reasoning sits above it exactly where it used to
sit above the rule.

```ts
/* Numeric cells are right-aligned and tabular so a column of readings scans as
   a column. `tabular-nums` matters more than it looks: proportional digits
   make 1.1 and 8.8 different widths, and a value that changes width every
   frame is unreadable at a glance. */
const NUM_CELL = 'text-right tabular-nums';
```

**Why this and not the alternatives:**

- *Comment above the element in markup* — describes the element, and repeats
  once per usage. The tables render cells through one snippet with eleven
  branches; the explanation would have to be duplicated or orphaned.
- *A separate design doc* — the reasoning stops being adjacent to the code,
  which is the entire property being protected.
- *`@utility` / `@apply` in app.css* — recreates named CSS classes in a global
  file, which is what the migration is moving away from, and puts component
  reasoning outside the component.

**It is also forced by the tables regardless of comments.** `ModelsTable`
renders eleven cell branches sharing eight class combinations. Repeating a
forty-character utility string eight times is worse than naming it, so the
constants would exist even if nothing needed explaining.

## What stays in a scoped style block

Not everything converts, and pretending otherwise produces worse code than the
CSS it replaced. Keep a residual block for:

- **Structural selectors** — `tbody tr:last-child td`, `tr.idle td`. Tailwind
  can express these as arbitrary variants (`[&:last-child>td]:border-b-0`) and
  the result is less legible than the selector.
- **Pseudo-elements with multiple states** — `ColumnGrip`'s `::after` cue needs
  hover, focus-visible and dragging, each carrying geometry. As variants the
  geometry scatters; as a rule it is stated once.
- **Keyframes** — `ConnectionState`'s theme-aware pulse. Tailwind registers an
  animation but the frames are hand-written CSS wherever they live.

A component that keeps a style block is not a failed conversion. Roughly 40% of
the phase-2 sample landed there, and the honest ones say why in the block.
