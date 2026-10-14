/** Polls /api/latency for the header (roadmap AT). Thirty seconds: the window
 *  is fifteen minutes, so a faster poll would only redraw the same figure. */
import { fetchWithTimeout } from './request';
import { poll } from './visibility.svelte';
import type { Latency } from './latency';

export const POLL_MS = 30_000;

export class LatencyFeed {
  latest = $state<Latency | null>(null);
  #stop: (() => void) | null = null;

  async load() {
    try {
      const resp = await fetchWithTimeout('/api/latency');
      if (!resp.ok) throw new Error(String(resp.status));
      this.latest = await resp.json();
    } catch {
      // Keep the last figure: a backend hiccup should not blank the header.
    }
  }

  start() {
    this.load();
    this.#stop = poll(() => this.load(), POLL_MS);
  }

  stop() {
    this.#stop?.();
    this.#stop = null;
  }
}
