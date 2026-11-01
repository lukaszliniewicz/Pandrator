<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import ReviewWorkspace from './ReviewWorkspace.svelte';
  import { ApiMutationAttempt, apiJson } from './api';
  import {
    publicLinkApi,
    type ProjectLink,
    type ReviewSubmission,
    type SubmissionDetail
  } from './public-link-api';
  import { appState } from './app-state.svelte';
  import { errorMessage } from './errors';
  let {
    projectId,
    sessionId,
    projectName,
    canManage,
    onclose,
    onNamedShare
  }: {
    projectId: string;
    sessionId: string;
    projectName: string;
    canManage: boolean;
    onclose: () => void;
    onNamedShare?: () => void;
  } = $props();
  let tab = $state<'links' | 'reviews'>(
    untrack(() => (canManage ? 'links' : 'reviews'))
  );
  let links = $state<ProjectLink[]>([]);
  let submissions = $state<ReviewSubmission[]>([]);
  let detail = $state<SubmissionDetail>();
  let detailId = $state('');
  let status = $state('pending');
  let offset = $state(0);
  let mode = $state<ProjectLink['mode']>('review_translation');
  let label = $state('');
  let days = $state(7);
  let translation = $state('');
  let error = $state('');
  let notice = $state('');
  let url = $state('');
  let busy = $state(false);
  let loading = $state(true);
  let requestKey = crypto.randomUUID();
  const decision = new ApiMutationAttempt();
  async function load() {
    loading = true;
    error = '';
    try {
      const [access, reviews] = await Promise.all([
        canManage
          ? publicLinkApi.list(projectId)
          : Promise.resolve({ items: [] }),
        publicLinkApi.submissions(projectId, status, offset)
      ]);
      links = access.items;
      submissions = reviews.items;
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      loading = false;
    }
  }
  async function loadTranslation() {
    try {
      const workflow = await apiJson<{
        stages: {
          key: string;
          selected_artifact_id?: string | null;
          artifact?: { id: string } | null;
        }[];
      }>(`/sessions/${sessionId}/workflow`);
      const stage = workflow.stages.find((item) => item.key === 'translate');
      translation = stage?.selected_artifact_id ?? stage?.artifact?.id ?? '';
    } catch {
      translation = '';
    }
  }
  async function create() {
    busy = true;
    error = '';
    notice = '';
    url = '';
    try {
      const result = await publicLinkApi.create(projectId, {
        label: label.trim(),
        mode,
        expires_in_days: days,
        ...(mode === 'review_translation'
          ? { target_artifact_id: translation }
          : {}),
        request_key: requestKey
      });
      url = result.url ?? '';
      notice = url
        ? 'Copy this link now. Its secret is shown only once.'
        : 'This request already created a link. Its secret cannot be recovered; revoke it and create a new one if necessary.';
      requestKey = crypto.randomUUID();
      await load();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
  async function revoke(link: ProjectLink) {
    busy = true;
    error = '';
    try {
      await publicLinkApi.revoke(link);
      await load();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
  async function inspect(id: string, at = 0) {
    busy = true;
    error = '';
    try {
      detail = await publicLinkApi.submission(id, at);
      detailId = id;
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
  async function decide(action: 'accept' | 'reject') {
    if (!detail) return;
    busy = true;
    error = '';
    try {
      await publicLinkApi.decide(detail, action, decision);
      notice =
        action === 'accept'
          ? 'Corrections accepted as a new reviewed translation and selected for output.'
          : 'Submission rejected; the project translation is unchanged.';
      await load();
      detail = await publicLinkApi.submission(detailId, detail.offset);
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
  async function copy() {
    try {
      await navigator.clipboard.writeText(url);
      notice = 'Link copied. Send it privately to its intended recipient.';
    } catch {
      notice = 'Select and copy the link from the field below.';
    }
  }
  onMount(() => {
    void load();
    if (canManage) void loadTranslation();
  });
</script>

<ReviewWorkspace
  titleId="project-links-title"
  closeLabel="Close project sharing"
  {onclose}
  {busy}
>
  {#snippet header()}<div>
      <h2 id="project-links-title" class="text-xl font-semibold">
        Share and review
      </h2>
      <p class="muted text-sm">{projectName}</p>
    </div>{/snippet}
  {#snippet notices()}
    {#if error}<p role="alert" class="p-3 text-red-700 dark:text-red-300">
        {error}
      </p>{/if}
    {#if notice}<p role="status" class="bg-[var(--accent-soft)] p-3 text-sm">
        {notice}
      </p>{/if}
  {/snippet}
  <div class="min-h-0 flex-1 overflow-auto p-4 sm:p-6">
    <nav class="mb-5 flex flex-wrap gap-2" aria-label="Sharing sections">
      {#if canManage}<button
          class="btn btn-secondary"
          aria-pressed={tab === 'links'}
          onclick={() => {
            tab = 'links';
            detail = undefined;
            notice = '';
            void load();
          }}>Access links</button
        >{/if}
      <button
        class="btn btn-secondary"
        aria-pressed={tab === 'reviews'}
        onclick={() => {
          tab = 'reviews';
          detail = undefined;
          notice = '';
          void load();
        }}>Submitted corrections</button
      >
      {#if onNamedShare}<button
          class="btn btn-secondary ml-auto"
          onclick={onNamedShare}>Named account access</button
        >{/if}
    </nav>
    {#if loading}<p role="status">Loading sharing…</p>{/if}
    {#if tab === 'links'}
      <p class="muted text-sm">
        Anyone holding a link can use its stated access. Create a separately
        labelled link for each recipient. Revocation stops new access; admitted
        processing work follows the normal project job rules.
      </p>
      {#if appState.publicLinksEnabled}<form
          class="mt-4 grid gap-4 rounded-xl border border-[var(--line)] p-4 sm:grid-cols-2"
          onsubmit={(event) => {
            event.preventDefault();
            void create();
          }}
        >
          <label class="text-sm font-semibold"
            >Recipient or purpose<input
              class="input mt-1 w-full"
              bind:value={label}
              maxlength="80"
              required
              disabled={busy}
            /></label
          >
          <label class="text-sm font-semibold"
            >Link access<select
              class="input mt-1 w-full"
              bind:value={mode}
              disabled={busy}
              ><option value="review_translation"
                >Review this translation</option
              ><option value="manage_project">Manage project</option></select
            ></label
          >
          <label class="text-sm font-semibold"
            >Expires after (days)<input
              class="input mt-1 w-full"
              type="number"
              min="1"
              max="30"
              bind:value={days}
              disabled={busy}
            /></label
          >
          <div class="self-end">
            <button
              class="btn btn-primary"
              disabled={busy ||
                !label.trim() ||
                (mode === 'review_translation' && !translation)}
              >{busy ? 'Creating…' : 'Create link'}</button
            >
          </div>
          <p class="muted text-sm sm:col-span-2">
            {mode === 'manage_project'
              ? 'Management permits editing, processing and exports using configured resources, including paid services. Installation, accounts, workspace libraries and further sharing remain private.'
              : translation
                ? 'Reviewers can correct the selected translation in this language version. Saves become pending submissions; a project editor accepts or rejects them.'
                : 'Select a current translation in this language version before creating a review link.'}
          </p>
        </form>{:else}<p
          class="mt-4 rounded-xl border border-[var(--line)] p-3 text-sm"
        >
          New links are disabled in General settings. Existing submissions
          remain reviewable.
        </p>{/if}
      {#if url}<div class="mt-4 rounded-xl bg-[var(--accent-soft)] p-4">
          <label class="block text-sm font-semibold"
            >New sharing link<textarea
              class="input mt-2 min-h-20 w-full"
              readonly
              value={url}
              spellcheck="false"></textarea></label
          ><button class="btn btn-secondary mt-2" onclick={copy}
            >Copy link</button
          >
        </div>{/if}
      <h3 class="mt-6 font-semibold">Issued links</h3>
      {#if !loading && links.length === 0}<p class="muted mt-2 text-sm">
          No links have been issued.
        </p>{/if}
      <ul class="mt-3 space-y-2">
        {#each links as link (link.id)}<li
            class="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-[var(--line)] p-3"
          >
            <div>
              <strong>{link.label}</strong>
              <p class="muted text-sm">
                {link.mode === 'manage_project'
                  ? 'Project management'
                  : 'Translation review'} · {link.revoked_at
                  ? 'Revoked'
                  : Date.parse(link.expires_at) < Date.now()
                    ? 'Expired'
                    : `Expires ${new Date(link.expires_at).toLocaleString()}`}
              </p>
            </div>
            <button
              class="btn btn-secondary"
              disabled={busy || Boolean(link.revoked_at)}
              onclick={() => void revoke(link)}>Revoke</button
            >
          </li>{/each}
      </ul>
    {:else if detail}
      <button
        class="btn btn-secondary"
        onclick={() => {
          detail = undefined;
        }}
        disabled={busy}>Back to submissions</button
      >
      <p class="mt-4 text-sm">
        {detail.submission.change_count} changes · {detail.submission.status} · Reviewer
        label: {detail.submission.reviewer_label || 'Not supplied'} (unverified)
      </p>
      {#if detail.source_unavailable}<p role="status" class="mt-3 text-sm">
          The pinned source is unavailable. Saved corrections remain visible;
          acceptance is blocked.
        </p>
      {:else if detail.selected_artifact_id !== detail.target_artifact_id}<p
          role="status"
          class="mt-3 text-sm"
        >
          The selected translation has changed. Compare these corrections with
          the current version before applying them manually.
        </p>{/if}
      <div class="mt-4 space-y-3">
        {#each detail.changes as change (change.segment_id)}<article
            class="rounded-xl border border-[var(--line)] p-3"
          >
            <h3 class="mb-2 text-sm font-semibold">
              {change.ordinal === null
                ? 'Saved passage'
                : `Passage ${change.ordinal + 1}`}
            </h3>
            <div class="grid gap-3 md:grid-cols-2">
              <div>
                <h4 class="muted text-xs font-semibold">Pinned translation</h4>
                <pre
                  class="mt-1 whitespace-pre-wrap break-words text-sm">{change.before ??
                    'Source unavailable'}</pre>
              </div>
              <div>
                <h4 class="muted text-xs font-semibold">
                  Submitted correction
                </h4>
                <pre
                  class="mt-1 whitespace-pre-wrap break-words text-sm">{change.after}</pre>
              </div>
            </div>
          </article>{/each}
      </div>
      <nav
        class="mt-4 flex justify-end gap-3"
        aria-label="Submitted correction pages"
      >
        <span class="mr-auto text-sm"
          >{detail.offset + 1}–{Math.min(
            detail.offset + detail.limit,
            detail.total
          )} of {detail.total}</span
        ><button
          class="btn btn-secondary"
          disabled={busy || detail.offset === 0}
          onclick={() =>
            void inspect(detailId, Math.max(0, detail!.offset - 50))}
          >Previous</button
        ><button
          class="btn btn-secondary"
          disabled={busy || detail.offset + detail.limit >= detail.total}
          onclick={() => void inspect(detailId, detail!.offset + 50)}
          >Next</button
        >
      </nav>
      {#if detail.submission.status === 'pending'}<div
          class="mt-6 flex flex-wrap gap-3 border-t border-[var(--line)] pt-4"
        >
          <button
            class="btn btn-primary"
            disabled={busy ||
              detail.source_unavailable ||
              detail.selected_artifact_id !== detail.target_artifact_id}
            onclick={() => void decide('accept')}
            >Accept and use reviewed subtitles</button
          ><button
            class="btn btn-secondary"
            disabled={busy}
            onclick={() => void decide('reject')}>Reject submission</button
          >
          <p class="muted w-full text-sm">
            Acceptance saves a new immutable translation revision and selects it
            for output. Intervening project changes cause a conflict.
          </p>
        </div>{/if}
    {:else}
      <label class="block max-w-xs text-sm font-semibold"
        >Submission status<select
          class="input mt-1 w-full"
          bind:value={status}
          onchange={() => {
            offset = 0;
            void load();
          }}
          ><option value="pending">Pending review</option><option
            value="accepted">Accepted</option
          ><option value="rejected">Rejected</option></select
        ></label
      >
      {#if !loading && submissions.length === 0}<p class="muted mt-4">
          No {status} submissions.
        </p>{/if}
      <ul class="mt-4 space-y-2">
        {#each submissions as submission}<li
            class="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-[var(--line)] p-3"
          >
            <div>
              <strong
                >{submission.reviewer_label ||
                  submission.link_label ||
                  'Translation review'}</strong
              >
              <p class="muted text-sm">
                {submission.change_count} changes · {new Date(
                  submission.created_at
                ).toLocaleString()} · {submission.status}
              </p>
            </div>
            <button
              class="btn btn-secondary"
              disabled={busy}
              onclick={() => void inspect(submission.submission_id)}
              >Review changes</button
            >
          </li>{/each}
      </ul>
      <nav
        class="mt-4 flex justify-end gap-2"
        aria-label="Submission list pages"
      >
        <button
          class="btn btn-secondary"
          disabled={loading || offset === 0}
          onclick={() => {
            offset = Math.max(0, offset - 25);
            void load();
          }}>Previous</button
        ><button
          class="btn btn-secondary"
          disabled={loading || submissions.length < 25}
          onclick={() => {
            offset += 25;
            void load();
          }}>Next</button
        >
      </nav>
    {/if}
  </div>
</ReviewWorkspace>
