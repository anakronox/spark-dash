/** Geometry and time labels shared by every grid of small multiples.
 *
 * System Activity and Network Activity each kept their own copy of the column
 * rule, and both picked columns from the VIEWPORT with media queries. A card
 * placed in a half-width column then drew four 190px charts on a wide screen,
 * because the screen was wide and the card was not. Columns now come from the
 * width the grid actually has.
 *
 * Plain TypeScript with no runes and no DOM, so tests/js runs it for real.
 */

/** Narrowest chart a grid will draw before it halves its columns. Four times
 *  this plus the gaps is what a full-width card has on a 1440px screen. */
export const MIN_CHART_PX = 300;

/** The time axis band, for the charts that draw one.
 *
 * One line of labels. uPlot's default is a 50px band that carries a second
 * line for the date on the first tick, and every chart in a grid repeated it:
 * measured on the live dashboard, 2026-10-12, a 132px chart had 65px of plot. */
export const TIME_AXIS_PX = 22;

/** The most columns a grid of `n` charts uses, snapping 1 / 2 / 4 so cards
 *  side by side line up. One chart gets the full width rather than a quarter. */
export function maxColsFor(n: number): number {
  return n >= 4 ? 4 : n >= 2 ? 2 : 1;
}

/** Columns for `n` charts in `width` px with `gap` px between them.
 *
 * Halves from the maximum until each chart is at least MIN_CHART_PX. An
 * unmeasured width (0, before the first layout) keeps the maximum, so the first
 * paint is not a single column that then jumps. */
export function gridCols(n: number, width: number, gap: number): number {
  let cols = maxColsFor(n);
  if (!(width > 0)) return cols;
  while (cols > 1 && (width - gap * (cols - 1)) / cols < MIN_CHART_PX) cols = cols === 4 ? 2 : 1;
  return cols;
}

/** Is chart `i` of `count` on the grid's bottom row? Only that row draws the
 *  time axis: every chart in a grid shares one window and one synced cursor,
 *  so the labels above it repeat the same times. */
export function onLastRow(i: number, count: number, cols: number): boolean {
  return Math.floor(i / cols) === Math.ceil(count / cols) - 1;
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

const dayOf = (d: Date) => `${MONTHS[d.getMonth()]} ${d.getDate()}`;

function clock(d: Date, minutes: boolean): string {
  const h = d.getHours() % 12 || 12;
  const ap = d.getHours() < 12 ? 'am' : 'pm';
  return minutes ? `${h}:${String(d.getMinutes()).padStart(2, '0')}${ap}` : `${h}${ap}`;
}

/** Does a window from `lo` to `hi` (unix seconds) cross a local midnight? */
export function crossesDay(lo: number, hi: number): boolean {
  return dayOf(new Date(lo * 1000)) !== dayOf(new Date(hi * 1000));
}

/** One line per tick, in local time.
 *
 * Within one day: `9:15pm`. Across days, a tick on a midnight is the date,
 * `Oct 12`, and the rest are times, minutes only when the tick has them. The
 * date appears where the day changes and nowhere else.
 *
 * Whether the window crosses a day is decided from the WINDOW, `lo` to `hi`,
 * not from the ticks: a 24h window from 10pm has ticks at 12am, 6am, 12pm and
 * 6pm, all on one date, and its midnight read "12:00am" until this took the
 * range. The ticks are the fallback when no range is given. */
export function timeLabels(
  splits: number[],
  lo: number = splits[0],
  hi: number = splits[splits.length - 1],
): string[] {
  if (!splits.length) return [];
  const multiDay = crossesDay(lo, hi);
  return splits.map((t) => {
    const d = new Date(t * 1000);
    if (!multiDay) return clock(d, true);
    if (d.getHours() === 0 && d.getMinutes() === 0) return dayOf(d);
    return clock(d, d.getMinutes() !== 0);
  });
}

/** The tooltip's time. It carries the date when the window crosses a day,
 *  since the axis then names a day only at midnight. */
export function stampOf(ts: number, withDate: boolean): string {
  const d = new Date(ts * 1000);
  return withDate ? `${dayOf(d)} ${clock(d, true)}` : clock(d, true);
}
