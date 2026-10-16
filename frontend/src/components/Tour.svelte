<script lang="ts">
  /* A short tour of the page (roadmap BE3).
   *
   * Tips pinned to the controls a reader would not find by looking: the card
   * corner, the move grip, `+ element`, a card's views and pickers, Ask,
   * alerts, Settings. Each step points at the real control on the page, so a
   * step whose control is not there (no Ask until it is set up) is skipped
   * rather than pointing at nothing. Once, and skippable; replayed from the
   * setup guide or Settings.
   */
  import { tick, untrack } from 'svelte';

  interface Props {
    open: boolean;
    onclose: () => void;
  }
  const { open, onclose }: Props = $props();

  interface Step {
    /** The first match is the one pointed at. */
    selector: string;
    title: string;
    text: string;
  }

  const STEPS: Step[] = [
    {
      selector: '[data-slot] .grip',
      title: 'Resize from the corner',
      text: 'Drag a card’s corner down for more rows or taller charts, and sideways to make it full width or half. The arrow keys work too.',
    },
    {
      selector: '[data-slot] .handle',
      title: 'Move a card',
      text: 'Drag the dots to move a card, or to set it beside another. Two half-width cards side by side end level.',
    },
    {
      selector: '[aria-label^="Add an element"]',
      title: 'A second copy of any card',
      text: '+ element adds another copy of a card, each with its own settings: one System Activity on charts and one on gauges, say.',
    },
    {
      selector: '[role="group"][aria-label="View"]',
      title: 'Charts or gauges',
      text: 'Activity and temperature cards switch between how things moved over a window and a live dial per node.',
    },
    {
      selector: '[data-slot] .trigger[aria-haspopup="true"]',
      title: 'Choose what a card shows',
      text: 'Each card’s menu picks its metrics, interface groups or sensor groups. Hidden ones are still alerted on.',
    },
    {
      selector: '[aria-label="Ask the dashboard"]',
      title: 'Ask',
      text: 'Questions about the cluster’s history, answered by your own model from what the dashboard holds.',
    },
    {
      selector: '[aria-label*="lerts and history"]',
      title: 'Alerts and their history',
      text: 'Quiet until something fires. Every episode is kept, with silences and maintenance windows beside them.',
    },
    {
      selector: '[aria-label="Open settings"]',
      title: 'Settings',
      text: 'Nodes, integrations and appearance, one pane each. The setup guide is there too, under Appearance.',
    },
  ];

  let steps = $state<Step[]>([]);
  let at = $state(0);
  let box = $state<{ top: number; left: number; width: number; height: number } | null>(null);

  function visible(el: Element | null): el is HTMLElement {
    if (!(el instanceof HTMLElement)) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }

  async function place() {
    const step = steps[at];
    const el = step ? document.querySelector(step.selector) : null;
    if (!visible(el)) {
      box = null;
      return;
    }
    el.scrollIntoView({ block: 'center', behavior: 'instant' as ScrollBehavior });
    await tick();
    const r = el.getBoundingClientRect();
    box = { top: r.top, left: r.left, width: r.width, height: r.height };
  }

  $effect(() => {
    if (!open) return;
    /* Untracked: this writes `steps` and `at` and reads them back through
       place(), and tracking them re-ran it on its own writes and lost the
       placement, leaving the scrim and no tip. Only `open` starts it. */
    untrack(() => {
      // Only the steps whose control is on the page now.
      steps = STEPS.filter((s) => visible(document.querySelector(s.selector)));
      at = 0;
      place();
    });
    const onResize = () => place();
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  });

  function go(d: 1 | -1) {
    const next = at + d;
    if (next >= steps.length) return finish();
    if (next < 0) return;
    at = next;
    place();
  }

  function finish() {
    box = null;
    onclose();
  }

  function onkey(e: KeyboardEvent) {
    if (!open) return;
    if (e.key === 'Escape') finish();
    else if (e.key === 'ArrowRight' || e.key === 'Enter') go(1);
    else if (e.key === 'ArrowLeft') go(-1);
    else return;
    e.preventDefault();
  }

  /* The tip sits below the control, or above it near the bottom of the
     window, and inside the window's width. */
  const TIP_W = 300;
  const tip = $derived.by(() => {
    if (!box) return null;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const below = box.top + box.height + 12;
    const top = below + 150 > vh ? Math.max(12, box.top - 162) : below;
    const left = Math.max(12, Math.min(box.left + box.width / 2 - TIP_W / 2, vw - TIP_W - 12));
    return { top, left };
  });
</script>

<svelte:window onkeydown={onkey} />

{#if open && steps.length}
  <div class="scrim" aria-hidden="true" onclick={finish}></div>
  {#if box}
    <div
      class="spot"
      aria-hidden="true"
      style:top="{box.top - 4}px"
      style:left="{box.left - 4}px"
      style:width="{box.width + 8}px"
      style:height="{box.height + 8}px"
    ></div>
  {/if}
  {#if tip}
    <div class="tip" role="dialog" aria-label="Tour" style:top="{tip.top}px" style:left="{tip.left}px">
      <p class="count">{at + 1} of {steps.length}</p>
      <h3>{steps[at].title}</h3>
      <p class="text">{steps[at].text}</p>
      <div class="row">
        <button class="link" onclick={finish}>skip</button>
        <span class="rest"></span>
        {#if at > 0}<button class="btn" onclick={() => go(-1)}>back</button>{/if}
        <button class="btn primary" onclick={() => go(1)}>{at === steps.length - 1 ? 'done' : 'next'}</button>
      </div>
    </div>
  {/if}
{/if}

<style>
  .scrim {
    position: fixed;
    inset: 0;
    z-index: 900;
    background: rgb(0 0 0 / 0.35);
  }

  .spot {
    position: fixed;
    z-index: 901;
    border: 2px solid var(--good);
    border-radius: var(--radius);
    box-shadow: 0 0 0 4px color-mix(in srgb, var(--good) 25%, transparent);
    pointer-events: none;
  }

  .tip {
    position: fixed;
    z-index: 902;
    width: min(300px, calc(100vw - 24px));
    padding: 12px 14px;
    background: var(--panel);
    color: var(--ink);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    box-shadow: 0 8px 24px rgb(0 0 0 / 0.35);
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .count {
    margin: 0;
    font-size: var(--text-micro);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-muted);
  }

  h3 {
    margin: 0;
    font-size: var(--text-body);
    font-weight: 600;
  }

  .text {
    margin: 0;
    font-size: var(--text-label);
    line-height: 1.5;
    color: var(--ink-2);
  }

  .row {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-top: 4px;
  }

  .rest {
    flex: 1;
  }

  .link {
    background: none;
    border: none;
    color: var(--ink-muted);
    font: inherit;
    font-size: var(--text-label);
    cursor: pointer;
    padding: 0;
  }

  .btn {
    font: inherit;
    font-size: var(--text-label);
    padding: 3px 10px;
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    background: var(--panel-raised);
    color: var(--ink-2);
    cursor: pointer;
  }

  .btn.primary {
    color: var(--ink);
    border-color: var(--good);
  }

  @media (prefers-reduced-motion: no-preference) {
    .spot {
      transition: top 200ms ease, left 200ms ease, width 200ms ease, height 200ms ease;
    }
  }
</style>
