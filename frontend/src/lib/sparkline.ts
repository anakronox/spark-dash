/** A row-height sparkline as an SVG path.
 *
 * NOT NETWORK-SPECIFIC, which is why it lives here rather than where it was
 * written. Any table of a metric over time wants the same thing, and the second
 * caller — the thermal card — would otherwise have imported it from
 * `network-history`, which is how a module becomes a junk drawer.
 */

const finite = (c: (number | null)[]) =>
  c.filter((v): v is number => v != null && isFinite(v));

/** An SVG path for a row-height sparkline.
 *
 * NOT A uPlot INSTANCE, and that is the entire point of the table. A canvas and
 * a ResizeObserver per row would reproduce, at 190 rows, exactly the cost the
 * table exists to escape. One `<path>` costs nothing and needs no observer.
 *
 * Downsampled by taking the MAX of each bucket, not the mean. A sparkline is
 * read for its shape, and averaging is what turns a two-minute spike into a
 * ripple — which is the one thing on the row worth noticing.
 *
 * Scaled to the ROW's own maximum, like the small multiples above it: a shared
 * scale would flatten every fabric link against the management port, which is
 * the six-orders-of-magnitude problem this card already solved once.
 */
export function sparkPath(
  values: (number | null)[],
  width: number,
  height: number,
  buckets = 24,
): string {
  if (!values.length || width <= 0 || height <= 0) return '';
  const n = Math.min(buckets, values.length);
  const size = values.length / n;

  const points: (number | null)[] = [];
  for (let i = 0; i < n; i++) {
    const slice = finite(values.slice(Math.floor(i * size), Math.floor((i + 1) * size)));
    points.push(slice.length ? Math.max(...slice) : null);
  }

  const top = Math.max(...points.filter((v): v is number => v != null), 0);
  const step = n > 1 ? width / (n - 1) : 0;
  // A flat line sits on the BASELINE, not halfway up: zero traffic should read
  // as zero, and a row of mid-height lines would imply activity everywhere.
  const y = (v: number) => (top > 0 ? height - (v / top) * height : height);

  let d = '';
  let pen = false;
  points.forEach((v, i) => {
    if (v == null) {
      // A gap is a gap. Bridging it would draw a line through an outage.
      pen = false;
      return;
    }
    d += `${pen ? 'L' : 'M'}${(i * step).toFixed(1)} ${y(v).toFixed(1)}`;
    pen = true;
  });
  return d;
}
