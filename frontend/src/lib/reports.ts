/** Reports (roadmap BI): the API the Reports panel and Settings use. A report
 *  is one JSON document; the page renders it, and the backend renders the
 *  downloads (Markdown, self-contained HTML that prints as the PDF, a zip of
 *  CSVs, JSON). */
import { fetchWithTimeout } from './request';

export interface ReportTable {
  title: string;
  columns: { key: string; label: string }[];
  rows: Record<string, string | number | null>[];
}

export interface ReportSection {
  id: string;
  title: string;
  about: string;
  lines: string[];
  tables: ReportTable[];
  error?: boolean;
}

export interface Report {
  id: string;
  title: string;
  created: string;
  window: { start: string; end: string; tz: string; start_local: string; end_local: string };
  nodes: string[] | null;
  schedule: { id: string; name: string } | null;
  sections: ReportSection[];
  narrative: { text?: string; model?: string; error?: string } | null;
  headline: string[];
  seconds: number;
  kept?: boolean;
}

export interface ReportSummary {
  id: string;
  title: string;
  created: string;
  window: Report['window'];
  schedule: string | null;
  headline: string[];
  sections: string[];
  narrative: boolean;
}

export type Frequency = 'daily' | 'weekly' | 'monthly' | 'hours';

export interface Schedule {
  id: string;
  name: string;
  frequency: Frequency;
  time: string;
  weekday: string;
  day: number;
  every_hours: number;
  nodes: string[];
  sections: string[];
  notify: boolean;
  narrative: boolean;
  enabled: boolean;
}

export interface ReportsConfig {
  timezone: string | null;
  keep_count: number;
  keep_days: number;
  public_url: string | null;
  schedules: Schedule[];
}

export interface ReportsMeta {
  sections: { id: string; title: string; about: string; available: boolean }[];
  default_sections: string[];
  nodes: string[];
  config: ReportsConfig;
  error: string | null;
  timezone: string;
  host_timezone: string;
  assistant: boolean;
  ntfy: boolean;
  schedule_state: Record<string, { last_due?: string; last_report?: string; last_run?: string; error?: string | null }>;
  next_runs: Record<string, string>;
}

/** Building a long report can take a while; the backend reads a few dozen
 *  series per section. */
const BUILD_TIMEOUT_MS = 180_000;

async function send<T>(method: string, path: string, body?: object, timeoutMs?: number): Promise<T> {
  const resp = await fetchWithTimeout(
    path,
    { method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined },
    timeoutMs,
  );
  const payload = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(payload?.detail || resp.statusText);
  return payload as T;
}

export const reportsMeta = () => send<ReportsMeta>('GET', '/api/reports/meta');
export const saveReportsConfig = (c: ReportsConfig) => send<ReportsMeta>('PUT', '/api/reports/config', c);
export const listReports = () => send<{ reports: ReportSummary[] }>('GET', '/api/reports');
export const getReport = (id: string) => send<Report>('GET', `/api/reports/${encodeURIComponent(id)}`);
export const deleteReport = (id: string) => send<{ deleted: string }>('DELETE', `/api/reports/${encodeURIComponent(id)}`);
export const keepReport = (doc: Report) => send<{ kept: string }>('POST', '/api/reports/keep', doc);
export const runSchedule = (id: string) =>
  send<{ id: string }>('POST', `/api/reports/schedules/${encodeURIComponent(id)}/run`, undefined, BUILD_TIMEOUT_MS);
export const buildReport = (b: {
  start?: number;
  end?: number;
  minutes?: number;
  nodes?: string[] | null;
  sections: string[];
  title?: string;
  narrative?: boolean;
}) => send<Report>('POST', '/api/reports/build', b, BUILD_TIMEOUT_MS);

export const downloadUrl = (id: string, format: 'md' | 'html' | 'csv' | 'json') =>
  `/api/reports/${encodeURIComponent(id)}/download?format=${format}`;
export const printUrl = (id: string) => `/api/reports/${encodeURIComponent(id)}/download?format=html&print=1`;

export function newSchedule(defaults: string[]): Schedule {
  return {
    id: Math.random().toString(16).slice(2, 10),
    name: 'Daily report',
    frequency: 'daily',
    time: '08:00',
    weekday: 'mon',
    day: 1,
    every_hours: 6,
    nodes: [],
    sections: [...defaults],
    notify: true,
    narrative: false,
    enabled: true,
  };
}

/** "Every day at 08:00", "Mondays at 09:30", "On day 31 (or the month's
 *  last) at 06:00", "Every 6 hours". */
export function describe(s: Schedule): string {
  const DAYS: Record<string, string> = {
    mon: 'Mondays', tue: 'Tuesdays', wed: 'Wednesdays', thu: 'Thursdays', fri: 'Fridays',
    sat: 'Saturdays', sun: 'Sundays',
  };
  if (s.frequency === 'daily') return `Every day at ${s.time}`;
  if (s.frequency === 'weekly') return `${DAYS[s.weekday] ?? s.weekday} at ${s.time}`;
  if (s.frequency === 'monthly')
    return `On day ${s.day}${s.day > 28 ? ' (or the month’s last)' : ''} at ${s.time}`;
  return `Every ${s.every_hours} hour${s.every_hours === 1 ? '' : 's'}`;
}

/** Every zone the browser knows, for the picker; UTC first. */
export function zones(): string[] {
  try {
    const all = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf?.('timeZone');
    if (all?.length) return ['UTC', ...all.filter((z) => z !== 'UTC')];
  } catch {
    // An old browser: the common few.
  }
  return ['UTC', 'Europe/London', 'Europe/Berlin', 'Europe/Moscow', 'America/New_York', 'America/Chicago',
    'America/Denver', 'America/Los_Angeles', 'Asia/Tokyo', 'Asia/Shanghai', 'Australia/Sydney'];
}
