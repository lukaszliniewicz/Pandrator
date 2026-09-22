<script lang="ts">
  import { errorMessage } from './errors';
  import {
    loadRepairBatch,
    undoRepairBatch,
    type RepairBatch,
    type RepairCheckpoint
  } from './repair-batches';

  let {
    sessionId,
    batch,
    disabled = false,
    onpreview,
    onundone
  }: {
    sessionId: string;
    batch: RepairBatch;
    disabled?: boolean;
    onpreview: (revisionId: string) => unknown;
    onundone: (revisionId: string) => unknown;
  } = $props();
  let expanded = $state(false);
  let loading = $state(false);
  let undoing = $state(false);
  let error = $state('');
  let items = $state<RepairCheckpoint[]>([]);
  let next = $state<number | null>(null);
  let refreshed = $state<RepairBatch | null>(null);
  let request = 0;
  const current = $derived(refreshed?.id === batch.id ? refreshed : batch);
  const statusLabel = (status: string) =>
    ({
      running: 'Repair in progress',
      completed: 'Repair completed',
      partial: 'Partially repaired',
      stopped: 'Repair stopped',
      failed: 'Repair failed',
      no_changes: 'No repairs applied'
    })[status] ?? status;
  const outcome = (status: string | null) =>
    ({
      applied: 'Accepted',
      not_applied: 'Not applied',
      pending: 'Pending',
      failed: 'Failed',
      stopped: 'Stopped'
    })[status ?? ''] ?? 'Outcome not recorded';
  const reason = (value: string | null) =>
    ({
      added_delay: 'Would add delay',
      missed_repair_anchor: 'Would miss an internal timing anchor',
      selection_changed: 'Plan or audio selection changed',
      generation_stopped: 'Generation stopped',
      generation_failed: 'Generation or validation failed'
    })[value ?? ''] ?? '';

  $effect(() => {
    void batch.id;
    request += 1;
    expanded = false;
    error = '';
    items = [];
    next = null;
    refreshed = null;
    loading = false;
    // Summary lists leave undo eligibility unchecked (can_undo null). Fetch the
    // authoritative batch detail without expanding, so undo stays disabled
    // until the server has actually evaluated its guards.
    if (batch.undo_checked === false) void refreshEligibility();
  });

  async function refreshEligibility() {
    const ticket = ++request;
    const batchId = batch.id;
    try {
      const result = await loadRepairBatch(sessionId, batchId);
      if (ticket !== request || batchId !== batch.id) return;
      refreshed = result.repair_batch;
    } catch {
      // Keep the unchecked summary state; the expanded details path surfaces
      // load errors when the user asks for them.
    }
  }

  async function details(append = false) {
    expanded = true;
    loading = true;
    error = '';
    const ticket = ++request;
    const batchId = batch.id;
    try {
      const result = await loadRepairBatch(
        sessionId,
        batchId,
        append ? next : null
      );
      if (ticket !== request || batchId !== batch.id) return;
      items = append ? [...items, ...result.items] : result.items;
      next = result.next_before_revision_number;
      refreshed = result.repair_batch;
    } catch (caught) {
      if (ticket === request) error = errorMessage(caught);
    } finally {
      if (ticket === request) loading = false;
    }
  }

  async function undo() {
    if (undoing || disabled || !current.can_undo) return;
    undoing = true;
    error = '';
    try {
      const result = await undoRepairBatch(sessionId, current);
      await onundone(result.plan_revision_id);
    } catch (caught) {
      error = errorMessage(caught);
      // Refresh eligibility after a conflict, without changing the preview or plan.
      try {
        refreshed = (await loadRepairBatch(sessionId, batch.id)).repair_batch;
      } catch {
        /* Keep the original actionable error. */
      }
    } finally {
      undoing = false;
    }
  }
</script>

<section
  class="repair-batch"
  aria-label="Automatic repair batch"
  aria-busy={undoing}
>
  <div class="batch-heading">
    <strong>{statusLabel(current.status)}</strong>
    <span class="muted"
      >{current.applied_count} accepted · {current.attempt_count} attempted</span
    >
  </div>
  <p class="muted explanation">
    One repair operation. Original plans and audio remain available.
  </p>
  <div class="batch-actions">
    <button
      class="btn"
      type="button"
      disabled={disabled || undoing || loading}
      aria-expanded={expanded}
      onclick={() => {
        if (expanded) expanded = false;
        else void details();
      }}
    >
      {expanded ? 'Hide repair details' : 'View repair details'}
    </button>
    <button
      class="btn"
      type="button"
      disabled={disabled || undoing}
      onclick={() => onpreview(current.base_revision_id)}
      >Preview original</button
    >
    <button
      class="btn btn-secondary"
      type="button"
      disabled={disabled || undoing || !current.can_undo}
      title={current.undo_checked === false
        ? 'Checking undo eligibility with the server…'
        : current.undo_disabled_reason ||
          'Restore the pre-repair plan and selected audio as one new revision'}
      onclick={() => void undo()}
      >{undoing ? 'Undoing repairs…' : 'Undo automatic repairs'}</button
    >
  </div>
  {#if current.undo_checked === false}<p class="muted explanation" role="status">
      Checking undo eligibility with the server…
    </p>{/if}
  {#if current.undo_disabled_reason}<p class="muted explanation">
      {current.undo_disabled_reason}
    </p>{/if}
  {#if error}<p role="alert" class="batch-error">{error}</p>{/if}
  {#if expanded}
    <div class="checkpoint-details">
      <p class="muted explanation">
        Checkpoints are diagnostic history, not separate editorial changes.
        Previewing does not select a plan.
      </p>
      {#if loading}<p role="status" class="muted">
          Loading repair details…
        </p>{/if}
      <ol aria-label="Repair attempts">
        {#each items as item (item.id)}
          <li>
            <div>
              <strong
                >{item.source_block_ordinal != null
                  ? `Block ${item.source_block_ordinal + 1}`
                  : `Checkpoint ${item.revision_number}`}</strong
              >
              <span> · {outcome(item.repair_status)}</span>
              {#if reason(item.repair_reason)}<p class="muted">
                  {reason(item.repair_reason)}
                </p>{/if}
            </div>
            <button
              class="btn"
              type="button"
              disabled={disabled || undoing || loading}
              aria-label={`Preview repair checkpoint ${item.revision_number}`}
              onclick={() => onpreview(item.id)}>Preview</button
            >
          </li>
        {/each}
      </ol>
      {#if next != null}<button
          class="btn"
          type="button"
          disabled={disabled || undoing || loading}
          onclick={() => void details(true)}>Load earlier attempts</button
        >{/if}
    </div>
  {/if}
</section>

<style>
  .repair-batch {
    border: 1px solid var(--line);
    border-radius: 0.8rem;
    padding: 0.8rem;
    margin-bottom: 1rem;
    min-width: 0;
  }
  .batch-heading,
  .batch-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem 0.8rem;
    align-items: center;
  }
  .batch-heading {
    font-size: 0.85rem;
  }
  .batch-actions {
    margin-top: 0.65rem;
    gap: 0.45rem;
  }
  .explanation {
    font-size: 0.76rem;
    line-height: 1.5;
    margin-top: 0.4rem;
  }
  .batch-error {
    color: var(--danger, #b91c1c);
    font-size: 0.8rem;
    margin-top: 0.5rem;
  }
  .checkpoint-details {
    border-top: 1px solid var(--line);
    margin-top: 0.7rem;
    padding-top: 0.4rem;
  }
  ol {
    list-style: none;
    padding: 0;
    margin: 0.5rem 0;
  }
  li {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    border-bottom: 1px solid var(--line);
    padding: 0.5rem 0;
    font-size: 0.78rem;
  }
  li > div {
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .btn {
    flex-shrink: 0;
  }
  @media (max-width: 480px) {
    .batch-actions .btn {
      max-width: 100%;
      white-space: normal;
      text-align: left;
    }
  }
</style>
