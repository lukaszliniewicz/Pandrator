<script lang="ts">
  import { apiJson } from './api';
  import { errorMessage } from './errors';
  import { modalDialog } from './modal-dialog';
  import AudioReuseNotice from './AudioReuseNotice.svelte';
  import RepairBatchPanel from './RepairBatchPanel.svelte';
  import type { RepairBatch } from './repair-batches';

  type Revision = {
    id: string;
    entry_id?: string;
    is_repair_checkpoint?: boolean;
    repair_batch?: RepairBatch;
    revision_number: number;
    parent_revision_id: string | null;
    summary: string;
    origin: string;
    segment_count: number;
    reusable_segment_count: number;
    stale_segment_count: number;
    audio_settings_stale_segment_count?: number;
    audio_identity_unknown_segment_count?: number;
    source_artifact_id: string | null;
    repair_status?: string | null;
    repair_reason?: string | null;
    source_block_ordinal?: number | null;
  };
  type Block = {
    id: string;
    ordinal: number;
    text: string;
    status: string;
    source_segment_ids: (string | number)[];
  };
  let {
    sessionId,
    activeRevisionId,
    disabled = false,
    onrestore,
    onselect,
    ongenerate
  }: {
    sessionId: string;
    activeRevisionId: string | null;
    disabled?: boolean;
    onrestore: (revisionId: string) => Promise<unknown>;
    onselect?: (revisionId: string) => Promise<unknown>;
    ongenerate: (staleOnly: boolean) => Promise<unknown>;
  } = $props();
  let open = $state(false);
  let busy = $state(false);
  let previewLoading = $state(false);
  let error = $state('');
  let revisions = $state<Revision[]>([]);
  let selected = $state('');
  let blocks = $state<Block[]>([]);
  let nextCursor = $state<number | null>(null);
  let nextRevision = $state<number | null>(null);
  let selectedEntry = $state('');
  let serverActiveRevisionId = $state<string | null | undefined>(undefined);
  const effectiveActiveRevisionId = $derived(
    serverActiveRevisionId ?? activeRevisionId
  );
  const current = $derived(
    revisions.find((item) => (item.entry_id ?? item.id) === selectedEntry)
  );
  const inspectingCheckpoint = $derived(
    Boolean(current && selected !== current.id)
  );

  function chooseRevision(revision: Revision) {
    selectedEntry = revision.entry_id ?? revision.id;
    return preview(revision.id);
  }
  const parentRevision = $derived(
    revisions.find((item) => item.id === current?.parent_revision_id)
  );
  const base = $derived(`/api/v1/sessions/${encodeURIComponent(sessionId)}`);
  let previewRequest = 0;

  async function preview(revisionId: string, append = false) {
    selected = revisionId;
    previewLoading = true;
    error = '';
    if (!append) {
      blocks = [];
      nextCursor = null;
    }
    const request = ++previewRequest;
    const query = new URLSearchParams({
      plan_revision_id: revisionId,
      view: 'compact',
      limit: '30',
      cursor: String(append ? (nextCursor ?? 0) : 0)
    });
    try {
      const result = await apiJson<{
        items: Block[];
        next_cursor: number | null;
      }>(`${base}/generation-segments?${query}`);
      if (request !== previewRequest) return;
      blocks = append ? [...blocks, ...result.items] : result.items;
      nextCursor = result.next_cursor;
    } catch (caught) {
      if (request === previewRequest) error = errorMessage(caught);
    } finally {
      if (request === previewRequest) previewLoading = false;
    }
  }

  async function loadHistory(append = false) {
    busy = true;
    error = '';
    try {
      const query = new URLSearchParams({ limit: '50' });
      if (append && nextRevision !== null)
        query.set('before_revision_number', String(nextRevision));
      const result = await apiJson<{
        items: Revision[];
        active_revision_id: string | null;
        next_before_revision_number: number | null;
      }>(`${base}/generation-plan/history?${query}`);
      revisions = append ? [...revisions, ...result.items] : result.items;
      serverActiveRevisionId = result.active_revision_id;
      nextRevision = result.next_before_revision_number;
      if (!append) {
        const active =
          revisions.find((item) => item.id === result.active_revision_id) ??
          revisions[0];
        if (active) await chooseRevision(active);
        else {
          selected = '';
          selectedEntry = '';
          blocks = [];
        }
      }
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }

  async function restore(copy = true) {
    if (!selected || selected === effectiveActiveRevisionId || previewLoading)
      return;
    busy = true;
    try {
      if (copy || !onselect) await onrestore(selected);
      else await onselect(selected);
      await loadHistory();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
  function close() {
    open = false;
    previewRequest += 1;
  }
  function repairLabel(status: string) {
    return (
      (
        {
          applied: 'Applied',
          not_applied: 'Not applied',
          failed: 'Failed',
          stopped: 'Stopped',
          pending: 'Pending',
          unknown: 'Outcome not recorded'
        } as Record<string, string>
      )[status] ?? 'Outcome not recorded'
    );
  }
  function repairDetail(reason: string) {
    return (
      (
        {
          added_delay: 'The replacement audio would have delayed later blocks.',
          selection_changed:
            'The plan or selected takes changed while replacements were being generated.',
          generation_stopped:
            'Generation was paused or stopped before this repair was applied.',
          generation_failed:
            'Replacement generation or timing validation did not complete.'
        } as Record<string, string>
      )[reason] ?? ''
    );
  }
</script>

<button
  type="button"
  class="btn"
  disabled={!activeRevisionId}
  onclick={() => {
    open = true;
    void loadHistory();
  }}>Speech plans</button
>
{#if open}
  <dialog
    use:modalDialog={{
      onclose: () => {
        if (!busy) close();
      }
    }}
    aria-labelledby="speech-plan-history-title"
    class="history-dialog"
  >
    <header class="history-header">
      <div>
        <h2 id="speech-plan-history-title" class="text-lg font-semibold">
          Versioned speech plans
        </h2>
        <p class="muted mt-1 text-sm">
          Browse earlier versions and inspect repairs. Previewing keeps your
          active plan unchanged.
        </p>
      </div>
      <button type="button" class="btn" disabled={busy} onclick={close}
        >Close</button
      >
    </header>
    {#if error}<p role="alert" class="mx-5 mb-3 text-sm text-red-700">
        {error}
      </p>{/if}
    <div class="history-body">
      <nav aria-label="Plan versions" class="version-list">
        {#if busy && !revisions.length}<p
            class="muted p-3 text-sm"
            role="status"
          >
            Loading versions…
          </p>{/if}
        {#each revisions as revision (revision.entry_id ?? revision.id)}
          <button
            type="button"
            class="version-card"
            class:selected={(revision.entry_id ?? revision.id) ===
              selectedEntry}
            aria-pressed={(revision.entry_id ?? revision.id) === selectedEntry}
            aria-label={revision.repair_batch
              ? revision.summary
              : `Version ${revision.revision_number}: ${revision.summary}`}
            disabled={busy}
            onclick={() => void chooseRevision(revision)}
          >
            <span class="flex flex-wrap items-center gap-2">
              <strong
                >{revision.repair_batch
                  ? 'Automatic timing repair'
                  : `Version ${revision.revision_number}`}</strong
              >
              {#if revision.id === effectiveActiveRevisionId}<span
                  class="version-badge active-badge">Active</span
                >{/if}
            </span>
            <span class="version-summary">{revision.summary}</span>
            <span class="muted text-xs"
              >{revision.origin === 'automatic' ? 'Automatic' : 'Manual'} · {revision.segment_count}
              blocks</span
            >
            {#if revision.repair_status}<span
                class="version-badge"
                class:applied={revision.repair_status === 'applied'}
                >{repairLabel(revision.repair_status)}</span
              >{/if}
          </button>
        {/each}
        {#if nextRevision !== null}<button
            type="button"
            class="btn mt-2 w-full"
            disabled={busy}
            onclick={() => void loadHistory(true)}
            >Load earlier revisions</button
          >{/if}
      </nav>
      <section
        aria-label="Version preview"
        class="version-preview"
        aria-busy={previewLoading}
      >
        {#if current}
          {#if current.repair_batch}
            {#key current.entry_id}
              <RepairBatchPanel
                {sessionId}
                batch={current.repair_batch}
                disabled={disabled || busy || previewLoading}
                onpreview={(revisionId) => void preview(revisionId)}
                onundone={async (revisionId) => {
                  serverActiveRevisionId = revisionId;
                  await loadHistory();
                }}
              />
            {/key}
          {/if}
          <div class="mb-5 border-b border-[var(--line)] pb-4">
            <h3 class="text-lg font-semibold">
              {inspectingCheckpoint
                ? selected === current.repair_batch?.base_revision_id
                  ? 'Original plan before repairs'
                  : 'Repair checkpoint preview'
                : current.repair_batch
                  ? current.is_repair_checkpoint
                    ? 'Selected repair checkpoint'
                    : current.repair_batch.applied_count
                      ? 'Repaired plan'
                      : 'Original plan — no repairs applied'
                  : `Version ${current.revision_number}`}
            </h3>
            <p class="mt-1 text-sm">{current.summary}</p>
            {#if current.repair_status}
              <p class="mt-3 text-sm">
                <strong>Repair: {repairLabel(current.repair_status)}</strong
                >{#if current.source_block_ordinal != null}
                  · Original block {current.source_block_ordinal + 1}{/if}
              </p>
              {#if current.repair_reason}<p class="muted mt-1 text-sm">
                  {repairDetail(current.repair_reason)}
                </p>{/if}
              {#if current.repair_status === 'unknown'}<p
                  class="muted mt-1 text-sm"
                >
                  This older version has no recorded repair outcome.
                </p>{/if}
            {/if}
            {#if !inspectingCheckpoint}
              <p class="muted mt-3 text-xs">
                {current.reusable_segment_count} reusable · {current.stale_segment_count}
                missing or stale
                {#if parentRevision}
                  · Based on version {parentRevision.revision_number}{/if}
              </p>
              <AudioReuseNotice
                settingsStale={current.audio_settings_stale_segment_count}
                identityUnknown={current.audio_identity_unknown_segment_count}
              />
            {/if}
            <div class="mt-4 flex flex-wrap gap-2">
              {#if selected !== effectiveActiveRevisionId}
                {#if onselect}<button
                    type="button"
                    class="btn btn-primary"
                    disabled={busy || disabled || previewLoading}
                    onclick={() => void restore(false)}
                    >Select this revision</button
                  >{/if}
                <button
                  type="button"
                  class="btn"
                  disabled={busy || disabled || previewLoading}
                  onclick={() => void restore()}
                  >Restore as a new active revision</button
                >
              {:else}
                <button
                  type="button"
                  class="btn btn-primary"
                  disabled={busy || disabled || previewLoading}
                  onclick={async () => {
                    close();
                    await ongenerate(false);
                  }}>Generate this revision</button
                >
                <button
                  type="button"
                  class="btn"
                  disabled={busy ||
                    disabled ||
                    previewLoading ||
                    current.stale_segment_count === 0}
                  onclick={async () => {
                    close();
                    await ongenerate(true);
                  }}>Generate missing / stale only</button
                >
              {/if}
            </div>
            <details class="muted mt-4 text-xs">
              <summary class="cursor-pointer">Revision details</summary>
              <p class="mt-2 break-all">Revision ID: {selected}</p>
              {#if current.parent_revision_id}<p class="mt-1 break-all">
                  Parent ID: {current.parent_revision_id}
                </p>{/if}
            </details>
          </div>
        {/if}
        {#if previewLoading && !blocks.length}<p
            class="muted text-sm"
            role="status"
          >
            Loading blocks…
          </p>{/if}
        <div class="space-y-3">
          {#each blocks as block (block.id)}
            <article class="rounded-xl border border-[var(--line)] p-4">
              <div class="muted mb-2 break-words text-xs">
                Block {block.ordinal + 1} · {block.status} · source cues {block.source_segment_ids.join(
                  ', '
                ) || '—'}
              </div>
              <p
                class="whitespace-pre-wrap break-words text-sm leading-relaxed"
              >
                {block.text}
              </p>
            </article>
          {/each}
        </div>
        {#if nextCursor !== null}<button
            type="button"
            class="btn mt-3"
            disabled={previewLoading}
            onclick={() => void preview(selected, true)}
            >Load more blocks</button
          >{/if}
      </section>
    </div>
  </dialog>
{/if}

<style>
  .history-dialog {
    width: min(70rem, calc(100vw - 2rem));
    height: min(54rem, 88dvh);
    max-height: 88dvh;
    max-width: none;
    margin: auto;
    padding: 0;
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 1.2rem;
    background: var(--paper);
    color: var(--ink);
    box-shadow: var(--shadow);
  }
  .history-dialog[open] {
    display: flex;
    flex-direction: column;
  }
  .history-dialog::backdrop {
    background: rgb(0 0 0 / 40%);
    backdrop-filter: blur(3px);
  }
  .history-header {
    display: flex;
    flex: none;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    padding: 1.25rem;
    border-bottom: 1px solid var(--line);
  }
  .history-body {
    display: grid;
    grid-template-columns: minmax(16rem, 19rem) minmax(0, 1fr);
    flex: 1;
    min-height: 0;
  }
  .version-list {
    overflow: auto;
    padding: 0.75rem;
    border-right: 1px solid var(--line);
    background: var(--paper-strong);
  }
  .version-card {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 0.5rem;
    width: 100%;
    padding: 0.85rem;
    margin-bottom: 0.5rem;
    border: 1px solid transparent;
    border-radius: 0.8rem;
    text-align: left;
    font-size: 0.8rem;
  }
  .version-card:hover {
    background: var(--accent-soft);
  }
  .version-card.selected {
    border-color: var(--accent);
    background: var(--accent-soft);
  }
  .version-card:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  .version-summary {
    overflow-wrap: anywhere;
    line-height: 1.5;
  }
  .version-badge {
    display: inline-block;
    width: fit-content;
    border-radius: 0.35rem;
    padding: 0.12rem 0.4rem;
    border: 1px solid var(--line);
    font-size: 0.65rem;
    font-weight: 600;
  }
  .active-badge,
  .applied {
    color: var(--accent);
    background: var(--paper);
  }
  .version-preview {
    min-width: 0;
    overflow: auto;
    padding: 1.25rem;
  }
  @media (max-width: 700px) {
    .history-body {
      grid-template-columns: minmax(0, 1fr);
      grid-template-rows: minmax(8rem, 32%) minmax(0, 1fr);
    }
    .version-list {
      border-right: 0;
      border-bottom: 1px solid var(--line);
    }
    .version-preview {
      padding: 1rem;
    }
  }
</style>
