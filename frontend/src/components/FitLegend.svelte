<script lang="ts">
  /* Node chips while they fit on the header's row, a menu once they would not.
   *
   * Three nodes' chips take about 230px; each more takes 65-80. At eight
   * nodes the chips alone would push an Activity card's controls onto a
   * second row, and at sixteen onto a third (BR). The header stays one row:
   * when the chips cannot share it, `collapsed` (a PickMenu) takes their place.
   *
   * DECIDED ON THE CHIPS' OWN WIDTH, never on the current state. A hidden,
   * unwrapped copy of the chips is always rendered and measured, and the rest
   * of the header is measured as the sum of its items rather than as boxes
   * that wrap. So collapsing cannot change the answer that caused it, and the
   * two states cannot flip back and forth at the threshold.
   *
   * The chips are each card's own snippet, so each keeps its own styles. */
  import { onMount, type Snippet } from 'svelte';

  interface Props {
    chips: Snippet;
    collapsed: Snippet;
  }
  const { chips, collapsed }: Props = $props();

  /* Room left on the row before the chips collapse. The user's screen has
     wrapped a header that measured 17px to spare here (BQ4); this keeps a
     margin rather than fitting to the pixel. */
  const SLACK = 32;

  let host = $state<HTMLElement | null>(null);
  let measure = $state<HTMLElement | null>(null);
  let folded = $state(false);

  const px = (v: string) => parseFloat(v) || 0;
  const width = (el: Element) => el.getBoundingClientRect().width;

  /** What a flex row needs on one line: its items and the gaps between them.
   *  `swap` stands in for one item's width. */
  function rowNeed(row: Element, swap?: { el: Element; w: number }): number {
    const items = [...row.children].filter((c) => getComputedStyle(c).position !== 'absolute');
    if (!items.length) return width(row);
    const gap = px(getComputedStyle(row).columnGap);
    return items.reduce((n, c) => n + (swap && c === swap.el ? swap.w : width(c)), 0) + gap * (items.length - 1);
  }

  function decide() {
    const header = host?.closest('header');
    if (!host || !measure || !header) return;
    const cs = getComputedStyle(header);
    const room = header.clientWidth - px(cs.paddingLeft) - px(cs.paddingRight);
    const chipsW = width(measure);
    const parts = [...header.children];
    const need =
      parts.reduce((n, part) => {
        if (part.contains(host)) {
          // The header's item holding the legend, with the chips in it.
          const holder = host!.parentElement === part ? host! : [...part.children].find((c) => c.contains(host))!;
          return n + rowNeed(part, { el: holder, w: chipsW });
        }
        return n + rowNeed(part);
      }, 0) +
      px(cs.columnGap) * (parts.length - 1);
    folded = need + SLACK > room;
  }

  onMount(() => {
    const header = host?.closest('header');
    if (!header || !measure) return;
    const ro = new ResizeObserver(() => decide());
    ro.observe(header);
    ro.observe(measure);
    // The other items change width too: a view switch swaps the controls, a
    // count on a menu trigger grows by a digit.
    for (const part of header.children) {
      ro.observe(part);
      for (const item of part.children) if (!host?.contains(item)) ro.observe(item);
    }
    decide();
    return () => ro.disconnect();
  });
</script>

<span class="fit" bind:this={host}>
  <!-- The measuring copy: never seen, never focused, never announced. -->
  <span class="measure" aria-hidden="true" inert bind:this={measure}>{@render chips()}</span>
  {#if folded}
    {@render collapsed()}
  {:else}
    {@render chips()}
  {/if}
</span>

<style>
  .fit {
    position: relative;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
  }

  .measure {
    position: absolute;
    top: 0;
    left: 0;
    width: max-content;
    visibility: hidden;
    pointer-events: none;
  }
</style>
