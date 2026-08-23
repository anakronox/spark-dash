/** Network history: the fabric, over time.
 *
 * SEPARATE FROM `history.ts` BECAUSE THE SHAPE IS DIFFERENT, not because the
 * subject is. Every metric there yields one series per node, and the charts key
 * on that: one chart per metric, one line per node, colour is the node. These
 * yield one series per INTERFACE PER DIRECTION — 28 series across 3 nodes on
 * this cluster — which is a second dimension no chip in that file has.
 *
 * The grouping that turns those series back into readable charts is here rather
 * than in the component so it can be tested without a DOM.
 */

import type { HistorySeries, MetricSpec } from './history';

/** Backend metric keys this card fetches. Order is the order they are drawn
 *  in, per interface. */
export const RX = 'network_rx_bits';
export const TX = 'network_tx_bits';
export const ERRORS = 'network_errors';
export const DROPS = 'network_drops';

/** The four queries, in one place, so a fetch loop and a test agree. */
export const NETWORK_METRICS = [RX, TX, ERRORS, DROPS] as const;

/** Sub-series names. Short: they are the tooltip's left column, repeated on
 *  every row, and "receive" would push the numbers off the edge. */
export const DIRECTIONS: Record<'rx' | 'tx', string> = { rx: 'rx', tx: 'tx' };

/** The separator inside a packed column name.
 *
 * NUL, not a space or a slash. These names round-trip through `toColumnar`,
 * which deals in plain strings, and every printable candidate turns up in a
 * real device name somewhere: `enP2p1s0f0np0` has no space today, and
 * `br-1a2b/vlan3` has a slash. A separator that cannot occur needs no escaping
 * and cannot be quietly wrong on hardware nobody here has.
 *
 * Written as an ESCAPE. The first cut of this file put the byte itself in the
 * source, where it is invisible in an editor, turns the file binary to `grep`,
 * and makes every later edit that types a plain space silently miss. */
const SEP = '\u0000';

/** One interface's identity, packed. */
export const linkKey = (node: string, iface: string) => `${node}${SEP}${iface}`;

export interface Link {
  key: string;
  node: string;
  iface: string;
}

export interface LinkChart {
  /** Unique across the grid — `{#each}` keys on it. */
  key: string;
  link: Link;
  metric: MetricSpec;
  names: string[];
  columns: (number | null)[][];
  /** True when every sample is zero or absent. Drives the idle filter. */
  quiet: boolean;
}

/** Series tagged with the direction they came from.
 *
 * The direction is not in the series' own labels — it is which QUERY returned
 * it — so it has to be attached at the point where that is still known. */
export interface Tagged {
  metric: string;
  series: HistorySeries;
}

/** Column name for one tagged series. Unique per (interface, query). */
export function columnName(t: Tagged): string {
  const node = t.series.labels.node ?? t.series.node ?? '';
  const iface = t.series.labels.interface ?? '';
  return `${linkKey(node, iface)}${SEP}${t.metric}`;
}

const parse = (name: string) => {
  const [node, iface, metric] = name.split(SEP);
  return { node, iface, metric };
};

/** The node out of a packed column name. Exported so nothing outside this file
 *  has to know what the separator is. */
export const columnNode = (name: string) => parse(name).node ?? '';

/** Every interface that returned a series, in a stable order.
 *
 * ORDERED BY NODE THEN BY NAME, never by traffic. Sorting the grid by how busy
 * a link is would rearrange it under the reader every time the data refreshed
 * — and the chart you were looking at would be the one that moved, since
 * whatever you are watching is whatever is changing.
 *
 * Node order comes from the caller so it matches the cards above; anything
 * Prometheus knows that the live inventory does not is appended rather than
 * dropped, the same rule the history legend follows.
 */
export function links(names: string[], nodeOrder: string[]): Link[] {
  const seen = new Map<string, Link>();
  for (const name of names) {
    const { node, iface } = parse(name);
    if (!iface) continue;
    const key = linkKey(node, iface);
    if (!seen.has(key)) seen.set(key, { key, node, iface });
  }
  const rank = (n: string) => {
    const i = nodeOrder.indexOf(n);
    return i === -1 ? nodeOrder.length : i;
  };
  return [...seen.values()].sort(
    (a, b) => rank(a.node) - rank(b.node) || a.iface.localeCompare(b.iface),
  );
}

const allQuiet = (cols: (number | null)[][]) =>
  cols.every((c) => c.every((v) => v == null || v === 0));

/** The charts for one interface: throughput always, faults only when there
 *  were any.
 *
 * FAULTS ARE SIGNAL-GATED, the same rule the Network table's `err` and `drop`
 * columns already follow: absent while they have nothing to say, and back in
 * view on their first non-zero sample. A chart of a flat zero is not a
 * reassurance, it is a chart-sized hole competing for space with one that
 * moves — and on a healthy fabric that would be 14 of them.
 *
 * What the gate buys, and why the answer is not simply "no chart": the table
 * can say errors exist, and cannot say WHEN. A fault chart that appears exactly
 * when there is a fault answers "they started at 02:14 and stopped", which is
 * the difference between a bad cable and something that happens during the
 * backup window.
 */
export function chartsFor(
  link: Link,
  byName: Map<string, (number | null)[]>,
): LinkChart[] {
  const col = (metric: string) => byName.get(`${link.key}${SEP}${metric}`);
  const out: LinkChart[] = [];

  const rx = col(RX);
  const tx = col(TX);
  const throughput = [rx, tx].filter((c): c is (number | null)[] => c != null);
  if (throughput.length) {
    out.push({
      key: `${link.key}${SEP}throughput`,
      link,
      metric: {
        key: `${link.key}${SEP}throughput`,
        label: link.iface,
        // Bits, matching the Network table above. `b/s` and not `bps` because
        // the SI prefix is prepended to it — "580Mb/s" reads, "580Mbps" invites
        // being read as one word.
        unit: 'b/s',
        si: true,
        dashed: DIRECTIONS.tx,
      },
      names: [rx && DIRECTIONS.rx, tx && DIRECTIONS.tx].filter(
        (n): n is string => !!n,
      ),
      columns: throughput,
      quiet: allQuiet(throughput),
    });
  }

  const errors = col(ERRORS);
  const drops = col(DROPS);
  const faults = [errors, drops].filter((c): c is (number | null)[] => c != null);
  if (faults.length && !allQuiet(faults)) {
    out.push({
      key: `${link.key}${SEP}faults`,
      link,
      metric: {
        key: `${link.key}${SEP}faults`,
        label: `${link.iface} faults`,
        unit: '/s',
        dashed: 'drops',
      },
      names: [errors && 'errors', drops && 'drops'].filter((n): n is string => !!n),
      columns: faults,
      // Never — it is only built when something is non-zero.
      quiet: false,
    });
  }

  return out;
}

/** The whole grid, in draw order.
 *
 * A fault chart sits IMMEDIATELY AFTER the interface it belongs to rather than
 * in a block of its own at the end. It breaks the rhythm of a 4-wide grid, and
 * that is the lesser cost: the question a fault chart exists to answer is
 * "were the errors while it was busy", and answering it should not require
 * scrolling between two halves of the same card.
 */
export function buildGrid(
  names: string[],
  columns: (number | null)[][],
  nodeOrder: string[],
  /** Nodes to draw. `null` means all of them. */
  activeNodes: string[] | null,
  /** Include interfaces that were flat zero for the whole window. */
  includeQuiet: boolean,
): { charts: LinkChart[]; quiet: number } {
  const byName = new Map(names.map((n, i) => [n, columns[i]]));
  const all = links(names, nodeOrder).filter(
    (l) => activeNodes === null || activeNodes.includes(l.node),
  );

  const charts: LinkChart[] = [];
  let quiet = 0;
  for (const link of all) {
    const built = chartsFor(link, byName);
    // "Quiet" is a property of the LINK, not of each chart: a fault chart is
    // never quiet by construction, and counting per-chart would report a
    // number that does not match the interfaces being hidden.
    if (built.length && built[0].quiet) {
      quiet++;
      if (!includeQuiet) continue;
    }
    charts.push(...built);
  }
  return { charts, quiet };
}
