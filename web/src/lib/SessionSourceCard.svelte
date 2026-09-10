<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import {
    FileInput,
    Eye,
    Replace,
    Trash2,
    Link2,
    AudioLines,
    LoaderCircle
  } from '@lucide/svelte';
  import AddSourceDialog from './AddSourceDialog.svelte';
  import ArtifactPreview from './ArtifactPreview.svelte';
  import { apiJson } from './api';
  import { errorMessage } from './errors';
  import { formatBytes } from './artifact-display';
  import { modalFocus } from './modal-focus';
  import { invalidationBus, invalidates } from './invalidation';
  import {
    sourceState,
    sessionFlowAction,
    notifySessionFlowChange,
    type SessionSourceState,
    type SessionSourceItem,
    type SourceChangeImpact
  } from './session-flow';

  let {
    sessionId,
    refreshKey,
    oninitialsource,
    onchanged
  }: {
    sessionId: string;
    refreshKey: string;
    oninitialsource: () => void;
    onchanged: (message: string) => Promise<unknown>;
  } = $props();
  let sourceInfo = $state<SessionSourceState | null>(null);
  let error = $state('');
  let message = $state('');
  let busy = $state(false);
  let picker = $state<'primary' | 'media' | null>(null);
  let preview = $state<SessionSourceItem | null>(null);
  let pending = $state<{
    role: 'primary' | 'media';
    sourceId: string | null;
    impact: SourceChangeImpact;
  } | null>(null);
  let alignmentOpen = $state(false);
  let method = $state<'ctc' | 'ctc_asr_fallback'>('ctc');
  let loadId = 0;

  async function load() {
    const id = ++loadId;
    try {
      const result = await sourceState(sessionId);
      if (id === loadId) sourceInfo = result;
    } catch (caught) {
      if (id === loadId) error = errorMessage(caught);
    }
  }
  $effect(() => {
    void refreshKey;
    void load();
  });
  onMount(() =>
    invalidationBus.subscribe((change) => {
      if (
        invalidates(change, 'sources', sessionId) ||
        invalidates(change, 'jobs', sessionId)
      )
        void load();
    })
  );

  async function propose(role: 'primary' | 'media', sourceId: string | null) {
    error = '';
    const impact = await sessionFlowAction<SourceChangeImpact>(
      sessionId,
      'sources/change-preview',
      { role, new_source_asset_id: sourceId },
      false
    );
    if (impact.blocked_reason) throw new Error(impact.blocked_reason);
    pending = { role, sourceId, impact };
    picker = null;
  }
  async function proposeRemoval(role: 'primary' | 'media') {
    try {
      await propose(role, null);
    } catch (caught) {
      error = errorMessage(caught);
    }
  }
  async function apply() {
    if (!pending || busy) return;
    busy = true;
    error = '';
    try {
      const change = pending;
      await sessionFlowAction(sessionId, 'sources/change', {
        role: change.role,
        new_source_asset_id: change.sourceId,
        expected_revision: change.impact.session_revision,
        impact_token: change.impact.impact_token
      });
      pending = null;
      message =
        change.role === 'primary'
          ? change.sourceId
            ? 'Source replaced. The previous derived workflow has been reset.'
            : 'Source removed and derived workflow reset.'
          : 'Associated recording updated. Text and reusable speech are preserved; recording-dependent results require regeneration.';
      await load();
      await onchanged(message);
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
  async function startNewSession() {
    if (!pending || busy) return;
    busy = true;
    error = '';
    try {
      const change = pending;
      const created = await sessionFlowAction<{ session_id: string }>(
        sessionId,
        'sources/start-new-session',
        {
          role: 'primary',
          new_source_asset_id: change.sourceId,
          expected_revision: change.impact.session_revision,
          impact_token: change.impact.impact_token
        }
      );
      pending = null;
      await goto(`/sessions/${created.session_id}`);
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
  async function confirmTiming() {
    if (!sourceInfo?.media || busy) return;
    busy = true;
    error = '';
    try {
      await sessionFlowAction(sessionId, 'sources/confirm-timing', {
        expected_revision: sourceInfo.session_revision,
        media_artifact_id: sourceInfo.media.artifact_id
      });
      message =
        'Recording timing confirmed. Regenerate the synchronized assembly and export.';
      await load();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
  async function align() {
    if (!sourceInfo || busy) return;
    busy = true;
    error = '';
    try {
      await sessionFlowAction(sessionId, 'sources/align-subtitles', {
        method,
        expected_revision: sourceInfo.session_revision
      });
      message =
        'Word alignment queued; subtitle wording remains authoritative. Follow progress in Activity.';
      await load();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
  async function adopt() {
    if (!sourceInfo?.subtitle.source_asset_id) return;
    busy = true;
    try {
      await apiJson(
        `/api/v1/sessions/${encodeURIComponent(sessionId)}/sources/adopt-subtitles`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'If-Match': `"${sourceInfo.session_revision}"`
          },
          body: JSON.stringify({
            source_asset_id: sourceInfo.subtitle.source_asset_id
          })
        }
      );
      notifySessionFlowChange(sessionId);
      await load();
      await onchanged('Existing subtitles registered without another upload.');
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
</script>

<section
  class="surface rounded-3xl border border-[var(--line)] p-5 sm:p-6"
  aria-label="Session source"
>
  <header class="flex items-start gap-4">
    <span
      class="grid size-11 shrink-0 place-items-center rounded-2xl bg-[var(--accent-soft)] text-[var(--accent)]"
      ><FileInput size={21} /></span
    >
    <div class="min-w-0 flex-1">
      <h2 class="text-lg font-semibold">Source</h2>
      <p class="muted mt-1 text-sm">
        {sourceInfo?.subtitle.supported
          ? 'Authoritative subtitle text and its optional recording.'
          : 'The input from which this session is built.'}
      </p>
    </div>
  </header>
  <div class="mt-4 space-y-3 sm:ml-[3.75rem]">
    {#if sourceInfo?.primary}
      <div
        class="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-[var(--line)] p-3"
      >
        <div class="min-w-0">
          <p class="muted text-xs">
            {sourceInfo.subtitle.supported
              ? 'Subtitle source'
              : 'Primary source'}
          </p>
          <p class="break-all text-sm font-semibold">
            {sourceInfo.primary.filename}
          </p>
          <p class="muted mt-1 text-xs">
            {sourceInfo.subtitle.supported
              ? `${sourceInfo.subtitle.cue_count} cues`
              : sourceInfo.primary.profile}{sourceInfo.primary.size_bytes
              ? ` · ${formatBytes(sourceInfo.primary.size_bytes)}`
              : ''}
          </p>
        </div>
        <div class="flex flex-wrap gap-2">
          <button
            class="btn btn-sm"
            onclick={() => (preview = sourceInfo?.primary ?? null)}
            ><Eye size={14} /> Preview</button
          >
          <button
            class="btn btn-sm"
            disabled={busy || Boolean(sourceInfo.blocked_reason)}
            onclick={() => (picker = 'primary')}
            ><Replace size={14} /> Replace source…</button
          >
          <button
            class="btn btn-sm"
            disabled={busy || Boolean(sourceInfo.blocked_reason)}
            onclick={() => void proposeRemoval('primary')}
            ><Trash2 size={14} /> Remove source…</button
          >
        </div>
      </div>
    {:else}
      <button
        class="btn btn-primary"
        disabled={busy || Boolean(sourceInfo?.blocked_reason)}
        onclick={oninitialsource}><FileInput size={16} /> Add source</button
      >
    {/if}
    {#if sourceInfo?.subtitle.supported}
      <div
        class="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-[var(--line)] p-3"
      >
        <div class="min-w-0">
          <p class="muted text-xs">Associated recording</p>
          <p class="break-all text-sm font-semibold">
            {sourceInfo.media?.filename ?? 'Not attached'}
          </p>
        </div>
        <div class="flex flex-wrap gap-2">
          {#if sourceInfo.media}<button
              class="btn btn-sm"
              onclick={() => (preview = sourceInfo?.media ?? null)}
              ><Eye size={14} /> Preview recording</button
            >{/if}
          <button
            class="btn btn-sm"
            disabled={busy || Boolean(sourceInfo.blocked_reason)}
            onclick={() => (picker = 'media')}
            ><Link2 size={14} />
            {sourceInfo.media
              ? 'Replace recording…'
              : 'Attach audio or video…'}</button
          >
          {#if sourceInfo.media}<button
              class="btn btn-sm"
              disabled={busy || Boolean(sourceInfo.blocked_reason)}
              onclick={() => void proposeRemoval('media')}
              ><Trash2 size={14} /> Remove recording…</button
            >{/if}
        </div>
      </div>
      <div class="flex flex-wrap gap-2">
        {#if sourceInfo.subtitle.adoption_required}<button
            class="btn btn-sm"
            disabled={busy}
            onclick={() => void adopt()}>Register existing subtitles</button
          >{/if}
        {#if sourceInfo.media}
          <button
            class="btn btn-sm"
            disabled={busy ||
              !sourceInfo.subtitle.can_align ||
              Boolean(sourceInfo.blocked_reason)}
            aria-expanded={alignmentOpen}
            onclick={() => (alignmentOpen = !alignmentOpen)}
            ><AudioLines size={15} /> Align existing words…</button
          >
        {/if}
      </div>
      {#if sourceInfo.timing_review?.required && sourceInfo.media}
        <div class="rounded-xl border border-amber-500/40 p-3 text-sm">
          <p>
            Check that the replacement uses the same cuts and timing. Realign
            and update the speech plan when necessary; synchronized export is
            blocked until timing is verified.
          </p>
          <button
            class="btn btn-sm mt-2"
            disabled={busy || Boolean(sourceInfo.blocked_reason)}
            onclick={() => void confirmTiming()}
            >I have checked the recording timing</button
          >
        </div>
      {/if}
      {#if alignmentOpen}
        <div class="rounded-xl border border-[var(--line)] p-3">
          <label class="text-sm"
            >Word-timing method<select
              class="field mt-1 w-full"
              bind:value={method}
              ><option value="ctc">Local CTC · keep subtitle wording</option
              ><option value="ctc_asr_fallback"
                >CTC with configured ASR fallback</option
              ></select
            ></label
          >
          {#if method === 'ctc_asr_fallback'}<p class="muted mt-2 text-xs">
              Fallback may use your configured cloud transcription provider.
            </p>{/if}
          <button
            class="btn btn-sm mt-3"
            disabled={busy || Boolean(sourceInfo.blocked_reason)}
            onclick={() => void align()}>Align words</button
          >
        </div>
      {/if}
      {#if sourceInfo.subtitle.word_timing_artifact_id}<p class="muted text-xs">
          Word-level timing is available for the current recording.
        </p>{/if}
    {/if}
    {#if sourceInfo?.blocked_reason}<p class="muted text-sm">
        {sourceInfo.blocked_reason}
      </p>{/if}
    {#if message}<p role="status" class="text-sm">{message}</p>{/if}
    {#if error && !pending}<p role="alert" class="text-sm text-red-600">
        {error}
      </p>{/if}
  </div>
</section>

{#if picker}<AddSourceDialog
    {sessionId}
    sourceRole={picker}
    onpick={(sourceId) => propose(picker ?? 'primary', sourceId)}
    onclose={() => (picker = null)}
    onadded={() => {}}
  />{/if}
{#if preview}<ArtifactPreview
    artifact={{
      id: preview.artifact_id,
      relative_path: preview.filename,
      content_hash: preview.content_hash,
      size_bytes: preview.size_bytes,
      role: 'source'
    }}
    onclose={() => (preview = null)}
  />{/if}
{#if pending}
  <div class="fixed inset-0 z-[85] grid place-items-center bg-black/45 p-4">
    <div
      use:modalFocus={{
        onclose: () => {
          if (!busy) pending = null;
        }
      }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="source-reset-heading"
      class="surface max-h-[90vh] w-full max-w-xl overflow-auto rounded-3xl p-6"
    >
      <h2 id="source-reset-heading" class="text-xl font-semibold">
        {pending.role === 'primary'
          ? pending.sourceId
            ? 'Replace source and reset this session?'
            : 'Remove source and reset this session?'
          : pending.sourceId
            ? 'Use this associated recording?'
            : 'Remove the associated recording?'}
      </h2>
      <p class="mt-4 text-sm leading-6">
        {pending.role === 'primary'
          ? 'The current source’s derived text versions, speech plans, generated takes, assemblies and exports will be removed from this session. This cannot be undone.'
          : 'Subtitle text, corrections, translations, speech plans and reusable raw takes will be kept. Results tied to the old recording’s timing or soundtrack will need regeneration.'}
      </p>
      {#if pending.role === 'primary'}
        <p class="mt-3 rounded-xl bg-[var(--accent-soft)] p-3 text-sm">
          {pending.impact.counts.documents ?? 0} text documents · {pending
            .impact.counts.generation_plans ?? 0} speech-plan histories · {pending
            .impact.counts.generation_runs ?? 0} audio runs · {pending.impact
            .derived_files} derived files
        </p>
        <p class="muted mt-3 text-sm">
          Source-library originals, files shared with other sessions, settings,
          voices and usage records are retained.
        </p>
      {/if}
      {#if error}<p role="alert" class="mt-3 text-sm text-red-600">
          {error}
        </p>{/if}
      <div class="mt-5 flex flex-wrap justify-end gap-2">
        <button class="btn" disabled={busy} onclick={() => (pending = null)}
          >Cancel</button
        >
        {#if pending.role === 'primary'}<button
            class="btn"
            disabled={busy}
            onclick={() => void startNewSession()}
            >Use a new session instead</button
          >{/if}
        <button
          class="btn btn-primary"
          disabled={busy}
          onclick={() => void apply()}
          >{#if busy}<LoaderCircle
              class="animate-spin"
              size={16}
            />{/if}{pending.role === 'primary'
            ? pending.sourceId
              ? 'Replace source and reset'
              : 'Remove source and reset'
            : pending.sourceId
              ? 'Use recording'
              : 'Remove recording'}</button
        >
      </div>
    </div>
  </div>
{/if}
