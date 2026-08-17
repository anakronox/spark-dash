<script lang="ts">
  /* Network interfaces and RDMA ports.
   *
   * RDMA leads when present, because on a clustered pair it's the interconnect
   * the distributed inference actually rides — a degraded RoCE link is the
   * difference between a model spanning two nodes usefully and not.
   *
   * The negotiated rate is shown verbatim rather than parsed into a number.
   * A ConnectX-7 that comes up at 10 Gb/sec instead of 200 is a known and
   * otherwise invisible failure, and the driver's own string is the clearest
   * statement of what actually happened.
   */
  import { num } from '../lib/format';
  import type { NodeSnapshot } from '../lib/types';

  interface Props {
    nodes: NodeSnapshot[];
  }
  const { nodes }: Props = $props();

  interface IfaceRow {
    key: string;
    node: string;
    name: string;
    up: boolean;
    speedMbps: number | null;
    rx: number;
    tx: number;
    errors: number;
    dropped: number;
  }

  interface RdmaRow {
    key: string;
    node: string;
    device: string;
    port: number;
    state: string;
    linkLayer: string;
    rate: string;
    iface: string;
    rx: number;
    tx: number;
    errors: number;
    active: boolean;
  }

  const interfaces = $derived.by<IfaceRow[]>(() =>
    nodes.flatMap((n) =>
      (n.network ?? []).map((i) => ({
        key: `${n.node_id}/${i.name}`,
        node: n.node_id,
        name: i.name,
        up: i.up,
        speedMbps: i.speed_mbps,
        rx: i.rx_bytes_per_sec,
        tx: i.tx_bytes_per_sec,
        errors: i.rx_errors + i.tx_errors,
        dropped: i.rx_dropped + i.tx_dropped,
      })),
    ),
  );

  /* Collector failures, surfaced verbatim.
   *
   * The previous empty state asserted a cause — "mount /proc and /sys" — that
   * it had no way of knowing. When the mounts were already right that sent the
   * reader after the wrong thing entirely. The agent records why each
   * collector failed; showing that is strictly better than a guess. */
  const failures = $derived.by<string[]>(() => {
    const out: string[] = [];
    for (const n of nodes) {
      for (const key of ['network', 'rdma']) {
        const message = n.errors?.[key];
        if (message) out.push(`${n.node_id}: ${key} — ${message}`);
      }
    }
    return out;
  });

  /** True when at least one node reported the field at all. An older agent
   *  omits it entirely, which is a different problem from finding nothing. */
  const reported = $derived(nodes.some((n) => Array.isArray(n.network)));

  const rdma = $derived.by<RdmaRow[]>(() =>
    nodes.flatMap((n) =>
      (n.rdma ?? []).map((p) => ({
        key: `${n.node_id}/${p.device}/${p.port}`,
        node: n.node_id,
        device: p.device,
        port: p.port,
        state: p.state,
        linkLayer: p.link_layer,
        rate: p.rate,
        iface: p.interface,
        rx: p.rx_bytes_per_sec,
        tx: p.tx_bytes_per_sec,
        errors: p.errors,
        active: p.state.toUpperCase().endsWith('ACTIVE'),
      })),
    ),
  );

  /** Bytes/sec as bits/sec — network gear is rated in bits, and comparing
   *  throughput against a "200 Gb/s" link is the whole point. */
  function bits(bytesPerSec: number): string {
    const b = bytesPerSec * 8;
    if (b >= 1e9) return `${num(b / 1e9, 2)} Gb/s`;
    if (b >= 1e6) return `${num(b / 1e6, 1)} Mb/s`;
    if (b >= 1e3) return `${num(b / 1e3, 0)} kb/s`;
    return `${num(b, 0)} b/s`;
  }

  function speed(mbps: number | null): string {
    if (mbps === null) return '—';
    return mbps >= 1000 ? `${num(mbps / 1000, 0)}G` : `${num(mbps, 0)}M`;
  }
</script>

<section class="panel">
  <header>
    <h2 class="eyebrow">Network</h2>
    <span class="dim count">
      {interfaces.length}
      {interfaces.length === 1 ? 'interface' : 'interfaces'}
      {#if rdma.length}
        · {rdma.length} RDMA
      {/if}
    </span>
  </header>

  {#if rdma.length}
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th scope="col">rdma port</th>
            <th scope="col">state</th>
            <th scope="col">link</th>
            <th scope="col">negotiated</th>
            <th scope="col">interface</th>
            <th scope="col">node</th>
            <th scope="col" class="r">rx</th>
            <th scope="col" class="r">tx</th>
            <th scope="col" class="r">err</th>
          </tr>
        </thead>
        <tbody>
          {#each rdma as p (p.key)}
            <tr class:down={!p.active}>
              <td class="name">{p.device}:{p.port}</td>
              <td>
                <span class="state" data-active={p.active}>
                  <span aria-hidden="true">{p.active ? '●' : '○'}</span>
                  {p.state || 'unknown'}
                </span>
              </td>
              <!-- RoCE vs InfiniBand: same sysfs tree, different fabric. -->
              <td class="dim">{p.linkLayer || '—'}</td>
              <!-- Blank while down: the driver reports a placeholder rate
                   there, and showing it would read as a negotiation fault. -->
              <td class="rate">{p.rate || '—'}</td>
              <td class="dim">{p.iface || '—'}</td>
              <td class="dim">{p.node}</td>
              <td class="r num">{bits(p.rx)}</td>
              <td class="r num">{bits(p.tx)}</td>
              <td class="r num" class:bad={p.errors > 0}>{p.errors}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}

  {#if interfaces.length}
    <div class="scroll" class:spaced={rdma.length > 0}>
      <table>
        <thead>
          <tr>
            <th scope="col">interface</th>
            <th scope="col">node</th>
            <th scope="col" class="r">link</th>
            <th scope="col" class="r">rx</th>
            <th scope="col" class="r">tx</th>
            <th scope="col" class="r">err</th>
            <th scope="col" class="r">drop</th>
          </tr>
        </thead>
        <tbody>
          {#each interfaces as i (i.key)}
            <tr class:down={!i.up}>
              <td class="name">
                <span class="state" data-active={i.up}>
                  <span aria-hidden="true">{i.up ? '●' : '○'}</span>
                  {i.name}
                </span>
              </td>
              <td class="dim">{i.node}</td>
              <td class="r num dim">{speed(i.speedMbps)}</td>
              <td class="r num">{bits(i.rx)}</td>
              <td class="r num">{bits(i.tx)}</td>
              <td class="r num" class:bad={i.errors > 0}>{i.errors}</td>
              <td class="r num" class:bad={i.dropped > 0}>{i.dropped}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {:else if failures.length}
    <div class="empty">
      <p>The network collector failed:</p>
      <ul>
        {#each failures as f (f)}
          <li>{f}</li>
        {/each}
      </ul>
    </div>
  {:else if !reported}
    <p class="empty">
      This agent doesn't report network data — it predates the collector.
      Rebuild and redeploy the agent image.
    </p>
  {:else}
    <p class="empty">
      No physical interfaces found. The agent ran but saw nothing with a
      <code>device</code> entry under <code>/sys/class/net</code>, which is how
      a real NIC is told from a virtual one.
    </p>
  {/if}
</section>

<style>
  section {
    padding: 14px 0 4px;
  }

  header {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    padding: 0 16px 10px;
  }

  .count {
    font-size: 11px;
  }

  .scroll {
    overflow-x: auto;
  }

  .scroll.spaced {
    margin-top: 14px;
    border-top: 1px solid var(--rule);
    padding-top: 10px;
  }

  table {
    font-size: 12px;
    min-width: 620px;
  }

  th {
    text-align: left;
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink-muted);
    padding: 0 12px 6px;
    border-bottom: 1px solid var(--rule);
    white-space: nowrap;
  }

  td {
    padding: 5px 12px;
    border-bottom: 1px solid color-mix(in srgb, var(--rule) 45%, transparent);
    white-space: nowrap;
  }

  tbody tr:last-child td {
    border-bottom: none;
  }

  tr.down td {
    color: var(--ink-muted);
  }

  .name {
    font-weight: 500;
  }

  /* Numeric columns shrink to their contents instead of sharing the slack.
     `width: 1%` with nowrap is the standard way to say "as narrow as the text
     allows" in an auto-layout table.

     Without it a wider page spreads every column equally, which pushed the
     numbers so far from the row's identity that tracking one across became
     unreliable — the exact failure the old 1180px cap was hiding. The slack
     now lands in the text columns, where longer names and addresses can use
     it, and the numbers stay in one readable block. */
  /* Row hover. Cheap, and it is what makes a wide table navigable: with the
     identity columns on the left and the numbers on the right, the eye needs
     something to hold the line across the gap between them. */
  tbody tr:hover {
    background: var(--panel-raised);
  }

  th.r,
  td.r {
    width: 1%;
    white-space: nowrap;
  }

  .r {
    text-align: right;
  }

  .state {
    display: inline-flex;
    align-items: baseline;
    gap: 6px;
  }

  .state[data-active='true'] {
    color: var(--good);
  }

  .state[data-active='false'] {
    color: var(--ink-muted);
  }

  /* Verbatim from the driver — the string itself is the diagnosis when a link
     comes up at the wrong speed. */
  .rate {
    color: var(--ink-2);
  }

  .bad {
    color: var(--warning);
  }

  .empty {
    padding: 0 16px 14px;
    font-size: 12px;
    color: var(--ink-2);
  }

  .empty p {
    margin: 0;
  }

  .empty ul {
    margin: 6px 0 0;
    padding-left: 18px;
  }

  .empty li {
    color: var(--warning);
  }

  code {
    color: var(--ink);
  }
</style>
