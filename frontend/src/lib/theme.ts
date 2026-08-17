/** Bridge between CSS custom properties and code that needs literal colours.
 *
 * uPlot draws to a canvas, so it can't consume `var(--series-1)` — it needs a
 * resolved value. Reading the tokens rather than duplicating hex here keeps one
 * source of truth: change a colour in app.css and the charts follow.
 */

export function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** Node identity colours — eight slots, then deliberately none.
 *
 * Was three, cycling with `--series-${slot % 3 + 1}`, which gave the fourth
 * node the first node's hue. Colour is supposed to follow the entity; once two
 * entities share one it has stopped identifying anything, and a four-node
 * cluster is the very first case that hits. NodeCard fixed that for the cards
 * and this function did not follow, so the History legend and the cards
 * disagreed from the fourth node on — the legend called gx10-d blue while its
 * card did not.
 *
 * `--chart-1..8` is the same palette extended: its first three ARE the old node
 * hues, so one, two and three-node setups are unchanged, and it is validated as
 * a categorical set for CVD separation against every theme's surface.
 *
 * PAST EIGHT, NO COLOUR — a neutral rule, and identity rides on the node name,
 * which is beside every swatch anyway. Generating a ninth hue or wrapping round
 * would both reintroduce the collision this exists to prevent. Never cycle.
 */
export const NODE_SLOTS = 8;

export function nodeColor(slot: number | undefined): string {
  /* An UNKNOWN node takes the neutral, not slot 0.
   *
   * Callers reach this as `nodeColor(slots.get(name))`, and a name absent from
   * the slot map used to fall back to `?? 0` — which silently painted it in the
   * FIRST node's colour. A history series can legitimately name a node the
   * current inventory does not: one recently removed from cluster.yml, or
   * renamed, still has samples in Prometheus for the rest of the window. Two
   * entities sharing a hue is precisely what this palette exists to prevent, so
   * an unrecognised one gets no hue at all rather than someone else's. */
  if (slot === undefined) return cssVar('--rule');
  return slot < NODE_SLOTS ? cssVar(`--chart-${slot + 1}`) : cssVar('--rule');
}

/** The same rule as a CSS value, for markup that can use `var()` directly.
 *  One definition, so a card and a chart cannot drift apart again. */
export function nodeColorVar(slot: number): string {
  return slot < NODE_SLOTS ? `var(--chart-${slot + 1})` : 'var(--rule)';
}

/** Stable slot per node id.
 *
 * Colour follows the node, not its position, so filtering out a node must not
 * repaint the survivors. Derived once from the ordered node list and reused by
 * both the cards and the charts, so a node is the same colour everywhere.
 */
export function nodeSlots(nodeIds: string[]): Map<string, number> {
  return new Map(nodeIds.map((id, i) => [id, i]));
}

export interface ChartTheme {
  ink: string;
  inkMuted: string;
  grid: string;
  axis: string;
  surface: string;
}

export function chartTheme(): ChartTheme {
  return {
    ink: cssVar('--ink'),
    inkMuted: cssVar('--ink-muted'),
    /* LINES stay recessive. A grid is a reference, not content, and one that
       competes with the data is worse than none. `--rule` is the hairline
       token and that is exactly right here. */
    grid: cssVar('--rule'),
    /* TEXT does not. uPlot uses an axis's `stroke` for its TICK LABELS, so this
       token is type, not a line — and it was `--rule` too, which painted every
       number on every axis in the border colour. Measured against the panel
       that is 1.24:1 in dark, 1.29:1 in light and 1.56:1 in cyberpunk, against
       a 4.5:1 floor for text this size: the labels were very nearly invisible,
       and an axis whose numbers cannot be read is an axis that is not doing its
       job.
       `--ink-muted` clears the floor in all three themes (6.02 / 4.61 / 6.18)
       while staying subordinate to the plotted lines. It is also what the table
       column headings use, so a chart's axis and a table's header now read at
       the same weight, which is what they are. */
    axis: cssVar('--ink-muted'),
    surface: cssVar('--panel'),
  };
}
