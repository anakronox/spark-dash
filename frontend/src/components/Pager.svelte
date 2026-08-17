<script lang="ts">
  /* The page control under a capped list.
   *
   * Extracted alongside SortButton when the cap went from one table to four.
   * Like that component it carries decisions rather than markup: what the
   * label says, and when the control appears at all.
   */
  import type { PageControl } from '../lib/table.svelte';

  interface Props {
    view: PageControl;
    /** Rows BEFORE paging — the whole set, not the page. */
    total: number;
    /** Names the control for screen readers: "Models pages". */
    label: string;
  }
  const { view, total, label }: Props = $props();
</script>

<!-- Rendered only when it does something. Under a six-row table with a cap of
     ten it would be chrome insisting there is more to see when there is not —
     and with the cap set to "all" it must never appear, which falls out of the
     same test because nothing is ever greater than Infinity. -->
{#if total > view.pageSize}
  <nav class="pager" aria-label={label}>
    <!-- The RANGE, not the page number: the question is "how much am I not
         looking at", and "11–20 of 288" answers it where "page 2 of 29" makes
         you do arithmetic. -->
    <span class="dim">{view.range(total)}</span>
    <span class="controls">
      <button class="page" disabled={view.current(total) === 0} onclick={() => view.go(-1, total)}
        >prev</button
      >
      <button
        class="page"
        disabled={view.current(total) >= view.pageCount(total) - 1}
        onclick={() => view.go(1, total)}>next</button
      >
    </span>
  </nav>
{/if}

<style>
  .pager {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    font-size: 11px;
    padding: 8px 16px 0;
  }

  .controls {
    display: inline-flex;
    gap: 4px;
  }

  .page {
    font-size: 10px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    padding: 2px 8px;
    border-radius: var(--radius);
    border: 1px solid var(--rule);
    color: var(--ink-muted);
    cursor: pointer;
  }

  .page:hover:not(:disabled) {
    color: var(--ink);
    border-color: var(--ink-muted);
  }

  .page:disabled {
    opacity: 0.4;
    cursor: default;
  }
</style>
