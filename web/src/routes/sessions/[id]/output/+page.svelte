<script lang="ts">
  import { errorMessage } from '$lib/errors';
  import { page } from '$app/state';
  import { onMount } from 'svelte';
  import {
    Check,
    CheckCircle2,
    CircleAlert,
    Copy,
    Download,
    Eye,
    FileAudio,
    FileText,
    FileVideo,
    LoaderCircle,
    PackageCheck,
    Trash2
  } from '@lucide/svelte';
  import type {
    ArtifactRecord,
    GenerationRun,
    JobRecord,
    SessionRecord,
    SettingsPayload
  } from '$lib/api-models';
  import {
    artifactApi,
    generationApi,
    jobApi,
    sessionApi
  } from '$lib/domain-api';
  import { appState } from '$lib/app-state.svelte';
  import {
    invalidates,
    invalidationBus,
    type InvalidationBatch
  } from '$lib/invalidation';
  import ArtifactPreview from '$lib/ArtifactPreview.svelte';
  import {
    artifactFilename,
    artifactRoleLabel,
    formatBytes
  } from '$lib/artifact-display';
  import OutputSettingsPanel from '$lib/OutputSettingsPanel.svelte';
  import OutputSettingsSnapshot from '$lib/OutputSettingsSnapshot.svelte';
  const sessionId = String(page.params.id);
  let artifacts = $state<ArtifactRecord[]>([]);
  let runs = $state<GenerationRun[]>([]);
  let exportJobs = $state<JobRecord[]>([]);
  let session = $state<SessionRecord | null>(null);
  let outputProfile = $state<SettingsPayload | null>(null);
  let selectedRunId = $state('');
  let busy = $state(false);
  let message = $state('');
  let error = $state('');
  let preview = $state<ArtifactRecord | null>(null);
  let deleting = $state<Record<string, boolean>>({});
  let copiedPath = $state('');
  const outputContext = $derived(
    outputProfile?.context && typeof outputProfile.context === 'object'
      ? (outputProfile.context as Record<string, unknown>)
      : {}
  );
  const hasSourceVideo = $derived(Boolean(outputContext.has_source_video));
  const hasSourceAudio = $derived(Boolean(outputContext.has_source_audio));
  const outputGroups = $derived([
    {
      label: 'Audio and video',
      items: artifacts.filter((item) =>
        /^(audio|video)\//.test(String(item.mime_type ?? ''))
      )
    },
    {
      label: 'Subtitles and documents',
      items: artifacts.filter(
        (item) => !/^(audio|video)\//.test(String(item.mime_type ?? ''))
      )
    }
  ]);
  type SaveOutputProfile = () => Promise<{
    output: Record<string, unknown>;
    audio: Record<string, unknown>;
  }>;
  let saveOutputProfile = $state<SaveOutputProfile | null>(null);
  async function load() {
    const [
      artifactPayload,
      runPayload,
      sessionPayload,
      settingsPayload,
      jobPayload
    ] = await Promise.all([
      artifactApi.list({ sessionId, limit: 300 }),
      generationApi.runs(sessionId),
      sessionApi.get(sessionId),
      sessionApi.settings(sessionId, 'output'),
      jobApi.list(500)
    ]);
    artifacts = artifactPayload.items
      .filter(
        (item) =>
          item.role === 'export' ||
          item.role.startsWith('export_') ||
          [
            'assembled_audio',
            'audiobook_audio',
            'dubbing_audio',
            'output_assembly',
            'rvc_audio'
          ].includes(item.role)
      )
      .sort((left, right) =>
        String(right.created_at).localeCompare(String(left.created_at))
      );
    runs = runPayload.items ?? [];
    exportJobs = (jobPayload.items ?? [])
      .filter(
        (item) => item.session_id === sessionId && item.kind === 'export.create'
      )
      .slice(0, 8);
    session = sessionPayload;
    outputProfile = settingsPayload;
    if (!selectedRunId || !runs.some((item) => item.id === selectedRunId))
      selectedRunId =
        runs.find((item) => item.status === 'completed')?.id ?? '';
  }
  async function waitForAssembly(runId: string) {
    for (let attempt = 0; attempt < 300; attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 800));
      const result = await generationApi.runs(sessionId);
      runs = result.items ?? [];
      const assembly = runs.find((item) => item.id === runId)?.assembly;
      if (assembly?.status === 'completed') return assembly;
      if (['failed', 'canceled'].includes(assembly?.status ?? ''))
        throw new Error(
          assembly?.error_message ||
            'The selected version could not be assembled.'
        );
    }
    throw new Error(
      'Assembly is still running. You can return later and export this version.'
    );
  }
  async function assemble() {
    const saveProfile = saveOutputProfile;
    if (!saveProfile) {
      error = 'Output settings are still loading. Please try again.';
      return;
    }
    busy = true;
    error = '';
    try {
      const savedProfile = await saveProfile();
      const selected = runs.find((item) => item.id === selectedRunId);
      const effective = savedProfile.output ?? {};
      // The API can omit inherited defaults from `effective`; resolve the same
      // workflow-aware fallbacks used by OutputSettingsPanel before deciding
      // whether a generation run must be assembled.
      const exportMode = String(
        effective.export_mode ??
          (session?.workflow_kind === 'subtitles' ? 'subtitles' : 'media')
      );
      const audioMode = String(
        effective.audio_mode ?? (hasSourceAudio ? 'mixed' : 'dubbing_only')
      );
      // Carry the displayed choice across every async boundary. Persisted
      // settings remain the profile, while this override is the immutable
      // contract for this particular assembly and export request.
      const runOverride = {
        output: {
          export_mode: exportMode,
          ...(session?.workflow_kind === 'voiceover'
            ? { audio_mode: audioMode }
            : {})
        }
      };
      const usesGeneratedAudio =
        session?.workflow_kind === 'audiobook' ||
        (exportMode === 'media' && audioMode !== 'preserve');
      if (usesGeneratedAudio && !selectedRunId)
        throw new Error(
          'Select a completed audio version for this media export.'
        );
      const needsAssembly = usesGeneratedAudio && Boolean(selectedRunId);
      const resolvedAssemblySettings = needsAssembly
        ? await sessionApi.resolveSettings(
            sessionId,
            ['audio', 'output'],
            runOverride
          )
        : null;
      const assemblyMatchesSettings =
        selected?.assembly?.settings_hash ===
        resolvedAssemblySettings?.settings_hash;
      const assemblyIsCurrent = Boolean(
        needsAssembly &&
        selected?.assembly?.status === 'completed' &&
        assemblyMatchesSettings
      );
      if (needsAssembly && !assemblyIsCurrent) {
        message = `Assembling ${selected?.label ?? 'the selected version'}…`;
        if (
          !assemblyMatchesSettings ||
          !['queued', 'running'].includes(selected?.assembly?.status ?? '')
        )
          await generationApi.createAssembly(
            sessionId,
            selectedRunId,
            runOverride
          );
        await waitForAssembly(selectedRunId);
      }
      const job = await sessionApi.runStage(sessionId, 'export', {
        ...runOverride,
        ...(needsAssembly ? { generation_run_id: selectedRunId } : {})
      });
      exportJobs = [
        job,
        ...exportJobs.filter((item) => item.id !== job.id)
      ].slice(0, 8);
      message = `Export ${job.id.slice(0, 8)} was submitted${needsAssembly && selected?.label ? ` from ${selected.label}` : ''}. Live progress is shown below.`;
      await load();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
  function canRemove(artifact: ArtifactRecord) {
    return (
      artifact.kind === 'export' ||
      artifact.role === 'export' ||
      artifact.role.startsWith('export_')
    );
  }
  function outputName(artifact: ArtifactRecord) {
    const extension = artifactFilename(artifact).match(/\.[^.]+$/)?.[0] ?? '';
    if (
      [
        'assembled_audio',
        'audiobook_audio',
        'dubbing_audio',
        'output_assembly',
        'rvc_audio'
      ].includes(artifact.role)
    )
      return `${session?.name ?? 'Session'} — assembled audio${extension}`;
    if (artifact.role.startsWith('export_subtitle_'))
      return `${session?.name ?? 'Session'} — ${artifactRoleLabel(artifact.role).toLowerCase()}${extension}`;
    if (artifact.role.startsWith('export_text_'))
      return `${session?.name ?? 'Session'} — ${artifactRoleLabel(artifact.role).toLowerCase()}${extension}`;
    return artifactFilename(artifact);
  }
  async function copyAbsolutePath(artifact: ArtifactRecord) {
    const path = String(artifact.path ?? '').trim();
    if (!path) return;
    try {
      if (navigator.clipboard?.writeText)
        await navigator.clipboard.writeText(path);
      else {
        const field = document.createElement('textarea');
        field.value = path;
        field.style.position = 'fixed';
        field.style.opacity = '0';
        document.body.append(field);
        field.select();
        document.execCommand('copy');
        field.remove();
      }
      copiedPath = artifact.id;
      window.setTimeout(() => {
        if (copiedPath === artifact.id) copiedPath = '';
      }, 1800);
    } catch (caught) {
      error = errorMessage(caught);
    }
  }
  async function removeExport(artifact: ArtifactRecord) {
    if (
      deleting[artifact.id] ||
      !window.confirm(
        `Remove ${artifact.relative_path.split('/').at(-1) ?? 'this export'}? This deletes the exported file but leaves its source artifacts intact.`
      )
    )
      return;
    deleting = { ...deleting, [artifact.id]: true };
    error = '';
    try {
      await sessionApi.removeOutput(sessionId, artifact.id);
      artifacts = artifacts.filter((item) => item.id !== artifact.id);
      if (preview?.id === artifact.id) preview = null;
      message = 'Export removed.';
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      deleting = { ...deleting, [artifact.id]: false };
    }
  }
  function jobLabel(job: JobRecord) {
    return job.status === 'running'
      ? 'Running'
      : job.status === 'queued'
        ? 'Queued'
        : job.status === 'succeeded'
          ? 'Completed'
          : job.status === 'failed'
            ? 'Failed'
            : job.status.replaceAll('_', ' ');
  }
  function progressPercent(job: JobRecord) {
    const value = Number(job.progress ?? 0);
    return Math.round(
      Math.max(0, Math.min(1, Number.isFinite(value) ? value : 0)) * 100
    );
  }
  function progressDetail(job: JobRecord) {
    return (
      job.progress_detail ||
      (job.status === 'queued' ? 'Waiting for an available worker' : '')
    );
  }
  function patchExportProgress(batch: InvalidationBatch) {
    for (const event of batch.events) {
      if (
        event.session_id !== sessionId ||
        event.job_kind !== 'export.create' ||
        !event.job_id
      )
        continue;
      const index = exportJobs.findIndex((job) => job.id === event.job_id);
      const current = index >= 0 ? exportJobs[index] : null;
      const next: JobRecord = {
        ...(current ?? {
          id: String(event.job_id),
          kind: 'export.create',
          session_id: sessionId,
          status: String(event.status ?? 'queued'),
          progress: Number(event.progress ?? 0),
          created_at: String(event.created_at ?? new Date().toISOString())
        }),
        ...(event.status ? { status: String(event.status) } : {}),
        ...(event.progress !== undefined
          ? { progress: Number(event.progress) }
          : {}),
        ...(event.detail !== undefined ? { progress_detail: event.detail } : {})
      };
      exportJobs =
        index >= 0
          ? exportJobs.map((job, jobIndex) => (jobIndex === index ? next : job))
          : [next, ...exportJobs].slice(0, 8);
    }
  }
  onMount(() => {
    let disposed = false;
    let refreshing = false;
    let refreshQueued = false;
    const refresh = async () => {
      if (disposed) return;
      if (refreshing) {
        refreshQueued = true;
        return;
      }
      refreshing = true;
      try {
        do {
          refreshQueued = false;
          try {
            await load();
          } catch (caught) {
            if (!disposed) error = errorMessage(caught);
          }
        } while (refreshQueued && !disposed);
      } finally {
        refreshing = false;
      }
    };
    refresh();
    const timer = window.setInterval(() => {
      if (
        !appState.eventsHealthy &&
        exportJobs.some((item) =>
          ['queued', 'running', 'cancel_requested'].includes(item.status)
        )
      )
        refresh();
    }, 5000);
    const changed = (batch: InvalidationBatch) => {
      patchExportProgress(batch);
      if (invalidates(batch, 'output', sessionId)) refresh();
    };
    const unsubscribe = invalidationBus.subscribe(changed);
    return () => {
      disposed = true;
      window.clearInterval(timer);
      unsubscribe();
    };
  });
</script>

<div class="space-y-5">
  <div class="flex flex-wrap items-end justify-between gap-4">
    <div>
      <h2 class="text-2xl font-semibold">
        {session?.workflow_kind === 'subtitles'
          ? 'Export subtitles'
          : session?.workflow_kind === 'audiobook'
            ? 'Audiobook output'
            : hasSourceVideo
              ? 'Video output'
              : 'Voiceover output'}
      </h2>
      <p class="muted mt-2">
        {session?.workflow_kind === 'subtitles'
          ? 'Save the selected subtitle document as SRT, WebVTT, or concatenated plain text.'
          : session?.workflow_kind === 'audiobook'
            ? 'Assemble the selected narration takes with book metadata, chapters, and optional cover artwork.'
            : hasSourceVideo
              ? 'Create a mixed, source-only, or voiceover-only video with optional subtitle tracks.'
              : 'Create standalone voiceover audio plus optional subtitle or text documents.'}
      </p>
    </div>
    <div class="flex flex-wrap items-end gap-2">
      {#if runs.length && session?.workflow_kind !== 'subtitles'}<label
          class="text-xs font-semibold"
          >Audio version<select
            bind:value={selectedRunId}
            class="mt-1 block max-w-sm rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] px-3 py-2 text-sm font-normal"
            ><option value="">Do not select generated audio</option
            >{#each runs.filter((item) => item.status === 'completed') as item}<option
                value={item.id}>{item.label}</option
              >{/each}</select
          ></label
        >{/if}<button
        onclick={assemble}
        disabled={busy || !saveOutputProfile}
        class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-50"
        >{#if busy}<LoaderCircle
            class="animate-spin"
            size={16}
          />{:else}<PackageCheck size={16} />{/if}
        {session?.workflow_kind === 'subtitles'
          ? 'Create subtitle export'
          : 'Create export'}</button
      >
    </div>
  </div>
  {#if message}<p class="rounded-xl bg-[var(--accent-soft)] p-3 text-sm">
      {message}
    </p>{/if}{#if error}<p
      class="rounded-xl bg-red-500/10 p-3 text-sm text-red-500"
    >
      {error}
    </p>{/if}
  {#if exportJobs.length}<section class="surface rounded-2xl p-5">
      <div class="eyebrow">Export activity</div>
      <div class="mt-4 space-y-3">
        {#each exportJobs.slice(0, 4) as job (job.id)}<div
            class="flex flex-wrap items-center gap-3 rounded-xl border border-[var(--line)] p-3"
          >
            {#if ['queued', 'running', 'cancel_requested'].includes(job.status)}<LoaderCircle
                class="animate-spin text-[var(--accent)]"
                size={18}
              />{:else if job.status === 'succeeded'}<CheckCircle2
                class="text-[var(--success)]"
                size={18}
              />{:else}<CircleAlert class="text-red-500" size={18} />{/if}
            <div class="min-w-0 flex-1">
              <div class="font-semibold">
                {jobLabel(job)} export
                <span class="muted font-mono text-xs">{job.id.slice(0, 8)}</span
                >
              </div>
              <div class="muted mt-1 text-xs">
                {progressPercent(job)}% · {new Date(
                  job.created_at
                ).toLocaleString()}
              </div>
              {#if progressDetail(job) && ['queued', 'running', 'cancel_requested'].includes(job.status)}<div
                  class="muted mt-1 truncate text-xs"
                  title={progressDetail(job)}
                >
                  {progressDetail(job)}
                </div>{/if}{#if job.error_message}<div
                  class="mt-1 text-xs text-red-500"
                >
                  {job.error_message}
                </div>{/if}
            </div>
            <div
              class="h-1.5 w-32 overflow-hidden rounded-full bg-[var(--line)]"
              role="progressbar"
              aria-label={`Export ${job.id.slice(0, 8)} progress`}
              aria-valuemin="0"
              aria-valuemax="100"
              aria-valuenow={progressPercent(job)}
            >
              <div
                class="h-full bg-[var(--accent)] transition-[width]"
                style={`width:${progressPercent(job)}%`}
              ></div>
            </div>
          </div>{/each}
      </div>
    </section>{/if}
  <OutputSettingsPanel
    {sessionId}
    generationRunId={selectedRunId}
    onSaveForExportReady={(save) => {
      saveOutputProfile = save;
    }}
  />
  <section class="surface rounded-2xl p-5">
    <div class="eyebrow">Completed outputs</div>
    {#if artifacts.length}<div class="mt-4 space-y-5">
        {#each outputGroups as group}
          {#if group.items.length}<div>
              <h3
                class="muted mb-2 text-xs font-semibold uppercase tracking-wide"
              >
                {group.label}
              </h3>
              <div class="space-y-2">
                {#each group.items as artifact}<article
                    class="w-full rounded-xl border border-[var(--line)] px-4 py-3"
                  >
                    <div class="flex w-full flex-wrap items-center gap-3">
                      <div
                        class="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-[var(--accent-soft)] text-[var(--accent)]"
                      >
                        {#if String(artifact.mime_type ?? '').startsWith('video/')}<FileVideo
                            size={19}
                          />{:else if String(artifact.mime_type ?? '').startsWith('audio/')}<FileAudio
                            size={19}
                          />{:else}<FileText size={19} />{/if}
                      </div>
                      <button
                        onclick={() => (preview = artifact)}
                        class="min-w-0 flex-1 text-left"
                      >
                        <div
                          class="flex min-w-0 flex-wrap items-baseline gap-x-2"
                        >
                          <strong class="truncate"
                            >{outputName(artifact)}</strong
                          >
                          <span class="muted text-xs"
                            >{artifactRoleLabel(artifact.role)}</span
                          >
                        </div>
                        <div
                          class="muted mt-1 flex flex-wrap gap-x-2 gap-y-1 text-xs"
                        >
                          <time datetime={artifact.created_at}
                            >{new Date(
                              artifact.created_at
                            ).toLocaleString()}</time
                          ><span
                            >· {artifact.mime_type ||
                              artifact.kind ||
                              'File'}</span
                          >{#if artifact.size_bytes != null}<span
                              >· {formatBytes(artifact.size_bytes)}</span
                            >{/if}
                        </div>
                      </button>
                      <div class="ml-auto flex items-center gap-1">
                        <button
                          onclick={() => (preview = artifact)}
                          class="rounded-lg p-2 hover:bg-[var(--accent-soft)]"
                          title="Preview output"
                          aria-label={`Preview ${outputName(artifact)}`}
                          ><Eye size={16} /></button
                        >{#if artifact.path}<button
                            onclick={() => copyAbsolutePath(artifact)}
                            class="rounded-lg p-2 hover:bg-[var(--accent-soft)]"
                            title="Copy absolute server path"
                            aria-label={`Copy absolute path for ${outputName(artifact)}`}
                            >{#if copiedPath === artifact.id}<Check
                                class="text-[var(--success)]"
                                size={16}
                              />{:else}<Copy size={16} />{/if}</button
                          >{/if}<a
                          href={`/api/v1/artifacts/${artifact.id}/content`}
                          download={artifactFilename(artifact)}
                          class="rounded-lg p-2 hover:bg-[var(--accent-soft)]"
                          title="Download output"
                          aria-label={`Download ${outputName(artifact)}`}
                          ><Download size={16} /></a
                        >{#if canRemove(artifact)}<button
                            onclick={() => removeExport(artifact)}
                            disabled={deleting[artifact.id]}
                            aria-label={`Remove export ${outputName(artifact)}`}
                            title="Remove export"
                            class="rounded-lg p-2 text-red-500 hover:bg-red-500/10 disabled:opacity-50"
                            >{#if deleting[artifact.id]}<LoaderCircle
                                class="animate-spin"
                                size={16}
                              />{:else}<Trash2 size={16} />{/if}</button
                          >{/if}
                      </div>
                    </div>
                    <OutputSettingsSnapshot
                      snapshot={artifact.metadata_json?.output_settings}
                    />
                  </article>{/each}
              </div>
            </div>{/if}
        {/each}
      </div>{:else}<p class="muted mt-4 text-sm">
        No completed outputs yet.
      </p>{/if}
  </section>
</div>
{#if preview}<ArtifactPreview
    artifact={preview}
    onclose={() => (preview = null)}
  />{/if}
