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

<!-- SPIKE NOTE (roadmap AB). `font: inherit; color: inherit; letter-spacing:
     inherit; text-transform: inherit` was four declarations saying one thing:
     this button must look like the header it sits in, whatever the header
     looks like. Tailwind has no "inherit everything typographic" utility, so
     it becomes four arbitrary values — longer, and the intent that the comment
     above them carried is now only in the comment.

     `hover:text-ink` and the active state are the opposite case: cleaner here
     than as two selectors sharing a rule. -->
<button
  class="[font:inherit] [color:inherit] [letter-spacing:inherit] [text-transform:inherit]
         inline-flex cursor-pointer items-center gap-[3px] p-0 hover:text-ink
         {view.sortKey === id ? 'text-ink' : ''}"
  onclick={() => view.toggle(id)}
>
  {label}<span class="text-[8px]" aria-hidden="true"
    >{view.sortKey === id ? (view.dir === 'asc' ? '▲' : '▼') : ''}</span>
</button>
