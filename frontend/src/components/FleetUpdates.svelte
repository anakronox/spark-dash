<script lang="ts">
  /* The fleet updater, as a right-anchored fly-out (roadmap AK).
   *
   * WHAT THIS IS. spark-fleet-updates is its own service with its own page:
   * every Spark on one line, is there an update, and a button to install it.
   * This is that page again, drawn in the dashboard's idiom and fed through
   * the backend proxy -- so the answer to "are the Sparks current?" is one
   * click from the numbers, behind the same OAuth as the rest of this page,
   * and without the fleet service's port having to be reachable from
   * wherever you are reading.
   *
   * WHAT IT IS NOT. Not the fleet service's administration. Which nodes are
   * on its list is decided in Settings, a checkbox under each node of the
   * cluster, since the dashboard already knows every id and host; the fleet
   * page, which the header links to, has the rest. Not a second opinion,
   * either: every word of status here is `lib/fleet.ts`, ported from the
   * fleet page's own functions, so the two never disagree about a Spark.
   *
   * THE SAME SHELL AS ALERTS AND SETTINGS -- <dialog> + showModal(), for
   * the platform's focus trap, Escape, backdrop and focus restore -- but
   * wider: a row here is name, host, status, a one-line summary and two
   * buttons, and under 1000px it wraps. Three panels is the point at which
   * Settings' comment said to extract the shell; not done in this change,
   * which is about the fleet, but it is now due.
   *
   * CONFIRMING IS INLINE, the MaintenanceControl pattern, not a nested
   * dialog: pressing Update turns the row's footer into the confirmation --
   * what it installs, who else is updated with it, the password field when
   * the Spark needs one -- with cancel beside it. The row stays in view
   * while you decide, which a modal over a modal would hide.
   */
  import Pager from './Pager.svelte';
  import PickMenu from './PickMenu.svelte';
  import { TableView } from '../lib/table.svelte';
  import { nodeColorVar } from '../lib/theme';
  import { poll } from '../lib/visibility.svelte';
  import type { FleetFeed } from '../lib/fleet.svelte';
  import { BUSY_MS } from '../lib/fleet.svelte';
  import {
    agoOf,
    bytes,
    clockOf,
    clusterSummary,
    installable,
    lastRunOf,
    lineOf,
    linkText,
    needsPassword,
    nextCheckText,
    pinnedOf,
    runProgress,
    shortFirmware,
    statusOf,
    summaryOf,
  } from '../lib/fleet';
  import type { FleetCluster, FleetNode, FleetPackage, FleetTone } from '../lib/fleet';

  interface Props {
    feed: FleetFeed;
    open: boolean;
    onclose: () => void;
    /** Node id -> palette slot, from the dashboard's own inventory. A Spark
     *  the dashboard does not monitor gets no hue rather than someone
     *  else's -- the rule `nodeColor` states. */
    slots: Map<string, number>;
  }
  const { feed, open, onclose, slots }: Props = $props();

  let dialog = $state<HTMLDialogElement | null>(null);
  let panel = $state<HTMLElement | null>(null);

  $effect(() => {
    if (!dialog) return;
    if (open && !dialog.open) {
      dialog.showModal();
      /* The element persists between openings, and so would its scroll:
         reopened, it should start at the top, not where it was left. */
      if (panel) panel.scrollTop = 0;
      feed.setOpen(true);
    } else if (!open && dialog.open) {
      dialog.close();
    }
  });

  /* The countdown and "N min so far" tick on their own; the feed refreshes
     at most every 3s and as rarely as every minute. */
  let now = $state(Date.now());
  $effect(() => {
    if (!open) return;
    const t = setInterval(() => (now = Date.now()), 15_000);
    return () => clearInterval(t);
  });

  const fleet = $derived(feed.fleet);
  const nodes = $derived(fleet?.nodes ?? []);
  const byName = $derived(new Map(nodes.map((n) => [n.name, n])));
  const clusters = $derived<FleetCluster[]>(fleet?.clusters ?? []);
  const inCluster = $derived(new Set(clusters.flatMap((c) => c.members)));
  const singles = $derived(nodes.filter((n) => !inCluster.has(n.name)));

  const colorOf = (name: string) => {
    const slot = slots.get(name);
    return slot === undefined ? 'var(--rule)' : nodeColorVar(slot);
  };

  // --- per-row state ------------------------------------------------------

  let expanded = $state<Record<string, boolean>>({});
  const toggle = (name: string) => (expanded[name] = !expanded[name]);

  /* One confirmation at a time: arming a second row disarms the first. */
  let arming = $state<{ name: string; rehearse: boolean } | null>(null);
  let password = $state('');
  let error = $state<string | null>(null);
  let busy = $state(false);
  let passwordInput = $state<HTMLInputElement | null>(null);
  $effect(() => {
    if (arming && passwordInput) passwordInput.focus();
  });

  function arm(name: string, rehearse = false) {
    arming = { name, rehearse };
    password = '';
    error = null;
  }
  function disarm() {
    arming = null;
    password = '';
    error = null;
  }

  /** Who is updated when this Spark is: itself first, then the rest of its
   *  cluster, the way the fleet service orders them. A rehearsal is one
   *  node, whatever it is cabled to. */
  const targets = (n: FleetNode, rehearse: boolean) =>
    rehearse ? [n.name] : [n.name, ...(n.pair ?? [])];

  const asksPassword = (n: FleetNode, rehearse: boolean) =>
    targets(n, rehearse).some((m) => needsPassword(byName.get(m)));

  const insecure = typeof location !== 'undefined' && location.protocol !== 'https:';

  async function go() {
    if (!arming || busy) return;
    const n = byName.get(arming.name);
    if (!n) return;
    const ask = asksPassword(n, arming.rehearse);
    if (ask && !password) {
      passwordInput?.focus();
      return;
    }
    busy = true;
    error = null;
    try {
      if (arming.rehearse) await feed.rehearse(n.name, ask ? password : undefined);
      else await feed.update(n.name, ask ? password : undefined);
      expanded[n.name] = false;
      disarm();
    } catch (err) {
      error = (err as Error).message;
    } finally {
      busy = false;
    }
  }

  /* A word back for the actions that only enqueue something -- "Checking
     sparky…" -- so a click is seen to land before the feed catches up. */
  let flash = $state<string | null>(null);
  let flashTimer: ReturnType<typeof setTimeout> | null = null;
  function say(msg: string) {
    flash = msg;
    if (flashTimer) clearTimeout(flashTimer);
    flashTimer = setTimeout(() => (flash = null), 3500);
  }
  async function act(fn: () => Promise<unknown>, msg: string) {
    try {
      await fn();
      say(msg);
    } catch (err) {
      say((err as Error).message);
    }
  }

  function menu(n: FleetNode, key: string) {
    if (key === 'check') act(() => feed.check(n.name), `Checking ${n.name}…`);
    else if (key === 'rehearse') arm(n.name, true);
    else if (key === 'log') {
      if (n.last_run) showLog(n.last_run.id, n.name);
      else say(`No update has run on ${n.name} yet.`);
    }
  }

  // --- the log --------------------------------------------------------------

  let logFor = $state<{ runId: string; node: string } | null>(null);
  let logLines = $state<string[]>([]);
  let logEl = $state<HTMLPreElement | null>(null);

  function showLog(runId: string, node: string) {
    logFor = logFor?.runId === runId && logFor.node === node ? null : { runId, node };
    logLines = [];
  }
  async function loadLog() {
    if (!logFor) return;
    const { runId, node } = logFor;
    try {
      const lines = await feed.log(runId, node);
      const el = logEl;
      const atEnd = el ? el.scrollTop + el.clientHeight >= el.scrollHeight - 20 : true;
      logLines = lines;
      if (el && atEnd) requestAnimationFrame(() => (el.scrollTop = el.scrollHeight));
    } catch {
      /* the next tick tries again */
    }
  }
  $effect(() => {
    if (!logFor || !open) return;
    loadLog();
    return poll(loadLog, BUSY_MS);
  });

  // --- the package list -----------------------------------------------------

  let allPkgsFor = $state<string | null>(null);
  let pkgFilter = $state('');
  const pkgView = new TableView<FleetPackage>([], 25);
  const pkgRows = (n: FleetNode) => {
    const all = n.updates?.packages ?? [];
    const q = pkgFilter.trim().toLowerCase();
    if (!q) return all;
    return all.filter(
      (p) =>
        p.name.toLowerCase().includes(q) ||
        (p.security && 'security'.includes(q)) ||
        (p.new && 'new'.includes(q)),
    );
  };

  // --- class strings: see lib/styles.md ------------------------------------

  const CLOSE_BTN =
    'text-glyph leading-none px-2 py-[2px] rounded-sm text-ink-muted hover:text-ink';

  /* Three kinds of button, the fleet page's own: an outlined one for anything
     that opens or asks, one filled in the good colour for the one that
     changes a Spark, and an outlined red one for stopping. Small caps,
     tracked, like the header's.

     The colours are NOT layered on a shared string. Two utilities that set
     the same property have equal specificity, so the one that wins is the
     one Tailwind happened to emit later -- and `bg-good` after
     `bg-panel-raised` lost, silently, leaving the primary button grey. Each
     variant names its own colours once. */
  const BTN_BASE =
    'inline-flex items-center gap-2 h-[26px] px-3 rounded-sm text-micro tracking-[0.1em] ' +
    'uppercase font-bold border whitespace-nowrap disabled:opacity-45 disabled:cursor-default';
  const BTN = `${BTN_BASE} border-rule text-ink-2 bg-panel-raised hover:not-disabled:border-ink-muted`;
  const BTN_PRIMARY = `${BTN_BASE} border-good text-page bg-good`;
  const BTN_DANGER = `${BTN_BASE} border-critical text-critical bg-transparent`;
  const LINK = 'text-micro tracking-[0.1em] uppercase text-ink-muted hover:text-ink whitespace-nowrap';

  const ROW = 'bg-panel border border-rule rounded-sm mb-[10px]';
  /* Wraps rather than truncates: under 1000px the summary line drops under
     the name, which beats an ellipsis hiding the one sentence that says what
     is wrong. */
  const ROWHEAD = 'flex flex-wrap items-center gap-x-[14px] gap-y-2 px-4 py-[12px] min-h-[52px]';
  const DOT = 'inline-block w-[7px] h-[7px] rounded-full flex-none';
  const NAME = 'text-title font-bold min-w-[110px]';
  const HOST = 'text-micro text-ink-muted min-w-[100px]';
  const SUB = 'flex-1 basis-[280px] min-w-0 text-ink-muted';

  /* Glyph + word + colour, never colour alone, like StatusPill -- but with
     the fleet's own states, which include one that is neither good nor bad. */
  const PILL =
    'inline-flex items-center gap-[6px] h-5 px-2 border rounded-sm text-micro ' +
    'tracking-[0.08em] uppercase whitespace-nowrap';
  const PILL_TONE: Record<FleetTone, string> = {
    good: 'border-good text-good',
    warning: 'border-warning text-warning',
    critical: 'border-critical text-critical',
    info: 'border-series-1 text-series-1',
    '': 'border-rule text-ink-2',
  };
  const GLYPH: Record<FleetTone, string> = {
    good: '✓',
    warning: '▲',
    critical: '■',
    info: '●',
    '': '·',
  };
  const TONE_TEXT: Record<FleetTone, string> = {
    good: 'text-good',
    warning: 'text-warning',
    critical: 'text-critical',
    info: 'text-series-1',
    '': 'text-ink-2',
  };

  const EXPAND = 'grid gap-6 px-4 py-4 border-t border-rule min-[1000px]:grid-cols-[1.1fr_1fr_1.2fr]';
  const H = 'text-label tracking-[0.1em] uppercase text-ink-muted mb-2';
  const LI = 'flex justify-between gap-3 py-[5px] border-b border-panel-raised last:border-b-0';
  /* A long value wraps; wrapped text that is right-aligned reads as ragged
     left edges. The block sits at the right, its lines start at the left. */
  const VALUE = 'text-left';
  const FOOT = 'flex flex-wrap items-center gap-[14px] px-4 pb-4';
  const NOTE = 'text-body text-ink-2 leading-[1.6] m-0';
  const DIM = 'text-micro text-ink-muted';

  const CONFIRM = 'flex flex-col gap-[10px] px-4 pb-4 pt-3 border-t border-rule';
  const FIELD = 'flex flex-col gap-[6px]';
  const EYEBROW = 'text-micro tracking-[0.12em] uppercase text-ink-muted';
  const INPUT =
    'h-8 px-[10px] rounded-sm border border-rule bg-page text-ink text-body ' +
    'focus:outline focus:outline-1 focus:outline-series-1 max-w-[360px]';

  const CLUSTER = 'border border-dashed border-rule rounded-sm px-3 pt-3 pb-[2px] mb-[10px]';
  const CLUSTER_HEAD = 'flex flex-wrap items-baseline gap-3 px-1 pb-[10px]';
  const CLUSTER_NAME = 'text-heading font-bold tracking-[0.08em] uppercase';

  const HEADER_META = 'text-label text-ink-muted';
</script>

<dialog
  bind:this={dialog}
  class="flyout"
  aria-label="Fleet updates"
  onclose={() => {
    feed.setOpen(false);
    disarm();
    logFor = null;
    onclose();
  }}
  onclick={(e) => {
    if (e.target === dialog) onclose();
  }}
>
  <div class="panel" bind:this={panel}>
    <header>
      <div class="flex flex-wrap items-baseline gap-x-4 gap-y-1 min-w-0">
        <h2 class="eyebrow">Fleet updates</h2>
        {#if fleet}
          <span class={HEADER_META}>{summaryOf(fleet)}</span>
        {/if}
        {#if flash}
          <span class="text-label text-ink-2">{flash}</span>
        {/if}
      </div>
      <div class="flex items-center gap-3">
        {#if fleet}
          <span class={HEADER_META}>{nextCheckText(fleet, now)}</span>
          <button
            class={BTN}
            disabled={feed.busy}
            onclick={() => act(() => feed.checkAll(), 'Checking every Spark…')}
          >
            check all now
          </button>
        {/if}
        {#if feed.publicUrl}
          <a class={LINK} href={feed.publicUrl} target="_blank" rel="noopener">fleet page ↗</a>
        {/if}
        <button class={CLOSE_BTN} aria-label="Close" onclick={onclose}>×</button>
      </div>
    </header>

    {#if !feed.configured}
      <p class={NOTE}>
        Fleet updates are not configured — set <code>FLEET_UPDATES_URL</code> on the backend.
      </p>
    {:else if !feed.available}
      <p class="{NOTE} text-warning">
        spark-fleet-updates is not answering. The dashboard keeps trying; nothing on the Sparks
        is affected.
      </p>
    {:else if !nodes.length}
      <p class={NOTE}>
        No Sparks on the fleet list yet. Tick <em>fleet updates</em> under a node in Settings.
      </p>
    {:else}
      <div>
        {#each clusters as c (c.members.join('+'))}
          <section class={CLUSTER} aria-label={c.name ?? c.members.join(' and ')}>
            <div class={CLUSTER_HEAD}>
              <span class={CLUSTER_NAME}>{c.name ?? c.members.join(' + ')}</span>
              <span class={HEADER_META}>
                {c.members.length} Sparks · {clusterSummary(c)} · auto-discovered
              </span>
              <span class="flex-1"></span>
              <span class={DIM} title="what each port sees over LLDP">{linkText(c)}</span>
            </div>
            {#each c.members as m (m)}
              {#if byName.get(m)}
                {@render row(byName.get(m) as FleetNode)}
              {/if}
            {/each}
          </section>
        {/each}
        {#each singles as n (n.name)}
          {@render row(n)}
        {/each}
      </div>
    {/if}
  </div>
</dialog>

{#snippet row(n: FleetNode)}
  {@const st = statusOf(n)}
  {@const isOpen = expanded[n.name] && n.reachable && !n.run}
  {@const canUpdate = Boolean(n.reachable && !n.run && n.release && installable(n))}
  {@const last = lastRunOf(n, now)}
  {@const me = n.run?.nodes.find((x) => x.name === n.name)}
  {@const prog = runProgress(me)}
  <article class={ROW} aria-label={n.name}>
    <div class={ROWHEAD}>
      <span class={DOT} style="background:{colorOf(n.name)}" aria-hidden="true"></span>
      <span class={NAME}>{n.name}</span>
      <span class={HOST}>{n.host}</span>
      <span class="{PILL} {PILL_TONE[st.tone]}">
        <span aria-hidden="true">{GLYPH[st.tone]}</span>
        {st.text}
      </span>
      <span class={SUB} title={lineOf(n)}>{lineOf(n)}</span>
      {#if n.run}
        <button class={BTN_DANGER} onclick={() => act(() => feed.stopRun(n.run!.id), 'Will stop after the current step.')}>
          stop after this step
        </button>
      {:else}
        <button class={BTN} disabled={!n.reachable} onclick={() => toggle(n.name)}>
          {isOpen ? 'hide updates ▴' : 'show updates ▾'}
        </button>
        <button
          class={n.release?.available ? BTN_PRIMARY : BTN}
          disabled={!canUpdate}
          onclick={() => arm(n.name)}
        >
          update
        </button>
        <PickMenu
          groups={[
            {
              items: [
                { key: 'check', label: 'Check now', checked: false },
                { key: 'rehearse', label: 'Rehearse an update', checked: false, note: 'nothing changes' },
                { key: 'log', label: 'Last update log', checked: false, disabled: !n.last_run },
              ],
            },
          ]}
          ontoggle={(key) => menu(n, key)}
          what="More actions"
          of={n.name}
          text="more"
          icon="list"
          mode="action"
          align="end"
          columns={1}
        />
      {/if}
    </div>

    {#if n.run && prog}
      <div class="flex flex-col gap-2 px-4 pb-4">
        <div class="track">
          <div class="fill" class:done={prog.done} style="width:{prog.pct}%"></div>
        </div>
        <div class="flex flex-wrap gap-[14px] text-ink-muted">
          <span class="text-ink-2">{prog.stepText}</span>
          {#if prog.detail}<span class={DIM}>{prog.detail}</span>{/if}
          <span class="flex-1"></span>
          <span>
            {prog.pct}% · step {prog.stepNo} of 4{prog.eta ? ` · ${prog.eta}` : ''}
          </span>
        </div>
        <div class={DIM}>
          Started {clockOf(n.run.started_at)} ·
          {Math.max(0, Math.round((now - Date.parse(n.run.started_at)) / 60000))} min so far ·
          keeps going if you close this page ·
          <button class="text-series-1 hover:underline" onclick={() => showLog(n.run!.id, n.name)}>
            {logFor?.runId === n.run.id && logFor.node === n.name ? 'hide the log' : 'watch the log'}
          </button>
        </div>
      </div>
    {/if}

    {#if last}
      <div class="{FOOT} text-ink-muted">
        <span class={TONE_TEXT[last.tone]}>
          {last.kind === 'ok' || last.kind === 'rehearsal' ? '✓' : last.kind === 'stopped' ? '■' : '▲'}
          {last.text}
        </span>
        <button class="text-series-1 hover:underline" onclick={() => showLog(last.runId, n.name)}>
          {logFor?.runId === last.runId && logFor.node === n.name ? 'hide log' : 'log'}
        </button>
        {#if last.kind === 'back'}
          <button
            class={BTN_PRIMARY}
            onclick={() => act(() => feed.verifyRun(last.runId), 'Verifying…')}
          >
            it's back — verify now
          </button>
        {/if}
      </div>
    {/if}

    {#if logFor && logFor.node === n.name && (logFor.runId === n.run?.id || logFor.runId === n.last_run?.id)}
      <div class="px-4 pb-4">
        <pre class="log" bind:this={logEl}>{logLines.join('\n') || '(nothing yet)'}</pre>
      </div>
    {/if}

    {#if isOpen && n.release}
      {@render expand(n)}
    {/if}

    {#if arming?.name === n.name}
      {@render confirm(n, arming.rehearse)}
    {/if}
  </article>
{/snippet}

{#snippet expand(n: FleetNode)}
  {@const r = n.release!}
  {@const u = n.updates ?? { total: null, security: null, packages: [] }}
  {@const fw = n.firmware_updates ?? []}
  {@const pkgs = u.packages ?? []}
  {@const fg = n.firmware_gap ?? { state: 'current', outstanding: [], not_reported: [] }}
  {@const pf = n.platform_firmware ?? { state: 'unknown' }}
  {@const sw = n.software ?? { state: 'unknown' }}
  {@const behindFw = fg.outstanding ?? []}
  {@const notReported = fg.not_reported ?? []}
  {@const showingAll = allPkgsFor === n.name}
  {@const rows = showingAll ? pkgRows(n) : pkgs.slice(0, 8)}
  <div class={EXPAND}>
    <!-- THREE LINES, KEPT APART: what apt can move; what the board's vendor
         has published; what NVIDIA's baseline still wants and who can supply
         it. A partner board that is as current as its vendor allows must not
         read the same as one that is genuinely behind, and neither must hide
         the other. -->
    <div>
      {#if r.available}
        <div class={H}>NVIDIA {r.latest_name} release</div>
        <p class={NOTE}>{r.description}</p>
        <p class="mt-[10px] mb-0">
          <a class="text-series-1" href={r.release_notes_url} target="_blank" rel="noopener">
            See release notes
          </a>
        </p>
      {:else}
        <div class={H}>NVIDIA release</div>
        <p class={NOTE}>On {r.on_name} — the latest.</p>
      {/if}
      <div class="mt-3">
        <div class={LI}>
          <span class="text-ink-muted">Software</span>
          <span class={VALUE}>
            {#if sw.state === 'installable'}
              {u.total ?? 0} package update{(u.total ?? 0) === 1 ? '' : 's'} to install
            {:else if sw.state === 'current'}
              <span class="text-good">current for {r.latest_name}</span>
            {:else}
              <span class="text-warning">held</span> —
              {(sw.behind ?? []).map((f) => `${f.name} ${f.installed} → ${f.target}`).join(', ')}
              with nothing apt can install
            {/if}
            {#if (sw.missing ?? []).length}
              <span class={DIM}>· not installed: {(sw.missing ?? []).map((f) => f.name).join(', ')}</span>
            {/if}
          </span>
        </div>
        <div class={LI}>
          <span class="text-ink-muted">Platform firmware</span>
          <span class={VALUE}>
            {#if pf.state === 'nvidia-release'}
              NVIDIA-built · firmware arrives with NVIDIA's release
            {:else if pf.newest}
              {pf.vendor} {pf.model} bundle <span class="text-ink-2">{pf.installed || 'unknown'}</span>{pf.bios_date ? ` (BIOS ${pf.bios_date})` : ''}
              · vendor's newest is {pf.newest.bundle}, published {pf.newest.published} ·
              {#if pf.state === 'current'}
                <span class="text-good">current per {pf.vendor}</span>
              {:else if pf.state === 'behind'}
                <span class="text-warning">newer bundle available from {pf.vendor}</span>
              {:else if pf.state === 'newer'}
                newer than the vendor table knows
              {:else}
                bundle not recognised
              {/if}
              <span class={DIM}>(table checked {pf.newest.checked})</span>
            {:else}
              {pf.vendor || 'unknown vendor'}{pf.bios_version ? ` · BIOS ${pf.bios_version}` : ''} · no vendor table entry for this board
            {/if}
          </span>
        </div>
        <div class={LI}>
          <span class="text-ink-muted">NVIDIA {r.latest_name} baseline</span>
          <span class={VALUE}>
            {#if fg.state === 'current'}
              <span class="text-good">met</span>
            {:else if fg.state === 'installable'}
              firmware updates offered — press Update
            {:else if fg.state === 'pending-vendor'}
              <span class="text-warning">pending {fg.vendor || 'vendor'} firmware</span>
              · outstanding: {behindFw.map((o) => o.label).join(', ')}
            {:else}
              <span class="text-warning">pending NVIDIA firmware</span>
              · outstanding: {behindFw.map((o) => o.label).join(', ')}
            {/if}
          </span>
        </div>
        {#if notReported.length}
          <div class="{DIM} mt-1">
            {notReported.map((o) => `${o.label}: not reported by this board (${shortFirmware(o.installed)})`).join(' · ')}
          </div>
        {/if}
      </div>
      <p class="{DIM} mt-[10px] mb-0">
        {#if r.available}
          {n.name} scores as the {r.on_name} release ({r.checks_passed} of {r.checks_total} checks pass for {r.latest_name}).
        {:else}
          {r.checks_passed} of {r.checks_total} checks pass.
        {/if}
      </p>
    </div>

    <div>
      <div class={H}>
        Firmware · {fw.length || behindFw.length} device{(fw.length || behindFw.length) === 1 ? '' : 's'}
      </div>
      {#if fw.length}
        {#each fw as f (f.device)}
          <div class={LI}>
            <span>{f.device}</span>
            <span class="text-ink-muted text-right">
              {f.now} → <span class="text-ink-2">{f.after}</span>
              {#if f.size}<span class="text-micro">{bytes(f.size)}</span>{/if}
            </span>
          </div>
        {/each}
      {:else if behindFw.length}
        {#each behindFw as b (b.name)}
          <div class={LI}>
            <span>{b.label}</span>
            <span class="text-ink-muted text-right">
              {shortFirmware(b.installed)} → <span class="text-ink-2">{b.target}</span>
            </span>
          </div>
        {/each}
      {:else}
        <div class="text-ink-muted">none</div>
      {/if}
      {#if n.cx7?.state === 'not-verifiable'}
        <p class="{DIM} mt-[10px] mb-0">Network card firmware can't be checked until a cable is plugged in.</p>
      {:else if n.cx7?.state === 'behind'}
        <p class="text-micro text-warning mt-[10px] mb-0">Network card firmware {n.cx7.version} is behind the release.</p>
      {/if}
    </div>

    <div>
      <div class={H}>
        Packages · {u.total ?? pkgs.length}
        <span class="normal-case tracking-normal">({u.security ?? 0} security)</span>
      </div>
      {#if showingAll}
        <input
          class="{INPUT} h-[26px] w-[180px] mb-2"
          placeholder="filter…"
          autocomplete="off"
          bind:value={pkgFilter}
          oninput={() => (pkgView.page = 0)}
        />
        {#each pkgView.slice(rows) as p (p.name)}
          <div class={LI}>
            <span class="min-w-0 truncate">{p.name}</span>
            <span class="text-right whitespace-nowrap">
              <span class="text-ink-muted">{p.from || '—'}</span> → <span class="text-ink-2">{p.to}</span>
              <span class={p.new ? 'text-good' : p.security ? 'text-warning' : 'text-ink-muted'}>
                · {p.new ? 'new' : p.security ? 'security' : p.source}
              </span>
            </span>
          </div>
        {:else}
          <div class="text-ink-muted">nothing matches</div>
        {/each}
        <Pager view={pkgView} total={rows.length} label="Package pages" />
        <button class="{LINK} mt-2" onclick={() => (allPkgsFor = null)}>show fewer</button>
      {:else}
        {#each rows as p (p.name)}
          <div class={LI}>
            <span class="min-w-0 truncate">{p.name}</span>
            <span class={p.new ? 'text-good' : 'text-ink-muted'}>
              {p.new ? 'new' : p.security ? 'security' : p.source}
            </span>
          </div>
        {:else}
          <div class="text-ink-muted">none</div>
        {/each}
        {#if pkgs.length > 8}
          <div class={LI}>
            <span class="text-ink-muted">and {pkgs.length - 8} more</span>
            <button
              class="text-series-1 hover:underline"
              onclick={() => {
                allPkgsFor = n.name;
                pkgFilter = '';
                pkgView.page = 0;
              }}
            >
              see all {pkgs.length} →
            </button>
          </div>
        {/if}
      {/if}
      <p class="{DIM} mt-2 mb-0">
        {u.counts ? `${u.counts.upgraded} upgraded, ${u.counts.new} new, ${u.counts.removed} removed` : ''}
        {u.as_of ? ` · counted ${agoOf(u.as_of, now)}` : ''}
      </p>
    </div>
  </div>
  {#if arming?.name !== n.name}
    <div class={FOOT}>
      <button class={BTN_PRIMARY} disabled={!installable(n)} onclick={() => arm(n.name)}>
        update {n.name}{n.pair?.length ? ` and ${n.pair.join(', ')}` : ''}
      </button>
      <span class="text-ink-muted">
        Takes {fw.length ? 'about 35' : 'about 15'} minutes and restarts {n.name} when it's done.{n.reboot?.required ? ' A restart is already pending.' : ''}
      </span>
    </div>
  {/if}
{/snippet}

{#snippet confirm(n: FleetNode, rehearse: boolean)}
  {@const all = targets(n, rehearse)}
  {@const others = all.slice(1)}
  {@const fw = n.firmware_updates?.length ?? 0}
  {@const u = n.updates?.total ?? 0}
  {@const ask = asksPassword(n, rehearse)}
  <div class={CONFIRM} role="group" aria-label={rehearse ? `Rehearse on ${n.name}` : `Update ${all.join(' and ')}`}>
    <div class="text-title-sm font-bold">
      {rehearse ? `Rehearse an update on ${n.name}?` : `Update ${all.join(' and ')}?`}
    </div>
    {#if rehearse}
      <p class={NOTE}>Every step runs except the install itself and the restart. Nothing changes on {n.name}.</p>
    {:else}
      <p class={NOTE}>
        {#if n.release?.available}
          This installs NVIDIA's <b>{n.release.latest_name}</b> release{others.length ? ' on both' : ''}:
          {u} package updates{fw ? ` and firmware for ${fw} device${fw > 1 ? 's' : ''}` : ''}, then restarts.
        {:else}
          This installs {u} package updates{fw ? ` and firmware for ${fw} device${fw > 1 ? 's' : ''}` : ''}, then restarts.
        {/if}
      </p>
      {#if others.length}
        <p class="{NOTE} text-ink-muted">
          {n.name} and {others.join(', ')} are linked, so {all.length === 2 ? 'both are' : 'all of them are'} updated one after the other.
        </p>
      {/if}
      {#if n.dashboard_auto_update}
        <p class="{NOTE} text-ink-muted">
          The DGX Dashboard's own updater is paused on {all.join(' and ')} while this installs, so two
          installers never run at once, and resumed the moment the install ends.
        </p>
      {/if}
      {#each all as m (m)}
        {@const pin = pinnedOf(byName.get(m) ?? n)}
        {#if pin}
          <p class="{NOTE} text-warning">
            {pin.count} package{pin.count === 1 ? ' is' : 's are'} pinned on {m} and will not be installed{pin.kept >
            pin.count
              ? `, holding back ${pin.kept} in total`
              : ''}. Unpin with <code>apt-mark unhold</code> on the Spark if that is not what you want.
          </p>
        {/if}
      {/each}
      <p class="{NOTE} text-ink-muted">
        Takes {fw ? 'about 35' : 'about 15'} minutes per Spark. If anything fails it stops and tells you.
      </p>
    {/if}
    {#if ask}
      <div class={FIELD}>
        <label class={EYEBROW} for="fleet-password">
          password for {fleet?.ssh_user || 'your user'} on the Spark
        </label>
        <input
          id="fleet-password"
          class={INPUT}
          type="password"
          autocomplete="off"
          placeholder="needed to run the update as root"
          bind:this={passwordInput}
          bind:value={password}
          onkeydown={(e) => {
            if (e.key === 'Enter') go();
          }}
        />
        <span class={DIM}>Used once, for this update only. Never stored. Sent over this connection.</span>
      </div>
      {#if insecure && !error}
        <p class="text-micro text-warning m-0">
          This page is not on HTTPS, so the fleet service will refuse the password. Open the dashboard
          at https:// or through the tunnel.
        </p>
      {/if}
    {/if}
    {#if error}
      <p class="text-micro text-critical m-0" role="alert">{error}</p>
    {/if}
    <div class="flex gap-[10px] justify-end mt-1">
      <button class={BTN} onclick={disarm}>cancel</button>
      <button class={BTN_PRIMARY} disabled={busy} onclick={go}>
        {busy ? 'starting…' : rehearse ? `rehearse on ${n.name}` : `update ${all.join(' and ')}`}
      </button>
    </div>
  </div>
{/snippet}

<style>
  /* Same shell as AlertHistory and Settings, wider. Three copies now; the
     extraction their comments defer to is due, and is not this change. */
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
    /* The body sets no size; every component names its own. This one is
       almost all body text, so it is named once here. */
    font-size: var(--text-body);
  }

  .flyout::backdrop {
    background: rgb(0 0 0 / 0.45);
  }

  .panel {
    display: flex;
    flex-direction: column;
    gap: 18px;
    height: 100%;
    overflow-y: auto;
    padding: 18px 20px 28px;
  }

  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    position: sticky;
    top: 0;
    z-index: 1;
    background: var(--panel);
    padding-bottom: 8px;
  }

  /* One bar for the whole update. Structural: a fill inside a track, with the
     width set from script. */
  .track {
    height: 8px;
    background: var(--track);
    border-radius: 2px;
    overflow: hidden;
  }
  .fill {
    height: 100%;
    background: var(--series-1);
    transition: width 0.6s;
  }
  .fill.done { background: var(--good); }

  .log {
    margin: 0;
    padding: 10px 12px;
    max-height: 260px;
    overflow: auto;
    background: var(--page);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    color: var(--ink-muted);
    font: inherit;
    font-size: var(--text-label);
    line-height: 1.55;
    white-space: pre-wrap;
  }

  @media (max-width: 640px) {
    .flyout { width: 100%; border-left: none; }
  }

  @media (prefers-reduced-motion: no-preference) {
    .flyout[open] { animation: slide-in 160ms ease-out; }
  }

  @keyframes slide-in {
    from { transform: translateX(12px); opacity: 0; }
    to { transform: translateX(0); opacity: 1; }
  }
</style>
