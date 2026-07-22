<script lang="ts">
  import { Check, ChevronDown, ChevronRight, Clock3, Eye, History, RotateCcw } from '@lucide/svelte';
  import { formatBytes } from './artifact-display';
  import {
    artifactDetails,
    artifactOptionLabel,
    formatArtifactDate,
    type StageArtifact
  } from './stage-artifacts';

  let {
    artifacts,
    selectedArtifactId,
    canPreview,
    onselect,
    onpreview,
    onclear
  }: {
    artifacts: StageArtifact[];
    selectedArtifactId?: string | null;
    canPreview: boolean;
    onselect: (artifactId: string) => void;
    onpreview: () => void;
    onclear: () => void;
  } = $props();

  const selectedArtifact = $derived(artifacts.find((artifact) => artifact.id === selectedArtifactId));
</script>

<div class="mt-3 max-w-3xl">
  <div class="flex flex-wrap items-center gap-2">
    <History class="muted" size={14}/>
    <label class="text-xs font-semibold">
      Selected version
      <select
        value={selectedArtifactId ?? ''}
        onchange={(event) => onselect(event.currentTarget.value)}
        class="ml-1 max-w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-2 py-1.5 font-normal"
      >
        {#if !selectedArtifactId}<option value="">No result selected</option>{/if}
        {#each artifacts as artifact (artifact.id)}
          <option value={artifact.id}>{artifactOptionLabel(artifact)}</option>
        {/each}
      </select>
    </label>
    {#if canPreview}
      <button onclick={onpreview} class="flex items-center gap-1 text-xs font-semibold text-[var(--accent)]">
        Preview selected<ChevronRight size={13}/>
      </button>
      <button onclick={onclear} class="muted text-xs font-semibold hover:text-red-500">Clear selection</button>
    {:else}
      <span class="muted text-xs">Choose an earlier version or run this stage for the selected input.</span>
    {/if}
  </div>

  <details class="history-disclosure mt-3 overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--paper)]">
    <summary class="flex cursor-pointer list-none items-center gap-3 px-3.5 py-3 text-xs font-semibold">
      <span class="grid size-7 place-items-center rounded-lg bg-[var(--accent-soft)] text-[var(--accent)]"><History size={14}/></span>
      <span class="min-w-0 flex-1">
        <span class="block">Version history</span>
        <span class="muted mt-0.5 block font-normal">{artifacts.length} saved {artifacts.length === 1 ? 'result' : 'results'}{selectedArtifact ? ` · v${selectedArtifact.version} selected` : ''}</span>
      </span>
      <span class="history-chevron muted shrink-0"><ChevronDown size={16}/></span>
    </summary>

    <div class="border-t border-[var(--line)] p-2 sm:p-3">
      <ol class="grid gap-2">
        {#each artifacts as artifact (artifact.id)}
          {@const details = artifactDetails(artifact)}
          {@const selected = artifact.id === selectedArtifactId}
          <li class:selected class="history-item rounded-xl border p-3">
            <div class="flex flex-col gap-3 sm:flex-row sm:items-center">
              <div class="flex min-w-0 flex-1 items-start gap-3">
                <div class:selected class="version-badge grid size-9 shrink-0 place-items-center rounded-xl text-xs font-bold">v{artifact.version}</div>
                <div class="min-w-0 flex-1">
                  <div class="flex flex-wrap items-center gap-1.5">
                    <strong class="text-sm">{selected ? 'Selected result' : `Version ${artifact.version}`}</strong>
                    {#if selected}<span class="selected-chip inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[.65rem] font-bold uppercase tracking-wide"><Check size={10}/> Selected</span>{/if}
                    {#if artifact.state && artifact.state !== 'current'}<span class="muted rounded-full bg-[var(--paper-strong)] px-2 py-0.5 text-[.65rem] font-semibold">{artifact.state === 'stale' ? 'Earlier path' : artifact.state}</span>{/if}
                  </div>
                  <div class="muted mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[.7rem]">
                    <span class="inline-flex items-center gap-1"><Clock3 size={11}/>{formatArtifactDate(artifact.created_at)}</span>
                    {#if artifact.size_bytes != null}<span>{formatBytes(artifact.size_bytes)}</span>{/if}
                  </div>
                  {#if details.length}
                    <dl class="mt-2 flex flex-wrap gap-1.5">
                      {#each details as detail}
                        <div class="rounded-lg bg-[var(--paper-strong)] px-2 py-1 text-[.68rem]"><dt class="muted inline">{detail.label}</dt><dd class="ml-1 inline font-semibold">{detail.value}</dd></div>
                      {/each}
                    </dl>
                  {:else}
                    <p class="muted mt-2 text-[.7rem]">No run metadata was recorded for this version.</p>
                  {/if}
                </div>
              </div>
              <div class="flex shrink-0 gap-2 sm:justify-end">
                {#if selected && canPreview}
                  <button onclick={onpreview} class="history-action"><Eye size={13}/> Preview</button>
                {:else}
                  <button onclick={() => onselect(artifact.id)} class="history-action"><RotateCcw size={13}/> Select</button>
                {/if}
              </div>
            </div>
          </li>
        {/each}
      </ol>
    </div>
  </details>
</div>

<style>
  .history-disclosure summary::-webkit-details-marker { display: none; }
  .history-chevron { transition: transform 160ms ease; }
  .history-disclosure[open] .history-chevron { transform: rotate(180deg); }
  .history-item { border-color: var(--line); background: color-mix(in srgb, var(--paper-strong) 72%, transparent); }
  .history-item.selected { border-color: color-mix(in srgb, var(--accent) 45%, var(--line)); background: color-mix(in srgb, var(--accent-soft) 35%, var(--paper-strong)); }
  .version-badge { color: var(--muted); background: var(--paper-strong); }
  .version-badge.selected { color: var(--accent); background: var(--accent-soft); }
  .selected-chip { color: var(--success); background: color-mix(in srgb, var(--success) 12%, transparent); }
  .history-action { display: inline-flex; align-items: center; gap: .3rem; border: 1px solid var(--line); border-radius: .6rem; padding: .45rem .65rem; font-size: .7rem; font-weight: 700; }
  .history-action:hover { border-color: color-mix(in srgb, var(--accent) 45%, var(--line)); background: var(--accent-soft); }

  @media (prefers-reduced-motion: reduce) {
    .history-chevron { transition: none; }
  }
</style>
