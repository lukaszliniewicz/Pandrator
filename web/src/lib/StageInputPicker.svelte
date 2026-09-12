<script lang="ts">
  import { Eye, LoaderCircle } from '@lucide/svelte';
  import type { WorkflowStage } from './api-models';
  import { artifactOptionLabel, type StageArtifact } from './stage-artifacts';
  let {
    label = 'Input',
    choices,
    value,
    stage,
    disabled = false,
    loadingMore = false,
    onchange,
    onselect,
    onpreview,
    onloadmore
  }: {
    label?: string;
    choices: { value: string; label: string }[];
    value: string;
    stage?: WorkflowStage;
    disabled?: boolean;
    loadingMore?: boolean;
    onchange: (value: string) => void;
    onselect: (id: string) => void;
    onpreview: (artifact: StageArtifact) => void;
    onloadmore: () => void;
  } = $props();
  const selected = $derived(
    stage?.artifacts?.find((item) => item.id === stage.selected_artifact_id)
  );
</script>

<div
  class="rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] p-3.5"
  aria-label={label}
>
  <div
    class="grid items-end gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,2fr)_auto]"
  >
    <label class="min-w-0 text-xs font-semibold"
      >{label}
      <select
        class="input-select mt-1.5"
        {value}
        disabled={disabled || choices.length < 2}
        onchange={(event) => onchange(event.currentTarget.value)}
      >
        {#each choices as choice}<option value={choice.value}
            >{choice.label}</option
          >{/each}
      </select>
    </label>
    <label class="min-w-0 text-xs font-semibold"
      >Version
      <select
        class="input-select mt-1.5"
        value={stage?.selected_artifact_id ?? ''}
        disabled={disabled || !stage?.artifacts?.length}
        onchange={(event) => onselect(event.currentTarget.value)}
      >
        {#if !stage?.selected_artifact_id}<option value=""
            >{stage?.artifacts?.length
              ? 'Choose a version'
              : 'No saved result yet'}</option
          >{/if}
        {#each stage?.artifacts ?? [] as artifact (artifact.id)}<option
            value={artifact.id}>{artifactOptionLabel(artifact)}</option
          >{/each}
      </select>
    </label>
    <button
      class="btn btn-sm btn-secondary border border-[var(--line)]"
      disabled={!selected}
      onclick={() => selected && onpreview(selected)}
      ><Eye size={14} /> Preview input</button
    >
  </div>
  {#if stage?.artifact_history_has_more}<button
      class="mt-2 flex items-center gap-1 text-xs font-semibold text-[var(--accent)]"
      disabled={loadingMore}
      onclick={onloadmore}
      >{#if loadingMore}<LoaderCircle size={13} class="animate-spin" />{/if}Load
      earlier versions</button
    >{/if}
</div>

<style>
  .input-select {
    width: 100%;
    min-width: 0;
    border: 1px solid var(--line);
    border-radius: 0.7rem;
    background: var(--paper);
    padding: 0.65rem 0.7rem;
    color: var(--ink);
    font-weight: 400;
  }
  .input-select:disabled {
    opacity: 0.7;
  }
</style>
