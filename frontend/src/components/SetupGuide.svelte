<script lang="ts">
  /* Get started (roadmap BE1, BE2).
   *
   * What to set up, required first, then recommended, then optional. Each
   * step's state comes from the backend, which checks the thing itself (the
   * agents answer, Prometheus is scraping, an alert would reach someone), so
   * a step reads done because it works, not because it was clicked. Each one
   * says why it matters and goes straight to where it is set.
   */
  import type { Level, SetupFeed, SetupItem } from '../lib/setup.svelte';
  import PresetPicker from './PresetPicker.svelte';

  interface Props {
    feed: SetupFeed;
    open: boolean;
    onclose: () => void;
    /** Open Settings on this pane. */
    onsettings: (pane: string) => void;
    ontour: () => void;
    /** The page differs from the preset it was set from. */
    customised: boolean;
    /** Whether this opens with the dashboard. */
    showOnStart: boolean;
    onshowonstart: (on: boolean) => void;
  }
  const { feed, open, onclose, onsettings, ontour, customised, showOnStart, onshowonstart }: Props = $props();

  let dialog = $state<HTMLDialogElement | null>(null);
  $effect(() => {
    if (!dialog) return;
    if (open && !dialog.open) {
      dialog.showModal();
      feed.load();
    } else if (!open && dialog.open) dialog.close();
  });

  const LEVELS: { id: Level; label: string; note: string }[] = [
    { id: 'required', label: 'Required', note: 'Without these the dashboard is blind or silent.' },
    { id: 'recommended', label: 'Recommended', note: 'Correct readings and less to maintain.' },
    { id: 'optional', label: 'Optional', note: 'Features to switch on if you want them.' },
  ];

  /* State in form and word, never colour alone. */
  const MARK: Record<SetupItem['state'], { glyph: string; word: string }> = {
    done: { glyph: '✓', word: 'done' },
    attention: { glyph: '▲', word: 'needs attention' },
    todo: { glyph: '○', word: 'not set up' },
    info: { glyph: 'i', word: 'tip' },
  };

  const items = (level: Level) => (feed.status?.items ?? []).filter((i) => i.level === level);
  const allRequired = $derived(feed.status != null && feed.missing === 0);
</script>

<dialog
  bind:this={dialog}
  class="flyout"
  aria-label="Get started"
  onclose={onclose}
  onclick={(e) => {
    if (e.target === dialog) onclose();
  }}
>
  <div class="panel">
    <header>
      <h2 class="eyebrow">Get started</h2>
      <button class="close" aria-label="Close" onclick={onclose}>×</button>
    </header>

    <p class="lede">
      {#if !feed.status}
        Checking what is set up…
      {:else if allRequired}
        Everything required is working. The rest is optional, in the order most installs want it.
      {:else}
        <span class="num">{feed.status.required_done} of {feed.status.required_total}</span> required steps
        working. Start at the top: each step says why it matters and where it is set.
      {/if}
    </p>

    <section class="level">
      <h3 class="eyebrow dim">Layout <span class="why">· a starting arrangement; change it later in Settings → Appearance.</span></h3>
      <PresetPicker {customised} />
    </section>

    {#each LEVELS as level (level.id)}
      {#if items(level.id).length}
        <section class="level">
          <h3 class="eyebrow dim">{level.label} <span class="why">· {level.note}</span></h3>
          <ol>
            {#each items(level.id) as it (it.id)}
              <li class="item" data-state={it.state}>
                <span class="mark" aria-hidden="true">{MARK[it.state].glyph}</span>
                <div class="body">
                  <div class="title">
                    {it.title}
                    <span class="state">{MARK[it.state].word}</span>
                  </div>
                  <p class="detail">{it.detail}</p>
                  {#if it.pane || (it.doc && it.state !== 'done')}
                    <div class="actions">
                      {#if it.pane}
                        <button class="mini" onclick={() => onsettings(it.pane!)}
                          >{it.state === 'done' ? 'open' : 'set up'} in Settings</button
                        >
                      {/if}
                      {#if it.doc && it.state !== 'done'}
                        <a class="mini" href={it.doc} target="_blank" rel="noopener">guide ↗</a>
                      {/if}
                    </div>
                  {/if}
                </div>
              </li>
            {/each}
          </ol>
        </section>
      {/if}
    {/each}

    <footer>
      <button class="mini tour" onclick={ontour}>Take the tour</button>
      <span class="hint">a minute: the controls that are easy to miss</span>
      <label class="check">
        <input
          type="checkbox"
          checked={showOnStart}
          onchange={(e) => onshowonstart((e.currentTarget as HTMLInputElement).checked)}
        />
        <span>show when the dashboard opens</span>
      </label>
    </footer>
  </div>
</dialog>

<style>
  .flyout {
    margin: 0 0 0 auto;
    height: 100%;
    max-height: 100%;
    width: min(560px, 100%);
    max-width: 100%;
    padding: 0;
    border: none;
    border-left: 1px solid var(--rule);
    background: var(--panel);
    color: var(--ink);
    font-size: var(--text-body);
  }

  .flyout::backdrop {
    background: rgb(0 0 0 / 0.45);
  }

  .panel {
    display: flex;
    flex-direction: column;
    gap: 16px;
    height: 100%;
    overflow-y: auto;
    padding: 18px 20px 24px;
  }

  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  h2 {
    margin: 0;
  }

  .close {
    border: none;
    background: none;
    color: var(--ink-muted);
    font-size: var(--text-title-sm);
    cursor: pointer;
  }

  .lede {
    margin: 0;
    color: var(--ink-2);
    line-height: 1.5;
  }

  .level h3 {
    margin: 0 0 6px;
  }

  .why {
    text-transform: none;
    letter-spacing: 0;
  }

  ol {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .item {
    display: grid;
    grid-template-columns: 18px minmax(0, 1fr);
    gap: 8px;
    padding: 8px 10px;
    border: 1px solid var(--rule);
    border-radius: var(--radius);
  }

  .mark {
    font-weight: 700;
    color: var(--ink-muted);
    text-align: center;
  }

  .item[data-state='done'] .mark,
  .item[data-state='done'] .state {
    color: var(--good);
  }

  .item[data-state='attention'] {
    border-color: var(--warning);
  }

  .item[data-state='attention'] .mark,
  .item[data-state='attention'] .state {
    color: var(--warning);
  }

  .body {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
  }

  .title {
    color: var(--ink);
    font-weight: 600;
    display: flex;
    gap: 8px;
    align-items: baseline;
    flex-wrap: wrap;
  }

  .state {
    font-weight: 400;
    font-size: var(--text-micro);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-muted);
  }

  .detail {
    margin: 0;
    color: var(--ink-muted);
    font-size: var(--text-label);
    line-height: 1.5;
  }

  .actions {
    display: flex;
    gap: 6px;
    margin-top: 2px;
  }

  .mini {
    font-size: var(--text-micro);
    padding: 2px 8px;
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    background: var(--panel-raised);
    color: var(--ink-2);
    cursor: pointer;
    text-decoration: none;
  }

  .mini:hover {
    color: var(--ink);
    border-color: var(--ink-muted);
  }

  footer {
    margin-top: auto;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px 12px;
    padding-top: 12px;
    border-top: 1px solid var(--rule);
  }

  .tour {
    font-size: var(--text-label);
    padding: 4px 12px;
  }

  .hint {
    color: var(--ink-muted);
    font-size: var(--text-label);
  }

  .check {
    margin-left: auto;
    display: flex;
    gap: 6px;
    align-items: center;
    font-size: var(--text-label);
    color: var(--ink-2);
  }

  @media (max-width: 640px) {
    .flyout {
      width: 100%;
      border-left: none;
    }

    .check {
      margin-left: 0;
    }
  }
</style>
