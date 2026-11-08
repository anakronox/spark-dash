/** API keys for llama.cpp routers and engines, and the agents' shared token.
 *  A key goes to the backend when set and never comes back: Settings learns
 *  only whether one is set and its last four characters. The token is shown
 *  once, when made, for the node stack's AGENT_TOKEN (the same value on every
 *  node). */
import { fetchWithTimeout } from './request';

export interface KeyState {
  set: boolean;
  hint: string | null;
}

export interface RouterKeysStatus {
  /** By keyId(node, port). */
  keys: Record<string, KeyState>;
  token_set: boolean;
  token_hint: string | null;
  /** On, keys go to agents only encrypted with the token (AGENT_SEALED_KEYS). */
  sealed_required?: boolean;
  /** Only in the answer that made it. */
  token?: string;
}

/** How the backend names a router or engine: its node and its port. */
export const keyId = (node: string, port: number) => `${node}:${port}`;

/** The same check the backend makes, so a bad paste is caught before it is
 *  sent: one token of printable ASCII, at most 512 characters. No minimum:
 *  the router's key is whatever its operator chose. */
export function checkKey(value: string): string | null {
  const v = value.trim();
  if (!v) return 'the key is empty';
  if (v.length > 512) return 'an API key is at most 512 characters';
  if (!/^[\x21-\x7e]+$/.test(v)) return 'an API key is printable ASCII without spaces';
  return null;
}

async function send<T>(method: string, path: string, body?: object): Promise<T> {
  const resp = await fetchWithTimeout(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  const payload = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(payload?.detail || resp.statusText);
  return payload as T;
}

const routerPath = (node: string, port: number) =>
  `/api/router-keys/${encodeURIComponent(node)}/${encodeURIComponent(String(port))}`;

export const routerKeysStatus = () => send<RouterKeysStatus>('GET', '/api/router-keys');
export const setRouterKey = (node: string, port: number, key: string) =>
  send<RouterKeysStatus>('PUT', routerPath(node, port), { key: key.trim() });
export const clearRouterKey = (node: string, port: number) =>
  send<RouterKeysStatus>('DELETE', routerPath(node, port));
export const newAgentToken = () => send<RouterKeysStatus>('POST', '/api/agent-token');
export const removeAgentToken = () => send<RouterKeysStatus>('DELETE', '/api/agent-token');
export const setSealedRequired = (on: boolean) =>
  send<RouterKeysStatus>('PUT', '/api/agent-token/sealed', { on });
