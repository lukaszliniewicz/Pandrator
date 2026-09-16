<script lang="ts">
  import type { GenerationRun } from './api-models';
  import {
    historyAriaLabel,
    historyCounts,
    historyDetailsSummary,
    historyHeading,
    historyHelper,
    historyInspectLabel,
    historyVersionOutcome,
    historyVersionReason,
    historyVersionTitle,
    secondPassKind
  } from './generation-history';

  let {
    run,
    versionId = '',
    activeMix = false,
    disabled = false,
    onSelectVersion
  }: {
    run: GenerationRun;
    versionId?: string;
    activeMix?: boolean;
    disabled?: boolean;
    onSelectVersion: (versionId: string) => void;
  } = $props();
  const repair = $derived(run.timing_repair);
  const kind = $derived(secondPassKind(run));
  const counts = $derived(repair ? historyCounts(repair) : null);
  const heading = $derived(historyHeading(run));
  const helper = $derived(historyHelper(run));
  const ariaLabel = $derived(historyAriaLabel(run));
  const detailsSummary = $derived(historyDetailsSummary(run));
  const selected = $derived(
    activeMix
      ? 'Active mix'
      : !versionId
        ? 'Final audio'
        : versionId === run.id
          ? 'Original audio'
          : `${kind === 'regroup' ? 'Group' : kind === 'repair' ? 'Repair' : 'Attempt'} ${Math.max(0, repair?.versions.findIndex((version) => version.generation_run_id === versionId) ?? -1) + 1}`
  );
  const outcome = (status: string) => historyVersionOutcome(run, status);
  const reason = (value?: string | null) => historyVersionReason(value);
</script>

{#if repair}
  <section class="repair-history" aria-label={ariaLabel}>
    <div class="repair-heading">
      <div class="min-w-0">
        <p class="font-semibold">
          {heading}
          <span class="font-normal text-[var(--muted)]">
            · {run.label.split(':')[0]}</span
          >
        </p>
        <p class="mt-1 text-[var(--muted)]">
          {helper}
        </p>
      </div>
      <div
        class="repair-choices"
        aria-label={kind === 'regroup'
          ? 'Regroup audio version'
          : kind === 'repair'
            ? 'Repair audio version'
            : 'Second-pass audio version'}
      >
        <button
          type="button"
          class:chosen={!activeMix && !versionId}
          aria-pressed={!activeMix && !versionId}
          {disabled}
          onclick={() => onSelectVersion('')}>Final audio</button
        >
        <button
          type="button"
          class:chosen={versionId === run.id}
          aria-pressed={versionId === run.id}
          {disabled}
          onclick={() => onSelectVersion(run.id)}>View original</button
        >
      </div>
    </div>
    <details class="mt-3">
      <summary
        >{detailsSummary}<span class="ml-2 font-normal text-[var(--muted)]"
          >Viewing: {selected}</span
        ></summary
      >
      <ol
        class="repair-versions mt-2"
        aria-label={kind === 'regroup'
          ? 'Individual regrouped groups'
          : kind === 'repair'
            ? 'Individual timing repairs'
            : 'Individual second-pass attempts'}
      >
        {#each repair.versions as version, index (version.generation_run_id)}
          <li>
            <div class="min-w-0">
              <p class="font-semibold">
                {historyVersionTitle(
                  run,
                  index
                )}{#if kind === 'repair' && version.source_block_ordinal != null}{` · Block ${version.source_block_ordinal + 1}`}{:else if kind === 'regroup' && version.member_count != null}{` · ${version.member_count} ${version.member_count === 1 ? 'passage' : 'passages'}`}{/if}
              </p>
              <p class="mt-0.5 text-[var(--muted)]">
                {outcome(version.repair_status)}{reason(version.repair_reason)
                  ? ` · ${reason(version.repair_reason)}`
                  : ''}
              </p>
            </div>
            <button
              type="button"
              class:chosen={versionId === version.generation_run_id}
              aria-pressed={versionId === version.generation_run_id}
              disabled={disabled || version.status !== 'completed'}
              onclick={() => onSelectVersion(version.generation_run_id)}
              aria-label={historyInspectLabel(run, index)}>Inspect audio</button
            >
          </li>
        {/each}
      </ol>
      {#if counts}
        <p class="mt-2 text-[var(--muted)]">
          {counts.attempted}
          {counts.attempted === 1 ? 'group' : 'groups'} regenerated: {counts.applied}
          accepted, {counts.rejected} rejected{#if counts.pending}
            · {counts.pending} checking{/if}
        </p>
      {/if}
    </details>
  </section>
{/if}

<style>
  .repair-history {
    border: 1px solid var(--line);
    border-radius: 0.8rem;
    background: var(--paper-strong);
    padding: 0.85rem 1rem;
    font-size: 0.75rem;
  }
  .repair-heading {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
  }
  .repair-choices {
    display: flex;
    gap: 0.4rem;
    flex-shrink: 0;
  }
  button {
    border: 1px solid var(--line);
    border-radius: 0.5rem;
    padding: 0.45rem 0.65rem;
    font-weight: 600;
    white-space: nowrap;
  }
  button.chosen {
    background: var(--accent-soft);
    border-color: var(--accent);
    color: var(--accent);
  }
  button:disabled {
    opacity: 0.5;
  }
  summary {
    cursor: pointer;
    font-weight: 600;
  }
  .repair-versions {
    max-height: 17rem;
    overflow-y: auto;
    overscroll-behavior: contain;
  }
  li {
    display: flex;
    gap: 1rem;
    align-items: center;
    justify-content: space-between;
    padding: 0.65rem 0;
    border-top: 1px solid var(--line);
  }
  @media (max-width: 640px) {
    .repair-heading {
      align-items: stretch;
      flex-direction: column;
      gap: 0.7rem;
    }
    .repair-choices button {
      flex: 1;
    }
  }
</style>
