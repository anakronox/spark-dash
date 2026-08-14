<script lang="ts">
  /* The instrument strip: the readings you'd glance at in nvtop.
   * Deliberately one dense row — scanning a row of aligned figures is faster
   * than reading a grid of labelled cards. */
  import { num, pct } from '../lib/format';
  import type { CpuMetrics, GpuMetrics, PsiMetrics } from '../lib/types';

  interface Props {
    gpu: GpuMetrics | null;
    cpu: CpuMetrics | null;
    psi: PsiMetrics | null;
  }
  const { gpu, cpu, psi }: Props = $props();

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
    <dd class="num">{gpu?.temp_c != null ? `${num(gpu.temp_c)}°C` : '—'}</dd>
  </div>
  <div class="v">
    <dt>power</dt>
    <dd class="num">{gpu?.power_w != null ? `${num(gpu.power_w)}W` : '—'}</dd>
  </div>
  <div class="v"><dt>cpu</dt><dd class="num">{cpu ? pct(cpu.util_pct) : '—'}</dd></div>
  <div class="v"><dt>pressure</dt><dd data-tone={psiTone}>{psi?.state ?? '—'}</dd></div>
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
