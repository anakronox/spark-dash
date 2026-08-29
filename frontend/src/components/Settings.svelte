<script lang="ts">
  /* Settings, as a right-anchored fly-out.
   *
   * WHY THIS EXISTS. Preferences were scattered across four independent
   * localStorage keys, each owned by whichever component happened to need it
   * and each with its own control: theme in a header <select>, section order on
   * drag handles, metric selection and range inside the System Activity panel. Nothing
   * told you they were settings, that they persisted, or where to find them.
   *
   * WHAT BELONGS HERE, AND WHAT DOES NOT. This holds preferences that are
   * GLOBAL and have no natural home on the page. Controls that sit next to the
   * thing they affect stay there — the metric chips belong beside the chart
   * they redraw, and moving them here would trade discoverability for a round
   * trip. A settings panel is where homeless options live, not a place to
   * collect every control.
   *
   * READ-ONLY IS ABOUT NODE DATA, NOT ABOUT THIS. The dashboard observes the
   * cluster and does not drive it. Everything in this panel is client-side and
   * never leaves the browser, so it touches none of that: no request reaches a
   * node, and nothing here can change what the agent polls. Writing cluster
   * membership from the UI is a genuinely different question — see roadmap L3.
   *
   * Same shell as AlertHistory deliberately: <dialog> + showModal() gives focus
   * trapping, Escape, the backdrop and focus restore from the platform, and
   * reusing it means the two panels behave identically rather than each being
   * subtly hand-rolled.
   */
  import { tick } from 'svelte';
  import { fetchWithTimeout } from '../lib/request';
  import { THEMES } from '../lib/theme.svelte';
  import type { Theme } from '../lib/theme.svelte';
  import type { Layout } from '../lib/layout.svelte';
  import { ZONES } from '../lib/layout.svelte';
  import { ENGINE_RUNTIMES } from '../lib/types';


  interface Props {
    theme: Theme;
    layout: Layout;
    open: boolean;
    onclose: () => void;
  }
  const { theme, layout, open, onclose }: Props = $props();

  /* IN PAGE ORDER, and no longer labelled by zone.
   *
   * The headers existed as feedback: this panel used to carry the width
   * toggle, and with the fly-out covering the page a row moving from "Left
   * column" to "Full width" was the only sign the click had done anything.
   * Width is dragged on the card now, in full view, so the headers were
   * captioning a change nobody makes from here.
   *
   * The ORDER still follows the zones, because that is the order the sections
   * read in on the page and this list is for finding one.
   *
   * `Hidden` keeps its heading: those cards are not on the page at all, so
   * their position in a page-ordered list would otherwise mean nothing. */
  const shownSections = $derived(ZONES.flatMap((z) => layout.inZone(z)));
  const hiddenSections = $derived(layout.order.filter((id) => layout.isHidden(id)));

  let dialog = $state<HTMLDialogElement | null>(null);

  /* CLUSTER CONFIG — editable.
   *
   * "Read-only" is a property of AGENT DATA: the dashboard observes nodes and
   * never drives them. This file is the dashboard's OWN configuration, and
   * editing it is the same kind of act as silencing an alert — it changes what
   * this service watches, not what any node does.
   *
   * The narrowing that keeps that cheap: runtimes are edited as PORTS, which
   * the backend resolves against the node's own host, so a write cannot name
   * an arbitrary URL. Hosts are validated against private ranges server-side.
   * Neither is the primary control — OAuth at the tunnel edge is — but the
   * agent polls whatever lands in `llama_routers`, so the value space is worth
   * keeping narrow rather than trusting the edge alone.
   *
   * A runtime that genuinely lives off-node comes back with `port: null`. Those
   * are shown but not editable here, because editing one would mean accepting a
   * free-text URL, which is the thing being avoided.
   */
  interface RuntimeRef {
    url: string;
    port: number | null;
    scrape_metrics?: boolean;
  }

  interface ConfiguredNode {
    node_id: string;
    host: string;
    cluster: string | null;
    agent_port: number;
    node_exporter_port: number;
    in_inventory: boolean;
    /** Where the AGENT says its runtimes came from, and when central last
     *  answered it. Null when the poller has no snapshot for this node yet. */
    config_source: 'central' | 'env' | 'unreachable' | null;
    config_fetched_at: string | null;
    /** Interfaces this node is currently reporting, so exclusions are ticked
     *  from a list rather than typed. Empty when the node is not being polled. */
    observed_interfaces: string[];
    /** Interfaces excluded from alerting, by name. */
    ignored_interfaces: string[];
    runtimes: { llama_routers: RuntimeRef[] } & Record<EngineRuntime, RuntimeRef[]>;
  }

  type EngineRuntime = (typeof ENGINE_RUNTIMES)[number];

  /** How the editor presents each engine, and the port it offers for a new
   *  entry. The DEPLOYMENT's conventional port, not the engine's upstream
   *  default — these are the ports the node stacks actually publish, and a
   *  prefilled value that has to be corrected every time is worse than none.
   *  Ordered as ENGINE_RUNTIMES is, so the panel and the file agree. */
  const ENGINE_UI: Record<EngineRuntime, { label: string; port: number }> = {
    vllm: { label: 'vLLM', port: 8120 },
    sglang: { label: 'SGLang', port: 30000 },
  };

  /** Tolerates a config from a backend that predates an engine: an absent key
   *  becomes an empty list rather than an exception in the editor. */
  function engineRefs(n: ConfiguredNode, runtime: EngineRuntime): RuntimeRef[] {
    return n.runtimes[runtime] ?? (n.runtimes[runtime] = []);
  }

  /** Every interface the editor should offer for this node: the ones it is
   *  currently reporting, plus any name already excluded that it is NOT
   *  reporting.
   *
   *  That second half is the important one. A NIC is absent because the node is
   *  down, or because it was renamed — and an editor that only knew what it
   *  could currently see would drop the exclusion on the next save, silently
   *  re-arming an alert someone deliberately turned off. Those rows are shown
   *  as `absent` and still written back.
   */
  function interfaceRows(n: ConfiguredNode): { name: string; observed: boolean }[] {
    const observed = n.observed_interfaces ?? [];
    const ignored = n.ignored_interfaces ?? [];
    const extra = ignored.filter((name) => !observed.includes(name));
    return [
      ...observed.map((name) => ({ name, observed: true })),
      ...extra.map((name) => ({ name, observed: false })),
    ];
  }

  /** Ticked means WATCHED, so the box reads the way the dashboard behaves:
   *  everything is watched unless you turn it off. Storing the inverse would
   *  put a checkbox labelled "ignore" next to a green dot meaning "up", and
   *  the two would disagree about what checked means. */
  function isWatched(n: ConfiguredNode, name: string): boolean {
    return !(n.ignored_interfaces ?? []).includes(name);
  }

  function setWatched(n: ConfiguredNode, name: string, watched: boolean): void {
    const current = n.ignored_interfaces ?? [];
    n.ignored_interfaces = watched
      ? current.filter((i) => i !== name)
      : [...current, name];
  }

  type Cfg = { source: string; path: string; nodes: ConfiguredNode[] };

  /** Bytes, or null when Prometheus could not answer. Never 0 on failure —
   *  zero would be a claim that monitoring is free, which is the one answer
   *  that is never true. */
  let footprint = $state<number | null>(null);

  let cfg = $state<Cfg | null>(null);
  let draft = $state<ConfiguredNode[] | null>(null);
  let cfgError = $state<string | null>(null);
  let saving = $state(false);
  let saveError = $state<string | null>(null);

  const dirty = $derived(
    !!draft && !!cfg && JSON.stringify(draft) !== JSON.stringify(cfg.nodes),
  );

  const clone = (n: ConfiguredNode[]) => JSON.parse(JSON.stringify(n)) as ConfiguredNode[];

  /** "did my edit reach spark3?" — answered here rather than over SSH.
   *
   * The SOURCE carries more than the age. A node on `env` is not managed
   * centrally at all; one on `unreachable` is asking and getting silence, so
   * it is on env by accident rather than by design. Both are different from a
   * node whose last successful fetch is simply old. */
  function configState(n: ConfiguredNode): { label: string; tone: 'ok' | 'warn' } | null {
    const live = cfg?.nodes.find((c) => c.node_id === n.node_id);
    if (!live || live.config_source === null) return null;
    if (live.config_source === 'unreachable')
      return { label: 'not reaching backend', tone: 'warn' };
    if (live.config_source === 'env') return { label: 'using env, not this file', tone: 'warn' };
    if (!live.config_fetched_at) return { label: 'central', tone: 'ok' };
    const age = Math.max(0, (Date.now() - Date.parse(live.config_fetched_at)) / 1000);
    const ago =
      age < 90 ? `${Math.round(age)}s` : age < 5400 ? `${Math.round(age / 60)}m` : `${Math.round(age / 3600)}h`;
    return { label: `fetched ${ago} ago`, tone: 'ok' };
  }

  /** F9: the block to paste into cluster.yml for a server that is running but
   *  not configured.
   *
   * Generated rather than written, and copied rather than saved — most of the
   * convenience of editing with none of the write path, which is a property
   * this dashboard was built to keep. */
  function yamlFor(n: ConfiguredNode): string {
    const lines = [`- id: ${n.node_id || 'NODE-ID'}`, `  host: ${n.host || 'HOST'}`];
    if (n.cluster) lines.push(`  cluster: ${n.cluster}`);
    if (n.agent_port !== 9500) lines.push(`  agent_port: ${n.agent_port}`);
    if (n.node_exporter_port !== 9100)
      lines.push(`  node_exporter_port: ${n.node_exporter_port}`);

    /* NESTED UNDER `runtimes:`, and vLLM ports are BARE NUMBERS.
     *
     * The first version of this emitted `llama_routers:` and `vllm:` at node
     * level with `- port: N` for both, which parses as valid YAML and loads as
     * a node with no runtimes at all. Pasting it would have appeared to work
     * and then silently collected nothing — the exact failure this whole
     * section exists to prevent, generated by the tool meant to prevent it.
     * Caught by round-tripping the output through the real loader, which is
     * now a test. */
    const routers = n.runtimes.llama_routers.filter((r) => r.port != null);
    const engineEntries = ENGINE_RUNTIMES.map(
      (runtime) => [runtime, engineRefs(n, runtime).filter((v) => v.port != null)] as const,
    ).filter(([, refs]) => refs.length);

    const ignored = n.ignored_interfaces ?? [];
    if (routers.length || engineEntries.length) {
      lines.push('  runtimes:');
      if (routers.length) {
        lines.push('    llama_routers:');
        for (const r of routers) {
          lines.push(`      - port: ${r.port}`);
          if (r.scrape_metrics) lines.push('        scrape_metrics: true');
        }
      }
      for (const [runtime, refs] of engineEntries) {
        lines.push(`    ${runtime}:`);
        for (const v of refs) lines.push(`      - ${v.port}`);
      }
    }
    /* Outside the `runtimes:` block, and that is load-bearing rather than
       stylistic: the agent reads every list under `runtimes:` as an engine's
       endpoints so it can pick up an engine a newer backend knows about, so a
       nested ignore list would parse as an engine named "interfaces" —
       scraped by nothing and silently wrong. */
    if (ignored.length) {
      lines.push('  interfaces:');
      lines.push('    ignore:');
      for (const name of ignored) lines.push(`      - ${name}`);
    }
    return lines.join('\n');
  }

  /* SHOWING THE YAML IS THE FEATURE; the clipboard is a shortcut on top.
   *
   * `navigator.clipboard` needs a SECURE CONTEXT, and this dashboard is served
   * over plain http on a LAN address — so on the real deployment the API is not
   * merely permission-gated, it is undefined. A button whose only behaviour was
   * to copy would therefore do nothing at all where it matters, while working
   * perfectly on localhost during development. Verified: isSecureContext is
   * true on 127.0.0.1 and false on http://192.168.50.156:8080.
   *
   * So the block is revealed and selected, which always works, and the copy is
   * attempted quietly alongside it. */
  let shown = $state<string | null>(null);
  let copied = $state<string | null>(null);
  let yamlBox = $state<HTMLTextAreaElement | null>(null);

  async function copyYaml(n: ConfiguredNode) {
    const already = shown === n.node_id;
    shown = already ? null : n.node_id;
    copied = null;
    if (already) return;

    await tick();
    yamlBox?.select();

    try {
      await navigator.clipboard?.writeText(yamlFor(n));
      copied = n.node_id;
      setTimeout(() => (copied = null), 1600);
    } catch {
      // Left visible and selected — the reader copies it themselves.
    }
  }

  async function loadFootprint() {
    try {
      const resp = await fetchWithTimeout('/api/monitoring-footprint');
      footprint = resp.ok ? ((await resp.json()).bytes ?? null) : null;
    } catch {
      footprint = null;
    }
  }

  async function loadConfig() {
    try {
      const resp = await fetchWithTimeout('/api/cluster/config');
      if (!resp.ok) throw new Error((await resp.json())?.detail ?? String(resp.status));
      cfg = await resp.json();
      draft = clone(cfg!.nodes);
      cfgError = null;
    } catch (err) {
      // Named plainly. A malformed cluster.yml is the likely cause and the
      // backend reports it verbatim, which is more useful than "unavailable".
      cfgError = (err as Error).message;
      cfg = null;
      draft = null;
    }
  }

  async function save() {
    if (!draft) return;
    saving = true;
    saveError = null;
    try {
      const resp = await fetchWithTimeout('/api/cluster/config', {
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          nodes: draft.map((n) => ({
            node_id: n.node_id,
            host: n.host,
            cluster: n.cluster || null,
            agent_port: n.agent_port,
            node_exporter_port: n.node_exporter_port,
            // Off-node runtimes have no port and cannot be expressed here;
            // they are preserved by the backend only if it can resolve them,
            // so they are filtered rather than sent as nulls.
            llama_routers: n.runtimes.llama_routers
              .filter((r) => r.port != null)
              .map((r) => ({ port: r.port, scrape_metrics: !!r.scrape_metrics })),
            ignored_interfaces: n.ignored_interfaces ?? [],
            ...Object.fromEntries(
              ENGINE_RUNTIMES.map((runtime) => [
                runtime,
                engineRefs(n, runtime)
                  .filter((v) => v.port != null)
                  .map((v) => v.port),
              ]),
            ),
          })),
        }),
      });
      const body = await resp.json();
      if (!resp.ok) throw new Error(body?.detail ?? String(resp.status));
      // Adopt what the server actually wrote, not what we sent — the two differ
      // if anything was normalised, and showing our own draft back would hide
      // that.
      cfg = body;
      draft = clone(body.nodes);
    } catch (err) {
      saveError = (err as Error).message;
    } finally {
      saving = false;
    }
  }

  function addNode() {
    draft = [
      ...(draft ?? []),
      {
        node_id: '',
        host: '',
        cluster: null,
        agent_port: 9500,
        node_exporter_port: 9100,
        in_inventory: false,
        config_source: null,
        config_fetched_at: null,
        observed_interfaces: [],
        ignored_interfaces: [],
        runtimes: {
          llama_routers: [],
          ...(Object.fromEntries(
            ENGINE_RUNTIMES.map((r) => [r, [] as RuntimeRef[]]),
          ) as unknown as Record<EngineRuntime, RuntimeRef[]>),
        },
      },
    ];
  }

  $effect(() => {
    if (!dialog) return;
    if (open && !dialog.open) {
      dialog.showModal();
      // Fetched on open only. It changes when someone edits a file on the VM,
      // not on a timer, so polling it would be pure waste.
      loadConfig();
      loadFootprint();
    } else if (!open && dialog.open) dialog.close();
  });
</script>

<dialog
  bind:this={dialog}
  class="flyout"
  aria-label="Settings"
  onclose={onclose}
  onclick={(e) => {
    // The dialog element itself is the backdrop's hit target, so a click
    // landing on it rather than on the panel is outside.
    if (e.target === dialog) onclose();
  }}
>
  <div class="panel">
    <header>
      <h2 class="eyebrow">Settings</h2>
      <button class="close" aria-label="Close" onclick={onclose}>×</button>
    </header>

    <!-- Appearance -->
    <section class="stack">
      <h3 class="eyebrow dim">Appearance</h3>
      <div class="choices" role="group" aria-label="Theme">
        {#each THEMES as t (t.id)}
          <button
            class="choice"
            class:active={theme.current === t.id}
            aria-pressed={theme.current === t.id}
            onclick={() => theme.set(t.id)}
          >{t.label}</button>
        {/each}
      </div>
      <!-- Each theme is stepped on its own surface rather than derived by
           inverting another, so this is a real choice between three palettes,
           not a light/dark switch with a skin. -->
      <!-- No note. The three buttons say what they do, and "every theme is
           validated separately for contrast and colourblind separation against
           its own background" is a fact about the palette rather than about
           this control — it belongs in app.css beside the tokens it constrains,
           which is where it is. -->
    </section>

    <!-- Layout -->
    <section class="stack">
      <h3 class="eyebrow dim">Node cards</h3>
      <!-- Deliberate, never automatic. Switching on node count would mean the
           page rearranges itself the moment a node joins — which is exactly
           when someone is watching it. -->
      <div class="choices" role="group" aria-label="Node card density">
        <button
          class="choice"
          class:active={!layout.compactCards}
          aria-pressed={!layout.compactCards}
          onclick={() => layout.setCompactCards(false)}
        >Full</button>
        <button
          class="choice"
          class:active={layout.compactCards}
          aria-pressed={layout.compactCards}
          onclick={() => layout.setCompactCards(true)}
        >Compact</button>
      </div>
      <!-- Why those three readings and not others: models, other GPU work and
           the system share ONE pool on this hardware, so the memory band is the
           reading that cannot be inferred from anywhere else on the page. And a
           node that is DOWN is never compacted — the moment a card matters most
           is the wrong moment to shrink it. Both are properties of the compact
           card, implemented in NodeCard, not instructions for this switch. -->
      <p class="note dim">Compact keeps name, status and the memory band.</p>
    </section>

    <section class="stack">
      <h3 class="eyebrow dim">Sections</h3>
      <!-- HIDE, not collapse. Collapsing already has a control on the section
           itself, and duplicating it here would be two ways to do one thing.
           Hiding is the one that has to live here: a hidden section renders
           nothing, so this panel is the only place it can be found again.

           The three controls per row, and the reasoning that used to be printed
           above them — a settings panel is not the place to teach the layout
           model, and this all belongs in the docs or in the code that
           implements it:

           - PLACEMENT (full / left / right). Full-width sections form a band
             above two columns that fill INDEPENDENTLY, so a short section can
             sit under another short one whatever the other column is doing.
             Below 1100px the columns stack and everything is full width
             regardless, because a half-width table is unreadable there.
           - ROWS, the cap before a section pages. It puts a ceiling on how tall
             a section can grow as nodes are added, so one long table cannot set
             the height of a whole column.
           - SHOWN / HIDDEN, per the note above.

           Not here: order and collapse live on the sections themselves, and
           which COLUMNS a table shows lives in each card's top-right corner,
           next to the data it affects. Reset below clears all of it. -->
      {#snippet sectionRow(id: string, hidden: boolean)}
        <li class="row" class:off={hidden}>
          <span class="name">{layout.label(id)}</span>
          <button
            class="mini"
            aria-pressed={!hidden}
            aria-label={`${hidden ? 'Show' : 'Hide'} ${layout.label(id)}`}
            onclick={() => layout.toggleHidden(id)}
          >{hidden ? 'hidden' : 'shown'}</button>
        </li>
      {/snippet}

      {#if shownSections.length}
        <ol class="sections">
          {#each shownSections as id (id)}
            {@render sectionRow(id, false)}
          {/each}
        </ol>
      {/if}

      {#if hiddenSections.length}
        <p class="eyebrow dim group">Hidden</p>
        <ol class="sections">
          {#each hiddenSections as id (id)}
            {@render sectionRow(id, true)}
          {/each}
        </ol>
      {/if}
      <!-- Offered only when there is something to undo: a reset that does
           nothing still invites the click that loses your arrangement. -->
      <button
        class="mini reset"
        disabled={layout.isDefault}
        onclick={() => layout.reset()}
      >Reset sections</button>
    </section>

    <!-- Cluster -->
    <section class="stack">
      <h3 class="eyebrow dim">Cluster</h3>
      {#if cfgError}
        <p class="note" data-tone="warning">Couldn't read the cluster config: {cfgError}</p>
      {:else if !draft}
        <p class="note dim">Loading…</p>
      {:else}
        <!-- Adding a node here really is all that is needed: the node's own
             stack carries nothing cluster-specific and asks for this on a
             timer. True and worth knowing once, which is what the README is
             for; the path is the part you need while looking at this panel. -->
        <p class="note dim">Stored in <code>{cfg?.path}</code> on the monitoring VM.</p>

        {#each draft as n, i (i)}
          <div class="node">
            <div class="node-head">
              <input
                class="in id"
                placeholder="node-id"
                aria-label="Node id"
                bind:value={n.node_id}
              />
              {#if !n.in_inventory && cfg?.nodes.some((c) => c.node_id === n.node_id)}
                <!-- Configured but not polled. Silent otherwise, and exactly
                     what a typo in the host produces. -->
                <span class="tag warn">not polled</span>
              {/if}
              <!-- F6: whether this node has actually FETCHED the config below
                   it. Without this, a central edit was a two-step guess —
                   change the file, then infer from whether metrics moved. -->
              {#if configState(n)}
                {@const state = configState(n)}
                <span class="tag" class:warn={state?.tone === 'warn'}>{state?.label}</span>
              {/if}
              <button
                class="mini"
                title="Copy this node's cluster.yml block"
                onclick={() => copyYaml(n)}
              >{copied === n.node_id ? 'copied' : shown === n.node_id ? 'hide yaml' : 'copy yaml'}</button>
              <button
                class="mini"
                aria-label={`Remove ${n.node_id || 'node'}`}
                onclick={() => (draft = draft!.filter((_, j) => j !== i))}
              >remove</button>
            </div>

            {#if shown === n.node_id}
              <!-- Selectable and pre-selected, so this works with no clipboard
                   API at all — which is the case on the real deployment. -->
              <textarea
                class="yaml"
                readonly
                rows={yamlFor(n).split('\n').length}
                bind:this={yamlBox}
                aria-label={`cluster.yml block for ${n.node_id}`}
                value={yamlFor(n)}
              ></textarea>
            {/if}

            <div class="fields">
              <label>host
                <input class="in" placeholder="192.168.1.10" bind:value={n.host} />
              </label>
              <label>cluster
                <!-- A NAME, never a count: "pair" stops being true at three
                     nodes. Blank means standalone. -->
                <input class="in" placeholder="(standalone)" bind:value={n.cluster} />
              </label>
              <label>agent port
                <input class="in num" type="number" min="1" max="65535" bind:value={n.agent_port} />
              </label>
            </div>

            <div class="rt">
              <span class="eyebrow dim">llama.cpp routers</span>
              {#each n.runtimes.llama_routers as r, ri (ri)}
                <div class="rt-row">
                  {#if r.port == null}
                    <span class="dim">{r.url}</span>
                    <span class="tag">off-node</span>
                  {:else}
                    <input class="in num" type="number" min="1" max="65535" aria-label="Router port" bind:value={r.port} />
                    <label class="check">
                      <input type="checkbox" bind:checked={r.scrape_metrics} />
                      <!-- Opt-in per router: this is what permits
                           /metrics?model=, which yields tokens/sec but wakes a
                           sleeping model on an autoload router. -->
                      metrics
                    </label>
                  {/if}
                  <button
                    class="mini"
                    aria-label="Remove router"
                    onclick={() => (n.runtimes.llama_routers = n.runtimes.llama_routers.filter((_, j) => j !== ri))}
                  >×</button>
                </div>
              {/each}
              <button
                class="mini add"
                onclick={() => (n.runtimes.llama_routers = [...n.runtimes.llama_routers, { url: '', port: 8001, scrape_metrics: false }])}
              >+ router</button>
            </div>

            <div class="rt ifaces">
              <span class="eyebrow dim">Interfaces watched by alerting</span>
              {#if interfaceRows(n).length}
                {#each interfaceRows(n) as iface (iface.name)}
                  <label class="iface">
                    <input
                      type="checkbox"
                      checked={isWatched(n, iface.name)}
                      onchange={(e) =>
                        setWatched(n, iface.name, (e.currentTarget as HTMLInputElement).checked)}
                    />
                    <span>{iface.name}</span>
                    {#if !iface.observed}
                      <!-- Configured but not currently reported. Kept, not
                           dropped: the node may be down or the NIC renamed,
                           and discarding it would re-arm an alert someone
                           turned off on purpose. -->
                      <span class="tag">absent</span>
                    {/if}
                  </label>
                {/each}
                <p class="hint dim">
                  Unticked stops <span>NetworkLinkDown</span> and its
                  RoCE port alerting. The interface keeps being collected and
                  charted.
                </p>
              {:else}
                <p class="hint dim">
                  Nothing to list until this node reports its interfaces.
                </p>
              {/if}
            </div>

            {#each ENGINE_RUNTIMES as runtime (runtime)}
              <div class="rt">
                <span class="eyebrow dim">{ENGINE_UI[runtime].label}</span>
                {#each engineRefs(n, runtime) as v, vi (vi)}
                  <div class="rt-row">
                    {#if v.port == null}
                      <span class="dim">{v.url}</span>
                      <span class="tag">off-node</span>
                    {:else}
                      <input class="in num" type="number" min="1" max="65535" aria-label="{ENGINE_UI[runtime].label} port" bind:value={v.port} />
                    {/if}
                    <button
                      class="mini"
                      aria-label="Remove {ENGINE_UI[runtime].label} endpoint"
                      onclick={() => (n.runtimes[runtime] = engineRefs(n, runtime).filter((_, j) => j !== vi))}
                    >×</button>
                  </div>
                {/each}
                <button
                  class="mini add"
                  onclick={() => (n.runtimes[runtime] = [...engineRefs(n, runtime), { url: '', port: ENGINE_UI[runtime].port }])}
                >+ {ENGINE_UI[runtime].label}</button>
              </div>
            {/each}
          </div>
        {/each}

        <div class="actions">
          <button class="mini add" onclick={addNode}>+ node</button>
          <button class="mini" disabled={!dirty || saving} onclick={() => (draft = clone(cfg!.nodes))}>Revert</button>
          <button class="mini save" disabled={!dirty || saving} onclick={save}>
            {saving ? 'Saving…' : 'Save cluster'}
          </button>
        </div>
        {#if saveError}
          <!-- Verbatim from the backend: it is the validator talking, and
               "node 'a': hosts must be private" is far more actionable than a
               generic failure. -->
          <p class="note" data-tone="warning">{saveError}</p>
        {/if}
        <!-- Kept, unlike the other notes: this is not background, it is what
             the button next to it will DO to a file the reader may have
             hand-edited. The pointer to central/cluster.yml.example went — that
             one is documentation. -->
        <p class="note dim">Saving rewrites the file; comments are not preserved.</p>
      {/if}
    </section>

    <!-- Where this lives -->
    <section class="stack">
      <h3 class="eyebrow dim">Monitoring footprint</h3>
      <!-- J4: the number that keeps the single-host argument honest.
           Monitoring a GB10 should cost it as little as possible, and on one
           box that cost comes out of the SAME unified pool the models use — so
           the claim is shown as a live figure rather than left in a README to
           age.

           Summed from components that measure THEMSELVES; nothing here guesses
           which processes are "monitoring". -->
      <p class="note">
        {#if footprint === null}
          <span class="dim">unavailable — Prometheus did not answer</span>
        {:else}
          <span class="num">{(footprint / 1024 ** 2).toFixed(0)} MiB</span>
          <span class="dim">
            resident, for Prometheus, Alertmanager and the monitoring host's
            exporter. Each agent's own cost is reported per node.
          </span>
        {/if}
      </p>

      <h3 class="eyebrow dim">Storage</h3>
      <!-- Said plainly because the alternative is discovering it: these do not
           follow you to another browser or machine, and there is no account to
           sync them to. The dashboard is deliberately stateless server-side. -->
      <p class="note dim">This browser only; clearing site data resets them.</p>
    </section>
  </div>
</dialog>

<style>
  /* Mirrors AlertHistory so the two panels are indistinguishable in behaviour
     and weight. Kept as its own copy rather than extracted: two users is not
     yet a pattern, and premature extraction would couple two panels that may
     diverge. Revisit at three. */
  .flyout {
    margin: 0 0 0 auto;
    height: 100%;
    max-height: 100%;
    width: min(420px, 100%);
    max-width: 100%;
    padding: 0;
    border: none;
    border-left: 1px solid var(--rule);
    background: var(--panel);
    color: var(--ink);
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
    position: sticky;
    top: 0;
    background: var(--panel);
    padding-bottom: 8px;
  }

  .close {
    font-size: var(--text-glyph);
    line-height: 1;
    padding: 2px 8px;
    border-radius: var(--radius);
    color: var(--ink-muted);
  }
  .close:hover { color: var(--ink); }

  .stack { display: flex; flex-direction: column; gap: 8px; }

  .note { font-size: var(--text-label); margin: 0; }

  .choices { display: flex; gap: 4px; flex-wrap: wrap; }

  .choice {
    font-size: var(--text-label);
    padding: 4px 10px;
    border-radius: var(--radius);
    border: 1px solid var(--rule);
    color: var(--ink-muted);
  }
  .choice:hover { color: var(--ink); }
  .choice.active {
    color: var(--ink);
    background: var(--rule);
  }

  .sections {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .row {
    display: flex;
    align-items: baseline;
    gap: 8px;
    font-size: var(--text-body);
    padding: 5px 8px;
    border-radius: var(--radius);
    border: 1px solid var(--rule);
  }

  .name { font-weight: 500; }

  .node {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 6px 8px;
    border-radius: var(--radius);
    border: 1px solid var(--rule);
  }

  .node-head { display: flex; align-items: baseline; gap: 8px; font-size: var(--text-body); }

  .ifaces label.iface {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 1px 0;
  }

  .ifaces .hint {
    margin: 4px 0 0;
    font-size: var(--text-label);
    line-height: 1.4;
  }

  .tag {
    font-size: var(--text-nano);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    padding: 1px 5px;
    border-radius: var(--radius);
    border: 1px solid var(--rule);
    color: var(--ink-muted);
  }


  .note[data-tone='warning'] { color: var(--warning); }
  code { font-size: var(--text-micro); }

  .tag.warn { color: var(--warning); border-color: var(--warning); }

  .fields { display: flex; flex-wrap: wrap; gap: 8px; }
  .fields label,
  .rt {
    display: flex;
    flex-direction: column;
    gap: 3px;
    font-size: var(--text-micro);
    color: var(--ink-muted);
  }

  .in {
    font: inherit;
    font-size: var(--text-label);
    color: var(--ink);
    background: var(--page);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    padding: 3px 6px;
    min-width: 0;
    width: 100%;
  }
  .in:focus { outline: 2px solid var(--series-1); outline-offset: 1px; }
  .in.id { font-weight: 500; max-width: 140px; }
  .in.num { max-width: 84px; }

  .rt { gap: 4px; margin-top: 4px; }
  .rt-row { display: flex; align-items: center; gap: 6px; }

  .check {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: var(--text-micro);
    color: var(--ink-muted);
    flex-direction: row;
  }

  .add { margin-left: 0; align-self: flex-start; }

  .actions { display: flex; gap: 6px; margin-top: 4px; }
  .actions .mini { margin-left: 0; }
  .save { color: var(--ink); border-color: var(--ink-muted); }

  /* A hidden row stays legible rather than being greyed to the edge of
     readability — this is the only place it can be switched back on, so it
     must not look disabled. */
  .row.off .name { color: var(--ink-muted); }

  .mini {
    font-size: var(--text-micro);
    padding: 1px 6px;
    border-radius: var(--radius);
    border: 1px solid var(--rule);
    color: var(--ink-muted);
    margin-left: auto;
  }
  .mini:hover:not(:disabled) { color: var(--ink); }
  .mini:disabled { opacity: 0.5; cursor: default; }


  .yaml {
    width: 100%;
    margin: 6px 0 2px;
    padding: 6px 8px;
    font-family: var(--font-mono, ui-monospace, monospace);
    font-size: var(--text-label);
    line-height: 1.45;
    color: var(--ink-2);
    background: var(--panel);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    resize: vertical;
    white-space: pre;
  }

  /* Zone headings inside the Sections block. Tight, because they are
     structure rather than content — the rows are what the reader is scanning. */
  .group {
    margin: 8px 0 3px;
    font-size: var(--text-nano);
  }

  .reset { margin-left: 0; align-self: flex-start; margin-top: 4px; }

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
