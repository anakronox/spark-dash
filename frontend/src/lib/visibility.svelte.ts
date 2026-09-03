/** Whether anyone can see the page, and what to do about it when they can't.
 *
 * THE PROBLEM THIS SOLVES. A tab left in the background for a while came back
 * showing the numbers from when it was last looked at, then fast-forwarded to
 * now. Both Safari and Chrome throttle a background tab's timers to a crawl
 * and then suspend its JavaScript outright (Safari with App Nap and Low Power
 * Mode; Chrome after ~5 minutes, sooner with Memory Saver). The WebSocket
 * stays open while that happens and the backend keeps pushing a snapshot every
 * 2s, so hundreds of frames queue in the browser and are delivered in one
 * burst on return — each one rendered in turn, which is the replay. The
 * history charts are the other half: their setInterval never fired while
 * suspended, so they stayed on whatever they last fetched until it did.
 *
 * TWO MECHANISMS, ONE STORE. `visibilitychange` is the load-bearing signal and
 * both browsers fire it. Chrome adds the Page Lifecycle `freeze`/`resume`
 * events, which say "about to suspend" rather than "hidden"; Safari has never
 * implemented them and never fires them, so listening costs nothing there.
 * `pagehide`/`pageshow` cover the back-forward cache, where a restored page
 * still holds every object it had — dead sockets included.
 *
 * WHAT CONSUMERS DO WITH IT. The live feed closes its socket after a grace
 * period and reopens on return; the backend hands a new subscriber the latest
 * frame at once, so there is nothing to replay — and its poller stops asking
 * the nodes for anything while nobody is watching, which is the property
 * architecture.md promised and a buried tab was quietly breaking. Every other
 * poller goes through `poll()` below, which skips ticks while hidden and
 * catches up on return instead of waiting out a throttled interval.
 */

type Listener = (hiddenForMs: number) => void;

class PageVisibility {
  hidden = $state(typeof document !== 'undefined' && document.hidden);

  #hiddenAt: number | null = null;
  #onVisible = new Set<Listener>();
  #onHidden = new Set<() => void>();

  constructor() {
    if (typeof document === 'undefined') return;

    const hide = () => {
      if (this.hidden) return;
      this.hidden = true;
      this.#hiddenAt = Date.now();
      for (const cb of this.#onHidden) cb();
    };
    const show = () => {
      if (!this.hidden) return;
      this.hidden = false;
      const hiddenFor = Date.now() - (this.#hiddenAt ?? Date.now());
      this.#hiddenAt = null;
      for (const cb of this.#onVisible) cb(hiddenFor);
    };

    document.addEventListener('visibilitychange', () => (document.hidden ? hide() : show()));
    // Chrome only. A resume in a tab that is still hidden is not a return.
    document.addEventListener('freeze', hide);
    document.addEventListener('resume', () => {
      if (!document.hidden) show();
    });
    // bfcache. `pageshow` also fires on the initial load, where `show()` is a
    // no-op because nothing was hidden — but a tab OPENED in the background
    // starts hidden, and must not be flipped visible by that first event.
    window.addEventListener('pagehide', hide);
    window.addEventListener('pageshow', () => {
      if (!document.hidden) show();
    });
  }

  /** Called when the page becomes visible again, with how long it was hidden.
   *  Returns the unsubscribe. */
  onVisible(cb: Listener): () => void {
    this.#onVisible.add(cb);
    return () => this.#onVisible.delete(cb);
  }

  onHidden(cb: () => void): () => void {
    this.#onHidden.add(cb);
    return () => this.#onHidden.delete(cb);
  }
}

export const pageVisibility = new PageVisibility();

/** How stale a poller may be on return before it refreshes at once rather
 *  than waiting for its next tick. A 30s poller catches up after any real
 *  absence; a 5-minute one does not refetch 7 days of history because you
 *  glanced at another tab for twenty seconds. */
const CATCH_UP_AFTER_MS = 60_000;

/** A visibility-aware setInterval.
 *
 * Ticks only while the page is visible — a hidden tab fetching history nobody
 * will see is load on Prometheus for nothing — and on return, runs `load` at
 * once if the last run is older than the period (or a minute, whichever is
 * shorter). Without that, a chart came back showing data from when you left
 * and stayed that way until its throttled interval finally fired.
 *
 * Does NOT run `load` immediately on creation: every caller already does its
 * own first load, several of them from an effect keyed on the range.
 */
export function poll(load: () => void, periodMs: number): () => void {
  let lastRun = Date.now();
  const run = () => {
    lastRun = Date.now();
    load();
  };
  const timer = setInterval(() => {
    if (!pageVisibility.hidden) run();
  }, periodMs);
  const off = pageVisibility.onVisible(() => {
    if (Date.now() - lastRun >= Math.min(periodMs, CATCH_UP_AFTER_MS)) run();
  });
  return () => {
    clearInterval(timer);
    off();
  };
}
