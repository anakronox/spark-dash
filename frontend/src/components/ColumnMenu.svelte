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
  /* THE MENU ITSELF LIVES IN PickMenu since the metric picker needed one too.
     This is the adapter: it turns column views into a list of items, and keeps
     the two facts that are specific to columns -- a required column is locked
     and says "always", and a column forced visible because it is the only one
     left says so in warning colour. */
  import type { ColumnView } from '../lib/columns.svelte';
  import PickMenu from './PickMenu.svelte';
  import type { PickGroup } from './PickMenu.svelte';

  interface Group {
    label?: string;
    view: ColumnView;
  }
  interface Props {
    groups: Group[];
    /** The card, for the accessible name. */
    of: string;
  }
  const { groups, of: cardName }: Props = $props();

  const hiddenCount = $derived(groups.reduce((n, g) => n + g.view.hiddenCount, 0));

  const items = $derived<PickGroup[]>(
    groups.map((g) => ({
      label: g.label,
      items: g.view.columns.map((col) => {
        const forced = g.view.isForced(col.key);
        return {
          key: col.key,
          label: col.label,
          checked: !g.view.isOff(col.key),
          disabled: col.required,
          note: col.required ? 'always' : forced ? 'shown — not zero' : undefined,
          warn: !col.required && forced,
        };
      }),
    })),
  );

  /* Toggling needs the view the key belongs to; keys are unique across a
     card's tables, so the first view that has the column is the one. */
  function toggle(key: string) {
    for (const g of groups) {
      if (g.view.columns.some((c) => c.key === key)) {
        g.view.toggle(key);
        return;
      }
    }
  }
</script>

<PickMenu
  groups={items}
  ontoggle={toggle}
  what="Columns"
  of={cardName}
  count={hiddenCount}
  countLabel="hidden"
  icon="columns"
/>
