<script lang="ts">
  /* The sort control inside a table header cell.
   *
   * Extracted the moment the second table needed it. The markup is small, but
   * it carries the interaction contract — descending first, an arrow only on
   * the active column, three states in the cycle — and four copies of that
   * would drift into four slightly different tables.
   *
   * It renders the BUTTON, not the `<th>`. The header cell stays with the
   * table so that column widths (`width: 1%` on numerics, an explicit width on
   * the share bar) keep working: Svelte scopes styles per component, so a `th`
   * emitted from here would be out of reach of the rules that size it.
   * `aria-sort` belongs on the cell for the same reason, and stays there.
   */
  import type { SortControl } from '../lib/table.svelte';

  interface Props {
    view: SortControl;
    /** Column id, matching the one given to TableView. */
    id: string;
    label: string;
  }
  const { view, id, label }: Props = $props();
</script>

<button class="sort" class:active={view.sortKey === id} onclick={() => view.toggle(id)}>
  {label}<span class="arrow" aria-hidden="true"
    >{view.sortKey === id ? (view.dir === 'asc' ? '▲' : '▼') : ''}</span>
</button>

<style>
  /* Kept looking like a header rather than growing borders and backgrounds:
     the affordance is the cursor and the arrow, and a row of chunky buttons
     would read as a toolbar sitting on the data. */
  .sort {
    font: inherit;
    color: inherit;
    letter-spacing: inherit;
    text-transform: inherit;
    padding: 0;
    display: inline-flex;
    align-items: center;
    gap: 3px;
    cursor: pointer;
  }

  .sort:hover,
  .sort.active {
    color: var(--ink);
  }

  .arrow {
    font-size: 8px;
  }
</style>
