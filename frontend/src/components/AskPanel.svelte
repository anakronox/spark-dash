<script lang="ts">
  /* Ask the dashboard (roadmap AY).
   *
   * A question about the cluster, answered by the model set in Settings from
   * the window's alerts, events and series. Read-only end to end: the model is
   * sent text and returns text.
   *
   * The answer is shown as text. Bold (**…**) and line breaks are kept, since
   * models write them; everything else is escaped first, so nothing a model
   * returns can become markup on this page.
   */
  import type { Answer, AssistantFeed } from '../lib/assistant.svelte';

  interface Props {
    feed: AssistantFeed;
    open: boolean;
    onclose: () => void;
  }
  const { feed, open, onclose }: Props = $props();

  const WINDOWS = [
    { label: '1h', minutes: 60 },
    { label: '6h', minutes: 360 },
    { label: '24h', minutes: 1440 },
  ];

  let dialog = $state<HTMLDialogElement | null>(null);
  let question = $state('');
  let minutes = $state(60);
  let busy = $state(false);
  let error = $state<string | null>(null);
  /* This session's questions, newest first. Not stored: an answer describes
     the cluster at one moment and goes stale with it. */
  let asked = $state<{ question: string; minutes: number; answer: Answer }[]>([]);

  $effect(() => {
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    else if (!open && dialog.open) dialog.close();
  });

  async function submit(e: Event) {
    e.preventDefault();
    const q = question.trim();
    if (!q || busy) return;
    busy = true;
    error = null;
    try {
      const answer = await feed.ask(q, minutes);
      asked = [{ question: q, minutes, answer }, ...asked];
      question = '';
    } catch (err) {
      error = (err as Error).message;
    } finally {
      busy = false;
    }
  }

  function escape(s: string): string {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  /** Escaped first, then bold put back. Nothing else becomes markup. */
  function render(s: string): string {
    return escape(s).replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  }

  const sentText = (a: Answer) =>
    `${a.model} · ${a.seconds}s · from ${a.sent.alert_episodes} alert episodes, ${a.sent.events} events, ` +
    `${a.sent.series} series and ${a.sent.nodes} nodes`;
</script>

<dialog
  bind:this={dialog}
  class="flyout"
  aria-label="Ask the dashboard"
  onclose={onclose}
  onclick={(e) => {
    if (e.target === dialog) onclose();
  }}
>
  <div class="panel">
    <header>
      <h2 class="eyebrow">Ask</h2>
      <button class="close" aria-label="Close" onclick={onclose}>×</button>
    </header>

    <form class="stack" onsubmit={submit}>
      <textarea
        id="ask-question"
        rows="3"
        maxlength="2000"
        placeholder="Why was sparky slow at 21:00?"
        aria-label="Your question"
        bind:value={question}
        onkeydown={(e) => {
          // Enter asks; Shift+Enter is a new line.
          if (e.key === 'Enter' && !e.shiftKey) submit(e);
        }}
      ></textarea>
      <div class="row">
        <div class="segmented" role="group" aria-label="Window">
          {#each WINDOWS as w (w.minutes)}
            <button
              type="button"
              class:active={minutes === w.minutes}
              aria-pressed={minutes === w.minutes}
              onclick={() => (minutes = w.minutes)}>{w.label}</button
            >
          {/each}
        </div>
        <button class="ask" type="submit" disabled={busy || !question.trim()}>
          {busy ? 'asking…' : 'ask'}
        </button>
      </div>
      <p class="note dim">
        {feed.status?.model ?? 'The model'} is sent this window's alerts, events and series, and the
        cluster as it stands. It reads; it cannot act.
      </p>
      {#if busy}
        <p class="note dim">A reasoning model can take most of a minute and a half.</p>
      {/if}
      {#if error}
        <p class="note" data-tone="warning">{error}</p>
      {/if}
    </form>

    {#each asked as a, i (asked.length - i)}
      <section class="qa">
        <p class="q">{a.question} <span class="dim">· last {WINDOWS.find((w) => w.minutes === a.minutes)?.label}</span></p>
        <!-- eslint-disable-next-line svelte/no-at-html-tags -- escaped in render() -->
        <div class="a">{@html render(a.answer.answer)}</div>
        <p class="note dim">{sentText(a.answer)}</p>
      </section>
    {/each}
  </div>
</dialog>

<style>
  .flyout {
    margin: 0 0 0 auto;
    height: 100%;
    max-height: 100%;
    width: min(680px, 100%);
    max-width: 100%;
    padding: 0;
    border: none;
    border-left: 1px solid var(--rule);
    background: var(--panel);
    color: var(--ink);
  }

  .flyout::backdrop {
    background: rgba(0, 0, 0, 0.45);
  }

  .panel {
    display: flex;
    flex-direction: column;
    gap: 18px;
    height: 100%;
    overflow-y: auto;
    padding: 18px 20px 28px;
  }

  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .close {
    border: none;
    background: none;
    color: var(--ink-muted);
    font-size: var(--text-title-sm);
    cursor: pointer;
  }

  .stack {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  textarea {
    width: 100%;
    resize: vertical;
    padding: 8px 10px;
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    background: var(--page, var(--panel));
    color: var(--ink);
    font: inherit;
    font-size: var(--text-body);
  }

  textarea:focus-visible {
    outline: 2px solid var(--accent, var(--ink-muted));
    outline-offset: 1px;
  }

  .row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }

  .ask {
    padding: 4px 14px;
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    background: var(--panel-raised, var(--panel));
    color: var(--ink);
    font: inherit;
    font-size: var(--text-label);
    cursor: pointer;
  }

  .ask:disabled {
    opacity: 0.5;
    cursor: default;
  }

  .note {
    margin: 0;
    font-size: var(--text-label);
  }

  .note[data-tone='warning'] {
    color: var(--warning);
  }

  .dim {
    color: var(--ink-muted);
  }

  .qa {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding-top: 12px;
    border-top: 1px solid var(--rule);
  }

  .q {
    margin: 0;
    font-weight: 600;
    font-size: var(--text-body);
  }

  .a {
    white-space: pre-wrap;
    font-size: var(--text-body);
    line-height: 1.55;
  }

  @media (max-width: 640px) {
    .flyout {
      width: 100%;
      border-left: none;
    }
  }

  @media (prefers-reduced-motion: no-preference) {
    .flyout[open] {
      animation: slide-in 160ms ease-out;
    }
  }

  @keyframes slide-in {
    from {
      transform: translateX(12px);
      opacity: 0;
    }
    to {
      transform: translateX(0);
      opacity: 1;
    }
  }
</style>
