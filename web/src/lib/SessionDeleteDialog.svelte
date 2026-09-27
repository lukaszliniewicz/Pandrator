<script lang="ts">
  import { onMount } from 'svelte';
  import { apiJson } from './api';
  import { errorMessage } from './errors';
  import { modalDialog } from './modal-dialog';
  import type { SessionRecord } from './api-models';
  let {
    session,
    onclose,
    ondeleted
  }: {
    session: SessionRecord;
    onclose: () => void;
    ondeleted: () => Promise<void>;
  } = $props();
  type Preview = {
    revision: number;
    impact_token: string;
    can_purge: boolean;
    blockers: string[];
    owned_file_count: number;
    owned_bytes: number;
    retained_shared_count: number;
    scheduled_delete_at: string | null;
  };
  let preview = $state<Preview | null>(null);
  let busy = $state(false);
  let error = $state('');
  let started = $state(false);
  async function load() {
    try {
      preview = await apiJson<Preview>(`/sessions/${session.id}/purge-preview`);
      error = '';
    } catch (caught) {
      error = errorMessage(caught);
    }
  }
  async function remove() {
    if (!preview?.can_purge || busy) return;
    busy = true;
    error = '';
    try {
      const result = await apiJson<{ state: string; error?: string }>(
        `/sessions/${session.id}/purge`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            expected_revision: preview.revision,
            impact_token: preview.impact_token
          })
        }
      );
      if (result.state !== 'complete') {
        started = true;
        throw new Error(
          result.error ||
            'Cleanup is incomplete. Retry to finish deleting the remaining files.'
        );
      }
      await ondeleted();
      onclose();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
  const blockerLabel = (reason: string) =>
    reason.startsWith('unfinished:')
      ? 'This session has unfinished work. Finish or cancel it before deleting.'
      : reason.startsWith('external_reference:')
        ? 'Another item depends on this session. Remove that dependency before deleting.'
        : reason.startsWith('unsafe_path:') ||
            reason.startsWith('unmanaged_artifact:')
          ? 'Some files cannot be safely removed from managed storage.'
          : reason === 'not_trashed'
            ? 'Move this session to Trash first.'
            : reason.replaceAll('_', ' ');
  onMount(() => {
    void load();
  });
</script>

<dialog
  use:modalDialog={{ onclose: () => !busy && onclose() }}
  aria-labelledby="delete-session-title"
  class="compact-confirmation m-auto max-h-[calc(100dvh-2rem)] w-[min(34rem,calc(100vw-2rem))] overflow-y-auto rounded-2xl border border-[var(--line)] bg-[var(--paper)] p-6 text-[var(--ink)] backdrop:bg-black/40"
>
  <h2 id="delete-session-title" class="text-xl font-semibold">
    Delete session permanently?
  </h2>
  <p class="mt-3 break-words font-semibold">{session.name}</p>
  <p class="muted mt-2 text-sm">
    This removes the session and its owned recordings, text revisions, and
    managed files. You cannot restore it from Trash afterward.
  </p>
  {#if preview}
    <p class="mt-4 text-sm">
      {preview.owned_file_count} managed files · {(
        preview.owned_bytes /
        1024 /
        1024
      ).toFixed(1)} MB
    </p>
    <p class="muted mt-2 text-xs">
      Shared source and voice-library files, external originals, and
      independently saved exports are retained.
    </p>
    {#if preview.retained_shared_count}<p class="muted mt-2 text-xs">
        {preview.retained_shared_count} shared files will be kept.
      </p>{/if}
    {#if preview.blockers.length}<ul
        class="mt-4 list-disc space-y-2 pl-5 text-sm text-amber-700"
      >
        {#each [...new Set(preview.blockers.map(blockerLabel))] as blocker}<li>
            {blocker}
          </li>{/each}
      </ul>{/if}
  {:else if !error}<p class="muted mt-4 text-sm" role="status">
      Checking files and dependencies…
    </p>{/if}
  {#if error}<p class="mt-4 text-sm text-red-600" role="alert">{error}</p>
    <button class="btn mt-2" disabled={busy} onclick={load}
      >Refresh deletion preview</button
    >{/if}
  {#if started}<p class="mt-2 text-sm">
      Deletion has started. Retry cleanup to finish; this session can no longer
      be restored.
    </p>{/if}
  <div class="mt-6 flex flex-wrap justify-end gap-3">
    <button class="btn" disabled={busy} onclick={onclose}
      >{started ? 'Close' : 'Keep in Trash'}</button
    >
    <button
      class="btn border-red-600 text-red-700"
      disabled={busy || !preview?.can_purge}
      onclick={remove}
      >{busy
        ? 'Deleting…'
        : started
          ? 'Retry deletion'
          : 'Delete permanently'}</button
    >
  </div>
</dialog>
