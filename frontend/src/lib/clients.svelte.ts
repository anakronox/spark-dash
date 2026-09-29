/** The gateway's status, and the three things Settings can do with it
 *  (roadmap AM5).
 *
 * Polled from the start rather than from Settings opening, because whether
 * the Clients card is on the page depends on it. The status route asks
 * Prometheus and never the gateway, so the minute's poll costs the gateway
 * nothing. Every write returns the new status, which is applied at once, so
 * the card appears or goes as soon as the switch is flipped.
 */
import { fetchWithTimeout } from './request';
import { poll } from './visibility.svelte';
import type { ClientsStatus, ProbeResult } from './clients';

export const POLL_MS = 60_000;

export class ClientsFeed {
  status = $state<ClientsStatus | null>(null);
  loaded = $state(false);

  #stop: (() => void) | null = null;

  /** On: an address is set and switched on. False hides the Clients card. */
  get configured(): boolean {
    return this.status?.configured ?? false;
  }

  async load() {
    try {
      const resp = await fetchWithTimeout('/api/clients/status');
      if (!resp.ok) throw new Error(String(resp.status));
      this.status = await resp.json();
    } catch {
      // Keep the last answer: a backend hiccup should not make the card
      // vanish and come back.
    } finally {
      this.loaded = true;
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

  async #send(method: string, path: string, body: object): Promise<unknown> {
    const resp = await fetchWithTimeout(path, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const payload = await resp.json().catch(() => ({}));
    // The backend's own sentence, verbatim: it names the field and the fix.
    if (!resp.ok) throw new Error(payload?.detail || resp.statusText);
    return payload;
  }

  /** Save the gateway's address, or remove it with null. */
  async setUrl(url: string | null) {
    this.status = (await this.#send('PUT', '/api/clients/config', { url })) as ClientsStatus;
  }

  async setEnabled(on: boolean) {
    this.status = (await this.#send('POST', '/api/clients/enabled', {
      enabled: on,
    })) as ClientsStatus;
  }

  /** Try an address without saving it. */
  async test(url: string): Promise<ProbeResult> {
    return (await this.#send('POST', '/api/clients/test', { url })) as ProbeResult;
  }
}
