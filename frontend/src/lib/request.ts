/** fetch with a hard deadline.
 *
 * WHY THIS EXISTS. `fetch` has NO default timeout. A request in flight when the
 * server goes away does not reject — it hangs until the OS gives up on the
 * socket, which can be minutes. The promise simply never settles.
 *
 * That stranded the History panel on "Loading…" after the backend stack was
 * recreated on 2026-08-16. The panel clears its loading flag in a `finally`,
 * and a promise that never settles never reaches one — so the flag stayed set
 * while live data (a WebSocket, which reconnects on its own) came back
 * normally. Nothing was broken server-side; the API was answering in
 * milliseconds the whole time.
 *
 * A timeout turns that silent hang into an ordinary rejection the caller
 * already knows how to render.
 *
 * TimeoutError is deliberately NOT named AbortError. Callers swallow
 * AbortError, because that means "a newer request superseded this one" and is
 * not worth showing. A timeout is the opposite: nothing replaced it, the
 * request failed, and the user should be told rather than left watching a
 * spinner.
 */

/** Generous: history queries span up to 7 days and Prometheus can be slow
 *  under load. This is a backstop against hanging forever, not a latency
 *  budget — cutting off a slow-but-working query would be its own bug. */
export const REQUEST_TIMEOUT_MS = 20_000;

export async function fetchWithTimeout(
  url: string,
  init: RequestInit = {},
  timeoutMs: number = REQUEST_TIMEOUT_MS,
): Promise<Response> {
  const { signal, ...rest } = init;
  const controller = new AbortController();

  // Chain the caller's signal onto ours so an explicit abort still propagates
  // with its original reason, and AbortError stays distinguishable from
  // TimeoutError downstream.
  const forward = () => controller.abort(signal?.reason);
  if (signal?.aborted) forward();
  signal?.addEventListener('abort', forward, { once: true });

  const timer = setTimeout(
    () => controller.abort(new DOMException(`Request timed out after ${timeoutMs}ms`, 'TimeoutError')),
    timeoutMs,
  );

  try {
    return await fetch(url, { ...rest, signal: controller.signal });
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', forward);
  }
}
