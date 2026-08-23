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
export const PORT_STATE = 'rdma_port_state';

/** The four queries, in one place, so a fetch loop and a test agree. */
export const NETWORK_METRICS = [RX, TX, ERRORS, DROPS, PORT_STATE] as const;

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

/** One RDMA port's state series, with the wire it shares a cable with. */
export interface Port {
  node: string;
  device: string;
  port: string;
  /** The paired Ethernet interface, or '' when the agent predates AC1c and so
   *  never said. An unpaired port is still charted — just not beside a link. */
  iface: string;
  column: (number | null)[];
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

/** Collapse the port-state series into one entry per physical port.
 *
 * TWO SERIES CAN DESCRIBE ONE PORT, and merging them is not tidiness — it is
 * the difference between a week of history and half of one. Measured here over
 * 7d: a `cluster` label was added to the targets part way through the window,
 * so 12 of 18 keys have two series, each null where the other has samples.
 * Taking one and discarding the other would silently truncate the chart at the
 * relabel, which looks exactly like the port having stopped reporting.
 *
 * They never overlap — max concurrent series per key is 1 — so a coalesce is
 * lossless. Where they somehow did, the later-listed sample wins rather than
 * being summed: on a two-state axis, 1 + 1 = 2 is off the top of the chart.
 *
 * The same merge covers the agent upgrade that ships the `interface` label: for
 * a window spanning it, one variant carries the pairing and the other does not,
 * and whichever has it wins.
 */
export function ports(
  rows: { labels: Record<string, string>; column: (number | null)[] }[],
): Port[] {
  const byKey = new Map<string, Port>();
  for (const t of rows) {
    const node = t.labels.node ?? '';
    const device = t.labels.device ?? '';
    if (!device) continue;
    const key = `${node}${SEP}${device}${SEP}${t.labels.port ?? ''}`;
    const column = t.column ?? [];
    const found = byKey.get(key);
    if (!found) {
      byKey.set(key, {
        node,
        device,
        port: t.labels.port ?? '',
        iface: t.labels.interface ?? '',
        column: [...column],
      });
      continue;
    }
    found.iface ||= t.labels.interface ?? '';
    for (let j = 0; j < column.length; j++) {
      if (column[j] != null) found.column[j] = column[j];
    }
  }
  return [...byKey.values()].sort(
    (a, b) => a.node.localeCompare(b.node) || a.device.localeCompare(b.device),
  );
}

/** A port worth drawing: one that was NOT up for the whole window.
 *
 * Signal-gated like the fault charts, and for the same reason — a flat line at
 * "up" is a chart-sized restatement of the green dot already on the Network
 * table. What it cannot say, and this can, is WHEN it was not.
 *
 * A port that was DOWN the whole window is very much worth drawing. Measured
 * here: two of sparky's ports have read 0 for seven days straight, which the
 * live table shows as a red dot with no indication that it has been that way
 * all week.
 */
export const portIsNotable = (p: Port) =>
  p.column.some((v) => v != null && v !== 1);

/** The chart for one port. */
export function portChart(p: Port): LinkChart {
  const link = { key: linkKey(p.node, p.iface || p.device), node: p.node, iface: p.iface };
  return {
    key: `${p.node}${SEP}${p.device}${SEP}${p.port}${SEP}state`,
    link,
    metric: {
      key: `${p.node}${SEP}${p.device}${SEP}${p.port}${SEP}state`,
      // The DEVICE names this chart, even when it sits under its interface's
      // throughput: `roceP2p1s0f1` is what `ibstat` prints and what the RDMA
      // table lists, and a chart captioned with the netdev name would be a
      // second chart apparently about the same thing.
      label: `${p.device} port ${p.port}`,
      unit: '',
      verbatim: true,
      states: ['down', 'up'],
    },
    names: ['state'],
    columns: [p.column],
    quiet: false,
  };
}

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
        verbatim: true,
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
        // No unit: the value is a COUNT over the chart's own rate window, not a
        // rate. "3/s" would be a claim about a second; "3" is a claim about
        // what happened, which is the question.
        unit: '',
        verbatim: true,
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
  /** RDMA ports, already merged. Empty when the cluster has no RoCE. */
  rdma: Port[] = [],
): { charts: LinkChart[]; quiet: number } {
  const byName = new Map(names.map((n, i) => [n, columns[i]]));
  const wanted = (node: string) => activeNodes === null || activeNodes.includes(node);
  const all = links(names, nodeOrder).filter((l) => wanted(l.node));

  /* A port chart goes to the interface it shares a cable with, which is the
     whole reason AC1c had to ship first. Keyed by node AND interface: `enP7s7`
     exists on all three boxes, and a key of the interface alone would hang
     sparkjr's port off sparky's chart. */
  const notable = rdma.filter((p) => wanted(p.node) && portIsNotable(p));
  const beside = new Map<string, Port[]>();
  const unpaired: Port[] = [];
  for (const p of notable) {
    const key = linkKey(p.node, p.iface);
    if (p.iface && byName.has(`${key}${SEP}${RX}`)) {
      beside.set(key, [...(beside.get(key) ?? []), p]);
    } else {
      // No pairing (an agent from before AC1c), or paired to an interface with
      // no throughput series of its own. Charted anyway, at the end of the
      // grid: an unplaceable chart is still a fact, and dropping it would make
      // a flapping port invisible on exactly the deployment least likely to
      // notice — the one running the older agent.
      unpaired.push(p);
    }
  }

  const charts: LinkChart[] = [];
  let quiet = 0;
  for (const link of all) {
    const built = chartsFor(link, byName);
    const attached = (beside.get(link.key) ?? []).map(portChart);
    // "Quiet" is a property of the LINK, not of each chart: a fault chart is
    // never quiet by construction, and counting per-chart would report a
    // number that does not match the interfaces being hidden.
    if (built.length && built[0].quiet) {
      quiet++;
      // ...unless a port on that cable flapped. A silent link whose RoCE port
      // dropped is not an idle link, it is the most interesting thing on the
      // card, and hiding it behind the idle toggle would bury the one chart
      // worth seeing.
      if (!includeQuiet && !attached.length) continue;
    }
    charts.push(...built, ...attached);
  }
  charts.push(...unpaired.map(portChart));
  return { charts, quiet };
}
