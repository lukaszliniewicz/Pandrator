<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/state';
  import { goto } from '$app/navigation';
  import { ApiError, ApiMutationAttempt, apiJson } from '$lib/api';
  import { publicLinkApi, type PublicReview } from '$lib/public-link-api';
  import {
    activeProjectLink,
    clearProjectLink,
    selectProjectLink
  } from '$lib/project-link-context';
  import { guardEditorNavigation } from '$lib/editor-navigation';
  import UnsavedChangesDialog from '$lib/UnsavedChangesDialog.svelte';
  import { errorMessage } from '$lib/errors';
  let preview = $state<PublicReview>();
  let drafts = $state<Record<string, string>>({});
  let originals = $state<Record<string, string>>({});
  let reviewer = $state('');
  let offset = $state(0);
  let loading = $state(true);
  let saving = $state(false);
  let error = $state('');
  let notice = $state('');
  let submitted = $state(false);
  let sessionEnded = $state(false);
  let renewalLink = $state('');
  let pendingLeave = $state<(() => void) | null>(null);
  let attempt = new ApiMutationAttempt();
  const changes = $derived(
    Object.entries(drafts)
      .filter(([id, text]) => text !== originals[id])
      .map(([segment_id, text]) => ({ segment_id, text }))
  );
  function requestLeave(action: () => void) {
    if (saving) return;
    if (changes.length) pendingLeave = action;
    else action();
  }
  function failed(caught: unknown) {
    error = errorMessage(caught);
    if (
      caught instanceof ApiError &&
      (caught.status === 401 || caught.code === 'csrf_failed')
    )
      sessionEnded = true;
  }
  async function renew() {
    saving = true;
    try {
      const url = new URL(renewalLink.trim());
      if (
        url.origin !== location.origin ||
        url.pathname !== `/share/${page.params.id}`
      )
        throw new Error('Use the original link for this review.');
      const token = new URLSearchParams(url.hash.slice(1)).get('token');
      if (!token)
        throw new Error(
          'Paste the complete review link, including its fragment.'
        );
      const result = await publicLinkApi.redeem(page.params.id!, token);
      selectProjectLink({
        id: result.link.id,
        csrf: result.csrf_token,
        mode: result.link.mode,
        projectId: result.link.project_id,
        rootSessionId: result.link.root_session_id,
        label: result.link.label
      });
      sessionEnded = false;
      renewalLink = '';
      error = '';
      notice = 'Visitor access renewed. Your draft is preserved.';
    } catch (caught) {
      failed(caught);
    } finally {
      saving = false;
    }
  }
  function downloadDraft() {
    const url = URL.createObjectURL(
      new Blob(
        [
          JSON.stringify(
            {
              target_artifact_id: preview?.target_artifact_id,
              expected_source_hash: preview?.target_hash,
              reviewer_label: reviewer,
              changes
            },
            null,
            2
          )
        ],
        { type: 'application/json' }
      )
    );
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = 'pandrator-translation-review-draft.json';
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  guardEditorNavigation({
    dirty: () => changes.length > 0,
    pending: () => saving,
    requestLeave
  });
  async function load(next = offset) {
    loading = true;
    error = '';
    try {
      const data = await publicLinkApi.preview(page.params.id!, next);
      preview = data;
      offset = next;
      for (const item of data.items) {
        if (!(item.id in originals)) originals[item.id] = item.text;
        if (!(item.id in drafts)) drafts[item.id] = item.text;
      }
    } catch (caught) {
      failed(caught);
    } finally {
      loading = false;
    }
  }
  async function save(): Promise<boolean> {
    if (!preview || !changes.length || saving) return false;
    saving = true;
    error = '';
    try {
      await publicLinkApi.submit(
        page.params.id!,
        {
          expected_source_hash: preview.target_hash,
          changes,
          reviewer_label: reviewer.trim() || null
        },
        attempt
      );
      for (const item of changes) originals[item.segment_id] = item.text;
      notice =
        'Corrections submitted for review. The selected project subtitles have not changed.';
      submitted = true;
      attempt = new ApiMutationAttempt();
      return true;
    } catch (caught) {
      failed(caught);
      return false;
    } finally {
      saving = false;
    }
  }
  async function leave() {
    try {
      await publicLinkApi.leave(page.params.id!);
      clearProjectLink();
      const { appState } = await import('$lib/app-state.svelte');
      await appState.initialize();
      await goto('/');
    } catch (caught) {
      failed(caught);
    }
  }
  async function saveAndLeave() {
    const action = pendingLeave;
    if (await save()) {
      pendingLeave = null;
      action?.();
    }
  }
  onMount(() => {
    if (activeProjectLink()?.id !== page.params.id) {
      loading = false;
      error = 'Open the complete review link to start a visitor session.';
    } else void load();
    const heartbeat = setInterval(() => {
      if (
        changes.length &&
        !submitted &&
        !saving &&
        document.visibilityState === 'visible'
      )
        void apiJson<{ authenticated: boolean }>('/auth/status')
          .then((status) => {
            if (!status.authenticated) {
              sessionEnded = true;
              error =
                'Visitor access has ended. Your draft is preserved; reopen access below or download the draft.';
            }
          })
          .catch(failed);
    }, 300000);
    return () => clearInterval(heartbeat);
  });
</script>

<main class="mx-auto flex min-h-screen max-w-6xl flex-col gap-4 p-4 sm:p-6">
  <header
    class="sticky top-0 z-10 -mx-4 flex flex-wrap items-center justify-between gap-3 border-b border-[var(--line)] bg-[var(--paper)] px-4 py-3 sm:-mx-6 sm:px-6"
  >
    <div>
      <h1 class="text-2xl font-semibold">Review subtitle translation</h1>
      <p class="muted mt-1 text-sm">
        {preview?.label ?? 'Shared translation'} · Text corrections only · Saved changes
        require project-editor acceptance.
      </p>
    </div>
    <div class="flex gap-2">
      <button
        class="btn btn-secondary"
        onclick={() => requestLeave(() => void leave())}
        disabled={saving}>Leave review</button
      >{#if !submitted}<button
          class="btn btn-primary"
          onclick={() => void save()}
          disabled={saving || changes.length === 0}
          >{saving
            ? 'Submitting…'
            : `Submit corrections${changes.length ? ` (${changes.length})` : ''}`}</button
        >{/if}
    </div>
  </header>
  {#if !submitted}<label class="text-sm"
      >Your name or review label (optional)<input
        class="input mt-1 block w-full max-w-md"
        bind:value={reviewer}
        maxlength="80"
        disabled={saving}
      /><span class="muted text-xs"
        >This is an unverified attribution label. Changes across pages are
        submitted together when you finish your review.</span
      ></label
    >{/if}
  {#if error}<p
      role="alert"
      class="rounded-xl border border-red-300 p-3 text-red-700 dark:text-red-300"
    >
      {error}
    </p>{/if}
  {#if notice}<p role="status" class="rounded-xl bg-[var(--accent-soft)] p-3">
      {notice}
    </p>{/if}
  {#if sessionEnded}<form
      class="surface rounded-xl p-4"
      onsubmit={(event) => {
        event.preventDefault();
        void renew();
      }}
    >
      <p class="text-sm">
        Your unsaved corrections are still here. Paste the original review link
        to renew visitor access.
      </p>
      <label class="mt-3 block text-sm"
        >Original review link<input
          class="input mt-1 w-full"
          type="url"
          bind:value={renewalLink}
          disabled={saving}
          autocomplete="off"
          spellcheck="false"
        /></label
      ><button
        class="btn btn-secondary mt-3"
        disabled={saving || !renewalLink.trim()}
        >Renew access and keep draft</button
      >
    </form>{/if}
  {#if changes.length && !submitted}<button
      class="btn btn-secondary self-start"
      onclick={downloadDraft}>Download unsaved draft</button
    >{/if}
  {#if loading}<p role="status">Loading translation…</p>{/if}
  {#if submitted}<section class="surface rounded-xl p-6">
      <h2 class="text-xl font-semibold">Review submitted</h2>
      <p class="muted mt-2">
        A project editor can inspect, accept or reject your corrections. You can
        leave this review or close the window.
      </p>
    </section>{/if}
  {#if preview && !submitted}<div
      class="space-y-3"
      aria-label="Translation passages"
    >
      {#each preview.items as item (item.id)}<article
          class="surface rounded-xl p-4"
        >
          <label class="block text-sm font-semibold"
            >Passage {item.ordinal + 1}<span class="muted ml-3 font-normal"
              >{item.start_ms === null || item.end_ms === null
                ? 'Untimed passage'
                : `${(item.start_ms / 1000).toFixed(2)}–${(item.end_ms / 1000).toFixed(2)} s`}</span
            ><textarea
              class="input mt-2 min-h-24 w-full font-normal"
              aria-label={`Translation passage ${item.ordinal + 1}`}
              bind:value={drafts[item.id]}
              disabled={saving}
              maxlength="20000"></textarea></label
          >
        </article>{/each}
    </div>
    <nav
      class="sticky bottom-0 flex items-center justify-end gap-3 bg-[var(--paper)] py-3"
      aria-label="Translation pages"
    >
      <span class="mr-auto text-sm"
        >{preview.total} passages · {changes.length} unsaved changes</span
      ><button
        class="btn btn-secondary"
        disabled={loading || saving || offset === 0}
        onclick={() => void load(Math.max(0, offset - 50))}>Previous</button
      ><button
        class="btn btn-secondary"
        disabled={loading || saving || offset + 50 >= preview.total}
        onclick={() => void load(offset + 50)}>Next</button
      >
    </nav>{/if}
</main>
{#if pendingLeave}<UnsavedChangesDialog
    busy={saving}
    {error}
    onsave={saveAndLeave}
    oncancel={() => (pendingLeave = null)}
    ondiscard={() => {
      const action = pendingLeave;
      pendingLeave = null;
      action?.();
    }}
    saveLabel="Submit and leave"
    description="Submit your corrections for project-editor review, discard this unsaved draft, or keep editing."
  />{/if}
