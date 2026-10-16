/** Temperature sensors, grouped and ranked.
 *
 * WHAT THIS IS FOR. The dashboard reported two temperatures — the GPU, and one
 * CPU reading psutil happened to pick first. A GB10 exposes 18-23. Measured
 * over 24h on this cluster, `acpitz` zone0 peaked at 95.4 °C while the GPU read
 * 72.0 °C at the same instant: a sensor 23 degrees hotter than the one being
 * charted, with nothing looking at it.
 *
 * Kept out of the component so the ranking can be executed in the node driver,
 * the same arrangement `network-history.ts` uses.
 */

import type { GaugeItem, GaugeState } from './gauges';
import type { NodeSnapshot } from './types';

/** Domains in the order they are drawn, with what each one means.
 *
 * ORDERED BY WHAT RUNS HOT, not alphabetically. The package zones are where the
 * 95 °C reading lives and where a thermal problem shows first; wireless is a
 * 42 °C radio nobody will ever look at twice.
 */
export const DOMAINS: { key: string; label: string; note: string }[] = [
  { key: 'package', label: 'Package', note: 'SoC thermal zones — GPU and Grace share one die' },
  { key: 'gpu', label: 'GPU', note: 'NVML, judged against the shutdown threshold' },
  { key: 'storage', label: 'Storage', note: 'NVMe controller and its own sensors' },
  { key: 'network', label: 'Network', note: 'ConnectX asic, one per port' },
  { key: 'wireless', label: 'Wireless', note: 'radio PHY' },
  { key: 'other', label: 'Other', note: 'a chip this build does not recognise' },
];

const LABELS = new Map(DOMAINS.map((d) => [d.key, d]));

export interface TempRow {
  key: string;
  node: string;
  domain: string;
  sensor: string;
  celsius: number;
  limitC: number | null;
  /** Degrees before this sensor's own limit. Null when it states none. */
  headroomC: number | null;
}

export function tempRows(nodes: NodeSnapshot[]): TempRow[] {
  const rows: TempRow[] = [];
  for (const node of nodes) {
    for (const t of node.temperatures ?? []) {
      rows.push({
        key: `${node.node_id}/${t.sensor}`,
        node: node.node_id,
        domain: t.domain,
        sensor: t.sensor,
        celsius: t.celsius,
        limitC: t.limit_c ?? null,
        headroomC: t.limit_c == null ? null : t.limit_c - t.celsius,
      });
    }
  }
  return rows;
}

/** Least headroom first.
 *
 * HEADROOM, NOT TEMPERATURE, and it is the whole reason the limits are
 * collected. A 52 °C NIC and an 85 °C GPU cannot be ranked as temperatures —
 * sorted that way the GPU looks like the problem, when the NIC has 53 degrees
 * of margin and the GPU has 5. As headroom they are directly comparable, and
 * the answer to "what is closest to trouble" is the top row.
 *
 * A sensor that states no limit sorts LAST rather than first. It has no known
 * margin, which is not the same as no margin left — putting an unmeasurable
 * wifi radio above a GPU five degrees from shutdown would be the exact
 * inversion this ranking exists to prevent.
 */
export function byHeadroom(a: TempRow, b: TempRow): number {
  if (a.headroomC == null || b.headroomC == null) {
    if (a.headroomC == null && b.headroomC == null) {
      return b.celsius - a.celsius || a.sensor.localeCompare(b.sensor);
    }
    return a.headroomC == null ? 1 : -1;
  }
  return (
    a.headroomC - b.headroomC ||
    b.celsius - a.celsius ||
    a.node.localeCompare(b.node) ||
    a.sensor.localeCompare(b.sensor)
  );
}

/** The hottest sensor on a node, which is what the card headlines.
 *
 * MAX, never mean. One of these boxes was measured holding 95.4 °C and 58.0 °C
 * at the same instant; their average describes nothing physical. Thermal risk
 * is a property of the hottest point.
 */
export function hottest(rows: TempRow[]): TempRow | null {
  let best: TempRow | null = null;
  for (const r of rows) if (!best || r.celsius > best.celsius) best = r;
  return best;
}

/** The row with the least headroom — what is closest to its own limit.
 *
 * Distinct from `hottest` and often a different sensor: a 52 °C NIC rated to
 * 105 is cooler and safer than an 85 °C GPU rated to 90. Both are worth
 * showing, and conflating them is what a single "temperature" number does.
 */
export function tightest(rows: TempRow[]): TempRow | null {
  const measured = rows.filter((r) => r.headroomC != null);
  if (!measured.length) return null;
  return measured.reduce((a, b) => (a.headroomC! <= b.headroomC! ? a : b));
}

export interface TempGroup {
  key: string;
  label: string;
  note: string;
  rows: TempRow[];
}

/** Rows split into domain sections, each ranked by headroom.
 *
 * A domain with no sensors is not drawn: sparky reports no NIC chips at all,
 * and an empty "Network" heading would read as a fault rather than as a
 * machine without that hardware.
 */
export function groupRows(rows: TempRow[]): TempGroup[] {
  const seen = new Map<string, TempRow[]>();
  for (const r of rows) seen.set(r.domain, [...(seen.get(r.domain) ?? []), r]);

  const known = DOMAINS.filter((d) => seen.has(d.key)).map((d) => ({
    ...d,
    rows: [...seen.get(d.key)!].sort(byHeadroom),
  }));
  // A domain the classifier invented and this list has never heard of still
  // gets drawn — the same rule as `other` in the agent. A sensor nobody
  // anticipated is the one most worth seeing.
  const extra = [...seen.keys()]
    .filter((k) => !LABELS.has(k))
    .sort()
    .map((k) => ({
      key: k,
      label: k,
      note: 'reported by the agent, unrecognised by this build',
      rows: [...seen.get(k)!].sort(byHeadroom),
    }));
  return [...known, ...extra];
}

/* --- Combined readings and gauges (roadmap BD) ---------------------- */

/** How a group's sensors are shown: each one, or one reading per node. */
export type Combine = 'each' | 'hottest' | 'average';

/** One node's sensors in one group, as a single row.
 *
 * `celsius` is the hottest sensor or the mean of them all. THE STATE IS NEVER
 * AVERAGED: `headroomC` is the least headroom of any sensor in the set and
 * `limitC` that sensor's limit, so an average of 61° can still read 5° left
 * and turn red. The mean is an overview of how warm the node runs (Brian,
 * 2026-10-16); the rule this file keeps everywhere else, MAX never mean,
 * still decides whether anything is wrong. */
export interface CombinedRow extends TempRow {
  how: 'hottest' | 'average';
  /** Sensors in the set. */
  count: number;
  /** The sensor closest to its limit, which the state comes from. */
  tightestSensor: string | null;
  /** The hottest sensor and its reading, so an average names what it hides. */
  hottestSensor: string;
  hottestC: number;
}

export function combineRows(rows: TempRow[], how: 'hottest' | 'average'): CombinedRow[] {
  const byNode = new Map<string, TempRow[]>();
  for (const r of rows) byNode.set(r.node, [...(byNode.get(r.node) ?? []), r]);
  const out: CombinedRow[] = [];
  for (const [node, rs] of byNode) {
    const hot = hottest(rs)!;
    const tight = tightest(rs);
    const celsius = how === 'hottest' ? hot.celsius : rs.reduce((s, r) => s + r.celsius, 0) / rs.length;
    out.push({
      key: `${node}/${rs[0].domain}/${how}`,
      node,
      domain: rs[0].domain,
      sensor: how === 'hottest' ? `hottest of ${rs.length}: ${hot.sensor}` : `average of ${rs.length}`,
      celsius,
      limitC: tight?.limitC ?? null,
      headroomC: tight?.headroomC ?? null,
      how,
      count: rs.length,
      tightestSensor: tight?.sensor ?? null,
      hottestSensor: hot.sensor,
      hottestC: hot.celsius,
    });
  }
  return out.sort(byHeadroom);
}

/** The card's own colouring of headroom, so a dial and the table row beside
 *  it always agree: 5° or less is critical, 15° or less a warning. */
export const HEADROOM_CRITICAL_C = 5;
export const HEADROOM_WARNING_C = 15;

export function headroomState(h: number | null): GaugeState {
  if (h == null) return 'ok';
  if (h <= HEADROOM_CRITICAL_C) return 'critical';
  if (h <= HEADROOM_WARNING_C) return 'warning';
  return 'ok';
}

/** The dial's scale starts here, as the table's bar does: every sensor in a
 *  running machine sits above it, so a scale from zero would show every dial
 *  about half full and never move. */
export const FLOOR_C = 40;

function sweep(c: number, limit: number): number {
  return Math.max(0, Math.min(1, (c - FLOOR_C) / (limit - FLOOR_C)));
}

/** A dial per node for one group: the combined reading from FLOOR_C to the
 *  tightest limit, with marks where the card turns amber and red. */
export function thermalItems(rows: CombinedRow[], colourOf: (node: string) => string): GaugeItem[] {
  return [...rows]
    .sort((a, b) => a.node.localeCompare(b.node))
    .map((r) => {
      const lim = r.limitC != null && r.limitC > FLOOR_C ? r.limitC : null;
      const what = r.how === 'hottest' ? `hottest of ${r.count}, ${r.hottestSensor}` : `average of ${r.count}`;
      const tight =
        r.tightestSensor && r.headroomC != null
          ? `; closest to its limit: ${r.tightestSensor}, ${r.headroomC.toFixed(1)}° left of ${r.limitC}°`
          : '; no stated limit';
      const hot = r.how === 'average' ? `; hottest ${r.hottestSensor} ${r.hottestC.toFixed(1)}°` : '';
      return {
        id: r.node,
        colour: colourOf(r.node),
        fraction: lim ? sweep(r.celsius, lim) : 0,
        value: `${r.celsius.toFixed(1)}°`,
        unit: r.how === 'average' ? 'avg °C' : '°C',
        state: headroomState(r.headroomC),
        bands: lim
          ? [sweep(lim - HEADROOM_WARNING_C, lim), sweep(lim - HEADROOM_CRITICAL_C, lim)]
          : null,
        scale: [`${FLOOR_C}°`, lim ? `${lim.toFixed(0)}°` : '—'],
        title: `${r.node}: ${what}${hot}${tight}`,
      };
    });
}
