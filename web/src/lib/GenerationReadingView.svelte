<script lang="ts">
  import {
    Flag,
    Play,
    RefreshCw,
    RotateCcw,
    Trash2,
    WandSparkles
  } from '@lucide/svelte';
  import type { GenerationSegment } from './api-models';
  import type { GenerationSegmentChanges } from './domain-api';
  import type { ReadingBlock } from './generation-view-models';

  let {
    blocks,
    selectedRunLabel,
    textMode,
    loaded,
    total,
    activePlayingId,
    selectedRows,
    loading,
    ontextmode,
    onactivate,
    onactivatekeyboard,
    ontext,
    onhasaudio,
    onplay,
    onregenerate,
    onregeneratewith,
    onpatch
  }: {
    blocks: ReadingBlock[];
    selectedRunLabel?: string | null;
    textMode: 'display' | 'speech';
    loaded: number;
    total: number;
    activePlayingId: string;
    selectedRows: string[];
    loading: boolean;
    ontextmode: (mode: 'display' | 'speech') => void;
    onactivate: (
      event: MouseEvent | KeyboardEvent,
      item: GenerationSegment
    ) => void;
    onactivatekeyboard: (event: KeyboardEvent, item: GenerationSegment) => void;
    ontext: (item: GenerationSegment) => string;
    onhasaudio: (item: GenerationSegment) => boolean;
    onplay: (item: GenerationSegment) => unknown;
    onregenerate: (item: GenerationSegment) => unknown;
    onregeneratewith: (item: GenerationSegment) => unknown;
    onpatch: (
      item: GenerationSegment,
      changes: GenerationSegmentChanges
    ) => unknown;
  } = $props();
</script>

<div class="reading-view mx-auto max-w-4xl px-5 py-7 sm:px-8">
  <div
    class="mb-7 flex flex-wrap items-end justify-between gap-3 border-b border-[var(--line)] pb-4"
  >
    <div>
      <div class="eyebrow">Continuous review</div>
      <h3 class="mt-1 text-xl font-semibold">Narration text</h3>
      <p class="muted mt-1 text-xs">
        Reviewing {selectedRunLabel ?? 'the active takes'}. Display text is
        shown by default; speech text is never used for subtitle or read-along
        copy.
      </p>
    </div>
    <div class="flex flex-wrap items-center justify-end gap-2">
      <div class="view-switch font-sans" aria-label="Reading text source">
        <button
          onclick={() => ontextmode('display')}
          class:active={textMode === 'display'}>Display</button
        >
        <button
          onclick={() => ontextmode('speech')}
          class:active={textMode === 'speech'}>Speech</button
        >
      </div>
      <span class="muted text-xs">Loaded {loaded} of {total}</span>
    </div>
  </div>

  {#each blocks as block (block.key)}
    {#if ['heading', 'chapter_marker'].includes(block.kind)}
      <h4
        class:now-playing={block.items.some(
          (item) => item.id === activePlayingId
        )}
        class:selected-heading={block.items.some((item) =>
          selectedRows.includes(item.id)
        )}
        class="reading-heading"
      >
        {#each block.items as item}
          <button
            onclick={(event) => onactivate(event, item)}
            class:removed={item.removed}>{item.text}</button
          >
        {/each}
      </h4>
    {:else}
      <p class="reading-paragraph">
        {#each block.items as item, index}
          <span
            class:now-playing={item.id === activePlayingId}
            class:selected-sentence={selectedRows.includes(item.id)}
            class:removed={item.removed}
            class="reading-segment"
          >
            <span
              role="button"
              tabindex="0"
              onclick={(event) => onactivate(event, item)}
              onkeydown={(event) => onactivatekeyboard(event, item)}
              class="reading-sentence"
              title={onhasaudio(item)
                ? `Play segment ${item.ordinal + 1}`
                : 'Select segment actions'}>{ontext(item)}</span
            >
            <span
              class="reading-actions"
              aria-label={`Actions for segment ${item.ordinal + 1}`}
            >
              <button
                onclick={(event) => {
                  event.stopPropagation();
                  onplay(item);
                }}
                disabled={!onhasaudio(item) || item.removed}
                title="Play segment"
                aria-label={`Play segment ${item.ordinal + 1}`}
                ><Play size={13} /></button
              >
              <button
                onclick={(event) => {
                  event.stopPropagation();
                  onregenerate(item);
                }}
                disabled={loading || item.removed}
                title="Regenerate segment"
                aria-label={`Regenerate segment ${item.ordinal + 1}`}
                ><RefreshCw size={13} /></button
              >
              <button
                onclick={(event) => {
                  event.stopPropagation();
                  onregeneratewith(item);
                }}
                disabled={loading || item.removed}
                title="Regenerate with alternate settings"
                aria-label={`Regenerate segment ${item.ordinal + 1} with alternate settings`}
                ><WandSparkles size={13} /></button
              >
              <button
                onclick={(event) => {
                  event.stopPropagation();
                  onpatch(item, { marked: !item.marked });
                }}
                class:active={item.marked}
                title={item.marked
                  ? 'Unmark segment'
                  : 'Mark for bulk regeneration'}
                aria-label={item.marked
                  ? `Unmark segment ${item.ordinal + 1}`
                  : `Mark segment ${item.ordinal + 1}`}
                ><Flag size={13} /></button
              >
              <button
                onclick={(event) => {
                  event.stopPropagation();
                  onpatch(item, { removed: !item.removed });
                }}
                title={item.removed ? 'Restore segment' : 'Remove segment'}
                aria-label={item.removed
                  ? `Restore segment ${item.ordinal + 1}`
                  : `Remove segment ${item.ordinal + 1}`}
              >
                {#if item.removed}<RotateCcw size={13} />{:else}<Trash2
                    size={13}
                  />{/if}
              </button>
            </span>
          </span>{#if index < block.items.length - 1}<span aria-hidden="true">
            </span>{/if}
        {/each}
      </p>
    {/if}
  {/each}

  {#if !blocks.length}
    <p class="muted py-16 text-center">No segments match this filter.</p>
  {/if}
</div>

<style>
  .view-switch {
    display: flex;
    border: 1px solid var(--line);
    border-radius: 0.6rem;
    background: var(--paper);
    padding: 0.15rem;
  }
  .view-switch button {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    border-radius: 0.45rem;
    padding: 0.3rem 0.5rem;
    font-size: 0.68rem;
    font-weight: 700;
    color: var(--muted);
  }
  .view-switch button.active {
    background: var(--accent-soft);
    color: var(--ink);
  }
  .reading-view {
    font-family: Georgia, 'Times New Roman', serif;
  }
  .reading-heading {
    margin: 2rem 0 0.75rem;
    font-size: 1.32rem;
    font-weight: 700;
    line-height: 1.35;
  }
  .reading-heading button {
    border-radius: 0.3rem;
    text-align: left;
  }
  .reading-heading.selected-heading button {
    background: var(--accent-soft);
  }
  .reading-heading.now-playing button {
    color: var(--accent);
  }
  .reading-heading button.removed {
    text-decoration: line-through;
    opacity: 0.42;
  }
  .reading-paragraph {
    margin: 0 0 1.2rem;
    font-size: 1.02rem;
    line-height: 1.9;
    white-space: normal;
  }
  .reading-segment {
    position: relative;
    display: inline;
  }
  .reading-sentence {
    display: inline;
    white-space: normal;
    border-radius: 0.28rem;
    cursor: pointer;
    text-align: left;
    transition:
      background 0.12s ease,
      color 0.12s ease;
  }
  .reading-sentence:focus-visible {
    outline: 3px solid color-mix(in srgb, var(--accent) 38%, transparent);
    outline-offset: 2px;
  }
  .reading-segment:hover .reading-sentence,
  .reading-segment:focus-within .reading-sentence,
  .reading-segment.selected-sentence .reading-sentence {
    background: var(--accent-soft);
  }
  .reading-segment.now-playing .reading-sentence {
    background: var(--action-bg);
    color: white;
    box-shadow: 0 0 0 0.16rem color-mix(in srgb, var(--accent) 18%, transparent);
  }
  .reading-segment.removed .reading-sentence {
    text-decoration: line-through;
    opacity: 0.42;
  }
  .reading-actions {
    position: absolute;
    bottom: calc(100% + 0.32rem);
    left: 50%;
    z-index: 25;
    display: flex;
    gap: 0.18rem;
    border: 1px solid var(--line);
    border-radius: 0.65rem;
    background: var(--paper-strong);
    padding: 0.22rem;
    box-shadow: var(--shadow);
    opacity: 0;
    pointer-events: none;
    transform: translate(-50%, 0.25rem);
    transition:
      opacity 0.12s ease,
      transform 0.12s ease;
  }
  .reading-segment:hover .reading-actions,
  .reading-segment:focus-within .reading-actions {
    opacity: 1;
    pointer-events: auto;
    transform: translate(-50%, 0);
  }
  .reading-actions button {
    display: grid;
    height: 1.8rem;
    width: 1.8rem;
    place-items: center;
    border-radius: 0.45rem;
    color: var(--muted);
  }
  .reading-actions button:hover:not(:disabled),
  .reading-actions button:focus-visible,
  .reading-actions button.active {
    background: var(--accent-soft);
    color: var(--accent);
  }
  .reading-actions button:disabled {
    opacity: 0.35;
  }
</style>
