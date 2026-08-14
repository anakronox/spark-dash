/** The live connection.
 *
 * A monitoring UI that silently shows stale numbers is worse than one that's
 * obviously broken — you'd trust it and act on it. So connection state is
 * first-class here, and the UI visibly degrades when data stops arriving
 * rather than continuing to render the last frame as if it were current.
 */

import type { ClusterSnapshot } from './types';

export type ConnectionState = 'connecting' | 'live' | 'reconnecting' | 'offline';

/** Data older than this is stale even if the socket believes it's open —
 *  a half-open TCP connection looks fine but delivers nothing. */
const STALE_AFTER_MS = 8000;

const RECONNECT_MIN_MS = 1000;
const RECONNECT_MAX_MS = 15000;

export class LiveFeed {
  snapshot = $state<ClusterSnapshot | null>(null);
  state = $state<ConnectionState>('connecting');
  /** Wall-clock ms of the last frame, for the staleness check. */
  lastFrameAt = $state<number>(0);
  /** Ticks on every frame; drives the heartbeat so "flowing" is visible. */
  tick = $state<number>(0);

  #socket: WebSocket | null = null;
  #retryMs = RECONNECT_MIN_MS;
  #staleTimer: ReturnType<typeof setInterval> | null = null;
  #closed = false;

  get stale(): boolean {
    if (!this.lastFrameAt) return false;
    return Date.now() - this.lastFrameAt > STALE_AFTER_MS;
  }

  get secondsSinceFrame(): number {
    if (!this.lastFrameAt) return 0;
    return Math.floor((Date.now() - this.lastFrameAt) / 1000);
  }

  connect(): void {
    this.#closed = false;
    this.#open();
    // Re-evaluated on a timer rather than only on message, so a connection
    // that goes quiet is noticed without needing traffic to notice it.
    this.#staleTimer ??= setInterval(() => {
      this.tick = this.tick;
      if (this.stale && this.state === 'live') this.state = 'reconnecting';
    }, 1000);
  }

  close(): void {
    this.#closed = true;
    if (this.#staleTimer) clearInterval(this.#staleTimer);
    this.#staleTimer = null;
    this.#socket?.close();
    this.#socket = null;
  }

  #open(): void {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new WebSocket(`${proto}//${location.host}/ws/live`);
    this.#socket = socket;

    socket.onopen = () => {
      this.state = 'live';
      this.#retryMs = RECONNECT_MIN_MS;
    };

    socket.onmessage = (event) => {
      try {
        this.snapshot = JSON.parse(event.data) as ClusterSnapshot;
        this.lastFrameAt = Date.now();
        this.tick += 1;
        this.state = 'live';
      } catch {
        // A malformed frame is a bug worth seeing, but not worth tearing the
        // connection down for — the next frame is 2s away.
        console.error('could not parse live frame');
      }
    };

    socket.onclose = () => {
      if (this.#closed) return;
      this.state = this.snapshot ? 'reconnecting' : 'offline';
      this.#scheduleReconnect();
    };

    socket.onerror = () => socket.close();
  }

  #scheduleReconnect(): void {
    // Exponential backoff: a backend that's down stays down for a while, and
    // hammering it adds nothing.
    const delay = this.#retryMs;
    this.#retryMs = Math.min(this.#retryMs * 2, RECONNECT_MAX_MS);
    setTimeout(() => {
      if (!this.#closed) this.#open();
    }, delay);
  }
}
