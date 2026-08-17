<script lang="ts">
  /* Settings, as a right-anchored fly-out.
   *
   * WHY THIS EXISTS. Preferences were scattered across four independent
   * localStorage keys, each owned by whichever component happened to need it
   * and each with its own control: theme in a header <select>, section order on
   * drag handles, metric selection and range inside the History panel. Nothing
   * told you they were settings, that they persisted, or where to find them.
   *
   * WHAT BELONGS HERE, AND WHAT DOES NOT. This holds preferences that are
   * GLOBAL and have no natural home on the page. Controls that sit next to the
   * thing they affect stay there — the metric chips belong beside the chart
   * they redraw, and moving them here would trade discoverability for a round
   * trip. A settings panel is where homeless options live, not a place to
   * collect every control.
   *
   * READ-ONLY IS ABOUT NODE DATA, NOT ABOUT THIS. The dashboard observes the
   * cluster and does not drive it. Everything in this panel is client-side and
   * never leaves the browser, so it touches none of that: no request reaches a
   * node, and nothing here can change what the agent polls. Writing cluster
   * membership from the UI is a genuinely different question — see roadmap L3.
   *
   * Same shell as AlertHistory deliberately: <dialog> + showModal() gives focus
   * trapping, Escape, the backdrop and focus restore from the platform, and
   * reusing it means the two panels behave identically rather than each being
   * subtly hand-rolled.
   */
  import { THEMES } from '../lib/theme.svelte';
  import type { Theme } from '../lib/theme.svelte';
  import type { Layout } from '../lib/layout.svelte';

  interface Props {
    theme: Theme;
    layout: Layout;
    open: boolean;
    onclose: () => void;
  }
  const { theme, layout, open, onclose }: Props = $props();

  let dialog = $state<HTMLDialogElement | null>(null);

  $effect(() => {
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    else if (!open && dialog.open) dialog.close();
  });
</script>

<dialog
  bind:this={dialog}
  class="flyout"
  aria-label="Settings"
  onclose={onclose}
  onclick={(e) => {
    // The dialog element itself is the backdrop's hit target, so a click
    // landing on it rather than on the panel is outside.
    if (e.target === dialog) onclose();
  }}
>
  <div class="panel">
    <header>
      <h2 class="eyebrow">Settings</h2>
      <button class="close" aria-label="Close" onclick={onclose}>×</button>
    </header>

    <!-- Appearance -->
    <section class="block">
      <h3 class="eyebrow dim">Appearance</h3>
      <div class="choices" role="group" aria-label="Theme">
        {#each THEMES as t (t.id)}
          <button
            class="choice"
            class:active={theme.current === t.id}
            aria-pressed={theme.current === t.id}
            onclick={() => theme.set(t.id)}
          >{t.label}</button>
        {/each}
      </div>
      <!-- Each theme is stepped on its own surface rather than derived by
           inverting another, so this is a real choice between three palettes,
           not a light/dark switch with a skin. -->
      <p class="note dim">
        Every theme is validated separately for contrast and colourblind
        separation against its own background.
      </p>
    </section>

    <!-- Layout -->
    <section class="block">
      <h3 class="eyebrow dim">Sections</h3>
      <!-- HIDE, not collapse. Collapsing already has a control on the section
           itself, and duplicating it here would be two ways to do one thing.
           Hiding is the one that has to live here: a hidden section renders
           nothing, so this panel is the only place it can be found again. -->
      <p class="note dim">
        Remove a section from the dashboard entirely. Reorder and collapse stay
        on the sections themselves.
      </p>
      <ol class="sections">
        {#each layout.order as id (id)}
          {@const hidden = layout.isHidden(id)}
          <li class="row" class:off={hidden}>
            <span class="name">{layout.label(id)}</span>
            <button
              class="mini"
              aria-pressed={!hidden}
              aria-label={`${hidden ? 'Show' : 'Hide'} ${layout.label(id)}`}
              onclick={() => layout.toggleHidden(id)}
            >{hidden ? 'hidden' : 'shown'}</button>
          </li>
        {/each}
      </ol>
      <!-- Offered only when there is something to undo: a reset that does
           nothing still invites the click that loses your arrangement. -->
      <button
        class="mini reset"
        disabled={layout.isDefault}
        onclick={() => layout.reset()}
      >Reset sections</button>
    </section>

    <!-- Where this lives -->
    <section class="block">
      <h3 class="eyebrow dim">Storage</h3>
      <!-- Said plainly because the alternative is discovering it: these do not
           follow you to another browser or machine, and there is no account to
           sync them to. The dashboard is deliberately stateless server-side. -->
      <p class="note dim">
        Preferences are stored in this browser only. They do not sync to other
        devices, and clearing site data resets them.
      </p>
    </section>
  </div>
</dialog>

<style>
  /* Mirrors AlertHistory so the two panels are indistinguishable in behaviour
     and weight. Kept as its own copy rather than extracted: two users is not
     yet a pattern, and premature extraction would couple two panels that may
     diverge. Revisit at three. */
  .flyout {
    margin: 0 0 0 auto;
    height: 100%;
    max-height: 100%;
    width: min(420px, 100%);
    max-width: 100%;
    padding: 0;
    border: none;
    border-left: 1px solid var(--rule);
    background: var(--panel);
    color: var(--ink);
  }

  .flyout::backdrop {
    background: rgb(0 0 0 / 0.45);
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
    position: sticky;
    top: 0;
    background: var(--panel);
    padding-bottom: 8px;
  }

  .close {
    font-size: 20px;
    line-height: 1;
    padding: 2px 8px;
    border-radius: var(--radius);
    color: var(--ink-muted);
  }
  .close:hover { color: var(--ink); }

  .block { display: flex; flex-direction: column; gap: 8px; }

  .note { font-size: 11px; margin: 0; }

  .choices { display: flex; gap: 4px; flex-wrap: wrap; }

  .choice {
    font-size: 11px;
    padding: 4px 10px;
    border-radius: var(--radius);
    border: 1px solid var(--rule);
    color: var(--ink-muted);
  }
  .choice:hover { color: var(--ink); }
  .choice.active {
    color: var(--ink);
    background: var(--rule);
  }

  .sections {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .row {
    display: flex;
    align-items: baseline;
    gap: 8px;
    font-size: 12px;
    padding: 5px 8px;
    border-radius: var(--radius);
    border: 1px solid var(--rule);
  }

  .name { font-weight: 500; }

  /* A hidden row stays legible rather than being greyed to the edge of
     readability — this is the only place it can be switched back on, so it
     must not look disabled. */
  .row.off .name { color: var(--ink-muted); }

  .mini {
    font-size: 10px;
    padding: 1px 6px;
    border-radius: var(--radius);
    border: 1px solid var(--rule);
    color: var(--ink-muted);
    margin-left: auto;
  }
  .mini:hover:not(:disabled) { color: var(--ink); }
  .mini:disabled { opacity: 0.5; cursor: default; }

  .reset { margin-left: 0; align-self: flex-start; margin-top: 4px; }

  @media (max-width: 640px) {
    .flyout { width: 100%; border-left: none; }
  }

  @media (prefers-reduced-motion: no-preference) {
    .flyout[open] { animation: slide-in 160ms ease-out; }
  }

  @keyframes slide-in {
    from { transform: translateX(12px); opacity: 0; }
    to { transform: translateX(0); opacity: 1; }
  }
</style>
