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
