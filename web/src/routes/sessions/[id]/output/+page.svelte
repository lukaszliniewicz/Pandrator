<script lang="ts">
  import { projectLinkUrl } from '$lib/project-link-context';
  import GenerationRunHistory from '$lib/GenerationRunHistory.svelte';
  import {
    generationHistoryVersion,
    generationHistoryStatus,
    visibleGenerationRuns
  } from '$lib/generation-history';
  import { isJobStatus, isStorageWait } from '$lib/job-status';
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
  import { useProjectInterface } from '$lib/project-interface.svelte';
  import OutputSettingsSnapshot from '$lib/OutputSettingsSnapshot.svelte';
  import BookExportNotice from '$lib/BookExportNotice.svelte';
  import {
    exportIntent,
    selectedSubtitleTracks,
    subtitleSelectionIssue,
    type ExportProfile
  } from '$lib/export-intent';
  const sessionId = String(page.params.id);
  const projectInterface = useProjectInterface();
  const simpleMode = $derived(projectInterface?.mode === 'simple');
  let artifacts = $state<ArtifactRecord[]>([]);
  let runs = $state<GenerationRun[]>([]);
  let exportJobs = $state<JobRecord[]>([]);
  let session = $state<SessionRecord | null>(null);
  let outputProfile = $state<SettingsPayload | null>(null);
  let liveExportProfile = $state<ExportProfile | null>(null);
  let selectedRunId = $state('');
  let selectedRunVersionId = $state('');
  let audioVersionSide = $state<'before' | 'after'>('after');
  const historyRuns = $derived(visibleGenerationRuns(runs));
  const selectedHistoryRun = $derived(
    historyRuns.find((item) => item.id === selectedRunId)
  );
  const selectedRun = $derived(
    generationHistoryVersion(runs, selectedRunId, selectedRunVersionId)
  );
  let busy = $state(false);
  // The settings panel loads its own data in parallel with this page. Keep it
  // hidden until this page's first load settles, so the summary, activity and
  // history sections never appear above a form the user is already using.
  let pageSettled = $state(false);
  let message = $state('');
  let error = $state('');
  let preview = $state<ArtifactRecord | null>(null);
  let deleting = $state<Record<string, boolean>>({});
  let copiedPath = $state('');
  let loadRevision = 0;
  let jobProgressRevision = 0;
  const jobProgressRevisions = new Map<string, number>();
  const outputContext = $derived(
    outputProfile?.context && typeof outputProfile.context === 'object'
      ? (outputProfile.context as Record<string, unknown>)
      : {}
  );
  const hasSourceVideo = $derived(Boolean(outputContext.has_source_video));
  const hasSourceAudio = $derived(Boolean(outputContext.has_source_audio));
  const outputValues = $derived(
    liveExportProfile?.output ?? outputProfile?.effective ?? {}
  );
  const intendedExport = $derived(
    exportIntent(
      session?.workflow_kind,
      outputValues,
      hasSourceVideo,
      hasSourceAudio
    )
  );
  const selectedSubtitleMasters = $derived(
    selectedSubtitleTracks(outputContext, outputValues.subtitle_selection)
  );
  const subtitleChoiceIssue = $derived(
    session &&
      outputProfile &&
      session.workflow_kind !== 'audiobook' &&
      intendedExport.includesSubtitles &&
      intendedExport.subtitleTextSource === 'current'
      ? subtitleSelectionIssue(outputContext, outputValues.subtitle_selection)
      : ''
  );
  const projectedDisplayRevision = $derived.by(() => {
    if (intendedExport.subtitleTextSource === 'selected_artifact') return '';
    if (intendedExport.usesHistoricalText)
      return selectedRun?.plan_revision_id ?? '';
    const raw = outputContext.subtitle_display_revision;
    if (!raw || typeof raw !== 'object') return '';
    const reference = raw as Record<string, unknown>;
    return selectedSubtitleMasters.some(
      (track) => track.artifact_id === reference.artifact_id
    )
      ? String(reference.revision_id ?? '')
      : '';
  });
  const exportFileLabel = $derived(
    intendedExport.exportMode === 'text'
      ? 'Plain-text transcript'
      : intendedExport.exportMode === 'subtitles'
        ? `${String(outputValues.subtitle_format ?? 'srt').toUpperCase()} subtitles`
        : intendedExport.exportMode === 'video_book'
          ? 'Video book'
          : intendedExport.rendersVideo
            ? 'MP4 video'
            : `${String(outputValues.format ?? 'wav').toUpperCase()} audio`
  );
  const subtitleTrackLabel = $derived.by(() => {
    if (session?.workflow_kind === 'audiobook') return 'Narration text';
    if (subtitleChoiceIssue) return 'Selected subtitle document unavailable';
    if (!selectedSubtitleMasters.length)
      return 'No available subtitle document';
    const hasTranslation = selectedSubtitleMasters.some(
      (track) => track.role === 'translation'
    );
    const hasSource = selectedSubtitleMasters.some(
      (track) => track.role !== 'translation'
    );
    if (hasSource && hasTranslation) return 'Source and translated text';
    if (hasSource) return 'Source text';
    return 'Translated text';
  });
  function updateExportProfile(profile: ExportProfile) {
    liveExportProfile = profile;
  }
  const assemblyRoles = new Set([
    'assembled_audio',
    'audiobook_audio',
    'dubbing_audio',
    'output_assembly',
    'rvc_audio'
  ]);
  const outputGroups = $derived([
    {
      label: 'Completed audio and video exports',
      description: '',
      items: artifacts.filter(
        (item) =>
          !assemblyRoles.has(item.role) &&
          /^(audio|video)\//.test(String(item.mime_type ?? ''))
      )
    },
    {
      label: 'Subtitles and documents',
      description: '',
      items: artifacts.filter(
        (item) =>
          !assemblyRoles.has(item.role) &&
          !/^(audio|video)\//.test(String(item.mime_type ?? ''))
      )
    },
    {
      label: 'Intermediate audio assemblies',
      description:
        'Intermediate/current audio used to build exports. A finished assembly does not mean a video export has finished.',
      items: artifacts.filter((item) => assemblyRoles.has(item.role))
    }
  ]);
  type SaveOutputProfile = () => Promise<{
    output: Record<string, unknown>;
    audio: Record<string, unknown>;
  }>;
  let saveOutputProfile = $state<SaveOutputProfile | null>(null);
  // Both the original direct exports and the durable assemble/export workflow
  // belong in this view. Use the same predicate for reloads and live events.
  function isExportJobKind(
    kind: unknown
  ): kind is 'export.create' | 'export.variant' {
    return kind === 'export.create' || kind === 'export.variant';
  }
  async function load() {
    const revision = ++loadRevision;
    const progressRevision = jobProgressRevision;
    const [
      artifactPayload,
      runPayload,
      sessionPayload,
      settingsPayload,
      jobPayload
    ] = await Promise.all([
      artifactApi.list({ sessionId, limit: 300, outputOnly: true }),
      generationApi.runs(sessionId),
      sessionApi.get(sessionId),
      sessionApi.settings(sessionId, 'output'),
      jobApi.exports(sessionId)
    ]);
    if (revision !== loadRevision) return;
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
    // A live event can arrive while this HTTP snapshot is in flight. Preserve
    // those newer jobs while still loading unrelated jobs from the snapshot.
    const newerJobs = exportJobs.filter(
      (job) => (jobProgressRevisions.get(job.id) ?? 0) > progressRevision
    );
    const newerJobIds = new Set(newerJobs.map((job) => job.id));
    const snapshotJobs = (jobPayload.items ?? []).filter(
      (item) => item.session_id === sessionId && isExportJobKind(item.kind)
    );
    exportJobs = [
      ...newerJobs,
      ...snapshotJobs.filter((job) => !newerJobIds.has(job.id))
    ].slice(0, 8);
    session = sessionPayload;
    outputProfile = settingsPayload;
    if (
      !selectedRunId ||
      !historyRuns.some((item) => item.id === selectedRunId)
    ) {
      selectedRunId =
        historyRuns.find(
          (item) => item.status === 'completed' || item.audio_version
        )?.id ?? '';
      selectedRunVersionId = '';
    }
  }
  async function assemble() {
    if (subtitleChoiceIssue) {
      error = subtitleChoiceIssue;
      return;
    }
    const saveProfile = saveOutputProfile;
    if (!saveProfile) {
      error = 'Output settings are still loading. Please try again.';
      return;
    }
    busy = true;
    error = '';
    try {
      const selected = selectedRun;
      const exportRunId = selected?.id ?? '';
      const exportSide = selected?.audio_version ? audioVersionSide : 'after';
      const savedProfile = await saveProfile();
      const effective = savedProfile.output ?? {};
      // The API can omit inherited defaults from `effective`; resolve the same
      // workflow-aware fallbacks used by OutputSettingsPanel before deciding
      // whether the export needs generated audio.
      const {
        exportMode,
        audioMode,
        usesHistoricalText,
        usesGeneratedAudio,
        subtitleTextSource
      } = exportIntent(
        session?.workflow_kind,
        effective,
        hasSourceVideo,
        hasSourceAudio
      );
      if (usesHistoricalText && !exportRunId)
        throw new Error(
          'Select an Audio version to use its historical subtitle text.'
        );
      // Pin the export intent at click time. The single runStage POST below is
      // the whole request: the export stage routes to the durable
      // export.variant job server-side and assembles from this same pinned
      // generation run when needed. No separate createAssembly/wait here, so a
      // second click can never build a duplicate assembly and navigation after
      // the accepted job never strands or repeats the server export.
      const runOverride = {
        output: {
          export_mode: exportMode,
          ...(session?.workflow_kind !== 'audiobook'
            ? {
                subtitle_text_source: usesHistoricalText
                  ? 'generation_run'
                  : subtitleTextSource === 'selected_artifact'
                    ? 'selected_artifact'
                    : 'current'
              }
            : {}),
          ...(exportMode === 'media' &&
          hasSourceVideo &&
          audioMode !== 'preserve'
            ? { video_tail_extension_policy: 'ask' }
            : {}),
          ...(session?.workflow_kind === 'voiceover'
            ? { audio_mode: audioMode }
            : {})
        }
      };
      if (
        usesGeneratedAudio &&
        !exportRunId &&
        !artifacts.some(
          (item) => item.role === 'assembled_audio' && item.state === 'current'
        )
      )
        throw new Error(
          'Assemble the current recordings in Generation before exporting.'
        );
      const needsAssembly = usesGeneratedAudio && Boolean(exportRunId);
      const label = selected?.label ?? 'the selected version';
      const job = await sessionApi.runStage(sessionId, 'export', {
        ...runOverride,
        ...(needsAssembly || usesHistoricalText
          ? { generation_run_id: exportRunId, audio_version_side: exportSide }
          : {})
      });
      exportJobs = [
        job,
        ...exportJobs.filter((item) => item.id !== job.id)
      ].slice(0, 8);
      message = `Export ${job.id.slice(0, 8)} was submitted${needsAssembly ? ` from ${label}` : ''}. Live progress is shown below.`;
      await load();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
  function isAssemblyArtifact(artifact: ArtifactRecord) {
    return assemblyRoles.has(artifact.role);
  }
  function canRemove(artifact: ArtifactRecord) {
    return (
      artifact.kind === 'export' ||
      artifact.role === 'export' ||
      artifact.role.startsWith('export_') ||
      ['assembled_audio', 'audiobook_audio', 'dubbing_audio'].includes(
        artifact.role
      )
    );
  }
  function outputName(artifact: ArtifactRecord) {
    const extension = artifactFilename(artifact).match(/\.[^.]+$/)?.[0] ?? '';
    if (artifact.role === 'export_saved_speech')
      return `${session?.name ?? 'Project'} — saved speech${extension}`;
    if (artifact.role === 'export_dubbed_audio')
      return `${session?.name ?? 'Project'} — voiceover audio${extension}`;
    if (
      [
        'assembled_audio',
        'audiobook_audio',
        'dubbing_audio',
        'output_assembly',
        'rvc_audio'
      ].includes(artifact.role)
    )
      return `${session?.name ?? 'Project'} — assembled audio${extension}`;
    if (artifact.role.startsWith('export_subtitle_'))
      return `${session?.name ?? 'Project'} — ${artifactRoleLabel(artifact.role).toLowerCase()}${extension}`;
    if (artifact.role.startsWith('export_text_'))
      return `${session?.name ?? 'Project'} — ${artifactRoleLabel(artifact.role).toLowerCase()}${extension}`;
    return artifactFilename(artifact);
  }
  async function copyAbsolutePath(artifact: ArtifactRecord) {
    const path = String(artifact.path ?? '').trim();
    if (!path) return;
    let field: HTMLTextAreaElement | null = null;
    try {
      let copied = false;
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(path);
        copied = true;
      } else {
        field = document.createElement('textarea');
        field.value = path;
        field.style.position = 'fixed';
        field.style.opacity = '0';
        document.body.append(field);
        field.select();
        copied = document.execCommand('copy');
      }
      if (!copied)
        throw new Error('Clipboard copy was rejected by the browser.');
      copiedPath = artifact.id;
      window.setTimeout(() => {
        if (copiedPath === artifact.id) copiedPath = '';
      }, 1800);
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      field?.remove();
    }
  }
  async function removeExport(artifact: ArtifactRecord) {
    const assembly = isAssemblyArtifact(artifact);
    const filename = artifact.relative_path.split('/').at(-1) ?? 'this output';
    if (
      deleting[artifact.id] ||
      !window.confirm(
        assembly
          ? `Remove intermediate assembly ${filename}? This deletes the assembled audio file but leaves source takes and final exports intact.`
          : `Remove ${filename}? This deletes the exported file but leaves its source artifacts intact.`
      )
    )
      return;
    deleting = { ...deleting, [artifact.id]: true };
    error = '';
    try {
      await sessionApi.removeOutput(sessionId, artifact.id);
      // A refresh started before removal may still contain the deleted output.
      loadRevision += 1;
      artifacts = artifacts.filter((item) => item.id !== artifact.id);
      if (preview?.id === artifact.id) preview = null;
      message = assembly ? 'Assembly removed.' : 'Export removed.';
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      deleting = { ...deleting, [artifact.id]: false };
    }
  }
  function isVideoTailWarning(job: JobRecord) {
    return (
      job.status === 'failed' && job.error_code === 'VideoTailExtensionRequired'
    );
  }
  async function decideVideoTail(job: JobRecord, action: 'stop' | 'extend') {
    if (busy) return;
    busy = true;
    error = '';
    try {
      const result = await jobApi.decideVideoTail(job.id, action);
      exportJobs = exportJobs.map((item) =>
        item.id === job.id
          ? {
              ...item,
              result_json: {
                ...item.result_json,
                video_tail_decision: action,
                ...(action === 'extend'
                  ? { continuation_job_id: result.id }
                  : {})
              }
            }
          : item
      );
      if (action === 'extend') {
        exportJobs = [
          result,
          ...exportJobs.filter((item) => item.id !== result.id)
        ].slice(0, 8);
      }
      message =
        action === 'stop'
          ? 'Export stopped. The prepared speech is available for another export.'
          : 'Export continuing with the last frame frozen for the full extra duration.';
      await load();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
  function jobLabel(job: JobRecord) {
    if (isVideoTailWarning(job)) {
      return job.result_json?.video_tail_decision === 'extend'
        ? 'Continued'
        : job.result_json?.video_tail_decision === 'stop'
          ? 'Stopped'
          : 'Needs your decision';
    }
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
  function measuredDurationSummary(artifact: ArtifactRecord) {
    const raw = artifact.metadata_json?.media_durations_ms;
    if (!raw || typeof raw !== 'object') return '';
    const values = raw as Record<string, unknown>;
    return (['video', 'audio', 'container'] as const)
      .flatMap((key) => {
        const value = values[key];
        if (typeof value !== 'number' || !Number.isFinite(value) || value < 0)
          return [];
        const milliseconds = Math.round(value);
        const hours = Math.floor(milliseconds / 3_600_000);
        const minutes = Math.floor(milliseconds / 60_000) % 60;
        const seconds = Math.floor(milliseconds / 1000) % 60;
        const time = `${hours ? `${hours}:` : ''}${String(minutes).padStart(hours ? 2 : 1, '0')}:${String(seconds).padStart(2, '0')}.${String(milliseconds % 1000).padStart(3, '0')}`;
        const label = { video: 'Video', audio: 'Audio', container: 'File' }[
          key
        ];
        return [`${label} ${time}`];
      })
      .join(' · ');
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
        !isExportJobKind(event.job_kind) ||
        !event.job_id
      )
        continue;
      jobProgressRevisions.set(String(event.job_id), ++jobProgressRevision);
      const index = exportJobs.findIndex((job) => job.id === event.job_id);
      const current = index >= 0 ? exportJobs[index] : null;
      const next: JobRecord = {
        ...(current ?? {
          id: String(event.job_id),
          kind: event.job_kind,
          session_id: sessionId,
          status: isJobStatus(event.status) ? event.status : 'queued',
          progress: Number(event.progress ?? 0),
          created_at: String(event.created_at ?? new Date().toISOString())
        }),
        ...(isJobStatus(event.status) ? { status: event.status } : {}),
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
          if (!disposed) pageSettled = true;
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
        {!session || !outputProfile
          ? 'Loading output…'
          : session.workflow_kind === 'subtitles'
            ? 'Export subtitles'
            : session?.workflow_kind === 'audiobook'
              ? 'Audiobook output'
              : hasSourceVideo
                ? 'Voiceover output'
                : 'Voiceover output'}
      </h2>
      <p class="muted mt-2">
        {!session || !outputProfile
          ? 'Loading project output settings.'
          : session.workflow_kind === 'subtitles'
            ? 'Save the selected subtitle document as SRT, WebVTT, or concatenated plain text.'
            : session?.workflow_kind === 'audiobook'
              ? 'Export the selected narration as audio, a video book, or timed subtitles. Audio versions retain book metadata, chapters, and artwork.'
              : hasSourceVideo
                ? 'Export video or an audio-only soundtrack, using original audio, generated speech, or the complete mix.'
                : 'Create standalone voiceover audio plus optional subtitle or text documents.'}
      </p>
    </div>
    <div class="flex flex-wrap items-end gap-2">
      {#if historyRuns.length && session?.workflow_kind !== 'subtitles'}<label
          class="text-xs font-semibold"
          >Audio version<select
            bind:value={selectedRunId}
            disabled={busy}
            onchange={() => {
              selectedRunVersionId = '';
              audioVersionSide = 'after';
            }}
            class="mt-1 block max-w-sm rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] px-3 py-2 text-sm font-normal"
            ><option value="">Do not select generated audio</option
            >{#each historyRuns.filter((item) => item.status === 'completed' || item.audio_version) as item}<option
                value={item.id}
                >{item.label}{item.timing_repair
                  ? ` · ${generationHistoryStatus(item)}`
                  : ''}</option
              >{/each}</select
          ></label
        >{/if}
      {#if selectedRun?.audio_version}
        <div
          role="group"
          aria-label="Recast export version"
          class="flex rounded-xl border border-[var(--line)] p-1"
        >
          <button
            class="btn"
            class:btn-primary={audioVersionSide === 'before'}
            aria-pressed={audioVersionSide === 'before'}
            disabled={busy}
            onclick={() => (audioVersionSide = 'before')}>Before recast</button
          >
          <button
            class="btn"
            class:btn-primary={audioVersionSide === 'after'}
            aria-pressed={audioVersionSide === 'after'}
            disabled={busy}
            onclick={() => (audioVersionSide = 'after')}>After recast</button
          >
        </div>
      {/if}<button
        onclick={assemble}
        disabled={busy ||
          !saveOutputProfile ||
          !session ||
          !outputProfile ||
          Boolean(subtitleChoiceIssue)}
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
  {#if subtitleChoiceIssue}<p
      role="alert"
      class="rounded-xl border border-amber-400/50 bg-amber-500/10 p-4 text-sm"
    >
      {subtitleChoiceIssue}
    </p>{/if}
  {#if session && outputProfile}
    <section aria-label="Export contents" class="surface rounded-2xl p-5">
      <div class="section-label">Export contents</div>
      <dl class="mt-3 grid gap-x-8 gap-y-3 text-sm sm:grid-cols-2">
        <div>
          <dt class="muted text-xs">File</dt>
          <dd class="mt-1 font-semibold">{exportFileLabel}</dd>
        </div>
        {#if !['subtitles', 'text'].includes(intendedExport.exportMode)}
          <div>
            <dt class="muted text-xs">Soundtrack</dt>
            <dd class="mt-1 font-semibold">
              {session.workflow_kind === 'audiobook'
                ? 'Generated narration'
                : intendedExport.audioMode === 'preserve'
                  ? 'Source audio'
                  : intendedExport.audioMode === 'mixed'
                    ? 'Source audio and generated voiceover'
                    : 'Generated voiceover'}
            </dd>
            {#if intendedExport.audioMode === 'mixed' && session.workflow_kind !== 'audiobook'}
              <dd class="muted mt-1 text-xs">
                Source {String(outputValues.mix_source_gain_db ?? 0)} dB · Voice {String(
                  outputValues.mix_voice_lufs ?? -16
                )} LUFS,
                {String(outputValues.mix_voice_gain_db ?? 0)} dB gain ·
                {String(outputValues.mix_ducking ?? 'strong')} ducking
              </dd>
            {/if}
          </div>
        {/if}
        {#if intendedExport.includesSubtitles}
          <div>
            <dt class="muted text-xs">
              {intendedExport.exportMode === 'text'
                ? 'Transcript'
                : 'Subtitle tracks'}
            </dt>
            <dd class="mt-1 font-semibold">
              {subtitleTrackLabel}{intendedExport.rendersVideo
                ? ` · ${String(outputValues.subtitle_mode ?? 'none') === 'burned' ? 'Burned into video' : 'Selectable'}`
                : ''}
            </dd>
            <dd class="muted mt-1 text-xs">
              {intendedExport.usesHistoricalText
                ? 'Historical text from the selected audio version'
                : intendedExport.subtitleTextSource === 'selected_artifact'
                  ? 'Selected subtitle revision'
                  : 'Current display text'}
            </dd>
            {#if selectedSubtitleMasters.length}
              <dd>
                <details class="muted mt-2 text-xs">
                  <summary class="cursor-pointer">Subtitle sources</summary>
                  <dl class="mt-2 space-y-2">
                    {#each selectedSubtitleMasters as track (track.artifact_id)}
                      <div>
                        <dt>
                          {track.role === 'translation'
                            ? 'Translated subtitles'
                            : 'Source subtitles'}
                        </dt>
                        <dd class="break-all font-mono">{track.artifact_id}</dd>
                        {#if track.content_hash}<dd
                            class="break-all font-mono"
                            title="Source content hash"
                          >
                            SHA-256 {track.content_hash}
                          </dd>{/if}
                        {#if track.revision_id}<dd
                            class="break-all font-mono"
                            title="Source document revision"
                          >
                            Revision {track.revision_id}
                          </dd>{/if}
                      </div>
                    {/each}
                    {#if projectedDisplayRevision}
                      <div>
                        <dt>Display edit revision</dt>
                        <dd class="break-all font-mono">
                          {projectedDisplayRevision}
                        </dd>
                      </div>
                    {/if}
                  </dl>
                </details>
              </dd>
            {/if}
          </div>
        {/if}
        {#if outputContext.source_name}
          <div>
            <dt class="muted text-xs">Source</dt>
            <dd class="mt-1 break-words font-medium">
              {String(outputContext.source_name)}
            </dd>
            {#if outputContext.source_artifact_id}
              <dd
                class="muted mt-1 font-mono text-xs"
                title={String(outputContext.source_artifact_id)}
              >
                {String(outputContext.source_artifact_id).slice(0, 8)}
              </dd>
            {/if}
          </div>
        {/if}
        {#if intendedExport.usesGeneratedAudio || intendedExport.usesHistoricalText}
          <div>
            <dt class="muted text-xs">Audio version</dt>
            <dd class="mt-1 font-medium">
              {selectedRun?.label ?? 'Current assembled recordings'}
              {selectedRun?.audio_version
                ? ` · ${audioVersionSide === 'before' ? 'Before recast' : 'After recast'}`
                : ''}
            </dd>
            {#if selectedRun?.id}
              <dd class="muted mt-1 font-mono text-xs" title={selectedRun.id}>
                {selectedRun.id.slice(0, 8)}
              </dd>
            {/if}
          </div>
        {/if}
        {#if ['media', 'audio', 'video_book'].includes(intendedExport.exportMode)}
          <div>
            <dt class="muted text-xs">Additional project sounds</dt>
            <dd class="mt-1 font-medium">
              {outputValues.include_sounds === false
                ? 'Disabled'
                : 'Enabled if present'}
            </dd>
          </div>
        {/if}
      </dl>
      <p class="muted mt-4 text-xs">
        Current controls are saved when you create the export.
        {#if intendedExport.usesGeneratedAudio}
          Recorded takes are reused; an audio assembly is prepared if needed.
        {/if}
      </p>
    </section>
  {/if}
  {#if message}<p class="rounded-xl bg-[var(--accent-soft)] p-3 text-sm">
      {message}
    </p>{/if}{#if error}<p
      class="rounded-xl bg-red-500/10 p-3 text-sm text-red-500"
    >
      {error}
    </p>{/if}
  {#if exportJobs.length}<section
      aria-label="Export activity"
      class="surface rounded-2xl p-5"
    >
      <div class="section-label">Export activity</div>
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
              />{:else}<CircleAlert
                class={isVideoTailWarning(job)
                  ? 'text-[var(--accent)]'
                  : 'text-red-500'}
                size={18}
              />{/if}
            <div class="min-w-0 flex-1">
              <div class="font-semibold">
                {isVideoTailWarning(job) &&
                !job.result_json?.video_tail_decision
                  ? 'Export needs your decision'
                  : isStorageWait(job)
                    ? 'Export waiting for storage'
                    : `${jobLabel(job)} export`}
                <span class="muted font-mono text-xs">{job.id.slice(0, 8)}</span
                >
              </div>
              <div class="muted mt-1 text-xs">
                {progressPercent(job)}% · {new Date(
                  job.created_at
                ).toLocaleString()}
              </div>
              {#if progressDetail(job) && !isStorageWait(job) && ['queued', 'running', 'cancel_requested'].includes(job.status)}<div
                  class="muted mt-1 truncate text-xs"
                  title={progressDetail(job)}
                >
                  {progressDetail(job)}
                </div>{/if}{#if job.error_message}<div
                  class="mt-1 text-sm leading-6"
                  class:text-red-500={!isVideoTailWarning(job) &&
                    !isStorageWait(job)}
                  role={isStorageWait(job)
                    ? 'status'
                    : isVideoTailWarning(job) &&
                        !job.result_json?.video_tail_decision
                      ? 'alert'
                      : undefined}
                >
                  {job.error_message}
                </div>{/if}
              {#if isStorageWait(job) && job.available_at}
                <p class="muted mt-2 text-xs">
                  Next check <time datetime={job.available_at}
                    >{new Date(job.available_at).toLocaleString()}</time
                  >
                </p>
              {/if}
              {#if isVideoTailWarning(job) && !job.result_json?.video_tail_decision}
                <div class="mt-3 flex flex-wrap gap-2">
                  <button
                    onclick={() => decideVideoTail(job, 'stop')}
                    disabled={busy}
                    class="rounded-xl border border-[var(--line)] px-4 py-2 text-sm font-semibold disabled:opacity-50"
                    >Stop export</button
                  >
                  <button
                    onclick={() => decideVideoTail(job, 'extend')}
                    disabled={busy}
                    class="rounded-xl bg-[var(--accent)] px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
                    >Freeze last frame and continue</button
                  >
                </div>
              {/if}
            </div>
            {#if !isVideoTailWarning(job)}<div
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
              </div>{/if}
          </div>{/each}
      </div>
    </section>{/if}
  {#if selectedHistoryRun?.timing_repair}
    <GenerationRunHistory
      run={selectedHistoryRun}
      versionId={selectedRunVersionId}
      disabled={busy}
      onSelectVersion={(versionId) => (selectedRunVersionId = versionId)}
    />
  {/if}
  <details
    open={!simpleMode}
    hidden={!pageSettled}
    class="rounded-2xl border border-[var(--line)] p-4"
  >
    <summary class="cursor-pointer text-sm font-semibold"
      >Format and export settings</summary
    >
    <div class="mt-3">
      <OutputSettingsPanel
        {sessionId}
        onExportProfileChanged={updateExportProfile}
        generationRunId={selectedRun?.id ?? ''}
        {audioVersionSide}
        onSaveForExportReady={(save) => {
          saveOutputProfile = save;
        }}
      />
    </div>
  </details>
  <section class="surface rounded-2xl p-5">
    <div class="section-label">Completed outputs</div>
    {#if artifacts.length}<div class="mt-4 space-y-5">
        {#each outputGroups as group}
          {#if group.items.length}<div>
              <h3
                class="muted mb-2 text-xs font-semibold uppercase tracking-wide"
              >
                {group.label}
              </h3>
              {#if group.description}<p class="muted mb-3 text-xs">
                  {group.description}
                </p>{/if}
              <div class="space-y-2">
                {#each group.items as artifact}<article
                    class="w-full rounded-xl border border-[var(--line)] px-4 py-3"
                  >
                    <div
                      class="grid w-full grid-cols-[auto_minmax(0,1fr)] items-start gap-3 sm:grid-cols-[auto_minmax(0,1fr)_auto]"
                    >
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
                          <strong
                            class="line-clamp-2 break-words [overflow-wrap:anywhere]"
                            >{outputName(artifact)}</strong
                          >
                          <span class="muted text-xs"
                            >{artifact.metadata_json?.preview === true
                              ? 'Export preview'
                              : artifactRoleLabel(artifact.role)}</span
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
                      <div
                        class="col-span-2 flex items-center justify-end gap-1 border-t border-[var(--line)] pt-2 sm:col-span-1 sm:border-0 sm:pt-0"
                      >
                        <button
                          onclick={() => (preview = artifact)}
                          class="grid min-h-11 min-w-11 place-items-center rounded-lg p-2 hover:bg-[var(--accent-soft)]"
                          title="Preview output"
                          aria-label={`Preview ${outputName(artifact)}`}
                          ><Eye size={16} /></button
                        >{#if artifact.path}<button
                            onclick={() => copyAbsolutePath(artifact)}
                            class="grid min-h-11 min-w-11 place-items-center rounded-lg p-2 hover:bg-[var(--accent-soft)]"
                            title="Copy absolute server path"
                            aria-label={`Copy absolute path for ${outputName(artifact)}`}
                            >{#if copiedPath === artifact.id}<Check
                                class="text-[var(--success)]"
                                size={16}
                              />{:else}<Copy size={16} />{/if}</button
                          >{/if}<a
                          href={projectLinkUrl(
                            `/api/v1/artifacts/${artifact.id}/content`
                          )}
                          download={outputName(artifact)}
                          class="grid min-h-11 min-w-11 place-items-center rounded-lg p-2 hover:bg-[var(--accent-soft)]"
                          title="Download output"
                          aria-label={`Download ${outputName(artifact)}`}
                          ><Download size={16} /></a
                        >{#if canRemove(artifact)}<button
                            onclick={() => removeExport(artifact)}
                            disabled={deleting[artifact.id]}
                            aria-label={isAssemblyArtifact(artifact)
                              ? `Remove assembly ${outputName(artifact)}`
                              : `Remove export ${outputName(artifact)}`}
                            title={isAssemblyArtifact(artifact)
                              ? 'Remove intermediate assembly'
                              : 'Remove export'}
                            class="grid min-h-11 min-w-11 place-items-center rounded-lg p-2 text-red-500 hover:bg-red-500/10 disabled:opacity-50"
                            >{#if deleting[artifact.id]}<LoaderCircle
                                class="animate-spin"
                                size={16}
                              />{:else}<Trash2 size={16} />{/if}</button
                          >{/if}
                      </div>
                    </div>
                    {#if measuredDurationSummary(artifact)}
                      <p
                        class="muted mt-3 text-xs"
                        aria-label="Measured media durations"
                      >
                        {measuredDurationSummary(artifact)}
                      </p>
                    {/if}
                    <OutputSettingsSnapshot
                      snapshot={artifact.metadata_json?.output_settings}
                    />
                    <BookExportNotice metadata={artifact.metadata_json} />
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
    displayName={outputName(preview)}
    onclose={() => (preview = null)}
  />{/if}
