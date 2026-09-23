<script lang="ts">
  import { Scissors, X } from '@lucide/svelte';
  import type { GenerationSegment } from './api-models';
  import {
    passageTime,
    type PassageBoundary,
    type PassageTextLayer
  } from './passage-structure';
  import { modalFocus } from './modal-focus';

  let {
    item,
    layer,
    boundary,
    disabledReason = '',
    busy = false,
    onclose,
    onsplit
  }: {
    item: GenerationSegment;
    layer: PassageTextLayer;
    boundary: PassageBoundary;
    disabledReason?: string;
    busy?: boolean;
    onclose: () => void;
    onsplit: () => void | Promise<void>;
  } = $props();
  let acknowledged = $state(false);
  const text = $derived(
    layer === 'speech' ? item.optimized_text || item.text : item.text
  );
  const halves = $derived([
    Array.from(text).slice(0, boundary.offset).join('').trim(),
    Array.from(text).slice(boundary.offset).join('').trim()
  ]);
  const companion = $derived(
    layer === 'speech' ? item.text : item.optimized_text || item.text
  );
  const companionOffset = $derived(
    layer === 'speech' ? boundary.display_offset : boundary.speech_offset
  );
  const reason = $derived(
    disabledReason || boundary.split_blocked_reason || ''
  );
</script>

<div
  class="fixed inset-0 z-[100] grid place-items-center bg-black/55 p-3 backdrop-blur-sm"
  role="presentation"
  onclick={(event) => {
    if (event.target === event.currentTarget && !busy) onclose();
  }}
>
  <div
    tabindex="-1"
    use:modalFocus={{
      onclose: () => {
        if (!busy) onclose();
      }
    }}
    role="dialog"
    aria-modal="true"
    aria-labelledby="passage-split-title"
    class="max-h-[90vh] w-full max-w-2xl overflow-auto rounded-2xl border border-[var(--line)] bg-[var(--paper)] p-5 shadow-xl sm:p-6"
  >
    <header class="flex items-start justify-between gap-4">
      <div>
        <p class="section-label">
          Block {item.ordinal + 1} · Passages {boundary.left_reference} / {boundary.right_reference}
        </p>
        <h3 id="passage-split-title" class="mt-1 text-xl font-semibold">
          Inspect passage boundary
        </h3>
      </div>
      <button
        type="button"
        aria-label="Close passage preview"
        class="action icon-action"
        disabled={busy}
        onclick={onclose}><X size={18} /></button
      >
    </header>
    <p class="muted mt-3 text-sm">
      {passageTime(boundary.left_end_ms)} to {passageTime(
        boundary.right_start_ms
      )} · {boundary.gap_ms >= 0
        ? `${(boundary.gap_ms / 1000).toFixed(2)} s source gap`
        : `${(-boundary.gap_ms / 1000).toFixed(2)} s source overlap`}
    </p>
    <p class="muted mt-2 text-sm">
      These are source passage windows, not word timings in the generated audio.
      A dot does not necessarily mark a natural speech break.
    </p>
    {#if boundary.warning}<p
        class="mt-4 rounded-lg border border-[var(--line)] bg-[var(--accent-soft)] p-3 text-sm"
      >
        {boundary.warning}
      </p>{/if}
    <div class="mt-4 grid gap-3">
      {#each halves as half, index}
        {@const window =
          index === 0 ? boundary.left_window : boundary.right_window}
        <section class="rounded-xl border border-[var(--line)] p-3">
          <h4 class="text-xs font-semibold">
            {index === 0 ? 'Left' : 'Right'} block · {Array.from(half).length} characters
            · {passageTime(window[0])} to {passageTime(window[1])}
          </h4>
          <p class="mt-2 whitespace-pre-wrap text-sm">{half}</p>
          {#if companion !== text && companionOffset !== null}
            <p class="muted mt-2 text-xs">
              {layer === 'display' ? 'Spoken text' : 'Script text'}: {Array.from(
                companion
              )
                .slice(
                  index === 0 ? 0 : companionOffset,
                  index === 0 ? companionOffset : undefined
                )
                .join('')
                .trim()}
            </p>
          {/if}
        </section>
      {/each}
    </div>
    {#if reason}<p class="mt-4 text-sm" role="status">{reason}</p>{/if}
    {#if !boundary.natural && boundary.split_allowed && !disabledReason}
      <label class="mt-4 flex items-start gap-2 text-sm"
        ><input type="checkbox" bind:checked={acknowledged} class="mt-1" />Split
        this unfinished phrase deliberately.</label
      >
    {/if}
    <p class="muted mt-4 text-xs">
      Splitting creates a new plan revision. Both new blocks need fresh
      generation; the previous take remains in history. No waveform is sliced.
    </p>
    <footer class="mt-5 flex justify-end gap-2">
      <button type="button" class="action" disabled={busy} onclick={onclose}
        >Cancel</button
      >
      <button
        type="button"
        class="action primary"
        disabled={busy ||
          Boolean(reason) ||
          !boundary.split_allowed ||
          (!boundary.natural && !acknowledged)}
        onclick={onsplit}
        ><Scissors size={15} />{busy ? 'Splitting…' : 'Split here'}</button
      >
    </footer>
  </div>
</div>

<style>
  .action {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 0.45rem;
    min-height: 2.4rem;
    padding: 0.5rem 0.85rem;
    border: 1px solid var(--line);
    border-radius: 0.55rem;
    background: var(--paper-strong);
    color: var(--ink);
    font-size: 0.8rem;
    font-weight: 650;
    cursor: pointer;
  }
  .action:hover:not(:disabled) {
    background: var(--accent-soft);
  }
  .action.primary {
    background: var(--action-bg);
    border-color: var(--action-bg);
    color: white;
  }
  .action.primary:hover:not(:disabled) {
    background: var(--action-hover);
  }
  .action:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }
  .action:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 3px;
  }
  .icon-action {
    padding: 0.45rem;
    min-width: 2.4rem;
  }
</style>
