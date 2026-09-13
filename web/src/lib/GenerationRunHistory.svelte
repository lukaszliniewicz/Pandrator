<script lang="ts">
  import type { GenerationRun } from './api-models';

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
  const selected = $derived(
    activeMix
      ? 'Active mix'
      : !versionId
        ? 'Final audio'
        : versionId === run.id
          ? 'Original audio'
          : `Repair ${Math.max(0, repair?.versions.findIndex((version) => version.generation_run_id === versionId) ?? -1) + 1}`
  );
  const outcome = (status: string) =>
    ({
      applied: 'Split applied',
      not_applied: 'Kept previous audio',
      pending: 'Checking timing',
      stopped: 'Stopped',
      failed: 'Repair failed'
    })[status] ?? 'Outcome unavailable';
  const reason = (value?: string | null) =>
    ({
      added_delay: 'The split would have added delay.',
      selection_changed: 'The selected plan or audio changed.',
      generation_stopped: 'Generation was stopped.'
    })[value ?? ''] ?? '';
</script>

{#if repair}
  <section class="repair-history" aria-label="Timing repair history">
    <div class="repair-heading">
      <div class="min-w-0">
        <p class="font-semibold">
          {#if repair.status === 'running'}Repairing timing
          {:else}{repair.applied_count}
            {repair.applied_count === 1 ? 'block' : 'blocks'} split{/if}
          <span class="font-normal text-[var(--muted)]">
            · {run.label.split(':')[0]}</span
          >
        </p>
        <p class="mt-1 text-[var(--muted)]">
          {#if repair.status === 'running'}Accepted splits become part of this
            run as they finish.
          {:else if repair.status === 'stopped' || repair.status === 'failed'}Timing
            repair stopped. The last accepted audio remains available.
          {:else if !repair.attempt_count}No split was needed. The original
            audio is retained.
          {:else}Automatic timing repairs are saved together in this run.{/if}
        </p>
      </div>
      <div class="repair-choices" aria-label="Repair audio version">
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
        >Repair details · {repair.attempt_count}
        {repair.attempt_count === 1 ? 'attempt' : 'attempts'}<span
          class="ml-2 font-normal text-[var(--muted)]">Viewing: {selected}</span
        ></summary
      >
      <ol class="repair-versions mt-2" aria-label="Individual timing repairs">
        {#each repair.versions as version, index (version.generation_run_id)}
          <li>
            <div class="min-w-0">
              <p class="font-semibold">
                Repair {index + 1}{version.source_block_ordinal != null
                  ? ` · Block ${version.source_block_ordinal + 1}`
                  : ''}
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
              aria-label={`Inspect repair ${index + 1} audio`}
              >Inspect audio</button
            >
          </li>
        {/each}
      </ol>
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
