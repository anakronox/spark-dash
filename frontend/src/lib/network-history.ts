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
export const LINK_UP = 'network_link_up';

/** The four queries, in one place, so a fetch loop and a test agree. */
export const NETWORK_METRICS = [RX, TX, ERRORS, DROPS, PORT_STATE, LINK_UP] as const;

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

/** Fill in a pairing the metric never carried, from the live snapshot.
 *
 * `rdma_port_info.interface` shipped in AC1c and only reaches Prometheus once a
 * node's stack is redeployed, so on a cluster running an older agent every port
 * arrives unpaired — and the `roce` column then reads "—" for every fabric link
 * on the page, which is the one column that division exists to explain.
 *
 * The live snapshot has known the pairing all along; it is the same fact by a
 * different transport, and it is what already decides the divisions. NOT a
 * regex over device names: `roceP2p1s0f0` against `enP2p1s0f0np0` looks
 * derivable and would be a guess that happens to work on this hardware.
 *
 * Only fills what is MISSING. Where the metric carries a pairing it wins: that
 * one is contemporaneous with the samples, and the live answer is not.
 */
export function pairPorts(
  rows: Port[],
  live: { node: string; device: string; iface: string }[],
): Port[] {
  if (!live.length) return rows;
  const known = new Map(live.map((l) => [`${l.node}${SEP}${l.device}`, l.iface]));
  return rows.map((p) =>
    p.iface ? p : { ...p, iface: known.get(`${p.node}${SEP}${p.device}`) ?? '' },
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

/** One labelled division of the card. */
export interface ChartGroup {
  key: 'fabric' | 'management';
  label: string;
  /** One line under the heading saying what the division MEANS, since "fabric"
   *  is a word people use loosely and this one has an exact definition. */
  note: string;
  charts: LinkChart[];
}

/** The divisions, named once.
 *
 * Shared by the chart grid and the overview table so a link cannot be filed
 * under Fabric in one view and Management in the other — which would be worse
 * than having no division at all, because each view on its own would look
 * right. */
export const DIVISIONS: { key: 'fabric' | 'management'; label: string; note: string }[] = [
  {
    key: 'fabric',
    label: 'Fabric',
    note: 'RoCE links — interfaces paired with an RDMA device',
  },
  {
    key: 'management',
    label: 'Management',
    note: 'everything else — no RDMA device on this interface',
  },
];

/** Which division a link belongs to.
 *
 * ONE RULE, in one place. An interface with an RDMA port observed on it is
 * fabric whatever the caller's set says: the pairing IS the definition, and an
 * attached port is that pairing observed directly. */
export function divide(
  key: string,
  hasPort: boolean,
  fabric: ReadonlySet<string>,
): 'fabric' | 'management' {
  return fabric.has(key) || hasPort ? 'fabric' : 'management';
}

/** The grid, divided into fabric and management.
 *
 * WHY THE DIVISION EXISTS. Small multiples put every link on its own axis,
 * which fixes the six-orders-of-magnitude problem and creates a second one: a
 * flat grid of fourteen charts gives a 200Gb RoCE link and a wifi port exactly
 * the same weight, and on an inference cluster those are not the same question.
 * Reading the fabric means picking its charts out of the management ports by
 * remembering which device names are which.
 *
 * FABRIC IS DEFINED BY THE RoCE PAIRING, not by link speed and not by name. A
 * speed threshold would be a guess that happens to work here — sort a 100Gb
 * storage NIC with no RDMA into "fabric" and it is wrong in a way nobody would
 * notice. The pairing is the agent's own answer to "is this a fabric link",
 * read from sysfs, and it is the same fact that already drives the paired alert
 * exclusion. Matching `roce*` against `en*` by string would be the third way to
 * do it and is the one the roadmap rejects: the real answer exists.
 *
 * A port chart is always fabric — it is an RDMA port by construction, including
 * when its own pairing is unknown.
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
  /** `linkKey(node, iface)` for every interface paired with an RDMA device. */
  fabric: ReadonlySet<string> = new Set(),
): { groups: ChartGroup[]; quiet: number } {
  const byName = new Map(names.map((n, i) => [n, columns[i]]));
  const wanted = (node: string) => activeNodes === null || activeNodes.includes(node);
  const all = links(names, nodeOrder).filter((l) => wanted(l.node));

  /* A port chart goes under the interface it shares a cable with, which is the
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
      // No pairing (an agent from before AC1c and no live snapshot either), or
      // paired to an interface with no throughput series of its own. Charted
      // anyway, at the end of the fabric group: an unplaceable chart is still a
      // fact, and dropping it would hide a flapping port on exactly the
      // deployment least likely to notice.
      unpaired.push(p);
    }
  }

  const inFabric: LinkChart[] = [];
  const inManagement: LinkChart[] = [];
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
    const isFabric = divide(link.key, attached.length > 0, fabric) === 'fabric';
    (isFabric ? inFabric : inManagement).push(...built, ...attached);
  }
  inFabric.push(...unpaired.map(portChart));

  const charts = { fabric: inFabric, management: inManagement };
  const groups: ChartGroup[] = DIVISIONS.filter((d) => charts[d.key].length).map((d) => ({
    ...d,
    charts: charts[d.key],
  }));
  return { groups, quiet };
}


/* ---------------------------------------------------------------- overview
 *
 * The card draws one chart per interface, which is readable and does not
 * scale: chart count grows with the cluster. Measured — a fully-populated GB10
 * has 6 interfaces and 4 RDMA ports, so 32 nodes is ~190 links and, at 7d with
 * faults and port states, ~500 charts. The wall is not the data (five range
 * queries serve the card however many links it draws) but uPlot INSTANCES:
 * every chart is a canvas plus a ResizeObserver.
 *
 * So the overview is a table — one row per link, constant screen cost at any
 * cluster size, nothing hidden — and the charts open from it.
 */

/** One link as a table row. Everything here derives from the queries the card
 *  already fetches; none of it is a second request. */
export interface LinkRow {
  key: string;
  node: string;
  iface: string;
  division: 'fabric' | 'management';
  /** rx + tx per sample. One wire, one line: the row answers "how much did
   *  this carry", and the direction split is what opening the chart is for. */
  series: (number | null)[];
  peak: number;
  /** The most recent sample that exists. Not the last ELEMENT, which is null
   *  whenever the newest bucket has not filled yet. */
  now: number;
  mean: number;
  /** peak / mean. See TIER_BURST for why this and not standard deviation. */
  burst: number;
  errors: number;
  drops: number;
  /** False when the interface was down at any point in the window. */
  up: boolean;
  /** Worst RDMA port state on this cable, or null when there is no RoCE here. */
  port: 'up' | 'down' | 'flapped' | null;
  /** Negotiated link speed in Mb/s, from the live snapshot. Null when the
   *  driver reports none — every wifi port here. */
  speedMbps: number | null;
  /** False when this node's config excludes the interface from alerting.
   *
   * SHOWN, never filtered on. Reusing `monitored` to decide what this card
   * DRAWS was rejected when the divisions shipped — that flag exists to decide
   * what alerts, and a flag serving two purposes is how a flag starts lying.
   * Reporting it is the opposite: it tells a reader why a link they can see is
   * not paging anyone. */
  monitored: boolean;
  tier: number;
  /** Which rule put this row where it is. A sort nobody can explain reads as
   *  the data being wrong — the same argument `dropSortWhenHidden` makes. */
  why: string;
}

/** Rank tiers, lexicographic and low-is-urgent.
 *
 * TIERS, NOT A WEIGHTED SCORE. A weighted sum of "downness" and "burstiness"
 * and "volume" needs three invented constants, and the moment anyone asks why
 * a row is third the only honest answer is "arithmetic". A tier is a sentence:
 * it is here because its port flapped.
 */
export const TIER_STATE = 0;
export const TIER_BURST = 1;
export const TIER_STEADY = 2;

/** peak/mean at or above which a link counts as bursty.
 *
 * MEASURED, not picked. Over 24h on this cluster the links separate into three
 * groups with an order of magnitude of clear air between them: 18.4-20.3 for
 * the three that actually did something, 1.5-1.8 for steady background
 * traffic, and 1.1 for the near-flat RoCE links. Four sits in the middle of
 * that gap with a factor of four of margin on both sides.
 *
 * Peak-over-mean rather than a coefficient of variation, which ranks the same
 * links in the same order (cv 2.21-2.73 / 0.18-0.20 / 0.02-0.03) and cannot be
 * explained in a tooltip. "It peaked at twenty times its average" is a sentence
 * a reader can check against the sparkline beside it.
 *
 * BOUNDED BY THE SAMPLE COUNT: with n points the ratio cannot exceed n, so a
 * short window compresses it. Harmless in practice — every range here returns
 * 60-170 samples by construction (see RangeSpec.step), and all links on one
 * render share a window, so the comparison between them is always fair. Worth
 * knowing anyway: it is what made the first version of this measure's test
 * wrong, using a two-sample fixture that could never exceed 2.
 */
export const BURST_RATIO = 4;

const finite = (c: (number | null)[]) =>
  c.filter((v): v is number => v != null && isFinite(v));

/** Total over the window for a counting series, ignoring gaps. */
const total = (c: (number | null)[] | undefined) =>
  c ? finite(c).reduce((a, b) => a + b, 0) : 0;

/** One link's row. `port` is the worst state among the RDMA ports on it. */
export function linkRow(
  link: Link,
  division: 'fabric' | 'management',
  byName: Map<string, (number | null)[]>,
  onThisLink: Port[] = [],
  /** Per-interface facts that have no time series: the negotiated speed and
   *  whether alerting watches this link. Both come from the live snapshot,
   *  which is where the agent has always reported them. */
  live: { speedMbps: number | null; monitored: boolean } = { speedMbps: null, monitored: true },
): LinkRow {
  const col = (metric: string) => byName.get(`${link.key}${SEP}${metric}`);
  const rx = col(RX);
  const tx = col(TX);
  const len = Math.max(rx?.length ?? 0, tx?.length ?? 0);

  const series: (number | null)[] = [];
  for (let i = 0; i < len; i++) {
    const a = rx?.[i];
    const b = tx?.[i];
    // null + 0 is 0, which would draw a sample that does not exist. A point is
    // only real when at least one direction reported.
    series.push(a == null && b == null ? null : (a ?? 0) + (b ?? 0));
  }

  const seen = finite(series);
  const peak = seen.length ? Math.max(...seen) : 0;
  const mean = seen.length ? seen.reduce((a, b) => a + b, 0) / seen.length : 0;
  const now = [...series].reverse().find((v) => v != null) ?? 0;

  const upCol = col(LINK_UP);
  const up = !(upCol ?? []).some((v) => v != null && v === 0);

  /* Worst wins. One dead port on a cable with three healthy ones is the fact
     about that cable, and a row summarising it as "up" would be a row that
     hides the only thing worth knowing. Ranked explicitly rather than resolved
     with a chain of conditions — the chain was correct and unreadable, which on
     a rule like this is the same as being unverifiable. */
  const SEVERITY = { up: 0, flapped: 1, down: 2 } as const;
  let port: LinkRow['port'] = null;
  for (const p of onThisLink) {
    const vals = finite(p.column);
    if (!vals.length) continue;
    const state = vals.every((v) => v === 0)
      ? 'down'
      : vals.some((v) => v !== 1)
        ? 'flapped'
        : 'up';
    if (port === null || SEVERITY[state] > SEVERITY[port]) port = state;
  }

  const errors = total(col(ERRORS));
  const drops = total(col(DROPS));
  const burst = mean > 0 ? peak / mean : 0;

  let tier = TIER_STEADY;
  let why = '';
  if (!up) {
    tier = TIER_STATE;
    why = 'link down';
  } else if (port === 'down') {
    tier = TIER_STATE;
    why = 'port down';
  } else if (port === 'flapped') {
    tier = TIER_STATE;
    why = 'port flapped';
  } else if (errors > 0) {
    // Errors before drops: a drop is usually backpressure, an error is usually
    // a cable. Same distinction the two chart lines are kept apart for.
    tier = TIER_STATE;
    why = 'errors';
  } else if (drops > 0) {
    tier = TIER_STATE;
    why = 'drops';
  } else if (burst >= BURST_RATIO) {
    tier = TIER_BURST;
    why = 'bursty';
  }

  return {
    key: link.key,
    node: link.node,
    iface: link.iface,
    division,
    series,
    peak,
    now,
    mean,
    burst,
    errors,
    drops,
    up,
    port,
    speedMbps: live.speedMbps,
    monitored: live.monitored,
    tier,
    why,
  };
}

/** Default order: tier first, then whatever that tier is actually about.
 *
 * Volume is never the first key. On its own it ranks a busy management port
 * above every fabric link on the cluster, every time, which is the opposite of
 * what anyone opened this card to see.
 *
 * BURST IS ONLY A SORT KEY INSIDE THE BURSTY TIER, which the first version got
 * wrong and the deployed page showed immediately. Sorting every tier by burst
 * ordered the four steady RoCE links 74, 76, 77, 79 kb/s — ascending, because
 * their burst ratios were 1.12 against 1.09 and that noise decided it. Two
 * links are never bursty to exactly the same four decimal places, so volume
 * never got a turn and the steady rows looked shuffled.
 *
 * Within a tier the reader is asking a different question. Among bursty links:
 * which moved most. Among steady ones: which is busiest. Among broken ones:
 * also which is busiest, since the `why` column already says what is wrong and
 * the interesting broken link is the one carrying traffic.
 */
export function byImportance(a: LinkRow, b: LinkRow): number {
  if (a.tier !== b.tier) return a.tier - b.tier;
  if (a.tier === TIER_BURST && a.burst !== b.burst) return b.burst - a.burst;
  return (
    b.peak - a.peak || a.node.localeCompare(b.node) || a.iface.localeCompare(b.iface)
  );
}


/** Every link as a row, in importance order, split by division.
 *
 * Shares `divide()` with the chart grid so a link cannot appear under Fabric in
 * one view and Management in the other.
 */
export function buildRows(
  names: string[],
  columns: (number | null)[][],
  nodeOrder: string[],
  activeNodes: string[] | null,
  rdma: Port[] = [],
  fabric: ReadonlySet<string> = new Set(),
  /** `linkKey(node, iface)` -> the facts with no time series. */
  live: ReadonlyMap<string, { speedMbps: number | null; monitored: boolean }> = new Map(),
): { key: 'fabric' | 'management'; label: string; note: string; rows: LinkRow[] }[] {
  const byName = new Map(names.map((n, i) => [n, columns[i]]));
  const wanted = (node: string) => activeNodes === null || activeNodes.includes(node);
  const portsOn = new Map<string, Port[]>();
  for (const p of rdma) {
    if (!p.iface || !wanted(p.node)) continue;
    const key = linkKey(p.node, p.iface);
    portsOn.set(key, [...(portsOn.get(key) ?? []), p]);
  }

  const rows = links(names, nodeOrder)
    .filter((l) => wanted(l.node))
    .map((l) =>
      linkRow(
        l,
        divide(l.key, (portsOn.get(l.key) ?? []).length > 0, fabric),
        byName,
        portsOn.get(l.key),
        live.get(l.key) ?? { speedMbps: null, monitored: true },
      ),
    )
    .sort(byImportance);

  return DIVISIONS.filter((d) => rows.some((r) => r.division === d.key)).map((d) => ({
    ...d,
    rows: rows.filter((r) => r.division === d.key),
  }));
}
