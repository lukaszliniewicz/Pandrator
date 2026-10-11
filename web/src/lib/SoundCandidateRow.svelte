<script lang="ts">
  import type { Snippet } from 'svelte';
  import {
    ChevronDown,
    ChevronUp,
    RotateCcw,
    Save,
    SlidersHorizontal,
    Trash2
  } from '@lucide/svelte';
  import SoundPreview from './SoundPreview.svelte';
  import {
    soundPending,
    type SoundCandidate,
    type SoundModel
  } from './sound-api';

  let {
    candidate,
    model,
    expanded = false,
    checked = false,
    busy = false,
    generationBusy = false,
    canDiscard = false,
    onexpand,
    oncheck,
    onreuse,
    onregenerate,
    onsave,
    ondiscard,
    onopen,
    children
  }: {
    candidate: SoundCandidate;
    model?: SoundModel;
    expanded?: boolean;
    checked?: boolean;
    busy?: boolean;
    generationBusy?: boolean;
    canDiscard?: boolean;
    onexpand: () => void;
    oncheck: (checked: boolean) => void;
    onreuse: () => void;
    onregenerate: () => void;
    onsave: () => void;
    ondiscard: () => void;
    onopen: () => void;
    children?: Snippet;
  } = $props();
  const title = $derived(
    candidate.provenance.prompt ??
      candidate.provenance.display_name ??
      'Imported audio'
  );
  const canRegenerate = $derived(
    Boolean(
      model &&
      model.available !== false &&
      model.backends.some(
        (backend) =>
          backend === (candidate.provenance.backend ?? model.backends[0])
      )
    )
  );
</script>

<article class="candidate-row" class:expanded aria-label="Candidate take">
  <div class="take-header">
    <input
      type="checkbox"
      {checked}
      disabled={busy || !canDiscard}
      aria-label={`Select candidate: ${title}`}
      onchange={(event) => oncheck(event.currentTarget.checked)}
      title={canDiscard
        ? 'Select for discard'
        : candidate.item_id
          ? 'Saved in the library'
          : soundPending(candidate)
            ? 'Generation is still active'
            : 'Already discarded'}
    />
    <button
      class="take-title"
      aria-expanded={expanded}
      disabled={busy}
      onclick={onexpand}
    >
      <span class="min-w-0"
        ><span class="line-clamp-2 block font-medium">{title}</span>
        <span class="muted mt-1 block text-xs"
          >{model?.label ??
            candidate.provenance.model_id ??
            'Imported audio'}{#if candidate.duration_seconds !== null}
            · {candidate.duration_seconds.toFixed(2)} s{:else if candidate.provenance.duration_seconds !== undefined}
            · requested {candidate.provenance.duration_seconds} s{/if}{#if candidate.provenance.backend}
            · {candidate.provenance.backend.toUpperCase()}{/if}{#if candidate.provenance.seed !== undefined}
            · seed {candidate.provenance.seed}{/if}</span
        >
      </span>
      {#if expanded}<ChevronUp size={16} />{:else}<ChevronDown size={16} />{/if}
    </button>
    <span class="status">{candidate.item_id ? 'Saved' : candidate.status}</span>
  </div>
  <div class="take-actions">
    {#if candidate.status === 'ready' && candidate.artifact_id}
      <div class="take-player"><SoundPreview {candidate} label={title} /></div>
    {/if}
    <div class="flex flex-wrap gap-2">
      <button
        type="button"
        class="btn btn-secondary"
        disabled={busy}
        onclick={onexpand}
        aria-expanded={expanded}
        ><SlidersHorizontal size={14} />
        {expanded ? 'Hide details' : 'Settings'}</button
      >
      {#if model}
        <button
          type="button"
          class="btn btn-secondary"
          disabled={busy}
          onclick={onreuse}>Use settings</button
        >
        <button
          type="button"
          class="btn btn-secondary"
          disabled={busy ||
            generationBusy ||
            !canRegenerate ||
            soundPending(candidate)}
          onclick={onregenerate}
          title={canRegenerate
            ? 'Generate another take with a fresh seed'
            : 'Use settings to choose an available backend first'}
          ><RotateCcw size={14} /> Regenerate</button
        >
      {/if}
      {#if candidate.item_id}
        <button
          type="button"
          class="btn btn-secondary"
          disabled={busy}
          onclick={onopen}>Open saved clip</button
        >
      {:else if candidate.status === 'ready'}
        <button
          type="button"
          class="btn btn-secondary"
          disabled={busy}
          onclick={onsave}><Save size={14} /> Save</button
        >
      {/if}
      {#if canDiscard}
        <button
          type="button"
          class="btn btn-secondary"
          disabled={busy}
          onclick={ondiscard}><Trash2 size={14} /> Discard</button
        >
      {/if}
    </div>
  </div>
  {#if expanded}
    <div class="take-details">{@render children?.()}</div>
  {/if}
</article>

<style>
  .candidate-row {
    border: 1px solid var(--line);
    border-radius: 0.8rem;
    background: var(--paper-strong);
    padding: 1rem;
  }
  .candidate-row.expanded {
    border-color: var(--accent);
    box-shadow: 0 0 0 1px color-mix(in srgb, var(--accent) 15%, transparent);
  }
  .take-header {
    display: flex;
    align-items: center;
    gap: 0.75rem;
  }
  .take-title {
    display: flex;
    min-width: 0;
    flex: 1;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    text-align: left;
  }
  .take-title:disabled {
    opacity: 0.65;
  }
  .take-title > :global(svg) {
    flex-shrink: 0;
  }
  .status {
    flex-shrink: 0;
    border-radius: 999px;
    background: var(--accent-soft);
    color: var(--ink);
    padding: 0.2rem 0.6rem;
    font-size: 0.7rem;
    text-transform: capitalize;
  }
  .take-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.75rem;
    margin-top: 0.8rem;
  }
  .take-player {
    width: 15rem;
    max-width: 100%;
  }
  .take-details {
    border-top: 1px solid var(--line);
    margin-top: 1rem;
    padding-top: 1rem;
  }
  @media (max-width: 640px) {
    .candidate-row {
      padding: 0.75rem;
    }
    .take-header {
      flex-wrap: wrap;
    }
    .take-player {
      width: 100%;
    }
  }
</style>
