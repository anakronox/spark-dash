/** Notifications through ntfy (roadmap BF): where alerts go, a test, and a
 *  new topic. The topic is the secret, so the status never carries it; it is
 *  asked for on purpose, to subscribe. */
import { fetchWithTimeout } from './request';

export interface NtfyStatus {
  capability: { ok: boolean; missing?: string; detail?: string };
  configured: boolean;
  server: string | null;
  topic_masked: string | null;
  template: boolean | null;
  guessable: boolean | null;
  public_server: boolean | null;
  alertmanager: { reachable: boolean; default_receiver: string | null; channels: string[] | null };
  setup_doc: string;
}

export interface NtfyTopic {
  server: string;
  topic: string;
  web: string;
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

export const ntfyStatus = () => send<NtfyStatus>('GET', '/api/notifications/status');
export const ntfyTopic = () => send<NtfyTopic>('GET', '/api/notifications/topic');
export const ntfySet = (server: string, topic?: string) =>
  send<NtfyStatus & NtfyTopic>('PUT', '/api/notifications/topic', { server, topic: topic || undefined });
export const ntfyTest = () =>
  send<{ ok: boolean; status: number; seconds: number; detail: string | null }>('POST', '/api/notifications/test');
