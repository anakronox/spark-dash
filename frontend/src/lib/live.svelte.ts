/** The live connection.
 *
 * A monitoring UI that silently shows stale numbers is worse than one that's
 * obviously broken — you'd trust it and act on it. So connection state is
 * first-class here, and the UI visibly degrades when data stops arriving
 * rather than continuing to render the last frame as if it were current.
 */

import type { ClusterSnapshot } from './types';
import { pageVisibility } from './visibility.svelte';

export type ConnectionState = 'connecting' | 'live' | 'reconnecting' | 'offline';

/** Data older than this is stale even if the socket believes it's open —
 *  a half-open TCP connection looks fine but delivers nothing. */
const STALE_AFTER_MS = 8000;

const RECONNECT_MIN_MS = 1000;
const RECONNECT_MAX_MS = 15000;

/** How long a tab stays hidden before the socket is closed. Long enough that
 *  flicking to another tab and back does not churn a connection; short enough
 *  that a tab you have genuinely left stops the backend polling the nodes
 *  within the minute. */
const SUSPEND_AFTER_MS = 20_000;

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

  /* SUSPENDED WHILE HIDDEN — see lib/visibility.svelte.ts for the failure
     this prevents. Distinct from `#closed`: a suspended feed reopens on its
     own when the page is looked at again; a closed one is gone. */
  #suspended = false;
  #graceTimer: ReturnType<typeof setTimeout> | null = null;
  #offVisibility: (() => void)[] = [];

  /* ONE FRAME PER PAINT. Messages are parked here and applied in the next
     animation frame, so however many arrive between two paints, the page
     renders the newest and skips the rest. This is what defuses a burst: a
     tab suspended inside the grace window still gets its queued frames on
     return, but they land before the first paint and only the last one is
     applied. It is also the honest cadence for a live view — nothing can be
     seen faster than it is painted. */
  #pending: string | null = null;
  #flush: number | null = null;

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
      if (this.stale && this.state === 'live' && !this.#suspended) this.state = 'reconnecting';
    }, 1000);

    if (!this.#offVisibility.length) {
      this.#offVisibility = [
        pageVisibility.onHidden(() => {
          this.#graceTimer ??= setTimeout(() => {
            this.#graceTimer = null;
            this.#suspend();
          }, SUSPEND_AFTER_MS);
        }),
        pageVisibility.onVisible(() => {
          if (this.#graceTimer) clearTimeout(this.#graceTimer);
          this.#graceTimer = null;
          if (this.#suspended) this.#resume();
        }),
      ];
    }
  }

  close(): void {
    this.#closed = true;
    if (this.#staleTimer) clearInterval(this.#staleTimer);
    this.#staleTimer = null;
    if (this.#graceTimer) clearTimeout(this.#graceTimer);
    this.#graceTimer = null;
    if (this.#flush != null) cancelAnimationFrame(this.#flush);
    this.#flush = null;
    this.#pending = null;
    for (const off of this.#offVisibility) off();
    this.#offVisibility = [];
    this.#socket?.close();
    this.#socket = null;
  }

  #suspend(): void {
    if (this.#closed || this.#suspended) return;
    this.#suspended = true;
    // onclose sees #suspended and does not schedule a reconnect.
    this.#socket?.close();
    this.#socket = null;
    this.#pending = null;
  }

  #resume(): void {
    this.#suspended = false;
    if (this.#closed) return;
    // Said as it is: the last frame IS old, and the header shows how old
    // until the reopened socket delivers a fresh one — which the backend
    // sends immediately on subscribe, so this is a moment, not a wait.
    this.state = this.snapshot ? 'reconnecting' : 'connecting';
    this.#retryMs = RECONNECT_MIN_MS;
    this.#open();
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
      this.#pending = event.data as string;
      this.#flush ??= requestAnimationFrame(() => this.#apply());
    };

    socket.onclose = () => {
      if (this.#closed || this.#suspended) return;
      this.state = this.snapshot ? 'reconnecting' : 'offline';
      this.#scheduleReconnect();
    };

    socket.onerror = () => socket.close();
  }

  #apply(): void {
    this.#flush = null;
    const data = this.#pending;
    this.#pending = null;
    if (data == null || this.#suspended) return;
    try {
      this.snapshot = JSON.parse(data) as ClusterSnapshot;
      this.lastFrameAt = Date.now();
      this.tick += 1;
      this.state = 'live';
    } catch {
      // A malformed frame is a bug worth seeing, but not worth tearing the
      // connection down for — the next frame is 2s away.
      console.error('could not parse live frame');
    }
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
