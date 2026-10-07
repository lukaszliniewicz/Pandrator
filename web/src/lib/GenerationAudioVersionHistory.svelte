<script lang="ts">
  import type { GenerationRun } from './api-models';
  import { ApiMutationAttempt } from './api';
  import { errorMessage } from './errors';
  import {
    previewUndoRecast,
    undoRecast,
    type RecastUndoPreview
  } from './generation-recast';
  let {
    run,
    side,
    disabled = false,
    onchoose,
    onundone
  }: {
    run: GenerationRun;
    side: 'before' | 'after';
    disabled?: boolean;
    onchoose: (side: 'before' | 'after') => void;
    onundone: (run: GenerationRun) => void;
  } = $props();
  let undoPreview = $state<RecastUndoPreview | null>(null);
  let busy = $state(false);
  let error = $state('');
  const attempt = new ApiMutationAttempt();
  const version = $derived(run.audio_version);
  $effect(() => {
    void run.id;
    undoPreview = null;
    error = '';
  });
  async function reviewUndo() {
    busy = true;
    error = '';
    try {
      undoPreview = await previewUndoRecast(run.id);
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
  async function restore() {
    if (!undoPreview || busy) return;
    busy = true;
    error = '';
    try {
      const result = await undoRecast(run.id, undoPreview, attempt);
      undoPreview = null;
      onundone(result.run);
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
</script>

{#if version}
  <div
    class="flex flex-wrap items-center justify-between gap-3 text-sm"
    data-testid="recast-audio-history"
  >
    <div>
      <p class="font-semibold">
        Recast {version.selectors.map((item) => item.label).join(', ')}
      </p>
      <p class="muted text-xs">
        {version.generated_count} of {version.target_count} replacements ready · {version.applied_count}
        applied{#if version.undone}&nbsp;· Audio restored{/if}
      </p>
    </div>
    <div class="flex flex-wrap gap-2">
      <div
        class="flex rounded-lg border border-[var(--line)] p-1"
        role="group"
        aria-label="Recast audio version"
      >
        <button
          class="btn text-xs"
          class:btn-primary={side === 'before'}
          aria-pressed={side === 'before'}
          disabled={disabled || !version.before_available}
          onclick={() => onchoose('before')}>Before recast</button
        >
        <button
          class="btn text-xs"
          class:btn-primary={side === 'after'}
          aria-pressed={side === 'after'}
          {disabled}
          onclick={() => onchoose('after')}>After recast</button
        >
      </div>
      {#if !version.undone && version.applied_count > 0}
        <button
          class="btn text-xs"
          disabled={disabled ||
            busy ||
            !['completed', 'partial', 'failed', 'canceled'].includes(
              run.status
            )}
          onclick={() => void reviewUndo()}>Undo this recast…</button
        >
      {/if}
    </div>
  </div>
  {#if undoPreview}
    <div class="mt-3 rounded-xl border border-[var(--line)] p-3 text-sm">
      {#if undoPreview.blocked_reason}<p role="alert">
          {undoPreview.blocked_reason}
        </p>
      {:else}<p>
          Restore {undoPreview.restore_count} previous audio
          {undoPreview.restore_count === 1 ? 'choice' : 'choices'}? Your saved
          cast and project settings stay as they are.
        </p>{/if}
      <div class="mt-2 flex flex-wrap justify-end gap-2">
        <button
          class="btn text-xs"
          disabled={busy}
          onclick={() => (undoPreview = null)}>Cancel</button
        >
        <button
          class="btn btn-primary text-xs"
          disabled={busy ||
            Boolean(undoPreview.blocked_reason) ||
            !undoPreview.restore_count}
          onclick={() => void restore()}>Restore previous audio</button
        >
      </div>
    </div>
  {/if}
  {#if error}<p role="alert" class="mt-2 text-sm text-red-700">
      {error}
      <button
        class="underline"
        disabled={busy}
        onclick={() => void reviewUndo()}>Refresh undo preview</button
      >
    </p>{/if}
{/if}
