/** Bridge between CSS custom properties and code that needs literal colours.
 *
 * uPlot draws to a canvas, so it can't consume `var(--series-1)` — it needs a
 * resolved value. Reading the tokens rather than duplicating hex here keeps one
 * source of truth: change a colour in app.css and the charts follow.
 */

export function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** Node identity colours, in the fixed categorical order.
 *
 * Three slots because that's the validated all-pairs limit for colourblind
 * separation — past three, hues start failing the contrast floors against each
 * other. A fourth node reuses slot 1 rather than inventing a hue, which would
 * be indistinguishable under CVD and break the guarantee for every other pair.
 */
export const NODE_SLOTS = 3;

export function nodeColor(slot: number): string {
  return cssVar(`--series-${(slot % NODE_SLOTS) + 1}`);
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
    grid: cssVar('--rule'),
    axis: cssVar('--rule'),
    surface: cssVar('--panel'),
  };
}
