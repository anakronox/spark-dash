<script lang="ts">
  /* Settings, as a right-anchored fly-out.
   *
   * WHY THIS EXISTS. Preferences were scattered across four independent
   * localStorage keys, each owned by whichever component happened to need it
   * and each with its own control: theme in a header <select>, section order on
   * drag handles, metric selection and range inside the History panel. Nothing
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
  import { fetchWithTimeout } from '../lib/request';
  import { THEMES } from '../lib/theme.svelte';
  import type { Theme } from '../lib/theme.svelte';
  import type { Layout } from '../lib/layout.svelte';
  import { ZONES, ZONE_LABEL } from '../lib/layout.svelte';

  interface Props {
    theme: Theme;
    layout: Layout;
    open: boolean;
    onclose: () => void;
  }
  const { theme, layout, open, onclose }: Props = $props();

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
    runtimes: { llama_routers: RuntimeRef[]; vllm: RuntimeRef[] };
  }

  type Cfg = { source: string; path: string; nodes: ConfiguredNode[] };

  let cfg = $state<Cfg | null>(null);
  let draft = $state<ConfiguredNode[] | null>(null);
  let cfgError = $state<string | null>(null);
  let saving = $state(false);
  let saveError = $state<string | null>(null);

  const dirty = $derived(
    !!draft && !!cfg && JSON.stringify(draft) !== JSON.stringify(cfg.nodes),
  );

  const clone = (n: ConfiguredNode[]) => JSON.parse(JSON.stringify(n)) as ConfiguredNode[];

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
            vllm: n.runtimes.vllm.filter((v) => v.port != null).map((v) => v.port),
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
        runtimes: { llama_routers: [], vllm: [] },
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
    <section class="block">
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
      <p class="note dim">
        Every theme is validated separately for contrast and colourblind
        separation against its own background.
      </p>
    </section>

    <!-- Layout -->
    <section class="block">
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
      <p class="note dim">
        Compact keeps the name, status and memory band — the reading that
        cannot be inferred from anywhere else, since models, other GPU work and
        the system share one pool. A node that is down stays full size.
      </p>
    </section>

    <section class="block">
      <h3 class="eyebrow dim">Sections</h3>
      <!-- HIDE, not collapse. Collapsing already has a control on the section
           itself, and duplicating it here would be two ways to do one thing.
           Hiding is the one that has to live here: a hidden section renders
           nothing, so this panel is the only place it can be found again. -->
      <p class="note dim">
        Placement and visibility. Order and collapse stay on the sections
        themselves. Full-width sections form a band above the two columns, which
        fill independently — so a short section can sit under another short one
        whatever the other column is doing. Below 1100px the columns stack and
        everything is full width regardless, because a half-width table is
        unreadable there.
      </p>
      <ol class="sections">
        {#each layout.order as id (id)}
          {@const hidden = layout.isHidden(id)}
          {@const zone = layout.zoneOf(id)}
          <li class="row" class:off={hidden}>
            <span class="name">{layout.label(id)}</span>
            <!-- Cycles full -> left -> right rather than offering three
                 buttons: it is the same control as the arrow keys on the
                 section's own handle, and a row of radio buttons per section
                 would be fifteen targets in a panel that has to stay
                 scannable. -->
            <button
              class="mini w"
              disabled={hidden}
              aria-label={`${layout.label(id)} placement: ${ZONE_LABEL[zone]}`}
              onclick={() =>
                layout.place(
                  id,
                  ZONES[(ZONES.indexOf(zone) + 1) % ZONES.length],
                  layout.inZone(ZONES[(ZONES.indexOf(zone) + 1) % ZONES.length]).length,
                )}
            >{zone}</button>
            <button
              class="mini"
              aria-pressed={!hidden}
              aria-label={`${hidden ? 'Show' : 'Hide'} ${layout.label(id)}`}
              onclick={() => layout.toggleHidden(id)}
            >{hidden ? 'hidden' : 'shown'}</button>
          </li>
        {/each}
      </ol>
      <!-- Offered only when there is something to undo: a reset that does
           nothing still invites the click that loses your arrangement. -->
      <button
        class="mini reset"
        disabled={layout.isDefault}
        onclick={() => layout.reset()}
      >Reset sections</button>
    </section>

    <!-- Cluster -->
    <section class="block">
      <h3 class="eyebrow dim">Cluster</h3>
      {#if cfgError}
        <p class="note" data-tone="warning">Couldn't read the cluster config: {cfgError}</p>
      {:else if !draft}
        <p class="note dim">Loading…</p>
      {:else}
        <p class="note dim">
          Stored in <code>{cfg?.path}</code> on the monitoring VM. Adding a node
          here is all that is needed — the node's own stack carries nothing
          cluster-specific and asks for this on a timer.
        </p>

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
              <button
                class="mini"
                aria-label={`Remove ${n.node_id || 'node'}`}
                onclick={() => (draft = draft!.filter((_, j) => j !== i))}
              >remove</button>
            </div>

            <div class="fields">
              <label>host
                <input class="in" placeholder="192.168.50.61" bind:value={n.host} />
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

            <div class="rt">
              <span class="eyebrow dim">vLLM</span>
              {#each n.runtimes.vllm as v, vi (vi)}
                <div class="rt-row">
                  {#if v.port == null}
                    <span class="dim">{v.url}</span>
                    <span class="tag">off-node</span>
                  {:else}
                    <input class="in num" type="number" min="1" max="65535" aria-label="vLLM port" bind:value={v.port} />
                  {/if}
                  <button
                    class="mini"
                    aria-label="Remove vLLM endpoint"
                    onclick={() => (n.runtimes.vllm = n.runtimes.vllm.filter((_, j) => j !== vi))}
                  >×</button>
                </div>
              {/each}
              <button
                class="mini add"
                onclick={() => (n.runtimes.vllm = [...n.runtimes.vllm, { url: '', port: 8120 }])}
              >+ vLLM</button>
            </div>
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
        <p class="note dim">
          Saving rewrites the file, so comments in it are not preserved. The
          documented reference is <code>central/cluster.yml.example</code>.
        </p>
      {/if}
    </section>

    <!-- Where this lives -->
    <section class="block">
      <h3 class="eyebrow dim">Storage</h3>
      <!-- Said plainly because the alternative is discovering it: these do not
           follow you to another browser or machine, and there is no account to
           sync them to. The dashboard is deliberately stateless server-side. -->
      <p class="note dim">
        Preferences are stored in this browser only. They do not sync to other
        devices, and clearing site data resets them.
      </p>
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
    font-size: 20px;
    line-height: 1;
    padding: 2px 8px;
    border-radius: var(--radius);
    color: var(--ink-muted);
  }
  .close:hover { color: var(--ink); }

  .block { display: flex; flex-direction: column; gap: 8px; }

  .note { font-size: 11px; margin: 0; }

  .choices { display: flex; gap: 4px; flex-wrap: wrap; }

  .choice {
    font-size: 11px;
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
    font-size: 12px;
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

  .node-head { display: flex; align-items: baseline; gap: 8px; font-size: 12px; }

  .tag {
    font-size: 9px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    padding: 1px 5px;
    border-radius: var(--radius);
    border: 1px solid var(--rule);
    color: var(--ink-muted);
  }


  .note[data-tone='warning'] { color: var(--warning); }
  code { font-size: 10px; }

  .tag.warn { color: var(--warning); border-color: var(--warning); }

  .fields { display: flex; flex-wrap: wrap; gap: 8px; }
  .fields label,
  .rt {
    display: flex;
    flex-direction: column;
    gap: 3px;
    font-size: 10px;
    color: var(--ink-muted);
  }

  .in {
    font: inherit;
    font-size: 11px;
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
    font-size: 10px;
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
    font-size: 10px;
    padding: 1px 6px;
    border-radius: var(--radius);
    border: 1px solid var(--rule);
    color: var(--ink-muted);
    margin-left: auto;
  }
  .mini:hover:not(:disabled) { color: var(--ink); }
  .mini:disabled { opacity: 0.5; cursor: default; }

  /* Width sits left of the visibility toggle, not pushed right with it —
     they are different questions and a shared right edge made them read as one
     control with two halves. */
  .mini.w { margin-left: auto; }
  .mini.w + .mini { margin-left: 0; }

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
