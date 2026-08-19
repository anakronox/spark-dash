<script lang="ts">
  /* The instrument strip: the readings you'd glance at in nvtop.
   * Deliberately one dense row — scanning a row of aligned figures is faster
   * than reading a grid of labelled cards. */
  import { gib, num, pct } from '../lib/format';
  import type {
    CpuMetrics,
    DiskMetrics,
    GpuMetrics,
    PsiMetrics,
    MemoryMetrics,
    TempBands,
  } from '../lib/types';

  interface Props {
    gpu: GpuMetrics | null;
    cpu: CpuMetrics | null;
    psi: PsiMetrics | null;
    /* The three readings the History chips used to carry and nothing else did.
       GPU, clock, temp, power and CPU were already on this strip, so dropping
       the chip values lost nothing for those — these would have gone dark. */
    memory: MemoryMetrics | null;
    disk: DiskMetrics | null;
    /** This node's OWN thresholds, so nothing here hardcodes a temperature. */
    tempBands: TempBands | null;
    tokensPerSec: number;
  }
  const { gpu, cpu, psi, memory, disk, tempBands, tokensPerSec }: Props = $props();

  const memPct = $derived(
    memory && memory.total_bytes > 0
      ? (memory.used_bytes / memory.total_bytes) * 100
      : null,
  );

  /* Percent full on the SAME basis as NodeDiskWarning and NodeDiskLow:
     used / (used + available). Using total as the denominator instead would
     read a few points lower than the alert that is about to fire. */
  const diskPct = $derived.by(() => {
    if (!disk) return null;
    const denom = disk.used_bytes + disk.available_bytes;
    return denom > 0 ? (disk.used_bytes / denom) * 100 : null;
  });

  /* Matches the alert tiers exactly, so the colour changes when the alert
     would, not near where it would. */
  const diskTone = $derived(
    diskPct == null ? undefined : diskPct >= 95 ? 'critical' : diskPct >= 90 ? 'warning' : undefined,
  );

  /* 56°C means nothing without a scale, and until now the card showed the
     number with none. The bands come from the NODE — read off the hardware
     where possible — which is also why nothing here hardcodes a GB10
     temperature: a dashboard that did would be a GB10 dashboard in a way this
     one does not have to be.

     STRICTLY GREATER THAN, matching health.py's `temp_c > temps.critical_c`.
     Using >= would tone the card a degree before the health pill agreed, and
     two indicators disagreeing about the same reading is worse than either
     being slightly conservative. */
  const tempTone = $derived.by(() => {
    const t = gpu?.temp_c;
    if (t == null || !tempBands) return undefined;
    if (t > tempBands.gpu_critical_c) return 'critical';
    if (t > tempBands.gpu_warning_c) return 'warning';
    return undefined;
  });

  /* `gpu_source` is the difference between a threshold you can trust and one
     that is a guess, so it is said out loud rather than dropped.

     It is a PROVENANCE LABEL, not a boolean — the vocabulary is
     `nvml-slowdown`, `acpi-critical-trip`, `override` and `fallback`. Only the
     last is untrustworthy; the rest name where the number genuinely came from
     and are more informative than any paraphrase, so they are shown verbatim.
     `fallback` is spelled out instead, because a guess presented in the same
     voice as a measurement is the failure this field exists to prevent. */
  const tempDetail = $derived.by(() => {
    if (!tempBands) return '';
    const b = tempBands;
    const origin =
      b.gpu_source === 'fallback'
        ? 'fallback estimate — not read from this device'
        : b.gpu_source;
    return `warns above ${b.gpu_warning_c}°C, critical above ${b.gpu_critical_c}°C (${origin})`;
  });

  // IDLE is not a fault — it means "not under load, so not evaluated". Only a
  // judgement made under load is worth colouring.
  const clockTone = $derived(
    gpu?.clock_state === 'THROTTLED'
      ? 'critical'
      : gpu?.clock_state === 'LOCKED'
        ? 'serious'
        : 'plain',
  );

  const psiTone = $derived(
    psi?.state === 'CRITICAL'
      ? 'critical'
      : psi?.state === 'HIGH'
        ? 'serious'
        : psi?.state === 'MOD'
          ? 'warning'
          : 'plain',
  );
</script>

<dl class="vitals">
  <div class="v"><dt>gpu</dt><dd class="num">{gpu ? pct(gpu.util_pct) : '—'}</dd></div>
  <div class="v">
    <dt>clock</dt>
    <dd class="num" data-tone={clockTone}>
      {gpu?.clock_mhz ? `${num(gpu.clock_mhz)}MHz` : '—'}
      <span class="state">{gpu?.clock_state ?? ''}</span>
    </dd>
  </div>
  <div class="v">
    <dt>temp</dt>
    <dd class="num" data-tone={tempTone} title={tempDetail}>
      {gpu?.temp_c != null ? `${num(gpu.temp_c)}°C` : '—'}
    </dd>
  </div>
  <div class="v">
    <dt>power</dt>
    <dd class="num">{gpu?.power_w != null ? `${num(gpu.power_w)}W` : '—'}</dd>
  </div>
  <div class="v"><dt>cpu</dt><dd class="num">{cpu ? pct(cpu.util_pct) : '—'}</dd></div>
  <div class="v"><dt>mem</dt><dd class="num">{memPct != null ? pct(memPct) : '—'}</dd></div>
  <div class="v">
    <dt>swap</dt>
    <!-- A LEVEL, not a symptom, and labelled plainly so it is not read as one.
         A node can hold gigabytes of cold pages here and be perfectly healthy;
         thrashing is a RATE and lives on the Swap I/O chart, which plots the
         same quantity SwapThrashing alerts on. Deliberately untoned for the
         same reason — colouring a non-zero value would assert trouble that a
         resident figure cannot establish.

         Worth showing at all because this is a unified-memory box: swap in use
         means the one pool that models live in is under real pressure, and
         until now nothing on the dashboard said so. -->
    <dd class="num">{memory ? `${gib(memory.swap_used_bytes)}G` : '—'}</dd>
  </div>
  <div class="v">
    <dt>disk</dt>
    <!-- Used/Total in GiB, not auto-scaled to TiB: see format.ts. One fixed
         unit means two nodes' disks compare without unit-checking, which is
         the case that matters once there are three of them. The tone carries
         "should I care", so the digits never need reading closely. -->
    <dd class="num" data-tone={diskTone}>
      {disk ? `${gib(disk.used_bytes, 0)}/${gib(disk.total_bytes, 0)}GiB` : '—'}
    </dd>
  </div>
  <div class="v">
    <dt>tok/s</dt>
    <dd class="num">{tokensPerSec > 0 ? tokensPerSec.toFixed(1) : '—'}</dd>
  </div>
  <div class="v">
    <dt>pressure</dt>
    <!-- The state carries the tone; the number is what the History chart plots,
         and reading "LOW" against a rising line was the gap. -->
    <dd data-tone={psiTone}>
      {psi?.state ?? '—'}
      <span class="state num">{psi ? `${psi.some_avg10.toFixed(0)}%` : ''}</span>
    </dd>
  </div>
</dl>

<style>
  .vitals {
    display: flex;
    flex-wrap: wrap;
    gap: 2px 22px;
    margin: 0;
    font-size: 12px;
  }
  .v {
    display: flex;
    align-items: baseline;
    gap: 6px;
  }
  dt {
    font-size: 10px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink-muted);
  }
  dd {
    margin: 0;
    color: var(--ink);
  }
  .state {
    font-size: 10px;
    color: var(--ink-muted);
    margin-left: 2px;
  }
  [data-tone='warning'] {
    color: var(--warning);
  }
  [data-tone='serious'] {
    color: var(--serious);
  }
  [data-tone='critical'] {
    color: var(--critical);
  }
</style>
