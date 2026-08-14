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
