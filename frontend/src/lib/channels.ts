/** Slack, Discord and Telegram (roadmap BJ). The secrets (a webhook URL, a
 *  bot token) go to the backend when set and never come back: a channel
 *  says only whether one is set and its last four characters. */
import { fetchWithTimeout } from './request';

export type ChannelType = 'slack' | 'discord' | 'telegram';

export interface Channel {
  id: string;
  type: ChannelType;
  name: string;
  enabled: boolean;
  alerts: 'all' | 'critical' | 'off';
  resolved: boolean;
  reports: boolean;
  chat_id?: string | null;
  secret_set?: boolean;
  secret_hint?: string | null;
  /** Only while being set or changed. */
  secret?: string;
}

export interface ChannelsStatus {
  channels: Channel[];
  error: string | null;
  types: ChannelType[];
  /** Alertmanager forwards alerts here (the shipped alertmanager.yml, loaded). */
  forwarding: boolean;
  alertmanager_reachable: boolean;
  /** The token Alertmanager presents exists in secrets/. */
  token: boolean;
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

export const channelsStatus = () => send<ChannelsStatus>('GET', '/api/channels');
export const saveChannels = (channels: Channel[]) =>
  send<ChannelsStatus>('PUT', '/api/channels', {
    channels: channels.map(({ secret_set: _s, secret_hint: _h, ...c }) => ({ ...c, secret: c.secret || undefined })),
  });
export const testChannel = (id: string) =>
  send<{ ok: boolean; status: number; detail: string | null }>('POST', `/api/channels/${encodeURIComponent(id)}/test`);

export const LABEL: Record<ChannelType, string> = { slack: 'Slack', discord: 'Discord', telegram: 'Telegram' };

/** Where each service's secret comes from, in a line. */
export const HOW: Record<ChannelType, string> = {
  slack: 'In Slack: an app with Incoming Webhooks, added to the channel; paste its webhook URL.',
  discord: 'In Discord: the channel’s settings, Integrations, Webhooks, New Webhook; paste its URL.',
  telegram:
    'In Telegram: @BotFather, /newbot, paste the token; add the bot to the chat. The chat id is in ' +
    'api.telegram.org/bot<token>/getUpdates after a message to it (groups start with -100).',
};

export function newChannel(type: ChannelType): Channel {
  return {
    id: Math.random().toString(16).slice(2, 10),
    type,
    name: LABEL[type],
    enabled: true,
    alerts: 'all',
    resolved: true,
    reports: true,
    chat_id: type === 'telegram' ? '' : undefined,
    secret: '',
  };
}
