<script lang="ts">
  /* The per-card control for switching columns off.
   *
   * A COLUMNS GLYPH, NOT A FUNNEL. A funnel conventionally means filtering
   * ROWS, which is a separate feature this dashboard still wants; spending the
   * icon on columns would leave that one without its obvious affordance.
   *
   * Hover-revealed like the drag handle, because a page meant to read as an
   * instrument panel should not carry a permanent row of chrome. But it STAYS
   * visible whenever a column is switched off: a missing column with no visible
   * cause reads as the backend having broken, and the control is the only thing
   * on the page that explains it.
   *
   * Takes a LIST of views so one card with two tables — Network — gets one
   * button and one menu with labelled groups, rather than two controls
   * competing for the same corner.
   */
  import type { ColumnView } from '../lib/columns.svelte';

  interface Group {
    /** Omitted when the card has only one table, where a heading would be
     *  restating the card's own title. */
    label?: string;
    view: ColumnView;
  }

  interface Props {
    groups: Group[];
    /** Card name, for the button's accessible name. */
    of: string;
  }
  const { groups, of: cardName }: Props = $props();

  let open = $state(false);
  let host = $state<HTMLElement | null>(null);

  const hiddenCount = $derived(groups.reduce((n, g) => n + g.view.hiddenCount, 0));

  function close() {
    open = false;
  }

  /* Dismissal is on the WINDOW rather than a backdrop element. This is a small
     popover inside a table header, and a full-page backdrop would sit over the
     dashboard while it is open — this panel is meant to be glanced at and
     dismissed, not modal like the settings fly-out. */
  $effect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      if (!host) return;
      /* Anything that is not a node inside this menu counts as outside.
         Written as an explicit instanceof rather than a cast because
         `contains()` given a non-Node is not reliably falsy, and the failure
         mode is a menu that will not close. */
      if (e.target instanceof Node && host.contains(e.target)) return;
      close();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        close();
        // Focus returns to the button, or it lands on <body> and the next Tab
        // starts from the top of the page.
        host?.querySelector<HTMLButtonElement>('.trigger')?.focus();
      }
    };
    window.addEventListener('pointerdown', onDown);
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('pointerdown', onDown);
      window.removeEventListener('keydown', onKey);
    };
  });
</script>

<span class="host" class:active={hiddenCount > 0} bind:this={host}>
  <button
    class="trigger"
    aria-expanded={open}
    aria-haspopup="true"
    aria-label={hiddenCount
      ? `Columns for ${cardName}. ${hiddenCount} hidden.`
      : `Columns for ${cardName}`}
    title="Choose columns"
    onclick={() => (open = !open)}
  >
    <!-- Three bars: the conventional "columns" mark, and it reads as a table
         rather than as a generic menu. -->
    <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
      <rect x="0.5" y="1" width="2.6" height="10" rx="0.7" />
      <rect x="4.7" y="1" width="2.6" height="10" rx="0.7" />
      <rect x="8.9" y="1" width="2.6" height="10" rx="0.7" />
    </svg>
    {#if hiddenCount}
      <!-- The count, not just a dot: "why is rx missing" is answered faster by
           "3 hidden" than by a mark that only says something is. -->
      <span class="badge num">{hiddenCount}</span>
    {/if}
  </button>

  {#if open}
    <div class="menu" role="group" aria-label={`Columns for ${cardName}`}>
      {#each groups as group, gi (gi)}
        {#if group.label}
          <p class="eyebrow dim group">{group.label}</p>
        {/if}
        {#each group.view.columns as col (col.key)}
          {@const forced = group.view.isForced(col.key)}
          <label class="row" class:locked={col.required}>
            <input
              type="checkbox"
              checked={!group.view.isOff(col.key)}
              disabled={col.required}
              onchange={() => group.view.toggle(col.key)}
            />
            <span class="name">{col.label}</span>
            {#if col.required}
              <!-- Named rather than silently disabled, so a control that cannot
                   be moved says why it cannot be moved. -->
              <span class="note dim">always</span>
            {:else if forced}
              <span class="note warn">shown — not zero</span>
            {/if}
          </label>
        {/each}
      {/each}
    </div>
  {/if}
</span>

<style>
  .host {
    position: relative;
    display: inline-flex;
    align-items: center;
  }

  .trigger {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 4px;
    border-radius: var(--radius);
    color: var(--ink-muted);
    cursor: pointer;
    /* Hidden until wanted — see the component comment. */
    opacity: 0;
    transition: opacity 120ms ease, color 120ms ease;
  }

  .trigger svg {
    display: block;
    fill: currentColor;
  }

  /* Revealed on hover of the whole card, not just of the button: a control you
     have to find before it appears is not a control. */
  :global(section.panel:hover) .trigger,
  .trigger:focus-visible,
  .host.active .trigger {
    opacity: 1;
  }

  .trigger:hover {
    color: var(--ink);
  }

  .host.active .trigger {
    color: var(--ink-2);
  }

  .badge {
    font-size: var(--text-nano);
    line-height: 1;
  }

  .menu {
    position: absolute;
    top: calc(100% + 6px);
    /* Anchored to the right edge because the button sits in the card's
       top-right; opening leftward keeps it inside the card at any width. */
    right: 0;
    z-index: 20;
    min-width: 168px;
    padding: 8px;
    display: flex;
    flex-direction: column;
    gap: 1px;
    background: var(--panel-raised);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    box-shadow: 0 8px 24px rgb(0 0 0 / 0.35);
  }

  .group {
    margin: 6px 0 3px;
    padding: 0 4px;
    font-size: var(--text-nano);
  }

  .group:first-child {
    margin-top: 0;
  }

  .row {
    display: flex;
    align-items: center;
    gap: 7px;
    padding: 3px 4px;
    border-radius: var(--radius);
    font-size: var(--text-label);
    cursor: pointer;
    white-space: nowrap;
  }

  .row:hover {
    background: var(--panel);
  }

  .row.locked {
    cursor: default;
  }

  .row input {
    margin: 0;
    cursor: inherit;
  }

  .name {
    flex: 1;
  }

  .note {
    font-size: var(--text-nano);
    letter-spacing: 0.04em;
  }

  .warn {
    color: var(--warning);
  }
</style>
