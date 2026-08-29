<script lang="ts">
  /* A fly-out of checkboxes behind one small trigger.
   *
   * EXTRACTED FROM ColumnMenu when the System Activity card needed the same
   * thing for its metrics. Twenty toggle chips took three rows of the card --
   * about 130px, measured -- which at the default plot height is a whole row
   * of charts. The chips' one real virtue was that OFF was as visible as ON;
   * a checkbox list keeps that and costs one line.
   *
   * DATA IN, NOT MARKUP IN. The rows are rendered here from a list rather than
   * passed as a snippet, because scoped styles do not reach snippet content
   * rendered by the caller: every caller would carry its own copy of the row
   * CSS, and the two menus on the page would drift apart. Callers describe
   * their items -- checked, disabled, a note -- and this draws them the one way.
   *
   * NOT MODAL, and no backdrop. This is glanced at and dismissed, not the
   * settings fly-out: it closes on a pointerdown anywhere outside it and on
   * Escape, which also returns focus to the trigger so the keyboard does not
   * lose its place.
   */
  export interface PickItem {
    key: string;
    label: string;
    checked: boolean;
    /** Cannot be changed -- always shown, or the last one standing. */
    disabled?: boolean;
    /** Why, when it cannot be changed, or anything else worth a word. */
    note?: string;
    /** The note is a warning rather than an explanation. */
    warn?: boolean;
  }
  export interface PickGroup {
    label?: string;
    items: PickItem[];
  }

  interface Props {
    groups: PickGroup[];
    ontoggle: (key: string) => void;
    /** What the menu chooses, for the accessible name: "Columns", "Metrics". */
    what: string;
    /** Whose: the card, so two menus on one page read differently. */
    of: string;
    /** Number on the trigger, or 0 for none. ColumnMenu counts hidden columns;
     *  the metric picker counts what is shown. The caller decides what the
     *  number means and says so in `countLabel`. */
    count?: number;
    /** Spoken after the count: "hidden", "shown". */
    countLabel?: string;
    /** Text on the trigger. With none the trigger is an icon that appears on
     *  hover, which suits a secondary control like the column picker; a primary
     *  one -- the only way to choose what a card draws -- must always be
     *  visible and say what it is. */
    text?: string;
    /** The trigger's mark. `columns` is three bars; `list` is three lines with
     *  boxes, the conventional checklist. */
    icon?: 'columns' | 'list' | 'plus';
    /** `check`: rows are checkboxes and `ontoggle` flips one. `action`: rows
     *  are buttons and `ontoggle` performs one -- the menu closes after. */
    mode?: 'check' | 'action';
    /** `accent`: the trigger is filled in the theme's accent, for the one
     *  control on a bar of outlines that is THE action. */
    tone?: 'plain' | 'accent';
  }
  const {
    groups,
    ontoggle,
    what,
    of: cardName,
    count = 0,
    countLabel = '',
    text,
    icon = 'columns',
    mode = 'check',
    tone = 'plain',
  }: Props = $props();

  let open = $state(false);
  let host = $state<HTMLElement | null>(null);

  const name = $derived(
    count ? `${what} for ${cardName}. ${count} ${countLabel}.` : `${what} for ${cardName}`,
  );

  function close() {
    open = false;
  }

  $effect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      if (!host) return;
      /* Written as an explicit instanceof rather than a cast because
         `contains()` given a non-Node is not reliably falsy, and the failure
         mode is a menu that will not close. */
      if (e.target instanceof Node && host.contains(e.target)) return;
      close();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        close();
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

<span
  class="host"
  class:active={count > 0}
  class:labelled={!!text}
  class:accent={tone === 'accent'}
  bind:this={host}
>
  <button
    class="trigger"
    aria-expanded={open}
    aria-haspopup="true"
    aria-label={name}
    title={`Choose ${what.toLowerCase()}`}
    onclick={() => (open = !open)}
  >
    {#if icon === 'columns'}
      <!-- Three bars: the conventional "columns" mark, and it reads as a table
           rather than as a generic menu. -->
      <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
        <rect x="0.5" y="1" width="2.6" height="10" rx="0.7" />
        <rect x="4.7" y="1" width="2.6" height="10" rx="0.7" />
        <rect x="8.9" y="1" width="2.6" height="10" rx="0.7" />
      </svg>
    {:else if icon === 'plus'}
      <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
        <rect x="5" y="1" width="2" height="10" rx="0.6" />
        <rect x="1" y="5" width="10" height="2" rx="0.6" />
      </svg>
    {:else}
      <!-- Three lines with boxes: a checklist, which is what opens. -->
      <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
        <rect x="0.5" y="0.5" width="3" height="3" rx="0.6" />
        <rect x="0.5" y="4.5" width="3" height="3" rx="0.6" />
        <rect x="0.5" y="8.5" width="3" height="3" rx="0.6" />
        <rect x="5" y="1.2" width="6.5" height="1.6" rx="0.6" />
        <rect x="5" y="5.2" width="6.5" height="1.6" rx="0.6" />
        <rect x="5" y="9.2" width="6.5" height="1.6" rx="0.6" />
      </svg>
    {/if}
    {#if text}
      <span class="text">{text}</span>
    {/if}
    {#if count}
      <!-- The count, not just a dot: "why is rx missing" is answered faster by
           "3 hidden" than by a mark that only says something is. -->
      <span class="badge num">{count}</span>
    {/if}
  </button>

  {#if open}
    <div class="menu" role="group" aria-label={name}>
      {#each groups as group, gi (gi)}
        {#if group.label}
          <p class="eyebrow dim group">{group.label}</p>
        {/if}
        {#each group.items as item (item.key)}
          {#if mode === 'action'}
            <!-- An action, not a state: a button, and the menu closes once it
                 has done its one thing. -->
            <button
              class="row"
              disabled={item.disabled}
              onclick={() => {
                ontoggle(item.key);
                close();
              }}
            >
              <span class="name">{item.label}</span>
              {#if item.note}
                <span class="note" class:warn={item.warn} class:dim={!item.warn}>{item.note}</span>
              {/if}
            </button>
          {:else}
          <label class="row" class:locked={item.disabled}>
            <input
              type="checkbox"
              checked={item.checked}
              disabled={item.disabled}
              onchange={() => ontoggle(item.key)}
            />
            <span class="name">{item.label}</span>
            {#if item.note}
              <!-- Named rather than silently disabled, so a control that cannot
                   be moved says why it cannot be moved. -->
              <span class="note" class:warn={item.warn} class:dim={!item.warn}>{item.note}</span>
            {/if}
          </label>
          {/if}
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
    /* Hidden until wanted, like every secondary control on a card: a menu you
       have to find before it appears is not a control, but a permanent icon on
       every table is noise on an instrument panel. */
    opacity: 0;
    transition: opacity 120ms ease, color 120ms ease;
  }

  .trigger svg {
    display: block;
    fill: currentColor;
  }

  /* A LABELLED trigger is a primary control and is always there. The metric
     picker is the only way to choose what System Activity draws; a control
     that only appears on hover would be a card that looks like it has no
     control at all. */
  .host.labelled .trigger {
    opacity: 1;
    padding: 3px 8px;
    border: 1px solid var(--rule);
    font-size: var(--text-label);
  }

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

  .host.labelled .trigger:hover {
    border-color: var(--ink-muted);
  }

  /* THE ACTION on a bar of outlines. Filled in the theme's accent -- the same
     green the resize corners light up with -- so it reads as the one thing
     here that does something rather than shows something, without shouting.
     Inherits each theme's accent, so it holds up in Forest, Paper and High
     Contrast alike. */
  .host.accent .trigger {
    background: var(--good);
    border-color: var(--good);
    color: var(--page);
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    font-size: var(--text-micro);
  }

  .host.accent .trigger:hover {
    color: var(--page);
    filter: brightness(1.12);
  }

  /* An action row is a button, styled as the checkbox rows are and with the
     same text alignment: a button centres by default. */
  button.row {
    width: 100%;
    text-align: left;
    color: inherit;
    font: inherit;
  }

  button.row:disabled {
    opacity: 0.5;
    cursor: default;
  }

  .badge {
    font-size: var(--text-nano);
    line-height: 1;
  }

  .menu {
    position: absolute;
    top: calc(100% + 6px);
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

  /* An icon trigger sits in the card's top-right, so its menu opens leftward
     to stay inside the card; a labelled one sits at the left of the title row
     and opens rightward for the same reason. */
  .host:not(.labelled) .menu {
    right: 0;
  }

  .host.labelled .menu {
    left: 0;
    /* Twenty metrics is a long single column; two columns keep the menu
       inside the viewport on a laptop.
       `max-content` tracks, NOT minmax(0, 1fr): an absolutely positioned box
       shrinks to fit, and fr tracks inside one resolved to nothing -- the menu
       measured 90px wide with every label clipped. The rule that fr must never
       have an auto minimum is about tracks that share a KNOWN width; these do
       not, and their content is the only thing that can size them. */
    display: grid;
    grid-template-columns: repeat(2, max-content);
    column-gap: 12px;
    min-width: 0;
    width: max-content;
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
