<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import { ApiError } from './api';
  import { errorMessage } from './errors';
  import { modalDialog } from './modal-dialog';
  import {
    confirmGenerationStart,
    nonzeroReasons,
    previewGenerationStart,
    previewModeForStartMode,
    startModeFlags,
    type GenerationPreview,
    type GenerationStartMode
  } from './generation-start';
  import type { GenerationRun } from './api-models';

  let {
    sessionId,
    planRevisionId,
    planLabel = '',
    initialMode = 'continue',
    pausedRunLabel = null,
    onclose,
    onstarted
  }: {
    sessionId: string;
    planRevisionId: string | null;
    planLabel?: string;
    initialMode?: GenerationStartMode;
    pausedRunLabel?: string | null;
    onclose: () => void;
    onstarted: (
      run: GenerationRun,
      preview: GenerationPreview | null
    ) => void | Promise<void>;
  } = $props();

  // Mount-time initial choice: parents render the dialog conditionally per
  // opening, so capturing the initial mode deliberately is correct.
  let mode = $state<GenerationStartMode>(untrack(() => initialMode));
  let preview = $state<GenerationPreview | null>(null);
  let previewLoading = $state(false);
  let previewError = $state('');
  let acknowledged = $state(false);
  let startBusy = $state(false);
  let startError = $state('');
  let startSucceeded = $state(false);
  let postError = $state('');
  let refreshedAfterConflict = $state(false);
  // Non-reactive ticket: guards async preview races without retriggering effects.
  let previewTicket = 0;
  let retryController: AbortController | null = null;
  let mounted = true;
  onMount(() => () => {
    mounted = false;
    retryController?.abort();
  });

  const reasons = $derived(preview ? nonzeroReasons(preview.reasons) : []);
  // Refresh/all replacements need explicit acknowledgment. Continue only
  // reconstructs edited blocks, where the count-labelled confirm is enough.
  const needsAck = $derived(
    mode !== 'continue' && (preview?.replace_count ?? 0) > 0
  );
  const noWork = $derived(
    preview !== null && !previewLoading && preview.generate_count === 0
  );
  const canSubmit = $derived(
    preview !== null &&
      !previewLoading &&
      !startBusy &&
      !startSucceeded &&
      preview.generate_count > 0 &&
      typeof preview.selection_hash === 'string' &&
      preview.selection_hash.trim().length > 0 &&
      !preview.blocked_reason &&
      preview.speech_plan_revision_id === planRevisionId &&
      preview.mode === previewModeForStartMode(mode) &&
      (!needsAck || acknowledged)
  );
  const submitLabel = $derived(
    startSucceeded
      ? 'Started'
      : !preview || previewLoading
        ? 'Generate'
        : preview.generate_count === 0
          ? 'Nothing to generate'
          : `Generate ${preview.generate_count} block${preview.generate_count === 1 ? '' : 's'}`
  );
  const ackLabel = $derived(
    `I understand ${(preview?.replace_count ?? 0) === 1 ? '1 existing recording' : `${preview?.replace_count ?? 0} existing recordings`} will be replaced. Previous takes stay in history.`
  );

  $effect(() => {
    if (!planRevisionId) {
      preview = null;
      previewError = 'No speech plan revision is selected.';
      return;
    }
    // Capture the current selection: late responses that no longer match it
    // are discarded, never applied.
    const requestedMode = mode;
    const requestedRevision = planRevisionId;
    const requestedSession = sessionId;
    const ticket = ++previewTicket;
    const controller = new AbortController();
    previewLoading = true;
    previewError = '';
    const flags = startModeFlags(requestedMode);
    previewGenerationStart(
      requestedSession,
      { speech_plan_revision_id: requestedRevision, ...flags },
      controller.signal
    )
      .then((result) => {
        if (
          ticket !== previewTicket ||
          controller.signal.aborted ||
          mode !== requestedMode ||
          planRevisionId !== requestedRevision ||
          sessionId !== requestedSession
        )
          return;
        if (result.speech_plan_revision_id !== requestedRevision) {
          preview = null;
          previewError =
            'The speech plan changed. Close this dialog and refresh the plan before generating.';
          return;
        }
        if (result.mode !== previewModeForStartMode(requestedMode)) {
          preview = null;
          previewError =
            'The preview did not match the selected choice. Retry the preview.';
          return;
        }
        if (
          typeof result.selection_hash !== 'string' ||
          !result.selection_hash.trim()
        ) {
          preview = null;
          previewError =
            'The generation preview was incomplete. Retry before generating.';
          return;
        }
        preview = result;
      })
      .catch((caught: unknown) => {
        if (
          ticket !== previewTicket ||
          controller.signal.aborted ||
          mode !== requestedMode ||
          planRevisionId !== requestedRevision ||
          sessionId !== requestedSession
        )
          return;
        preview = null;
        previewError = errorMessage(caught);
      })
      .finally(() => {
        if (ticket === previewTicket && !controller.signal.aborted)
          previewLoading = false;
      });
    return () => controller.abort();
  });

  // A refreshed preview (new selection hash, or any conflict refresh)
  // invalidates the previous acknowledgment: the user must re-confirm.
  $effect(() => {
    void preview?.selection_hash;
    acknowledged = false;
  });

  // Switching choice clears the stale preview and its acknowledgment
  // immediately; the effect aborts the previous request via its ticket and
  // cleanup, then fetches the new selection read-only.
  function selectMode(next: GenerationStartMode) {
    if (next === mode || startBusy) return;
    preview = null;
    acknowledged = false;
    mode = next;
  }

  async function retryPreview() {
    // Read-only retry: refetches the preflight, never starts generation and
    // never clears a start/conflict explanation; the user must still click
    // confirm explicitly after any refresh.
    const requestedMode = mode;
    const requestedRevision = planRevisionId;
    const requestedSession = sessionId;
    const ticket = ++previewTicket;
    retryController?.abort();
    const controller = new AbortController();
    retryController = controller;
    if (!requestedRevision) return;
    previewLoading = true;
    previewError = '';
    try {
      const flags = startModeFlags(requestedMode);
      const result = await previewGenerationStart(
        requestedSession,
        {
          speech_plan_revision_id: requestedRevision,
          ...flags
        },
        controller.signal
      );
      if (
        !mounted ||
        ticket !== previewTicket ||
        controller.signal.aborted ||
        mode !== requestedMode ||
        planRevisionId !== requestedRevision ||
        sessionId !== requestedSession
      )
        return;
      if (result.speech_plan_revision_id !== requestedRevision) {
        preview = null;
        previewError =
          'The speech plan changed. Close this dialog and refresh the plan before generating.';
        return;
      }
      if (result.mode !== previewModeForStartMode(requestedMode)) {
        preview = null;
        previewError =
          'The preview did not match the selected choice. Retry the preview.';
        return;
      }
      if (
        typeof result.selection_hash !== 'string' ||
        !result.selection_hash.trim()
      ) {
        preview = null;
        previewError =
          'The generation preview was incomplete. Retry before generating.';
        return;
      }
      preview = result;
    } catch (caught) {
      if (
        !mounted ||
        ticket !== previewTicket ||
        controller.signal.aborted ||
        mode !== requestedMode ||
        planRevisionId !== requestedRevision ||
        sessionId !== requestedSession
      )
        return;
      preview = null;
      previewError = errorMessage(caught);
    } finally {
      if (mounted && ticket === previewTicket) previewLoading = false;
    }
  }

  async function submit() {
    if (!canSubmit || !preview || !planRevisionId) return;
    startBusy = true;
    startError = '';
    postError = '';
    refreshedAfterConflict = false;
    try {
      const flags = startModeFlags(mode);
      const run = await confirmGenerationStart(sessionId, {
        speech_plan_revision_id: planRevisionId,
        ...flags,
        expected_selection_hash: preview.selection_hash
      });
      if (!mounted) return;
      // The generation run started: a later UI-refresh failure must never
      // offer another start.
      startSucceeded = true;
      const boundPreview = preview;
      try {
        await onstarted(run, boundPreview);
      } catch (caught) {
        if (mounted)
          postError = `Generation started, but refreshing the view failed: ${errorMessage(caught)} Close and reload the session to see the new run.`;
      }
    } catch (caught) {
      // No automatic retry or restart after a conflict: preserve the error,
      // refresh the read-only preview, and require a new explicit click.
      startError = errorMessage(caught);
      refreshedAfterConflict =
        caught instanceof ApiError && caught.status === 409;
      acknowledged = false;
      await retryPreview();
    } finally {
      if (mounted) startBusy = false;
    }
  }
</script>

<dialog
  use:modalDialog={{ onclose, closeOnEscape: !startBusy }}
  aria-labelledby="generation-start-title"
  class="generation-start-dialog"
  data-testid="generation-start-dialog"
>
  <header class="dialog-header">
    <div>
      <h2 id="generation-start-title" class="text-lg font-semibold">
        Start generation{#if planLabel}
          · {planLabel}{/if}
      </h2>
      <p class="muted mt-1 text-sm">
        Choose what to generate. Nothing is generated until you confirm below.
      </p>
    </div>
    <button
      type="button"
      class="btn"
      disabled={startBusy}
      onclick={onclose}
      data-testid="generation-start-close">Close</button
    >
  </header>

  <div class="dialog-body">
    {#if pausedRunLabel}
      <p class="resume-note text-sm" data-testid="generation-start-resume-note">
        <strong>Resume paused run is separate.</strong> It continues “{pausedRunLabel}”
        with its saved voice and settings, re-checking each recording first, so
        unverified recordings may be regenerated. It does not switch to your
        current settings. The choices below start a new run with your current
        settings instead.
      </p>
    {:else}
      <p class="muted text-sm">
        This starts a new run with the current voice and settings. Completed
        recordings are only replaced where the choice below says so.
      </p>
    {/if}

    <fieldset class="mode-group" disabled={startBusy}>
      <legend class="text-sm font-semibold">What should be generated?</legend>
      <label class="mode-option">
        <input
          type="radio"
          name="generation-start-mode"
          value="continue"
          checked={mode === 'continue'}
          onchange={() => selectMode('continue')}
          data-testid="generation-start-mode-continue"
        />
        <span>
          <strong>Continue unfinished audio</strong>
          <span class="muted block text-xs">
            Keeps every completed recording, even ones made with older or
            different settings. Only blocks that are missing, edited, or
            previously failed are generated with the current voice and settings.
          </span>
        </span>
      </label>
      <label class="mode-option">
        <input
          type="radio"
          name="generation-start-mode"
          value="refresh"
          checked={mode === 'refresh'}
          onchange={() => selectMode('refresh')}
          data-testid="generation-start-mode-refresh"
        />
        <span>
          <strong>Refresh changed audio</strong>
          <span class="muted block text-xs">
            Regenerates blocks whose recordings no longer match the current
            voice or settings, plus any missing, edited, or failed blocks.
            Matching completed recordings are kept.
          </span>
        </span>
      </label>
      <label class="mode-option">
        <input
          type="radio"
          name="generation-start-mode"
          value="all"
          checked={mode === 'all'}
          onchange={() => selectMode('all')}
          data-testid="generation-start-mode-all"
        />
        <span>
          <strong>Regenerate everything</strong>
          <span class="muted block text-xs">
            Regenerates every block with the current voice and settings. Every
            completed recording is replaced.
          </span>
        </span>
      </label>
      <p class="muted mt-2 text-xs">
        Replaced recordings keep their previous takes in history.
      </p>
    </fieldset>

    {#if previewLoading && !preview}
      <p
        role="status"
        class="muted text-sm"
        data-testid="generation-start-loading"
      >
        Loading generation preview…
      </p>
    {/if}
    {#if previewError}
      <p
        role="alert"
        class="preview-error text-sm"
        data-testid="generation-start-error"
      >
        {previewError}
        <button
          type="button"
          class="btn mt-2"
          disabled={previewLoading || startBusy}
          onclick={() => void retryPreview()}
          data-testid="generation-start-retry">Retry preview</button
        >
      </p>
    {/if}

    {#if preview}
      <section
        aria-label="Generation preview"
        aria-busy={previewLoading}
        class="preview-panel"
        data-testid="generation-start-preview"
      >
        <p class="text-sm" data-testid="generation-start-counts">
          <strong>{preview.total_count} blocks total</strong> ·
          {preview.generate_count} to generate · {preview.preserve_count} kept{#if preview.replace_count > 0}
            · {preview.replace_count} existing recording{preview.replace_count ===
            1
              ? ''
              : 's'} replaced{/if}
        </p>
        {#if preview.generate_count > 0 && preview.first_generate_ordinal !== null}
          <p class="muted mt-1 text-xs" data-testid="generation-start-first">
            First block to generate: block {preview.first_generate_ordinal + 1}
          </p>
        {/if}
        {#if reasons.length}
          <ul
            class="mt-2 space-y-1 text-xs"
            data-testid="generation-start-reasons"
          >
            {#each reasons as reason (reason.key)}
              <li>{reason.label}: {reason.count}</li>
            {/each}
          </ul>
        {/if}
        {#if preview.settings_summary}
          <p class="muted mt-2 text-xs" data-testid="generation-start-settings">
            Current settings: {preview.settings_summary.service} · {preview
              .settings_summary.model} · voice {preview.settings_summary.voice}
          </p>
        {/if}
        {#if noWork}
          <p class="mt-2 text-sm" data-testid="generation-start-noop">
            {preview.blocked_reason ?? 'Nothing to generate for this choice.'}
          </p>
        {/if}
        {#if needsAck}
          <p
            role="alert"
            class="replace-warning mt-3 text-sm"
            data-testid="generation-start-warning"
          >
            This choice replaces {preview.replace_count} existing recording{preview.replace_count ===
            1
              ? ''
              : 's'}. Previous takes stay in history.
          </p>
        {/if}
      </section>
    {/if}

    {#if startError}
      <p
        role="alert"
        class="preview-error text-sm"
        data-testid="generation-start-submit-error"
      >
        {startError}
        {#if refreshedAfterConflict}
          <span class="muted block text-xs">
            The preview above was refreshed. Review it and confirm again to
            start.
          </span>
        {/if}
      </p>
    {/if}

    {#if postError}
      <p
        role="alert"
        class="preview-error text-sm"
        data-testid="generation-start-post-error"
      >
        {postError}
      </p>
    {/if}

    {#if needsAck && !noWork}
      <label class="ack-row mt-3 flex items-start gap-2 text-sm">
        <input
          type="checkbox"
          class="mt-1"
          checked={acknowledged}
          disabled={previewLoading || startBusy}
          onchange={(event) =>
            (acknowledged = (event.currentTarget as HTMLInputElement).checked)}
          data-testid="generation-start-ack"
        />
        <span>{ackLabel}</span>
      </label>
    {/if}

    <div class="dialog-footer mt-4 flex flex-wrap gap-2">
      <button
        type="button"
        class="btn btn-primary"
        disabled={!canSubmit}
        onclick={() => void submit()}
        data-testid="generation-start-submit">{submitLabel}</button
      >
      <button type="button" class="btn" disabled={startBusy} onclick={onclose}
        >Cancel</button
      >
      {#if startBusy}
        <span role="status" class="muted self-center text-sm">Starting…</span>
      {/if}
    </div>
  </div>
</dialog>

<style>
  .generation-start-dialog {
    width: min(42rem, calc(100vw - 2rem));
    max-height: 88dvh;
    margin: auto;
    padding: 0;
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 1.2rem;
    background: var(--paper);
    color: var(--ink);
    box-shadow: var(--shadow);
  }
  .generation-start-dialog[open] {
    display: flex;
    flex-direction: column;
  }
  .generation-start-dialog::backdrop {
    background: rgb(0 0 0 / 40%);
    backdrop-filter: blur(3px);
  }
  .dialog-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    padding: 1.25rem;
    border-bottom: 1px solid var(--line);
  }
  .dialog-body {
    overflow: auto;
    padding: 1.25rem;
  }
  .resume-note {
    border: 1px solid var(--line);
    border-radius: 0.8rem;
    background: var(--paper-strong);
    padding: 0.75rem 1rem;
    margin-bottom: 1rem;
  }
  .mode-group {
    display: grid;
    gap: 0.6rem;
    margin: 1rem 0;
  }
  .mode-option {
    display: flex;
    gap: 0.6rem;
    align-items: flex-start;
    border: 1px solid var(--line);
    border-radius: 0.8rem;
    padding: 0.7rem 0.9rem;
    cursor: pointer;
  }
  .mode-option input {
    margin-top: 0.2rem;
  }
  .preview-panel {
    border: 1px solid var(--line);
    border-radius: 0.8rem;
    background: var(--paper-strong);
    padding: 0.75rem 1rem;
    margin-top: 1rem;
  }
  .preview-error {
    border: 1px solid rgb(248 113 113 / 40%);
    border-radius: 0.8rem;
    background: rgb(239 68 68 / 10%);
    padding: 0.75rem 1rem;
    margin-top: 1rem;
    color: var(--ink);
  }
  .replace-warning {
    border: 1px solid rgb(245 158 11 / 50%);
    border-radius: 0.8rem;
    background: rgb(245 158 11 / 12%);
    padding: 0.6rem 0.9rem;
  }
  .ack-row {
    cursor: pointer;
  }
  .dialog-footer {
    align-items: center;
  }
</style>
