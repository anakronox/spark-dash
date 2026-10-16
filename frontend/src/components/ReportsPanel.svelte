<script lang="ts">
  /* Reports (roadmap BI4, BI5).
   *
   * Kept reports on the left, newest first; the one chosen, rendered from its
   * document, on the right, with print (the self-contained HTML opens in a
   * tab and asks to print: that is the PDF) and the downloads. **Build** makes
   * a report now from a window, nodes and the sections a reader picks; it is
   * kept only when asked. Schedules are set in Settings → Reports.
   */
  import {
    buildReport,
    deleteReport,
    downloadUrl,
    getReport,
    keepReport,
    listReports,
    pdfUrl,
    printUrl,
    type Orientation,
    reportsMeta,
    type Report,
    type ReportSummary,
    type ReportsMeta,
  } from '../lib/reports';

  interface Props {
    open: boolean;
    onclose: () => void;
    /** Open on this report: a notification's link carries it. */
    focus?: string | null;
    onschedules?: () => void;
  }
  const { open, onclose, focus = null, onschedules }: Props = $props();

  let dialog = $state<HTMLDialogElement | null>(null);
  let tab = $state<'kept' | 'build'>('kept');
  let list = $state<ReportSummary[]>([]);
  let meta = $state<ReportsMeta | null>(null);
  let shown = $state<Report | null>(null);
  let busy = $state(false);
  let error = $state<string | null>(null);
  let armed = $state<string | null>(null);

  async function load(pick?: string | null) {
    error = null;
    /* Separately, not together: the list failing (2026-10-17, a file in
       the reports directory that was not a report) took the catalogue with
       it, and with no sections the builder could build nothing. */
    const [l, m] = await Promise.allSettled([listReports(), reportsMeta()]);
    if (m.status === 'fulfilled') {
      meta = m.value;
      if (!bSections.length) bSections = [...m.value.default_sections];
    } else error = `could not load the report settings: ${(m.reason as Error).message}`;
    if (l.status === 'fulfilled') {
      list = l.value.reports;
      const want = pick ?? shown?.id ?? list[0]?.id;
      if (want && list.some((r) => r.id === want)) await choose(want);
      else if (!list.length) shown = null;
    } else error = `could not list the kept reports: ${(l.reason as Error).message}`;
  }

  async function choose(id: string) {
    error = null;
    armed = null;
    try {
      shown = await getReport(id);
    } catch (err) {
      error = (err as Error).message;
    }
  }

  $effect(() => {
    if (!dialog) return;
    if (open && !dialog.open) {
      dialog.showModal();
      tab = 'kept';
      load(focus);
    } else if (!open && dialog.open) dialog.close();
  });

  /* Two presses: a deleted report is gone. */
  async function remove(id: string) {
    if (armed !== id) {
      armed = id;
      setTimeout(() => (armed = armed === id ? null : armed), 4000);
      return;
    }
    armed = null;
    try {
      await deleteReport(id);
      shown = null;
      await load(null);
    } catch (err) {
      error = (err as Error).message;
    }
  }

  // --- the builder ---------------------------------------------------------------------
  const PRESETS = [
    { id: '24h', label: 'last 24 h', minutes: 1440 },
    { id: '7d', label: 'last 7 days', minutes: 10080 },
    { id: '30d', label: 'last 30 days', minutes: 43200 },
    { id: 'custom', label: 'from … to', minutes: 0 },
  ];
  let bPreset = $state('24h');
  /* datetime-local values, in the browser's own zone. */
  let bFrom = $state('');
  let bTo = $state('');
  let bNodes = $state<string[]>([]);
  let bSections = $state<string[]>([]);
  let bNarrative = $state(false);
  let bTitle = $state('');
  let built = $state<Report | null>(null);

  function toggle(list: string[], v: string): string[] {
    return list.includes(v) ? list.filter((x) => x !== v) : [...list, v];
  }
  /* Kept in the catalogue's order, whatever order they were ticked in. */
  const orderedSections = $derived(
    (meta?.sections ?? []).map((s) => s.id).filter((id) => bSections.includes(id)),
  );

  async function build() {
    if (busy || !orderedSections.length) return;
    busy = true;
    error = null;
    built = null;
    try {
      const p = PRESETS.find((x) => x.id === bPreset)!;
      const window =
        p.minutes > 0
          ? { minutes: p.minutes }
          : { start: new Date(bFrom).getTime() / 1000, end: new Date(bTo).getTime() / 1000 };
      if (p.minutes === 0 && !(window.start! < window.end!)) throw new Error('choose a start before the end');
      built = await buildReport({
        ...window,
        nodes: bNodes.length && bNodes.length < (meta?.nodes.length ?? 0) ? bNodes : null,
        sections: orderedSections,
        title: bTitle.trim() || undefined,
        narrative: bNarrative,
      });
    } catch (err) {
      error = (err as Error).message;
    } finally {
      busy = false;
    }
  }

  async function keep() {
    if (!built) return;
    try {
      await keepReport(built);
      const id = built.id;
      built = null;
      tab = 'kept';
      await load(id);
    } catch (err) {
      error = (err as Error).message;
    }
  }

  /* THE PDF, made by the backend (A4), previewed in a frame before it is
     downloaded: the browser's print dialog lays a page out for the default
     printer's paper, which on a Mac whose only printer is an Instax is a
     photo-sized page. The orientation is remembered in this browser. */
  const ORIENT_KEY = 'spark-dash.report-pdf-orientation.v1';
  let pdfFor = $state<string | null>(null);
  let orientation = $state<Orientation>(
    (() => {
      try {
        return localStorage.getItem(ORIENT_KEY) === 'portrait' ? 'portrait' : 'landscape';
      } catch {
        return 'landscape';
      }
    })(),
  );
  function setOrientation(o: Orientation) {
    orientation = o;
    try {
      localStorage.setItem(ORIENT_KEY, o);
    } catch {
      // Kept for this visit.
    }
  }

  const cell = (v: unknown) => (v === null || v === undefined || v === '' ? '—' : String(v));
  const numeric = (v: unknown) => typeof v === 'number';
</script>

{#snippet viewer(r: Report, kept: boolean)}
  <article class="report">
    <header class="rhead">
      <h3>{r.title}</h3>
      <p class="meta">
        {r.window.start_local} to {r.window.end_local}, times in {r.window.tz} · nodes:
        {r.nodes?.length ? r.nodes.join(', ') : 'all'} · built in {r.seconds}s
        {#if r.schedule}· from the schedule <b>{r.schedule.name}</b>{/if}
      </p>
      <div class="actions">
        {#if kept}
          <button class="btn primary" onclick={() => (pdfFor = pdfFor === r.id ? null : r.id)}
            aria-expanded={pdfFor === r.id}>PDF</button
          >
          <a class="btn" href={printUrl(r.id)} target="_blank" rel="noopener">print</a>
          <a class="btn" href={downloadUrl(r.id, 'md')} download>Markdown</a>
          <a class="btn" href={downloadUrl(r.id, 'html')} download>HTML</a>
          <a class="btn" href={downloadUrl(r.id, 'csv')} download>CSV</a>
          <a class="btn" href={downloadUrl(r.id, 'json')} download>JSON</a>
          <button class="btn quiet" onclick={() => remove(r.id)}>
            {armed === r.id ? 'press again to delete' : 'delete'}
          </button>
        {:else}
          <button class="btn primary" onclick={keep}>keep</button>
          <span class="hint">Keep it to print or download it.</span>
        {/if}
      </div>
    </header>
    {#if kept && pdfFor === r.id}
      <div class="pdf">
        <div class="pdf-bar">
          <span class="hint">A4</span>
          <div class="segmented" role="group" aria-label="PDF orientation">
            {#each ['portrait', 'landscape'] as const as o (o)}
              <button class:active={orientation === o} aria-pressed={orientation === o}
                onclick={() => setOrientation(o)}>{o}</button
              >
            {/each}
          </div>
          <a class="btn primary" href={pdfUrl(r.id, orientation)} download>download PDF</a>
          <button class="btn quiet" onclick={() => (pdfFor = null)}>close</button>
        </div>
        <!-- Keyed on the orientation, so switching reloads the preview. -->
        {#key orientation}
          <iframe
            class="pdf-frame"
            class:portrait={orientation === 'portrait'}
            title="PDF preview, {orientation}"
            src={pdfUrl(r.id, orientation, true)}
          ></iframe>
        {/key}
      </div>
    {/if}
    {#if r.narrative?.text}
      <div class="narrative">
        <b>Summary, written by {r.narrative.model ?? 'the assistant'}:</b>
        {r.narrative.text}
      </div>
    {:else if r.narrative?.error}
      <p class="hint">No summary: {r.narrative.error}</p>
    {/if}
    {#each r.sections as s (s.id)}
      <section class="sec" class:failed={s.error}>
        <h4>{s.title}</h4>
        <p class="about">{s.about}</p>
        {#if s.lines.length}
          <ul>{#each s.lines as l, i (i)}<li>{l}</li>{/each}</ul>
        {/if}
        {#each s.tables as t, ti (ti)}
          {#if !t.rows.length}
            <p class="hint">{t.title}: nothing in this period.</p>
          {:else}
            <div class="scroll">
              <table>
                <caption>{t.title}</caption>
                <thead><tr>{#each t.columns as c (c.key)}<th>{c.label}</th>{/each}</tr></thead>
                <tbody>
                  {#each t.rows as row, ri (ri)}
                    <tr>
                      {#each t.columns as c (c.key)}
                        <td class:num={numeric(row[c.key])}>{cell(row[c.key])}</td>
                      {/each}
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
          {/if}
        {/each}
      </section>
    {/each}
  </article>
{/snippet}

<dialog
  bind:this={dialog}
  class="flyout"
  aria-label="Reports"
  onclose={onclose}
  onclick={(e) => {
    if (e.target === dialog) onclose();
  }}
>
  <div class="panel">
    <header class="top">
      <h2 class="eyebrow">Reports</h2>
      <div class="segmented" role="group" aria-label="Reports view">
        <button class:active={tab === 'kept'} aria-pressed={tab === 'kept'} onclick={() => (tab = 'kept')}
          >kept · {list.length}</button
        >
        <button class:active={tab === 'build'} aria-pressed={tab === 'build'} onclick={() => (tab = 'build')}
          >build</button
        >
      </div>
      {#if onschedules}
        <button class="btn quiet" onclick={onschedules}>schedules</button>
      {/if}
      <button class="close" aria-label="Close" onclick={onclose}>×</button>
    </header>

    {#if error}<p class="err">{error}</p>{/if}

    {#if tab === 'kept'}
      <div class="body">
        <ol class="list" aria-label="Kept reports">
          {#each list as r (r.id)}
            <li>
              <button class="item" aria-current={shown?.id === r.id ? 'true' : undefined} onclick={() => choose(r.id)}>
                <span class="t">{r.schedule ?? 'On demand'}</span>
                <span class="w">{r.window.start_local} → {r.window.end_local}</span>
                {#if r.headline[0]}<span class="h">{r.headline[0]}</span>{/if}
              </button>
            </li>
          {:else}
            <li class="empty">
              No reports kept yet. Build one, or set a schedule in Settings → Reports.
            </li>
          {/each}
        </ol>
        <div class="view">
          {#if shown}
            {@render viewer(shown, true)}
          {:else if list.length}
            <p class="hint">Choose a report.</p>
          {/if}
        </div>
      </div>
    {:else}
      <div class="build">
        <form
          class="form"
          onsubmit={(e) => {
            e.preventDefault();
            build();
          }}
        >
          <fieldset>
            <legend>Period</legend>
            <div class="segmented" role="group" aria-label="Period">
              {#each PRESETS as p (p.id)}
                <button type="button" class:active={bPreset === p.id} aria-pressed={bPreset === p.id}
                  onclick={() => (bPreset = p.id)}>{p.label}</button
                >
              {/each}
            </div>
            {#if bPreset === 'custom'}
              <div class="row">
                <label>from <input class="in" type="datetime-local" bind:value={bFrom} /></label>
                <label>to <input class="in" type="datetime-local" bind:value={bTo} /></label>
                <span class="hint">in your browser's time; at most 31 days</span>
              </div>
            {/if}
          </fieldset>
          <fieldset>
            <legend>Nodes</legend>
            <div class="checks">
              {#each meta?.nodes ?? [] as n (n)}
                <label class="check">
                  <input type="checkbox" checked={!bNodes.length || bNodes.includes(n)}
                    onchange={() => (bNodes = toggle(bNodes.length ? bNodes : [...(meta?.nodes ?? [])], n))} />
                  {n}
                </label>
              {/each}
            </div>
          </fieldset>
          <fieldset>
            <legend>Sections, in this order</legend>
            <div class="sections">
              {#each meta?.sections ?? [] as s (s.id)}
                <label class="check" title={s.about} class:unavailable={!s.available}>
                  <input type="checkbox" checked={bSections.includes(s.id)}
                    onchange={() => (bSections = toggle(bSections, s.id))} />
                  <span><b>{s.title}</b> <span class="hint">{s.available ? s.about : 'not set up here'}</span></span>
                </label>
              {/each}
            </div>
          </fieldset>
          <div class="row">
            <input class="in grow-in" placeholder="Title (optional)" aria-label="Report title" bind:value={bTitle} />
            {#if meta?.assistant}
              <label class="check">
                <input type="checkbox" bind:checked={bNarrative} />
                add a summary written by the assistant
              </label>
            {/if}
            <button class="btn primary" type="submit" disabled={busy || !orderedSections.length}>
              {busy ? 'building…' : 'build report'}
            </button>
          </div>
        </form>
        {#if built}
          {@render viewer(built, false)}
        {/if}
      </div>
    {/if}
  </div>
</dialog>

<style>
  .flyout {
    margin: 0 0 0 auto;
    height: 100%;
    max-height: 100%;
    width: min(1100px, 100%);
    max-width: 100%;
    padding: 0;
    border: none;
    border-left: 1px solid var(--rule);
    background: var(--panel);
    color: var(--ink);
    font-size: var(--text-body);
  }
  .flyout::backdrop {
    background: rgb(0 0 0 / 0.45);
  }
  .panel {
    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 0;
  }
  .top {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 14px 20px 12px;
    border-bottom: 1px solid var(--rule);
  }
  .top h2 {
    margin: 0 auto 0 0;
  }
  .close {
    border: none;
    background: none;
    color: var(--ink-muted);
    font-size: var(--text-title-sm);
    cursor: pointer;
  }
  .err {
    margin: 8px 20px 0;
    color: var(--warning);
    font-size: var(--text-label);
  }

  .body {
    flex: 1;
    min-height: 0;
    display: grid;
    grid-template-columns: 260px minmax(0, 1fr);
  }
  .list {
    list-style: none;
    margin: 0;
    padding: 10px 8px;
    overflow-y: auto;
    border-right: 1px solid var(--rule);
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .item {
    display: flex;
    flex-direction: column;
    gap: 2px;
    width: 100%;
    text-align: left;
    padding: 8px 10px;
    border-radius: var(--radius);
    background: none;
    border: 1px solid transparent;
    color: var(--ink-2);
    cursor: pointer;
    font: inherit;
  }
  .item:hover {
    background: var(--panel-raised);
  }
  .item[aria-current='true'] {
    background: var(--panel-raised);
    border-color: var(--rule);
    box-shadow: inset 2px 0 0 var(--series-1);
  }
  .item .t {
    color: var(--ink);
    font-weight: 600;
  }
  .item .w,
  .item .h {
    font-size: var(--text-micro);
    color: var(--ink-muted);
  }
  .empty {
    padding: 10px;
    color: var(--ink-muted);
    font-size: var(--text-label);
  }
  .view,
  .build {
    overflow-y: auto;
    padding: 16px 22px 28px;
    min-width: 0;
  }
  .build {
    flex: 1;
  }

  .report {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .rhead h3 {
    margin: 0;
    font-size: var(--text-title-sm);
  }
  .meta,
  .about,
  .hint {
    margin: 0;
    color: var(--ink-muted);
    font-size: var(--text-label);
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: center;
    margin: 8px 0 4px;
  }
  .pdf {
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    padding: 8px;
    background: var(--panel-raised);
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .pdf-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  /* The page's own proportions, A4: 297 x 210 landscape, 210 x 297
     portrait, as tall as the window allows. */
  .pdf-frame {
    width: 100%;
    aspect-ratio: 297 / 210;
    max-height: 75vh;
    border: 1px solid var(--rule);
    background: #fff;
  }
  .pdf-frame.portrait {
    aspect-ratio: 210 / 297;
    width: auto;
    height: 75vh;
    max-width: 100%;
    align-self: center;
  }
  .narrative {
    border-left: 3px solid var(--series-1);
    padding: 8px 12px;
    background: var(--panel-raised);
    line-height: 1.5;
  }
  .sec {
    margin-top: 14px;
  }
  .sec h4 {
    margin: 0;
    font-size: var(--text-label);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    padding-bottom: 3px;
    border-bottom: 1px solid var(--rule);
  }
  .sec.failed h4 {
    color: var(--warning);
  }
  .sec ul {
    margin: 6px 0;
    padding-left: 18px;
    line-height: 1.5;
  }
  .scroll {
    overflow-x: auto;
  }
  table {
    border-collapse: collapse;
    width: 100%;
    margin: 4px 0 8px;
    font-size: var(--text-label);
    font-variant-numeric: tabular-nums;
  }
  caption {
    text-align: left;
    color: var(--ink-2);
    font-weight: 600;
    padding: 4px 0;
  }
  th,
  td {
    text-align: left;
    padding: 3px 8px;
    border-bottom: 1px solid color-mix(in srgb, var(--rule) 60%, transparent);
    white-space: nowrap;
  }
  th {
    color: var(--ink-muted);
    font-weight: 500;
  }
  td.num {
    text-align: right;
  }

  .btn {
    font: inherit;
    font-size: var(--text-label);
    padding: 3px 10px;
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    background: var(--panel-raised);
    color: var(--ink-2);
    cursor: pointer;
    text-decoration: none;
  }
  .btn:hover {
    color: var(--ink);
    border-color: var(--ink-muted);
  }
  .btn.primary {
    color: var(--ink);
    border-color: var(--good);
  }
  .btn.quiet {
    background: none;
  }
  .btn:disabled {
    opacity: 0.5;
    cursor: default;
  }

  .form {
    display: flex;
    flex-direction: column;
    gap: 12px;
    margin-bottom: 16px;
  }
  fieldset {
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    padding: 8px 12px 10px;
    margin: 0;
  }
  legend {
    font-size: var(--text-micro);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-muted);
    padding: 0 4px;
  }
  .row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 12px;
    align-items: center;
    margin-top: 6px;
  }
  .checks {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 16px;
  }
  .sections {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 6px 16px;
  }
  .check {
    display: flex;
    gap: 6px;
    align-items: flex-start;
    font-size: var(--text-label);
    color: var(--ink-2);
  }
  .check input {
    margin-top: 2px;
  }
  .check.unavailable {
    opacity: 0.6;
  }
  .in {
    font: inherit;
    font-size: var(--text-label);
    background: var(--page, var(--panel));
    color: var(--ink);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    padding: 4px 7px;
  }
  .grow-in {
    flex: 1;
    min-width: 200px;
  }

  @media (max-width: 720px) {
    .flyout {
      width: 100%;
      border-left: none;
    }
    .body {
      grid-template-columns: minmax(0, 1fr);
      grid-template-rows: auto minmax(0, 1fr);
    }
    .list {
      max-height: 30vh;
      border-right: none;
      border-bottom: 1px solid var(--rule);
    }
    .view,
    .build {
      padding: 14px 14px 24px;
    }
  }
</style>
