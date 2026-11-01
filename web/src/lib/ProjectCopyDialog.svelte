<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { Copy, X } from '@lucide/svelte';
  import { ApiError, ApiMutationAttempt } from './api';
  import { appState } from './app-state.svelte';
  import { jobApi } from './domain-api';
  import { errorMessage } from './errors';
  import { modalFocus } from './modal-focus';
  import {
    projectCopyApi,
    type ProjectCopyConnectionChoice,
    type ProjectCopyPreview,
    type ProjectCopyResult,
    type ProjectCopyStatus
  } from './project-copy-api';

  let {
    projectId,
    sourceWorkspaceId,
    projectName,
    onclose,
    onopen
  }: {
    projectId: string;
    sourceWorkspaceId: string;
    projectName: string;
    onclose: () => void;
    onopen: (result: ProjectCopyResult) => Promise<void>;
  } = $props();
  const destinations = $derived(
    appState.workspaces.filter(
      (item) => item.state === 'active' && item.allowed_actions.includes('edit')
    )
  );
  let destination = $state('');
  let name = $state('');
  let preview = $state<ProjectCopyPreview | null>(null);
  let connectionSelections = $state<
    Record<string, ProjectCopyConnectionChoice>
  >({});
  let loading = $state(false);
  let submitting = $state(false);
  let canceling = $state(false);
  let opening = $state(false);
  let status = $state<ProjectCopyStatus | null>(null);
  let error = $state('');
  let lookupFailed = $state(false);
  let lookingUp = $state(false);
  let active = true;
  let previewGeneration = 0;
  let previewController: AbortController | undefined;
  let statusController: AbortController | undefined;
  const attempt = new ApiMutationAttempt();
  const terminal = new Set(['succeeded', 'failed', 'canceled']);
  const running = $derived(Boolean(status && !terminal.has(status.state)));
  const observed = $derived(
    status ? appState.jobs.find((job) => job.id === status?.id) : undefined
  );
  const progress = $derived(
    Math.max(0, Math.min(1, observed?.progress ?? status?.progress ?? 0))
  );
  const destinationName = $derived(
    destinations.find((item) => item.id === destination)?.name ??
      'the destination workspace'
  );
  const reasons = $derived(
    preview ? [...new Set(preview.blockers.map(blockerLabel))] : []
  );
  const connectionChoices = $derived(
    Object.values(connectionSelections).sort((left, right) =>
      left.requirement_id.localeCompare(right.requirement_id)
    )
  );
  const fieldClass =
    'mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2.5 disabled:opacity-60';

  onMount(() => {
    name = `${Array.from(projectName).slice(0, 248).join('')} (copy)`;
    destination =
      destinations.find(
        (item) => item.kind === 'team' && item.id !== sourceWorkspaceId
      )?.id ??
      destinations.find((item) => item.id === sourceWorkspaceId)?.id ??
      destinations[0]?.id ??
      '';
    if (destination) void loadPreview();
  });
  onDestroy(() => {
    active = false;
    previewGeneration += 1;
    previewController?.abort();
    statusController?.abort();
  });

  // Normal progress arrives through the existing application event connection.
  // A narrow fallback runs only while this dialog has an active copy that the
  // shared connection/list cannot currently represent.
  $effect(() => {
    if (
      status &&
      observed &&
      terminal.has(observed.status) &&
      !terminal.has(status.state) &&
      !lookupFailed
    )
      void refreshStatus();
  });
  $effect(() => {
    if (!running || (appState.eventsHealthy && observed) || lookupFailed)
      return;
    const timer = window.setInterval(() => void refreshStatus(), 10_000);
    return () => window.clearInterval(timer);
  });

  function blockerLabel(
    blocker: ProjectCopyPreview['blockers'][number]
  ): string {
    if (blocker.code === 'destination_binding_required') {
      const needsModel = preview?.connection_requirements?.some(
        (item) =>
          item.purpose === blocker.domain && !item.selected_provider_model_id
      );
      return needsModel
        ? 'Choose destination models for new processing below.'
        : 'This project uses private service settings that copying cannot rebind yet.';
    }
    const labels: Record<string, string> = {
      external_source:
        'Import linked source files into Pandrator before copying.',
      unhashed_file: 'Some source files still need verification.',
      trashed_language_versions:
        'Restore trashed language versions to include them in the copy.',
      editorial_operation:
        'Finish or cancel pending editorial work before copying.',
      generation_plans:
        'This speech plan has saved choices that copying cannot preserve yet.',
      audio_takes:
        'Some selected recordings cannot be copied yet. Finish generation or review their saved state.',
      project_speech_references:
        'This project has saved speech choices that copying cannot preserve yet.',
      media_edit_plans: 'This saved video edit plan cannot be copied yet.',
      project_sound_plans:
        'Update the sound cues to match the current speech plan before copying.',
      performance_plans:
        'Finish active annotation analysis or review the adopted speech directions before copying.',
      pronunciation_entries:
        'Copying saved pronunciations is not available yet.',
      subtitle_evidence:
        'Copying saved subtitle evidence is not available yet.',
      active_evidence:
        'Finish or cancel referenced audio evidence before copying.',
      knowledge_ledgers:
        'This project has saved research or casting choices that copying cannot preserve yet.',
      output_assemblies: 'Copying saved audio assemblies is not available yet.',
      reusable_editorial_dependency:
        'Copying the selected voice or reusable resource is not available yet.',
      custom_settings:
        'This project has additional settings that copying cannot preserve yet.',
      workflow_kind: 'Copying this project type is not available yet.'
    };
    return (
      labels[blocker.domain] ?? 'Some saved project data cannot be copied yet.'
    );
  }

  function connectionPurpose(purpose: string): string {
    const labels: Record<string, string> = {
      correction: 'Correction model',
      translation: 'Translation model',
      speech_preparation: 'Speech preparation model'
    };
    return labels[purpose] ?? 'Processing model';
  }

  async function chooseConnection(requirementId: string, modelId: string) {
    const option = preview?.connection_options?.find(
      (item) => item.provider_model_id === modelId
    );
    const next = { ...connectionSelections };
    if (option)
      next[requirementId] = {
        requirement_id: requirementId,
        provider_id: option.provider_id,
        provider_model_id: option.provider_model_id
      };
    else delete next[requirementId];
    connectionSelections = next;
    await loadPreview();
  }

  async function loadPreview(resetConnections = false) {
    previewController?.abort();
    previewController = new AbortController();
    const generation = ++previewGeneration;
    if (resetConnections) {
      connectionSelections = {};
      preview = null;
    }
    error = '';
    loading = true;
    if (!destination) {
      loading = false;
      return;
    }
    try {
      const result = await projectCopyApi.preview(
        projectId,
        destination,
        connectionChoices,
        previewController.signal
      );
      if (active && generation === previewGeneration) preview = result;
    } catch (caught) {
      if (
        active &&
        generation === previewGeneration &&
        !previewController.signal.aborted
      )
        error = errorMessage(caught);
    } finally {
      if (active && generation === previewGeneration) loading = false;
    }
  }

  async function start() {
    if (
      !preview?.ready ||
      loading ||
      error ||
      submitting ||
      status ||
      !name.trim()
    )
      return;
    const body = {
      destination_workspace_id: destination,
      expected_fingerprint: preview.fingerprint,
      name: name.trim(),
      ...(connectionChoices.length
        ? { destination_connections: connectionChoices }
        : {})
    };
    submitting = true;
    error = '';
    try {
      const result = await projectCopyApi.start(projectId, body, attempt);
      if (!active) return;
      status = result;
      void appState.refreshJobs().catch(() => undefined);
      if (!terminal.has(result.state)) void refreshStatus();
    } catch (caught) {
      if (!active) return;
      if (
        caught instanceof ApiError &&
        ['copy_preview_stale', 'copy_connection_unavailable'].includes(
          caught.code
        )
      ) {
        await loadPreview(true);
        error =
          'The project or its destination models changed. Review the refreshed preview before copying.';
      } else error = errorMessage(caught);
    } finally {
      if (active) submitting = false;
    }
  }

  async function refreshStatus() {
    if (!status || lookingUp || !active) return;
    lookingUp = true;
    lookupFailed = false;
    statusController?.abort();
    statusController = new AbortController();
    try {
      const result = await projectCopyApi.get(
        status.id,
        statusController.signal
      );
      if (active) status = result;
    } catch (caught) {
      if (active && !statusController.signal.aborted) {
        lookupFailed = true;
        error =
          caught instanceof ApiError && caught.status === 404
            ? 'This copy is unavailable or your access has changed.'
            : errorMessage(caught);
      }
    } finally {
      if (active) lookingUp = false;
    }
  }

  async function cancel() {
    if (!status || canceling || !running) return;
    canceling = true;
    error = '';
    try {
      await jobApi.cancel(status.id);
      if (active) await refreshStatus();
    } catch (caught) {
      if (active) error = errorMessage(caught);
    } finally {
      if (active) canceling = false;
    }
  }

  function close() {
    if (!submitting && !opening) onclose();
  }

  async function openCopy() {
    if (!status?.result || opening) return;
    opening = true;
    error = '';
    try {
      await onopen(status.result);
    } catch (caught) {
      if (active) error = errorMessage(caught);
    } finally {
      if (active) opening = false;
    }
  }
</script>

<div
  class="fixed inset-0 z-[70] grid place-items-center bg-black/45 p-4 backdrop-blur-sm"
  role="presentation"
>
  <div
    use:modalFocus={{ onclose: close }}
    class="surface max-h-[92vh] w-full max-w-xl overflow-auto rounded-3xl p-5 sm:p-7"
    role="dialog"
    aria-modal="true"
    aria-labelledby="project-copy-title"
  >
    <header class="flex items-center justify-between gap-4">
      <h2 id="project-copy-title" class="text-xl font-semibold">
        Copy project to a workspace
      </h2>
      <button
        class="btn btn-icon btn-secondary"
        aria-label="Close project copy"
        onclick={close}
        disabled={submitting || opening}><X size={18} /></button
      >
    </header>
    <p class="muted mt-4 break-words text-sm">
      Copy the current saved state of <strong>{projectName}</strong>. The
      original keeps its history and running jobs.
    </p>
    {#if !status}
      <form
        class="mt-5 space-y-4"
        onsubmit={(event) => {
          event.preventDefault();
          void start();
        }}
      >
        <label class="block text-sm font-semibold"
          >Destination workspace
          <select
            class={fieldClass}
            bind:value={destination}
            onchange={() => void loadPreview(true)}
            disabled={submitting}
            required
          >
            <option value="" disabled>Choose a workspace</option>
            {#each destinations as item (item.id)}<option value={item.id}
                >{item.name}{item.kind === 'personal'
                  ? ' (private)'
                  : ''}</option
              >{/each}
          </select>
        </label>
        <label class="block text-sm font-semibold"
          >Copy name
          <input
            class={fieldClass}
            bind:value={name}
            maxlength="255"
            required
            disabled={submitting}
            autocomplete="off"
          />
        </label>
        {#if loading}<p class="muted text-sm" role="status">
            Checking saved project state…
          </p>
        {/if}
        {#if preview}
          <div class="rounded-xl border border-[var(--line)] p-4 text-sm">
            <p>
              Includes the source{preview.counts.translation_project_branches
                ? ` and ${preview.counts.translation_project_branches} language version${preview.counts.translation_project_branches === 1 ? '' : 's'}`
                : ''}, saved text and review state{(preview.counts
                .knowledge_ledgers ?? 0) >
              (preview.counts.generation_controls ?? 0)
                ? ', research and terminology'
                : ''}{preview.counts.generation_controls
                ? ', saved casting choices'
                : ''}{preview.counts.generation_plan_revisions
                ? ', the current speech plan'
                : ''}{preview.counts.performance_plans
                ? `, ${preview.counts.performance_plans} speech annotation plan${preview.counts.performance_plans === 1 ? '' : 's'}`
                : ''}{preview.counts.audio_takes
                ? `, ${preview.counts.audio_takes} selected speech recording${preview.counts.audio_takes === 1 ? '' : 's'}`
                : ''}{preview.counts.saved_speech_outputs
                ? `, ${preview.counts.saved_speech_outputs} saved audio output${preview.counts.saved_speech_outputs === 1 ? '' : 's'}`
                : ''}{preview.counts.project_sound_cues
                ? `, ${preview.counts.project_sound_cues} sound cue${preview.counts.project_sound_cues === 1 ? '' : 's'}`
                : ''}{preview.counts.subtitle_evidence
                ? ', saved audio evidence'
                : ''}{preview.counts.media_edit_plan_revisions
                ? ', saved video edits'
                : ''}, settings, and {preview.file_count}
              file{preview.file_count === 1 ? '' : 's'}.
            </p>
            <p class="muted mt-2 text-xs">
              The copy starts unfiled in {destinationName}. Private connection
              details stay in the original workspace.
            </p>
            {#if preview.counts.saved_speech_outputs}<p
                class="muted mt-2 text-xs"
              >
                Saved audio outputs are copied as downloads. Assemble copied
                recordings or render sound cues to create new exports.
              </p>{/if}
            {#if preview.counts.sound_needs_placement}<p
                class="muted mt-2 text-xs"
              >
                Review placement for {preview.counts.sound_needs_placement} sound
                cue{preview.counts.sound_needs_placement === 1 ? '' : 's'} in the
                copy before rendering.
              </p>{/if}
            {#if preview.counts.generation_plan_revisions}<p
                class="muted mt-2 text-xs"
              >
                Only annotation drafts that match the current speech plan are
                copied. Stale drafts and analysis history stay in the original.
              </p>{/if}
            {#if preview.active_job_count}<p class="muted mt-2 text-xs">
                Work is still running or queued in the original. Later changes
                stay there.
              </p>{/if}
          </div>
          {#if preview.connection_requirements?.length}
            <fieldset
              class="space-y-4 rounded-xl border border-[var(--line)] p-4"
            >
              <legend class="px-1 text-sm font-semibold"
                >Destination models</legend
              >
              <p class="muted text-sm">
                Choose models for new processing in {destinationName}. Saved
                results keep their content.
              </p>
              {#each preview.connection_requirements as requirement (requirement.id)}
                <label class="block text-sm font-semibold">
                  {connectionPurpose(requirement.purpose)}
                  <span
                    class="muted mt-1 block break-words text-xs font-normal"
                  >
                    {requirement.session_names.join(', ')}
                  </span>
                  <select
                    class={fieldClass}
                    value={connectionSelections[requirement.id]
                      ?.provider_model_id ??
                      requirement.selected_provider_model_id ??
                      ''}
                    onchange={(event) =>
                      void chooseConnection(
                        requirement.id,
                        event.currentTarget.value
                      )}
                    disabled={submitting ||
                      loading ||
                      !preview.connection_options?.length}
                    required
                  >
                    <option value="">Choose a model</option>
                    {#each preview.connection_options ?? [] as option (option.provider_model_id)}
                      <option value={option.provider_model_id}
                        >{option.provider_label} · {option.model_name}</option
                      >
                    {/each}
                  </select>
                </label>
              {/each}
              {#if !preview.connection_options?.length}
                <p class="muted text-sm">
                  Ask a workspace manager to configure an enabled text model in {destinationName}.
                </p>
              {/if}
            </fieldset>
          {/if}
          {#if reasons.length}<div
              class="rounded-xl border border-[var(--line)] p-4 text-sm"
              role="status"
            >
              <p class="font-semibold">
                This project needs attention before copying.
              </p>
              <ul class="mt-2 list-disc space-y-2 pl-5">
                {#each reasons as reason}<li>{reason}</li>{/each}
              </ul>
            </div>{/if}
        {/if}
        {#if error}<p
            class="text-sm text-red-700 dark:text-red-300"
            role="alert"
          >
            {error}
          </p>
          <button
            class="btn btn-secondary"
            type="button"
            disabled={loading || submitting}
            onclick={() => void loadPreview(true)}>Refresh preview</button
          >{/if}
        <div class="flex flex-wrap justify-end gap-3 pt-2">
          <button
            class="btn btn-secondary"
            type="button"
            onclick={close}
            disabled={submitting}>Close</button
          ><button
            class="btn btn-primary"
            type="submit"
            disabled={!preview?.ready ||
              loading ||
              submitting ||
              Boolean(error) ||
              !name.trim()}
            ><Copy size={15} />{submitting
              ? 'Preparing copy…'
              : 'Make copy'}</button
          >
        </div>
      </form>
    {:else}
      <div class="mt-5 space-y-4">
        {#if running}<div role="status">
            <p class="font-semibold">
              {canceling || observed?.status === 'cancel_requested'
                ? 'Canceling copy…'
                : 'Copying project…'}
            </p>
            <progress
              class="mt-3 w-full"
              max="1"
              value={progress}
              aria-label="Project copy progress"
            ></progress>
            <p class="muted mt-2 text-xs">
              The copy will appear in {destinationName} when ready. It continues if
              you close this window.
            </p>
          </div>
        {:else if status.state === 'succeeded' && status.result}<p
            class="rounded-xl bg-[var(--accent-soft)] p-4 text-sm"
            role="status"
          >
            The copy is ready in {destinationName}. Your original project
            remains available.
          </p>
        {:else if status.state === 'canceled'}<p class="text-sm" role="status">
            Copy canceled. The original is unchanged.
          </p>
        {:else}<p class="text-sm text-red-700 dark:text-red-300" role="alert">
            {status.error?.message ?? 'The project could not be copied.'}
          </p>{/if}
        {#if error}<p
            class="text-sm text-red-700 dark:text-red-300"
            role="alert"
          >
            {error}
          </p>{/if}
        {#if lookupFailed}<button
            class="btn btn-secondary"
            onclick={() => void refreshStatus()}
            disabled={lookingUp}>Refresh copy status</button
          >{/if}
        <div class="flex flex-wrap justify-end gap-3">
          <button class="btn btn-secondary" onclick={close} disabled={opening}
            >Close</button
          >
          {#if running && appState.principal?.scopes.includes('app.cancel')}<button
              class="btn btn-secondary"
              onclick={() => void cancel()}
              disabled={canceling}>Cancel copy</button
            >{/if}
          {#if status.state === 'succeeded' && status.result}<button
              class="btn btn-primary"
              onclick={() => void openCopy()}
              disabled={opening}>{opening ? 'Opening…' : 'Open copy'}</button
            >{/if}
        </div>
      </div>
    {/if}
  </div>
</div>
