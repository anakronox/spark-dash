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
  import { ENGINE_RUNTIMES } from '../lib/types';
  import type { FleetFeed } from '../lib/fleet.svelte';
  import type { ClientsFeed } from '../lib/clients.svelte';
  import type { AssistantFeed, McpStatus } from '../lib/assistant.svelte';
  import { CLIENTS, clientOr, setupFor, type ClientId } from '../lib/mcpClients';
  import { probeLine, sameAddress, stateLine, viewOf } from '../lib/clients';
  import type { ProbeResult } from '../lib/clients';


  interface Props {
    theme: Theme;
    layout: Layout;
    open: boolean;
    onclose: () => void;
    /** The fleet updater, when the backend has one (roadmap AK). Absent or
     *  unconfigured, nothing about it renders. */
    fleet?: FleetFeed;
    /** Client stats from a LiteLLM gateway (roadmap AM5). */
    clients?: ClientsFeed;
    assistant?: AssistantFeed;
  }
  const { theme, layout, open, onclose, fleet, clients, assistant }: Props = $props();

  /* ASSISTANT (roadmap AY). An OpenAI-compatible endpoint, its key and a
     model. The key goes to the backend once and is never sent back: the field
     starts empty, and saving with it empty keeps the stored key. */
  let aUrl = $state('');
  let aKey = $state('');
  let aModel = $state('');
  let aModels = $state<string[]>([]);
  let aBusy = $state(false);
  let aError = $state<string | null>(null);
  let aNote = $state<string | null>(null);

  async function assistantAction(fn: () => Promise<void>) {
    if (!assistant || aBusy) return;
    aBusy = true;
    aError = null;
    aNote = null;
    try {
      await fn();
    } catch (err) {
      aError = (err as Error).message;
    } finally {
      aBusy = false;
    }
  }
  const detectModels = () =>
    assistantAction(async () => {
      aModels = await assistant!.detect(aUrl, aKey);
      if (!aModel && aModels.length) aModel = aModels[0];
      aNote = `${aModels.length} model${aModels.length === 1 ? '' : 's'} found`;
    });
  const testAssistant = () =>
    assistantAction(async () => {
      const r = await assistant!.test(aUrl, aModel, aKey);
      aNote = `answered in ${r.seconds}s: “${r.reply}”`;
    });
  const saveAssistant = () =>
    assistantAction(async () => {
      await assistant!.save({ url: aUrl, model: aModel, key: aKey || undefined });
      aKey = '';
      aNote = 'saved';
    });
  const removeAssistant = () =>
    assistantAction(async () => {
      await assistant!.remove();
      aUrl = aModel = aKey = '';
      aModels = [];
    });
  const setAssistantEnabled = (on: boolean) =>
    assistantAction(async () => {
      await assistant!.save({ url: aUrl, model: aModel, enabled: on });
    });
  const setAssistantTools = (on: boolean) =>
    assistantAction(async () => {
      await assistant!.save({ url: aUrl, model: aModel, tools: on });
    });

  /* TOOLS FOR AGENTS (roadmap BB). Two switches and a key. The key comes back
     once, in the answer that made it, and is shown here until Settings
     closes; after that only its last four are known to the browser. */
  let mcp = $state<McpStatus | null>(null);
  let mKey = $state<string | null>(null);
  let mBusy = $state(false);
  let mError = $state<string | null>(null);
  let mBox = $state<HTMLInputElement | null>(null);

  async function mcpAction(fn: () => Promise<McpStatus>) {
    if (!assistant || mBusy) return;
    mBusy = true;
    mError = null;
    try {
      mcp = await fn();
      if (mcp.key) {
        mKey = mcp.key;
        await tick();
        mBox?.select();
      }
    } catch (err) {
      mError = (err as Error).message;
    } finally {
      mBusy = false;
    }
  }
  const mcpUrl = () => `${location.origin}${mcp?.path ?? '/mcp'}`;

  /* Which agent the snippet is written for, remembered in this browser. */
  const CLIENT_KEY = 'spark-dash.mcp-client.v1';
  let mClient = $state<ClientId>(
    clientOr((() => {
      try {
        return localStorage.getItem(CLIENT_KEY);
      } catch {
        return null;
      }
    })()),
  );
  function chooseClient(id: ClientId) {
    mClient = id;
    try {
      localStorage.setItem(CLIENT_KEY, id);
    } catch {
      // Private window: the choice lasts until reload.
    }
  }
  /* Without the key in hand (it is shown once), the snippet says where it
     goes and the reader pastes it in. */
  const mSetup = $derived(setupFor(mClient, mcpUrl(), mKey ?? '<key>'));


  /* CLIENTS — a gateway address and a switch, AL5's two levels (roadmap AM).
   *
   * The address is capability and the switch is use; both live in
   * cluster.yml's `gateway:` block. Test tries an address without saving it,
   * so a typo is found before it is written. Save, the switch and Remove
   * apply at once, like the fleet checkboxes, and each answers with the new
   * status, so the Clients card appears or goes on the same click.
   */
  let clientsDraft = $state('');
  let clientsBusy = $state(false);
  let clientsError = $state<string | null>(null);
  let probe = $state<ProbeResult | null>(null);

  /** Linked by URL for the same reason as SETUP_GUIDE below: this is read
   *  through a tunnel, where a relative docs link goes nowhere. */
  const CLIENTS_NOTES =
    'https://github.com/anakronox/spark-dash/blob/main/docs/deployment.md#client-stats--optional';

  const clientsView = $derived(viewOf(clients?.status ?? null));
  const clientsDirty = $derived(!sameAddress(clientsDraft, clients?.status?.url ?? null));

  async function clientsAction(fn: () => Promise<void>) {
    if (!clients || clientsBusy) return;
    clientsBusy = true;
    clientsError = null;
    try {
      await fn();
    } catch (err) {
      clientsError = (err as Error).message;
    } finally {
      clientsBusy = false;
    }
  }

  const testGateway = () =>
    clientsAction(async () => {
      probe = await clients!.test(clientsDraft);
    });

  const saveGateway = () =>
    clientsAction(async () => {
      await clients!.setUrl(clientsDraft);
      clientsDraft = clients!.status?.url ?? '';
    });

  const removeGateway = () =>
    clientsAction(async () => {
      await clients!.setUrl(null);
      clientsDraft = '';
      probe = null;
    });

  const setClientsEnabled = (on: boolean) => clientsAction(() => clients!.setEnabled(on));

  /* FLEET UPDATES — a checkbox per node, not a second inventory.
   *
   * The fleet service keeps its own list of Sparks: name and host. Every
   * node in cluster.yml already has both, so enrolling one is a tick that
   * hands them over, and unticking removes it. Applied at once, like a
   * silence, not on "Save cluster": it is a write to the fleet service, not
   * to the file, and a tick that waited on an unrelated save would look
   * broken. A node's id here is its name there, which is also why a rename
   * is a remove and an add and not a third route.
   *
   * Per node rather than one master switch, on purpose: a master switch
   * would have to re-derive the fleet list on every save and would enrol a
   * host added for monitoring only; a tick is one honest write with its
   * result visible on the row.
   */
  let fleetBusy = $state<string | null>(null);
  let fleetError = $state<string | null>(null);

  /** Linked by URL rather than by path: this warning is read by people whose
   *  dashboard is behind a tunnel, where a relative docs link goes nowhere. */
  const SETUP_GUIDE =
    'https://github.com/anakronox/spark-dash/blob/main/docs/fleet-updates-setup.md';

  const missingRequirements = $derived(
    Object.entries(fleet?.requirements ?? {})
      .filter(([, ok]) => !ok)
      .map(([name]) => name),
  );

  async function setFleetEnabled(on: boolean) {
    if (!fleet || fleetBusy) return;
    fleetBusy = '*';
    fleetError = null;
    try {
      await fleet.setEnabled(on);
    } catch (err) {
      fleetError = (err as Error).message;
    } finally {
      fleetBusy = null;
    }
  }

  async function setEnrolled(name: string, host: string, on: boolean) {
    if (!fleet || fleetBusy) return;
    fleetBusy = name;
    fleetError = null;
    try {
      if (on) await fleet.enrol(name, host);
      else await fleet.remove(name);
    } catch (err) {
      fleetError = `${name}: ${(err as Error).message}`;
    } finally {
      fleetBusy = null;
    }
  }

  /** On the fleet list but not in cluster.yml — a renamed or retired node,
   *  or one added on the fleet page. Shown so it can be taken off here. */
  const fleetOrphans = $derived(
    (fleet?.fleet?.nodes ?? []).filter((f) => !(draft ?? []).some((n) => n.node_id === f.name)),
  );
  const fleetState = (name: string) => fleet?.fleet?.nodes.find((f) => f.name === name);


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
    // 8003, where the danflashes pair publishes it; TensorFold's own default
    // is 8080 (cli_args.py, v0.6.0).
    tensorfold: { label: 'TensorFold', port: 8003 },
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
      // The fleet feed idles at a minute between loads; the checkboxes
      // should reflect the list as it is now, not as it was.
      fleet?.load();
      // The field starts from what is saved; a test result from last time
      // would describe an address that may no longer be in it.
      probe = null;
      clientsError = null;
      clients?.load().then(() => (clientsDraft = clients?.status?.url ?? ''));
      aError = aNote = null;
      aKey = '';
      mKey = mError = null;
      assistant?.mcpStatus().then((m) => (mcp = m)).catch(() => (mcp = null));
      assistant?.load().then(() => {
        aUrl = assistant?.status?.url ?? '';
        aModel = assistant?.status?.model ?? '';
        aModels = aModel ? [aModel] : [];
      });
    } else if (!open && dialog.open) dialog.close();
  });

  /* THE SHELL (roadmap BC). One pane at a time beside a rail, in a fly-out
     as wide as the update manager's and resizable from its left edge. The
     pane and the width are this browser's, like the rest of the layout. */
  type Pane = 'appearance' | 'nodes' | 'updates' | 'clients' | 'assistant' | 'mcp';
  type Dot = 'on' | 'off' | 'warn' | 'edit';
  const DOT_WORD: Record<Dot, string> = { on: 'on', off: 'off', warn: 'needs setup', edit: 'unsaved' };
  const PANE_KEY = 'spark-dash.settings-pane.v1';
  const WIDTH_KEY = 'spark-dash.settings-width.v1';
  const DEFAULT_WIDTH = 1000;
  /* The rail and a pane still usable beside it. */
  const MIN_WIDTH = 560;

  function stored(key: string): string | null {
    try {
      return localStorage.getItem(key);
    } catch {
      return null;
    }
  }
  function store(key: string, value: string) {
    try {
      localStorage.setItem(key, value);
    } catch {
      // Private window: kept until reload.
    }
  }

  let pane = $state<Pane>((stored(PANE_KEY) as Pane) || 'appearance');
  function setPane(id: Pane) {
    pane = id;
    store(PANE_KEY, id);
  }

  const RAIL = $derived<{ label: string; items: { id: Pane; label: string; shown: boolean; dot?: Dot; state?: string }[] }[]>([
    { label: 'Page', items: [{ id: 'appearance', label: 'Appearance', shown: true }] },
    {
      label: 'Cluster',
      items: [{
        id: 'nodes', label: 'Nodes', shown: true,
        dot: dirty ? 'edit' : undefined,
        state: draft ? `${draft.length}${dirty ? ' · unsaved' : ''}` : undefined,
      }],
    },
    {
      label: 'Integrations',
      items: [
        {
          id: 'updates', label: 'DGX OS updates', shown: !!fleet,
          dot: !fleet?.capability ? 'warn' : fleet.enabled ? 'on' : 'off',
          state: !fleet?.capability ? 'setup' : fleet.enabled ? undefined : 'off',
        },
        {
          id: 'clients', label: 'Clients', shown: !!clients,
          dot: clients?.configured ? 'on' : 'off', state: clients?.configured ? undefined : 'off',
        },
        {
          id: 'assistant', label: 'Assistant', shown: !!assistant,
          dot: assistant?.status?.enabled ? 'on' : 'off',
          state: assistant?.status?.enabled ? undefined : assistant?.status?.url ? 'off' : 'not set up',
        },
        {
          id: 'mcp', label: 'Agent tools', shown: !!assistant,
          dot: mcp?.enabled ? 'on' : 'off', state: mcp?.enabled ? undefined : 'off',
        },
      ],
    },
  ]);
  /* A remembered pane this deployment does not have (no fleet service, say)
     falls back to the first. */
  $effect(() => {
    const all = RAIL.flatMap((g) => g.items).filter((it) => it.shown);
    if (!all.some((it) => it.id === pane)) pane = 'appearance';
  });
  const paneLabel = $derived(
    pane === 'mcp' ? 'Agent tools (MCP)' : (RAIL.flatMap((g) => g.items).find((it) => it.id === pane)?.label ?? ''),
  );

  let panelWidth = $state(Number(stored(WIDTH_KEY)) || DEFAULT_WIDTH);
  function setWidth(w: number, save = false) {
    panelWidth = Math.round(Math.max(MIN_WIDTH, Math.min(w, window.innerWidth)));
    if (save) store(WIDTH_KEY, String(panelWidth));
  }
  function startResize(e: PointerEvent) {
    if (e.button !== 0) return;
    const handle = e.currentTarget as HTMLElement;
    handle.setPointerCapture(e.pointerId);
    // Anchored right, so the width is the distance from the pointer to the
    // window's right edge.
    const move = (ev: PointerEvent) => setWidth(window.innerWidth - ev.clientX);
    const up = () => {
      handle.removeEventListener('pointermove', move);
      handle.removeEventListener('pointerup', up);
      handle.removeEventListener('pointercancel', up);
      setWidth(panelWidth, true);
    };
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', up);
    handle.addEventListener('pointercancel', up);
    e.preventDefault();
  }
  function keyResize(e: KeyboardEvent) {
    const step = e.key === 'ArrowLeft' ? 32 : e.key === 'ArrowRight' ? -32 : 0;
    if (!step) return;
    e.preventDefault();
    setWidth(panelWidth + step, true);
  }

  /* RESET LAYOUT: every view preference this page keeps, except the theme.
     They all share one prefix, so a key added later is reset too without an
     edit here. Reloads, since each store read its key once at start. */
  const KEEP = new Set(['spark-dash.theme.v1']);
  let resetArmed = $state(false);
  let resetTimer: ReturnType<typeof setTimeout> | undefined;
  function resetLayout() {
    if (!resetArmed) {
      resetArmed = true;
      clearTimeout(resetTimer);
      resetTimer = setTimeout(() => (resetArmed = false), 4000);
      return;
    }
    try {
      for (const k of Object.keys(localStorage)) {
        if (k.startsWith('spark-dash.') && !KEEP.has(k)) localStorage.removeItem(k);
      }
    } catch {
      // Nothing stored, so nothing to reset.
    }
    location.reload();
  }

  function watchedCount(n: ConfiguredNode): string {
    const rows = interfaceRows(n);
    return rows.length ? `${rows.filter((r) => isWatched(n, r.name)).length} of ${rows.length}` : 'none yet';
  }
  function engineCount(n: ConfiguredNode): number {
    return ENGINE_RUNTIMES.reduce((sum, r) => sum + engineRefs(n, r).length, 0);
  }
</script>

<dialog
  bind:this={dialog}
  class="flyout"
  style:--settings-w="{panelWidth}px"
  aria-label="Settings"
  onclose={onclose}
  onclick={(e) => {
    // The dialog element itself is the backdrop's hit target, so a click
    // landing on it rather than on the panel is outside.
    if (e.target === dialog) onclose();
  }}
>
  <!-- Drag to resize, from the left edge: the panel is anchored right. The
       arrow keys do the same, 32px a press, and a double click goes back to
       the default. A focusable separator is ARIA's own resize widget, which
       the two rules below do not know. -->
  <!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
  <div
    class="edge-drag"
    role="separator"
    aria-orientation="vertical"
    aria-label="Resize settings"
    aria-valuenow={Math.round(panelWidth)}
    aria-valuemin={MIN_WIDTH}
    tabindex="0"
    onpointerdown={startResize}
    onkeydown={keyResize}
    ondblclick={() => setWidth(DEFAULT_WIDTH, true)}
  ></div>
  <div class="panel">
    <header>
      <h2 class="eyebrow">Settings</h2>
      <button class="close" aria-label="Close" onclick={onclose}>×</button>
    </header>

    <div class="body">
      <nav class="rail" aria-label="Settings sections">
        {#each RAIL as group (group.label)}
          {@const items = group.items.filter((it) => it.shown)}
          {#if items.length}
            <div class="group">
              <p class="eyebrow dim">{group.label}</p>
              {#each items as it (it.id)}
                <button
                  class="nav"
                  aria-current={pane === it.id ? 'page' : undefined}
                  onclick={() => setPane(it.id)}
                >
                  {#if it.dot}<span class="dot {it.dot}" title={DOT_WORD[it.dot]}></span>{/if}
                  <span class="label">{it.label}</span>
                  {#if it.state}<span class="state">{it.state}</span>{/if}
                </button>
              {/each}
            </div>
          {/if}
        {/each}

        <!-- J4: the number that keeps the single-host argument honest.
             Monitoring a GB10 should cost it as little as possible, and on one
             box that cost comes out of the SAME unified pool the models use.
             A line under the rail rather than a section: one figure, always in
             view while Settings is open. Summed from components that measure
             THEMSELVES; nothing here guesses which processes are monitoring. -->
        <p
          class="footprint note"
          title="Resident memory of Prometheus, Alertmanager and the monitoring host's exporter. Each agent's own cost is reported per node."
        >
          {#if footprint === null}
            <span class="dim">monitoring: unavailable</span>
          {:else}
            <span class="dim">monitoring uses</span>
            <span class="num">{(footprint / 1024 ** 2).toFixed(0)} MiB</span>
          {/if}
        </p>
      </nav>

      <div class="pane">
        <h3 class="pane-title">{paneLabel}</h3>
      {#if pane === 'appearance'}
    <!-- Appearance -->
    <section class="stack">
      <h3 class="eyebrow dim">Theme</h3>
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
      <h3 class="eyebrow dim">Elements</h3>
      <!-- What a card does when its content is taller than it is. Paging keeps
           the drag coupled to rows; scrolling makes the drag the height and
           renders everything. A wheel over a scrolling card scrolls the card
           until it reaches its end, then the page -- ordinary browser
           behaviour, and worth saying once. -->
      <div class="choices" role="group" aria-label="When content is taller than its element">
        <button
          class="choice"
          class:active={layout.overflow === 'page'}
          aria-pressed={layout.overflow === 'page'}
          onclick={() => layout.setOverflow('page')}
        >Paginate</button>
        <button
          class="choice"
          class:active={layout.overflow === 'scroll'}
          aria-pressed={layout.overflow === 'scroll'}
          onclick={() => layout.setOverflow('scroll')}
        >Scroll</button>
      </div>
      <p class="note dim">
        Paginate keeps rows to the element's height. Scroll shows every row and
        scrolls inside the element; the wheel reaches the page again at its end.
      </p>

      <h3 class="eyebrow dim">Node elements</h3>
      <!-- Deliberate, never automatic. Switching on node count would mean the
           page rearranges itself the moment a node joins — which is exactly
           when someone is watching it. -->
      <div class="choices" role="group" aria-label="Node element density">
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
      <h3 class="eyebrow dim">This browser</h3>
      <!-- Said plainly because the alternative is discovering it: these do not
           follow you to another browser or machine, and there is no account to
           sync them to. The dashboard is deliberately stateless server-side. -->
      <p class="note dim">
        The theme, the page's arrangement, each element's view and size, and column widths are kept
        in this browser only.
      </p>
      <!-- Two presses, since the page cannot ask with confirm(): the first
           arms it and says what will go, the second does it. -->
      <div class="rt-row">
        <button class="mini add" onclick={resetLayout}>
          {resetArmed ? 'press again to reset' : 'Reset layout'}
        </button>
        {#if resetArmed}
          <span class="note dim">Puts every element back where it started; keeps the theme.</span>
        {/if}
      </div>
    </section>
      {/if}
      {#if pane === 'nodes'}
    <!-- Cluster -->
    <section class="stack wide">
      {#if cfgError}
        <p class="note" data-tone="warning">Couldn't read the cluster config: {cfgError}</p>
      {:else if !draft}
        <p class="note dim">Loading…</p>
      {:else}
        <!-- Adding a node here really is all that is needed: the node's own
             stack carries nothing cluster-specific and asks for this on a
             timer. True and worth knowing once, which is what the README is
             for; the path is the part you need while looking at this panel. -->
        <p class="note dim">Stored in <code>central/cluster/cluster.yml</code> on the monitoring host
          (<code>{cfg?.path}</code> inside the backend container).</p>

        <div class="nodes">
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

            {#if fleet?.configured}
              {@const f = fleetState(n.node_id)}
              <div class="rt">
                <label class="check fleet">
                  <input
                    type="checkbox"
                    checked={fleet.has(n.node_id)}
                    disabled={fleetBusy !== null || !n.node_id || !n.host || !fleet.available}
                    onchange={(e) =>
                      setEnrolled(n.node_id, n.host, (e.currentTarget as HTMLInputElement).checked)}
                  />
                  <span>DGX OS updates</span>
                  {#if fleetBusy === n.node_id}
                    <span class="tag">…</span>
                  {:else if f?.reachable === false}
                    <span class="tag warn">unreachable</span>
                  {:else if f && f.reachable == null}
                    <span class="tag">checking</span>
                  {/if}
                </label>
              </div>
            {/if}

            <details class="rt" open={n.runtimes.llama_routers.length > 0}>
              <summary class="eyebrow dim">
                llama.cpp routers
                <span class="count">{n.runtimes.llama_routers.length || 'none'}</span>
              </summary>
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
            </details>

            <details class="rt ifaces">
              <summary class="eyebrow dim">
                Interfaces watched by alerting
                <span class="count">{watchedCount(n)}</span>
              </summary>
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
              {:else}
                <p class="hint dim">
                  Nothing to list until this node reports its interfaces.
                </p>
              {/if}
            </details>

            <details class="rt" open={engineCount(n) > 0}>
              <summary class="eyebrow dim">
                Engines: {ENGINE_RUNTIMES.map((r) => ENGINE_UI[r].label).join(', ')}
                <span class="count">{engineCount(n) || 'none'}</span>
              </summary>
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
            </details>
          </div>
        {/each}
        </div>

        <div class="actions savebar">
          <button class="mini add" onclick={addNode}>+ node</button>
          <!-- Kept, unlike the other notes: this is not background, it is what
               the button beside it will DO to a file the reader may have
               hand-edited. -->
          <span class="note dim grow">Saving rewrites the file; comments are not preserved.</span>
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
      {/if}
    </section>

      {/if}
      {#if pane === 'updates'}
    <!-- Fleet updates (roadmap AK). Only when the backend has a fleet
         service; a section about a thing that is not there would be noise.
         Nothing here is editable on purpose: the login and the cadence are
         the fleet container's own environment, and saying WHERE beats a
         control that could not actually change them. -->
    {#if fleet}
      <section class="stack">

        <!-- AL5: the switch is shown whatever the state, because Settings is
             where a person goes to find out how to turn a thing ON. When the
             compose file has not granted the capability it cannot complete,
             and says which piece is missing rather than "not configured",
             which is a dead end for whoever is reading it. -->
        <label class="check">
          <input
            type="checkbox"
            checked={fleet.enabled && fleet.capability}
            disabled={fleetBusy !== null}
            onchange={(e) => setFleetEnabled((e.currentTarget as HTMLInputElement).checked)}
          />
          <span>check my Sparks for updates</span>
        </label>

        {#if !fleet.capability}
          <p class="note" data-tone="warning">
            DGX OS updates need an SSH key the dashboard can use and a login on each Spark that can run
            <code>apt</code> — a change to your compose file and a one-time step per Spark, not
            something the dashboard can do for itself.
            {#if missingRequirements.length}
              <br />Missing: {#each missingRequirements as m, i (m)}<code>{m}</code>{i <
                missingRequirements.length - 1
                  ? ', '
                  : ''}{/each}.
            {/if}
            <br /><a href={SETUP_GUIDE} target="_blank" rel="noopener">Quick setup guide ↗</a>
          </p>
        {:else if !fleet.enabled}
          <p class="note dim">
            Set up and switched off. Nothing is being checked and no Spark is contacted; the key
            stays mounted. Turn it back on above.
          </p>
        {:else if !fleet.available}
          <p class="note" data-tone="warning">The updater is not answering. The checkboxes above wait for it.</p>
        {:else if fleet.fleet}
          <p class="note">
            <span class="num">{fleet.fleet.nodes.length}</span>
            <span class="dim">
              Spark{fleet.fleet.nodes.length === 1 ? '' : 's'} being checked — tick
              <em>DGX OS updates</em> under a node above to add it. Checked every
              <span class="num">{fleet.fleet.interval_min}</span> min
              {#if fleet.fleet.ssh_user}as <code>{fleet.fleet.ssh_user}</code>{/if}.
            </span>
          </p>
          {#if fleetOrphans.length}
            <div class="rt">
              <span class="eyebrow dim">Being checked but not in this cluster</span>
              {#each fleetOrphans as f (f.name)}
                <div class="rt-row">
                  <span>{f.name}</span>
                  <span class="dim">{f.host}</span>
                  <button
                    class="mini"
                    disabled={fleetBusy !== null}
                    onclick={() => setEnrolled(f.name, f.host, false)}
                  >remove</button>
                </div>
              {/each}
            </div>
          {/if}
          <p class="note dim">
            Addresses and which Sparks update together come from <code>cluster.yml</code>, so
            there is one list and not two. The login and the cadence are
            <code>FLEET_SSH_USER</code> and <code>FLEET_INTERVAL_MIN</code>; the key is
            mounted into this backend. <a href={SETUP_GUIDE} target="_blank" rel="noopener">Setup guide ↗</a>
          </p>
        {/if}

        <!-- Outside the branches above on purpose: the commonest failure is
             switching the feature on before the compose change has arrived,
             and that refusal happens in the state where none of them render. -->
        {#if fleetError}
          <p class="note" data-tone="warning">{fleetError}</p>
        {/if}
      </section>
    {/if}

      {/if}
      {#if pane === 'clients'}
    <!-- Client stats (roadmap AM5). Always present when the feed exists, like
         DGX OS updates above: Settings is where a person goes to find out how
         to turn a thing on. -->
    {#if clients}
      <section class="stack">

        <label class="check">
          <input
            type="checkbox"
            checked={clients.configured}
            disabled={clientsBusy || clientsView === 'no-cluster-file'}
            onchange={(e) => setClientsEnabled((e.currentTarget as HTMLInputElement).checked)}
          />
          <span>show which clients call the engines</span>
        </label>

        {#if clientsView === 'no-cluster-file'}
          <p class="note" data-tone="warning">
            The gateway's address is kept in <code>cluster.yml</code>, and this deployment takes its
            nodes from <code>SPARK_NODES</code> instead. Move the nodes into <code>cluster.yml</code>
            first. <a href={CLIENTS_NOTES} target="_blank" rel="noopener">Deployment notes ↗</a>
          </p>
        {:else}
          <div class="rt-row">
            <input
              class="in"
              placeholder="http://gateway-host:4000"
              aria-label="LiteLLM gateway address"
              bind:value={clientsDraft}
            />
            <button class="mini" disabled={clientsBusy || !clientsDraft.trim()} onclick={testGateway}
              >Test</button
            >
            <button
              class="mini save"
              disabled={clientsBusy || !clientsDraft.trim() || !clientsDirty}
              onclick={saveGateway}>Save</button
            >
            {#if clients.status?.capability}
              <button class="mini" disabled={clientsBusy} onclick={removeGateway}>Remove</button>
            {/if}
          </div>

          {#if probe}
            {@const line = probeLine(probe)}
            <p class="note" data-tone={line.tone || undefined}>{line.text}</p>
          {/if}

          {#if clientsView === 'invalid'}
            <p class="note" data-tone="warning">
              The <code>gateway:</code> block in <code>cluster.yml</code> has a mistake:
              {clients.status?.error} Saving an address here replaces it.
            </p>
          {:else if clientsView === 'absent'}
            <p class="note dim">
              Needs a LiteLLM proxy in front of the engines, set up outside this dashboard. The
              engines never record who called them; the gateway does.
              <a href={CLIENTS_NOTES} target="_blank" rel="noopener">Deployment notes ↗</a>
            </p>
          {:else if clientsView === 'off'}
            <p class="note dim">
              Set up and switched off. Nothing is scraped and the Clients card is hidden; the history
              already collected is kept.
            </p>
          {:else if clients.status}
            {@const line = stateLine(clients.status)}
            <p class="note" data-tone={line.tone || undefined}>{line.text}</p>
          {/if}
        {/if}

        <!-- Outside the branches, as the fleet error is: the refusals worth
             reading (no address yet, no cluster file) arrive in states where
             none of them render. -->
        {#if clientsError}
          <p class="note" data-tone="warning">{clientsError}</p>
        {/if}
      </section>
    {/if}

      {/if}
      {#if pane === 'assistant'}
    {#if assistant}
      <section class="cols">
       <div class="stack">
        <h3 class="eyebrow dim">Model</h3>
        <p class="note dim">Any OpenAI-compatible endpoint: an engine, or the gateway.</p>
        {@render assistantModel()}
       </div>
       <div class="stack">
        <h3 class="eyebrow dim">Behaviour</h3>
        <p class="note dim">
          Ask questions about the cluster from the header. The model is sent the window's alerts,
          events and series as text, and may call the read-only agent tools; it cannot act on
          anything.
        </p>
        {#if assistant.status?.url}
          <label class="check">
            <input
              type="checkbox"
              checked={assistant.status?.enabled ?? false}
              disabled={aBusy}
              onchange={(e) => setAssistantEnabled((e.currentTarget as HTMLInputElement).checked)}
            />
            <span>show the ask button</span>
          </label>
          <label class="check">
            <input
              type="checkbox"
              checked={assistant.status?.tools ?? true}
              disabled={aBusy}
              onchange={(e) => setAssistantTools((e.currentTarget as HTMLInputElement).checked)}
            />
            <span>let it look further with the agent tools (needs a model that calls tools)</span>
          </label>
        {:else}
          <p class="note dim">Set a model and save to switch it on.</p>
        {/if}
       </div>
      </section>
    {/if}
    {#snippet assistantModel()}
        <div class="rt-row">
          <input
            id="assistant-url"
            class="in"
            placeholder="http://host:4000/v1"
            aria-label="OpenAI-compatible endpoint"
            bind:value={aUrl}
          />
        </div>
        <div class="rt-row">
          <input
            id="assistant-key"
            class="in"
            type="password"
            autocomplete="off"
            placeholder={assistant?.status?.key_set ? `key set (${assistant?.status.key_hint}); leave empty to keep` : 'API key, if it needs one'}
            aria-label="API key"
            bind:value={aKey}
          />
        </div>
        <div class="rt-row">
          <select id="assistant-model" class="in" aria-label="Model" bind:value={aModel} disabled={!aModels.length}>
            {#if !aModels.length}<option value="">press Detect</option>{/if}
            {#each aModels as m (m)}<option value={m}>{m}</option>{/each}
          </select>
          <button class="mini" disabled={aBusy || !aUrl.trim()} onclick={detectModels}>Detect</button>
          <button class="mini" disabled={aBusy || !aUrl.trim() || !aModel} onclick={testAssistant}>Test</button>
          <button class="mini save" disabled={aBusy || !aUrl.trim() || !aModel} onclick={saveAssistant}>Save</button>
          {#if assistant?.status?.url}
            <button class="mini" disabled={aBusy} onclick={removeAssistant}>Remove</button>
          {/if}
        </div>
        {#if aBusy}<p class="note dim">working…</p>{/if}
        {#if aNote}<p class="note">{aNote}</p>{/if}
        {#if aError}<p class="note" data-tone="warning">{aError}</p>{/if}
        {#if assistant?.status?.error}
          <p class="note" data-tone="warning">
            The <code>assistant:</code> block in <code>cluster.yml</code> has a mistake:
            {assistant?.status.error} Saving here replaces it.
          </p>
        {/if}
    {/snippet}

      {/if}
      {#if pane === 'mcp'}
    <!-- Tools for agents (roadmap BB). With the assistant, since Ask uses
         the same tools; reachable whether or not Ask is set up. -->
    {#if assistant}
      <section class="cols">
       <div class="stack">
        <h3 class="eyebrow dim">Access</h3>
        <p class="note dim">
          Lets an agent such as Claude Code query the dashboard itself: the cluster now, any series
          over any window, alerts, events, latency, clients, and what moved around a moment.
          Read-only. It needs a key, made here.
        </p>
        <label class="check">
          <input
            type="checkbox"
            checked={mcp?.enabled ?? false}
            disabled={mBusy || !mcp}
            onchange={(e) =>
              mcpAction(() => assistant!.mcpSave({ enabled: (e.currentTarget as HTMLInputElement).checked }))}
          />
          <span>serve the tools at <code>{mcp?.path ?? '/mcp'}</code></span>
        </label>
        <label class="check">
          <input
            type="checkbox"
            checked={mcp?.promql ?? false}
            disabled={mBusy || !mcp}
            onchange={(e) =>
              mcpAction(() => assistant!.mcpSave({ promql: (e.currentTarget as HTMLInputElement).checked }))}
          />
          <span>also allow raw PromQL queries</span>
        </label>
        {#if mcp?.enabled}
          <label class="field">
            <span class="dim">endpoint</span>
            <input class="in mono" readonly value={mcpUrl()} onfocus={(e) => e.currentTarget.select()} />
          </label>
          <label class="field">
            <span class="dim">key</span>
            {#if mKey}
              <input
                class="in mono"
                readonly
                value={mKey}
                bind:this={mBox}
                onfocus={(e) => e.currentTarget.select()}
              />
            {:else}
              <span class="note">
                <span class="num">{mcp.key_hint ?? 'none'}</span>
                <span class="dim">· shown only when made</span>
              </span>
            {/if}
            <button class="mini" disabled={mBusy} onclick={() => mcpAction(() => assistant!.mcpNewKey())}
              >new key</button
            >
          </label>
          {#if mKey}
            <p class="note" data-tone="warning">
              Copy the key now: it is not shown again. A lost key is replaced with <b>new key</b>.
            </p>
          {/if}
        {/if}
        {#if mError}<p class="note" data-tone="warning">{mError}</p>{/if}
        {#if mcp?.error}
          <p class="note" data-tone="warning">
            The <code>mcp:</code> block in <code>cluster.yml</code> has a mistake: {mcp.error}
          </p>
        {/if}
       </div>
       <div class="stack">
        <h3 class="eyebrow dim">Connect an agent</h3>
        {#if mcp?.enabled}
          <label class="field">
            <span class="dim">agent</span>
            <select
              class="in"
              aria-label="Agent to connect"
              value={mClient}
              onchange={(e) => chooseClient((e.currentTarget as HTMLSelectElement).value as ClientId)}
            >
              {#each CLIENTS as c (c.id)}<option value={c.id}>{c.label}</option>{/each}
            </select>
          </label>
          <p class="note dim">{mSetup.where}{mKey ? '' : ', with your key in place of <key>'}:</p>
          <textarea
            class="yaml snippet"
            readonly
            rows={mSetup.text.split('\n').length + 1}
            aria-label="Configuration for the chosen agent"
            value={mSetup.text}
            onfocus={(e) => e.currentTarget.select()}
          ></textarea>
          {#if mSetup.then}<p class="note dim">{mSetup.then}</p>{/if}
        {:else}
          <p class="note dim">
            Switch the tools on to get a key and the setup for Claude Code, Hermes Agent, Codex,
            Gemini CLI, Cursor, VS Code or OpenCode.
          </p>
        {/if}
       </div>
      </section>
    {/if}

      {/if}
      </div>
    </div>
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
    /* The update manager's width by default (roadmap BC), set from the
       left-edge drag. */
    width: min(var(--settings-w, 1000px), 100%);
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

  /* The header across the top; under it the rail and the pane, each
     scrolling on its own. */
  .panel {
    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 0;
  }

  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 14px 20px 12px;
    border-bottom: 1px solid var(--rule);
  }

  .edge-drag {
    position: absolute;
    top: 0;
    bottom: 0;
    left: 0;
    width: 7px;
    cursor: ew-resize;
    z-index: 2;
    touch-action: none;
  }
  .edge-drag:hover,
  .edge-drag:focus-visible {
    background: color-mix(in srgb, var(--series-1) 35%, transparent);
    outline: none;
  }

  .body {
    flex: 1;
    min-height: 0;
    display: grid;
    grid-template-columns: 184px minmax(0, 1fr);
  }

  .rail {
    display: flex;
    flex-direction: column;
    gap: 14px;
    padding: 12px 8px;
    border-right: 1px solid var(--rule);
    overflow-y: auto;
  }
  .rail .group { display: flex; flex-direction: column; gap: 2px; }
  .rail .group > .eyebrow { margin: 0; padding: 0 8px 4px; }

  .nav {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    padding: 6px 8px;
    border-radius: var(--radius);
    text-align: left;
    font-size: var(--text-label);
    color: var(--ink-2);
  }
  .nav:hover { background: var(--panel-raised); color: var(--ink); }
  .nav[aria-current='page'] {
    background: var(--panel-raised);
    color: var(--ink);
    box-shadow: inset 2px 0 0 var(--series-1);
  }
  .nav .label { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .nav .state {
    margin-left: auto;
    font-size: var(--text-micro);
    color: var(--ink-muted);
    white-space: nowrap;
  }

  /* State on the rail, never colour alone: the word beside it says it too,
     and `title` names it. */
  .dot {
    flex: none;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    border: 1px solid var(--ink-muted);
  }
  .dot.on { background: var(--good); border-color: var(--good); }
  .dot.warn { background: var(--warning); border-color: var(--warning); }
  .dot.edit { background: var(--series-1); border-color: var(--series-1); }

  /* `.rail` in the selector, or `.note`'s margin, later in the file, wins. */
  .rail .footprint { margin-top: auto; padding: 8px 8px 0; border-top: 1px solid var(--rule); }

  .pane {
    display: flex;
    flex-direction: column;
    gap: 18px;
    padding: 16px 22px 0;
    overflow-y: auto;
    min-width: 0;
  }
  /* Room at the end, except where the save bar sits there. */
  .pane::after { content: ''; flex: none; height: 10px; }

  .pane-title {
    margin: 0;
    font-size: var(--text-title-sm);
    font-weight: 600;
    color: var(--ink);
  }

  /* A single column of controls stays at a readable width; Nodes, a grid
     of cards, takes the pane. */
  .pane > .stack:not(.wide) { max-width: 680px; }
  .savebar .grow { flex: 1; text-align: right; }

  /* Two columns where the width allows, one where it does not. */
  .cols {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 16px 24px;
    align-items: start;
  }

  .nodes {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
    gap: 10px;
    align-items: start;
  }

  details.rt > summary {
    cursor: pointer;
    list-style: none;
    display: flex;
    gap: 6px;
    align-items: baseline;
  }
  details.rt > summary::-webkit-details-marker { display: none; }
  details.rt > summary::before { content: '▸'; color: var(--ink-muted); }
  details.rt[open] > summary::before { content: '▾'; }
  summary .count {
    margin-left: auto;
    text-transform: none;
    letter-spacing: 0;
  }

  /* Save stays in view at the foot of the pane, however many nodes. */
  .savebar {
    position: sticky;
    bottom: 0;
    margin: 0 -22px;
    padding: 10px 22px;
    background: var(--panel);
    border-top: 1px solid var(--rule);
    align-items: center;
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
    background: var(--fill);
  }




  .node {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 10px 12px;
    border-radius: var(--radius);
    border: 1px solid var(--rule);
  }

  .node-head { display: flex; align-items: baseline; gap: 8px; font-size: var(--text-body); flex-wrap: wrap; }
  .node-head .in.id { flex: 1 1 110px; }
  .node-head .tag,
  .node-head .mini { white-space: nowrap; }

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

  /* The fleet tick sits with the node's fields, one size up from the router
     scrape checkbox: it is a decision about the node, not a detail of one
     endpoint. */
  .check.fleet { font-size: var(--text-label); color: var(--ink-2); gap: 6px; }
  .check.fleet input:disabled + span { color: var(--ink-muted); }

  .actions { display: flex; gap: 6px; margin-top: 4px; }
  .actions .mini { margin-left: 0; }
  .save { color: var(--ink); border-color: var(--ink-muted); }


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

  /* A label, its value, and sometimes a button, on one line. */
  .field {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: var(--text-label);
  }

  .field > .dim:first-child {
    flex: 0 0 64px;
  }

  .field .in {
    flex: 1;
    min-width: 0;
  }

  /* Indentation kept (YAML needs it), long lines wrapped so the key and the
     address show without scrolling sideways. */
  .yaml.snippet {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  .in.mono {
    font-family: var(--font-mono, ui-monospace, monospace);
  }



  /* A phone: full width, no drag, and the rail becomes a row of tabs over
     the pane. */
  @media (max-width: 640px) {
    .flyout { width: 100%; border-left: none; }
    .edge-drag { display: none; }
    .body { grid-template-columns: minmax(0, 1fr); grid-template-rows: auto minmax(0, 1fr); }
    .rail {
      flex-direction: row;
      gap: 4px;
      overflow-x: auto;
      border-right: none;
      border-bottom: 1px solid var(--rule);
    }
    .rail .group { flex-direction: row; }
    .rail .group > .eyebrow,
    .nav .state,
    .rail .footprint { display: none; }
    .nav { width: auto; white-space: nowrap; }
    .pane { padding: 14px 14px 0; }
    .savebar { margin: 0 -14px; padding: 10px 14px; }
    .nodes { grid-template-columns: minmax(0, 1fr); }
  }

  @media (prefers-reduced-motion: no-preference) {
    .flyout[open] { animation: slide-in 160ms ease-out; }
  }

  @keyframes slide-in {
    from { transform: translateX(12px); opacity: 0; }
    to { transform: translateX(0); opacity: 1; }
  }
</style>
