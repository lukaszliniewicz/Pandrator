<script lang="ts">
  import { apiJson } from './api';
  import { errorMessage } from './errors';
  import { modalFocus } from './modal-focus';

  type Revision = {
    id: string;
    revision_number: number;
    parent_revision_id: string | null;
    summary: string;
    origin: string;
    segment_count: number;
    reusable_segment_count: number;
    stale_segment_count: number;
    source_artifact_id: string | null;
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
  let error = $state('');
  let revisions = $state<Revision[]>([]);
  let selected = $state('');
  let blocks = $state<Block[]>([]);
  let nextCursor = $state<number | null>(null);
  let nextRevision = $state<number | null>(null);
  const current = $derived(revisions.find((item) => item.id === selected));
  const base = $derived(`/api/v1/sessions/${encodeURIComponent(sessionId)}`);
  let previewRequest = 0;

  async function preview(revisionId: string, append = false) {
    selected = revisionId;
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
        next_before_revision_number: number | null;
      }>(`${base}/generation-plan/revisions?${query}`);
      revisions = append ? [...revisions, ...result.items] : result.items;
      nextRevision = result.next_before_revision_number;
      if (!append) await preview(activeRevisionId ?? revisions[0]?.id ?? '');
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }

  async function restore(copy = true) {
    if (!selected || selected === activeRevisionId) return;
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
  <div
    class="fixed inset-0 z-[90] flex items-center justify-center bg-black/40 p-4"
  >
    <!-- svelte-ignore a11y_no_noninteractive_element_to_interactive_role -->
    <section
      use:modalFocus={{ onclose: () => (open = false) }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="speech-plan-history-title"
      class="max-h-[85vh] w-full max-w-3xl overflow-auto rounded-2xl border border-[var(--line)] bg-[var(--paper)] p-5 shadow-xl"
    >
      <div class="mb-4 flex items-center justify-between gap-4">
        <h2 id="speech-plan-history-title" class="text-lg font-semibold">
          Versioned speech plans
        </h2>
        <button type="button" class="btn" onclick={() => (open = false)}
          >Close</button
        >
      </div>
      <p class="muted mb-4 text-sm">
        Inspect an earlier plan without changing the active one. Restoring
        creates a new revision; existing history and reusable takes are
        retained.
      </p>
      {#if error}<p role="alert" class="mb-3 text-red-700">{error}</p>{/if}
      <label class="mb-3 block text-sm"
        >Speech-plan revision
        <select
          class="field mt-1 w-full"
          value={selected}
          disabled={busy}
          onchange={(event) => void preview(event.currentTarget.value)}
        >
          {#each revisions as revision (revision.id)}
            <option value={revision.id}
              >r{revision.revision_number} · {revision.origin} · {revision.summary}
              · {revision.segment_count} blocks{revision.id === activeRevisionId
                ? ' · active'
                : ''}</option
            >
          {/each}
        </select>
      </label>
      {#if nextRevision !== null}<button
          type="button"
          class="btn mb-3"
          disabled={busy}
          onclick={() => void loadHistory(true)}>Load earlier revisions</button
        >{/if}
      {#if current}
        <p class="muted mb-3 text-sm">
          {current.reusable_segment_count} reusable · {current.stale_segment_count}
          missing or stale. Revision ID: <code>{current.id}</code>
        </p>
        <div class="mb-4 flex flex-wrap gap-2">
          {#if selected !== activeRevisionId}
            {#if onselect}<button
                type="button"
                class="btn btn-primary"
                disabled={busy || disabled}
                onclick={() => void restore(false)}>Select this revision</button
              >{/if}
            <button
              type="button"
              class="btn btn-primary"
              disabled={busy || disabled}
              onclick={() => void restore()}
              >Restore as a new active revision</button
            >
          {:else}
            <button
              type="button"
              class="btn btn-primary"
              disabled={busy || disabled}
              onclick={async () => {
                open = false;
                await ongenerate(false);
              }}>Generate this revision</button
            >
            <button
              type="button"
              class="btn"
              disabled={busy || disabled || current.stale_segment_count === 0}
              onclick={async () => {
                open = false;
                await ongenerate(true);
              }}>Generate missing / stale only</button
            >
          {/if}
        </div>
      {/if}
      <div class="space-y-2">
        {#each blocks as block (block.id)}
          <article class="rounded-lg border border-[var(--line)] p-3">
            <div class="muted mb-1 text-xs">
              Block {block.ordinal + 1} · {block.status} · source cues {block.source_segment_ids.join(
                ', '
              ) || '—'}
            </div>
            <p class="whitespace-pre-wrap text-sm">{block.text}</p>
          </article>
        {/each}
      </div>
      {#if nextCursor !== null}<button
          type="button"
          class="btn mt-3"
          onclick={() => void preview(selected, true)}>Load more blocks</button
        >{/if}
    </section>
  </div>
{/if}
