/** Value formatting.
 *
 * Consistency matters more than precision here: the same quantity must read
 * the same everywhere, so a number in a table can be compared against one in a
 * band without mental conversion.
 */

import type { HealthState, ModelState, ProcessInfo } from './types';
import { LLM_RUNTIMES } from './types';

const GIB = 1024 ** 3;

/** Bytes as GiB. Everything on a GB10 is tens of GiB, so a single unit across
 *  the whole interface beats auto-scaling into MB/GB/TB — no unit-checking
 *  before you can compare two numbers. */
export function gib(bytes: number, digits = 1): string {
  return (bytes / GIB).toFixed(digits);
}

export function pct(value: number, digits = 0): string {
  return `${value.toFixed(digits)}%`;
}

export function ratioPct(part: number, whole: number): number {
  return whole > 0 ? (part / whole) * 100 : 0;
}

/** Short form for a number nobody reads digit by digit.
 *
 * For prefill specifically. Measured over six hours on `danflashes`, prefill is
 * non-zero 1% of the time and peaks at 110,571 tok/s — so the only question it
 * ever answers is "what order of magnitude", and rendering all six digits beside
 * a two-digit decode rate makes the smaller, more meaningful number look like
 * the incidental one.
 */
export function compact(value: number): string {
  if (value >= 999_500) return `${(value / 1e6).toFixed(1)}M`;
  if (value >= 999.5) return `${Math.round(value / 1000)}k`;
  return String(Math.round(value));
}

export function num(value: number | null | undefined, digits = 0, dash = '—'): string {
  if (value === null || value === undefined || Number.isNaN(value)) return dash;
  return value.toFixed(digits);
}

/** Status glyphs. Every status carries an icon AND a word, so meaning never
 *  rides on colour alone — necessary for colour-blind readers, and it survives
 *  a screenshot pasted into a monochrome context. */
export const HEALTH_GLYPH: Record<HealthState, string> = {
  good: '●',
  warning: '▲',
  serious: '▲',
  critical: '■',
};

export const HEALTH_LABEL: Record<HealthState, string> = {
  good: 'good',
  warning: 'warning',
  serious: 'serious',
  critical: 'critical',
};

/** Model lifecycle glyphs. `sleeping` gets a distinct mark because it's the
 *  operationally interesting state — a warm process with weights released,
 *  which is neither loaded nor a cold start. */
export const MODEL_GLYPH: Record<ModelState, string> = {
  active: '●',
  sleeping: '◐',
  loading: '◔',
  unloaded: '○',
  unknown: '?',
};

export function relativeTime(iso: string): string {
  const delta = (Date.now() - new Date(iso).getTime()) / 1000;
  if (delta < 2) return 'now';
  if (delta < 60) return `${Math.floor(delta)}s ago`;
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  return `${Math.floor(delta / 3600)}h ago`;
}

export interface MemoryBreakdown {
  llmBytes: number;
  otherGpuBytes: number;
  systemBytes: number;
  freeBytes: number;
  totalBytes: number;
}

/** Split the unified pool into what's consuming it.
 *
 * This is the whole point of the memory band. On GB10 there is no separate
 * VRAM, so image generation and model weights draw on the same pool — "19GiB
 * used" is unactionable until you know which of those it was.
 *
 * `systemBytes` is everything not attributed to a GPU process: page cache,
 * the OS, other services. It's derived rather than measured, so it absorbs any
 * attribution gap instead of the numbers silently failing to add up.
 */
export function breakdown(
  totalBytes: number,
  usedBytes: number,
  processes: ProcessInfo[],
): MemoryBreakdown {
  let llmBytes = 0;
  let otherGpuBytes = 0;

  for (const p of processes) {
    if (p.runtime && LLM_RUNTIMES.has(p.runtime)) llmBytes += p.gpu_mem_bytes;
    else otherGpuBytes += p.gpu_mem_bytes;
  }

  const attributed = llmBytes + otherGpuBytes;
  const systemBytes = Math.max(0, usedBytes - attributed);
  const freeBytes = Math.max(0, totalBytes - usedBytes);

  return { llmBytes, otherGpuBytes, systemBytes, freeBytes, totalBytes };
}

/** Compact age from an ISO timestamp: "just now", "8m", "3h", "2d".
 *
 * Shared because the alert banner and the history fly-out must describe the
 * same instant the same way — two formats for one concept reads as two
 * different pieces of information.
 */
export function age(iso: string | null): string {
  if (!iso) return '';
  return since(new Date(iso).getTime());
}

/** Same, from epoch seconds — what the history API returns. */
export function ageFromEpoch(seconds: number): string {
  return since(seconds * 1000);
}

function since(ms: number): string {
  const mins = Math.floor((Date.now() - ms) / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  return hours < 24 ? `${hours}h` : `${Math.floor(hours / 24)}d`;
}

/** A span in seconds, rendered for a table: "<1m", "4m", "2h 10m". */
export function duration(seconds: number): string {
  const mins = Math.round(seconds / 60);
  if (mins < 1) return '<1m';
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  const rest = mins % 60;
  return rest ? `${hours}h ${rest}m` : `${hours}h`;
}
