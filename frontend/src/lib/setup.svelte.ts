/** The onboarding checklist (roadmap BE1), as the backend works it out:
 *  each step is done when the thing it asks for is working, not when it was
 *  clicked. Polled slowly, so the header's chip clears once a step is done. */
import { fetchWithTimeout } from './request';
import { poll } from './visibility.svelte';

export type Level = 'required' | 'recommended' | 'optional';
export type ItemState = 'done' | 'attention' | 'todo' | 'info';

export interface SetupItem {
  id: string;
  level: Level;
  title: string;
  state: ItemState;
  detail: string;
  /** The Settings pane where it is set, when there is one. */
  pane: string | null;
  doc: string | null;
}

export interface SetupStatus {
  items: SetupItem[];
  required_done: number;
  required_total: number;
}

export { SEEN_KEY, TOUR_KEY } from './onboarding';

export function flag(key: string): boolean {
  try {
    return localStorage.getItem(key) === '1';
  } catch {
    return false;
  }
}

export function setFlag(key: string, on: boolean) {
  try {
    if (on) localStorage.setItem(key, '1');
    else localStorage.removeItem(key);
  } catch {
    // Private window: asked again next visit.
  }
}

export class SetupFeed {
  status = $state<SetupStatus | null>(null);
  #stop: (() => void) | null = null;

  /** Required steps not yet done; the header's chip shows while any are. */
  get missing(): number {
    return this.status ? this.status.required_total - this.status.required_done : 0;
  }

  async load() {
    try {
      const resp = await fetchWithTimeout('/api/setup', {});
      if (resp.ok) this.status = (await resp.json()) as SetupStatus;
    } catch {
      // Keep the last answer.
    }
  }

  start() {
    this.load();
    this.#stop = poll(() => this.load(), 60_000);
  }

  stop() {
    this.#stop?.();
    this.#stop = null;
  }
}
