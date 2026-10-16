/** Ask the dashboard (roadmap AY): its settings and its questions.
 *
 * The status is polled so the header's ask button appears or goes when the
 * assistant is switched on or off. The API key is sent when saved and never
 * comes back: the status says only whether one is set and its last four. */
import { fetchWithTimeout } from './request';
import { poll } from './visibility.svelte';

export interface AssistantStatus {
  configured: boolean;
  enabled: boolean;
  url: string | null;
  model: string | null;
  /** BB4: the model may call the read-only tools. */
  tools: boolean;
  key_set: boolean;
  key_hint: string | null;
  error: string | null;
}

export interface Answer {
  answer: string;
  model: string;
  seconds: number;
  sent: { nodes: number; alerts_firing: number; alert_episodes: number; events: number; series: number; clients?: number };
  context_chars: number;
  /** BB4: what the model looked up, in order. */
  tools_used?: { tool: string; args: Record<string, unknown>; error: boolean }[];
  /** Set when the endpoint refused tools and was asked without. */
  tools_note?: string;
}

/** Tools for agents (roadmap BB): the MCP endpoint's switches and key. */
export interface McpStatus {
  enabled: boolean;
  promql: boolean;
  key_set: boolean;
  key_hint: string | null;
  path: string;
  error: string | null;
  /** Only in the answer that made a key; never again. */
  key?: string;
}

export interface Draft {
  url: string;
  model: string;
  key?: string;
  clear_key?: boolean;
  enabled?: boolean;
  tools?: boolean;
}

/** A question can take a reasoning model most of a minute and a half, and
 *  three minutes with tools (the backend's limits, plus room). */
const ASK_TIMEOUT_MS = 200_000;

async function send<T>(method: string, path: string, body?: object, timeoutMs?: number): Promise<T> {
  const resp = await fetchWithTimeout(
    path,
    { method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined },
    timeoutMs,
  );
  const payload = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(payload?.detail || resp.statusText);
  // A streamed answer (tools on) sends its status before it knows the
  // outcome, so a failure arrives as {detail, status} under a 200.
  if (typeof payload?.status === 'number' && payload.status >= 400 && payload.detail)
    throw new Error(payload.detail);
  return payload as T;
}

export class AssistantFeed {
  status = $state<AssistantStatus | null>(null);
  #stop: (() => void) | null = null;

  get configured(): boolean {
    return this.status?.configured ?? false;
  }

  async load() {
    try {
      this.status = await send<AssistantStatus>('GET', '/api/assistant/status');
    } catch {
      // Keep the last answer rather than hiding the button on a hiccup.
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

  async save(d: Draft) {
    this.status = await send<AssistantStatus>('PUT', '/api/assistant/config', d);
  }

  async remove() {
    this.status = await send<AssistantStatus>('DELETE', '/api/assistant/config');
  }

  /** The endpoint's models, for the dropdown. No key: the stored one. */
  async detect(url: string, key?: string): Promise<string[]> {
    return (await send<{ models: string[] }>('POST', '/api/assistant/models', { url, key: key || undefined })).models;
  }

  async test(url: string, model: string, key?: string): Promise<{ reply: string; seconds: number }> {
    return send('POST', '/api/assistant/test', { url, model, key: key || undefined }, ASK_TIMEOUT_MS);
  }

  async ask(question: string, minutes: number): Promise<Answer> {
    return send('POST', '/api/assistant/ask', { question, minutes }, ASK_TIMEOUT_MS);
  }

  async mcpStatus(): Promise<McpStatus> {
    return send('GET', '/api/mcp/status');
  }

  async mcpSave(d: { enabled?: boolean; promql?: boolean }): Promise<McpStatus> {
    return send('PUT', '/api/mcp/config', d);
  }

  async mcpNewKey(): Promise<McpStatus> {
    return send('POST', '/api/mcp/key');
  }
}
