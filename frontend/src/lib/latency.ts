/** Time to first token in the header (roadmap AT): types and formatting.
 *
 * Plain TypeScript, so tests/js runs it; the polling is in latency.svelte.ts. */

export interface ModelLatency {
  model: string;
  node: string | null;
  runtime: string | null;
  /** `engine`: the engine's own histogram. `gateway`: LiteLLM's, for a model
   *  no engine measures (a llama.cpp router publishes no histogram). */
  source: 'engine' | 'gateway';
  p50_s: number | null;
  p95_s: number | null;
  per_min: number;
  /** Speculative decoding: accepted draft tokens over drafted, 0-1. */
  accept: number | null;
}

export interface Latency {
  window: string;
  busiest: ModelLatency | null;
  models: ModelLatency[];
}

/** 850ms below a second, 1.5s from there, 12s from ten. */
export function seconds(s: number | null | undefined): string {
  if (s == null || !isFinite(s)) return '—';
  if (s < 1) return `${Math.round(s * 1000)}ms`;
  return s < 10 ? `${s.toFixed(1)}s` : `${Math.round(s)}s`;
}

/** The hover: every model with a figure, busiest first. */
export function latencyTitle(l: Latency | null): string {
  if (!l) return 'Time to first token';
  const lines = l.models
    .filter((m) => m.p50_s != null || m.accept != null)
    .map((m) => {
      const where = [m.node, m.runtime].filter(Boolean).join(' · ') || (m.source === 'gateway' ? 'via the gateway' : '');
      const parts = [`p50 ${seconds(m.p50_s)}`, `p95 ${seconds(m.p95_s)}`, `${m.per_min} req/min`];
      if (m.accept != null) parts.push(`draft acceptance ${Math.round(m.accept * 100)}%`);
      return `${m.model}${where ? ` (${where})` : ''}: ${parts.join(', ')}`;
    });
  const head = `Time to first token, last ${l.window}, the busiest model shown.`;
  return lines.length ? `${head}\n${lines.join('\n')}` : `${head}\nNo requests in the window.`;
}
