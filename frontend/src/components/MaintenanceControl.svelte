<script lang="ts">
  /* Declare, or end, a maintenance window on one node or one cluster.
   *
   * THE ACTION LIVES ON THE THING ITSELF (roadmap AH6): on the node card and
   * on the cluster frame, not in settings. Maintenance is an alarm action
   * like silencing, and it belongs next to what it is about.
   *
   * Two states, deliberately asymmetric. Starting takes a reason and a
   * duration, because "why" and "how long" are the two things the next
   * reader — probably you, later — will want. Ending is one click with no
   * confirmation: it is the undo, and undoing must never be harder than
   * doing. The 24h cap is what makes a forgotten window survivable, and the
   * "end" here is the way out early (decision 2).
   *
   * Duration chips start the window immediately, like the silence chips do.
   * The 4h chip is marked as the default, and Enter in the reason field takes
   * it, so the common case is: type why, press Enter.
   */
  import {
    MAINTENANCE_DEFAULT_HOURS,
    SILENCE_DURATIONS,
    endMaintenance,
    startMaintenance,
    timeLeft,
  } from '../lib/alerts.svelte';
  import type { AlertFeed, MaintenanceWindow } from '../lib/alerts.svelte';

  interface Props {
    scope: 'node' | 'cluster';
    name: string;
    feed: AlertFeed;
    /** The window covering this scope right now, if any. For a node this
     *  may be a cluster-scope window — the control then names the cluster. */
    window: MaintenanceWindow | null;
    /** Where it sits. The notice line already says "maintenance"; the card
     *  and the cluster frame need the word because nothing else there does. */
    label?: boolean;
  }
  const { scope, name, feed, window: win, label = true }: Props = $props();

  let arming = $state(false);
  let reason = $state('');
  let busy = $state(false);
  let error = $state<string | null>(null);
  let input = $state<HTMLInputElement | null>(null);

  /* Time left ticks on its own; the feed only refreshes every 30s. */
  let now = $state(Date.now());
  $effect(() => {
    const t = setInterval(() => (now = Date.now()), 20_000);
    return () => clearInterval(t);
  });
  const left = $derived.by(() => {
    void now;
    return win ? timeLeft(win.ends_at) : '';
  });

  $effect(() => {
    if (arming) input?.focus();
  });

  async function start(hours: number) {
    busy = true;
    error = null;
    try {
      const created = await startMaintenance(scope, name, hours, reason.trim());
      /* Reflect the click at once. The feed's next poll confirms it. */
      feed.maintenance = [...feed.maintenance.filter((w) => w.id !== created.id), created];
      arming = false;
      reason = '';
      void feed.load();
    } catch (err) {
      error = (err as Error).message || 'could not start';
    } finally {
      busy = false;
    }
  }

  async function end() {
    if (!win) return;
    busy = true;
    error = null;
    try {
      await endMaintenance(win.id);
      feed.maintenance = feed.maintenance.filter((w) => w.id !== win.id);
      void feed.load();
    } catch (err) {
      error = (err as Error).message || 'could not end';
    } finally {
      busy = false;
    }
  }

  /* A node under a CLUSTER window says so: "end" on this card ends the
     window for every member, and the reader should know that before
     clicking. */
  const via = $derived(win && win.scope !== scope ? win.name : null);

  /* Small controls in the card's label size. Muted ink until hovered, like
     the silence buttons in the history fly-out: present, never loud. The
     active state is muted too — this is a state you chose, not a problem, so
     it must not borrow a status colour. */
  const MINI =
    'text-micro tracking-[0.04em] px-[6px] py-px rounded-sm border border-rule ' +
    'text-ink-muted hover:text-ink disabled:opacity-50 disabled:cursor-default';
  const DEFAULT_CHIP = 'text-ink border-ink-muted';
  const ACTIVE = 'inline-flex items-baseline gap-[6px] text-label text-ink-muted whitespace-nowrap';
  const FORM = 'inline-flex items-baseline gap-[4px] text-label';
  const REASON =
    'w-[15ch] min-w-0 px-[6px] py-px text-label bg-transparent border-b border-rule ' +
    'text-ink placeholder:text-ink-muted focus:outline-none focus:border-ink-muted';
  const ERROR = 'text-micro text-warning';
</script>

{#if win}
  <span class={ACTIVE} data-testid="maintenance-active">
    {#if label}
      <span class="text-nano leading-none" aria-hidden="true">◌</span>
      <span>maintenance</span>
    {/if}
    {#if via}<span>({via})</span>{/if}
    {#if win.reason}<span class="text-ink-2">{win.reason}</span>{/if}
    <span>{left}</span>
    <button class={MINI} disabled={busy} onclick={end} title="End this window now">end</button>
  </span>
{:else if arming}
  <form
    class={FORM}
    onsubmit={(e) => {
      e.preventDefault();
      start(MAINTENANCE_DEFAULT_HOURS);
    }}
  >
    <input
      class={REASON}
      bind:this={input}
      bind:value={reason}
      maxlength="200"
      placeholder="why (optional)"
      aria-label="Reason for maintenance"
      onkeydown={(e) => {
        if (e.key === 'Escape') arming = false;
      }}
    />
    {#each SILENCE_DURATIONS as d (d.hours)}
      <button
        type="button"
        class="{MINI} {d.hours === MAINTENANCE_DEFAULT_HOURS ? DEFAULT_CHIP : ''}"
        disabled={busy}
        title={d.hours === MAINTENANCE_DEFAULT_HOURS
          ? `Start a ${d.label} window (default)`
          : `Start a ${d.label} window`}
        onclick={() => start(d.hours)}
      >
        {d.label}
      </button>
    {/each}
    <button type="button" class={MINI} aria-label="Cancel" onclick={() => (arming = false)}>×</button>
  </form>
{:else}
  <button
    class={MINI}
    title={scope === 'cluster'
      ? `Mute alerts for every node in ${name} while you work`
      : `Mute alerts for ${name} while you work`}
    onclick={() => (arming = true)}
  >
    maintenance
  </button>
{/if}
{#if error}
  <span class={ERROR} role="alert">{error}</span>
{/if}
