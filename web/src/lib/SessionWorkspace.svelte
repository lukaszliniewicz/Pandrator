<script lang="ts">
  import {
    ChevronRight,
    CircleAlert,
    Crop,
    LoaderCircle,
    Play,
    Sparkles
  } from '@lucide/svelte';
  import StageInputPicker from './StageInputPicker.svelte';
  import SessionSourceCard from './SessionSourceCard.svelte';
  import TranslationVersions from './TranslationVersions.svelte';
  import VoiceSetupCard from './VoiceSetupCard.svelte';
  import SpeechPlanCard from './SpeechPlanCard.svelte';
  import WorkflowStageCard from './WorkflowStageCard.svelte';
  import SpeechPlanPicker from './SpeechPlanPicker.svelte';
  import WorkflowRunDialogs from './WorkflowRunDialogs.svelte';
  import GenerationStartDialog from './GenerationStartDialog.svelte';
  import GuidedTour from './GuidedTour.svelte';
  import SpeechPlanSettings from './SpeechPlanSettings.svelte';
  import StageSettingsDialog from './StageSettingsDialog.svelte';
  import { errorMessage } from './errors';
  import { jobApi, sessionApi } from './domain-api';
  import type {
    OutcomePlan,
    SessionRecord,
    SettingsPayload,
    StageRerunImpact,
    StageSettingsMismatch,
    WorkflowStage
  } from './api-models';
  import { appState } from './app-state.svelte';
  import type { PreviewableArtifact } from './artifact-display';
  import { onMount, onDestroy, untrack } from 'svelte';
  import { type WorkflowStore } from './workflow-store.svelte';
  import type PdfEditor from './PdfEditor.svelte';
  import type AddSourceDialog from './AddSourceDialog.svelte';
  import type { CastDraftController } from './generation-controls';
  import type { GenerationStartMode } from './generation-start';
  import {
    speechPlanState,
    sessionFlowAction,
    openSpeechPlanEditor,
    type SpeechPlanState
  } from './session-flow';
  import { invalidationBus, invalidates } from './invalidation';
  import type ArtifactPreview from './ArtifactPreview.svelte';
  import type SessionForkDialog from './SessionForkDialog.svelte';
  import type SubtitleReview from './SubtitleReview.svelte';
  import type TextOptimizationReview from './TextOptimizationReview.svelte';
  import type { StageArtifact } from './stage-artifacts';

  let {
    session,
    outcome: initialOutcome,
    workflowStore,
    onupdated,
    initialSettingsStage = '',
    scope = 'all',
    workspaceMode = $bindable<'review' | 'automatic'>('review'),
    manageMode = true,
    active = true
  }: {
    session: SessionRecord;
    outcome: OutcomePlan;
    workflowStore: WorkflowStore;
    onupdated: (session: SessionRecord) => void;
    initialSettingsStage?: string;
    scope?: 'all' | 'source' | 'language';
    workspaceMode?: 'review' | 'automatic';
    manageMode?: boolean;
    active?: boolean;
  } = $props();

  let audiobookCastPanel = $state<CastDraftController>();

  type Stage = WorkflowStage;

  const snapshot = $derived(workflowStore.snapshot);
  const sharedStages = new Set([
    'transcribe',
    'prepare_text',
    'edit_media',
    'correct'
  ]);
  const visibleStages = $derived(
    snapshot?.stages.filter(
      (stage) =>
        scope === 'all' ||
        (scope === 'source'
          ? sharedStages.has(stage.key)
          : !sharedStages.has(stage.key))
    ) ?? []
  );

  let speechPlan = $state<SpeechPlanState | null>(null);

  let planBusy = $state(false);

  let planSettingsOpen = $state(false);

  let planRequest = 0;

  let generationStartDialog = $state<{ mode: GenerationStartMode } | null>(
    null
  );

  let generationNotice = $state('');

  let planNotice = $state('');

  let planPreparationJobId = $state('');

  const selectedSpeechPlan = $derived(
    speechPlan?.items.find(
      (item) => item.id === speechPlan?.selected_revision_id
    )
  );

  async function loadSpeechPlan() {
    if (
      scope === 'source' ||
      !active ||
      !workflowStore.snapshot?.stages.some(
        (item) => item.key === 'generate_audio'
      )
    )
      return;
    const request = ++planRequest;
    try {
      const value = await speechPlanState(session.id, { summary: true });
      if (request === planRequest) speechPlan = value;
      if (planPreparationJobId) {
        const jobId = planPreparationJobId;
        const job = await jobApi.get(jobId);
        if (request === planRequest && jobId === planPreparationJobId) {
          if (job.status === 'succeeded') {
            planNotice = 'The optimized speech plan is ready for review.';
            planPreparationJobId = '';
          } else if (
            ['failed', 'canceled', 'interrupted'].includes(job.status)
          ) {
            planNotice = `Speech preparation ${job.status}. The previous plan remains selected.`;
            if (job.error_message) error = job.error_message;
            planPreparationJobId = '';
          }
        }
      }
    } catch (caught) {
      if (request === planRequest) error = errorMessage(caught);
    }
  }

  async function planAction(
    action: 'prepare' | 'select' | 'review',
    revisionId = ''
  ) {
    if (!speechPlan || planBusy) return;
    planBusy = true;
    error = '';
    try {
      const body =
        action === 'prepare'
          ? {
              expected_revision: speechPlan.session_revision,
              expected_plan_revision_id: speechPlan.selected_revision_id,
              source_artifact_id: speechPlan.current_input?.artifact_id
            }
          : action === 'select'
            ? {
                revision_id: revisionId,
                expected_plan_revision_id: speechPlan.selected_revision_id
              }
            : {
                revision_id: speechPlan.selected_revision_id,
                content_signature: speechPlan.content_signature
              };
      const result = await sessionFlowAction(
        session.id,
        `generation-plan/${action}`,
        body
      );
      planNotice = result.job_id
        ? 'Speech preparation queued. The finished plan will appear here for review; audio generation starts separately.'
        : '';
      planPreparationJobId =
        typeof result.job_id === 'string' ? result.job_id : '';
      await loadSpeechPlan();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      planBusy = false;
    }
  }

  async function generateSelectedPlan(staleOnly = false) {
    if (planBusy) return;
    const chosen = speechPlan?.selected_revision_id;
    planBusy = true;
    error = '';
    try {
      const current = await speechPlanState(session.id, { summary: true });
      speechPlan = current;
      if (!chosen || current.selected_revision_id !== chosen)
        throw new Error(
          'The selected speech plan changed. Review its selection before generating.'
        );
      if (!current.can_generate)
        throw new Error(
          current.warning ||
            current.generation_blocked_reason ||
            'Prepare a speech plan from the selected text first.'
        );
      const serviceProblem = await generationServiceProblem();
      if (serviceProblem) throw new Error(serviceProblem);
      // Full-run starts always go through the explicit choice dialog: the
      // dialog previews the selection read-only and starts only on confirm.
      // The stale entry point preselects refresh; the run entry point
      // preselects continue so completed recordings are kept by default.
      generationStartDialog = { mode: staleOnly ? 'refresh' : 'continue' };
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      planBusy = false;
    }
  }

  async function handleGenerationStarted(
    _run: unknown,
    preview: {
      generate_count: number;
      preserve_count: number;
    } | null
  ) {
    generationStartDialog = null;
    generationNotice =
      preview !== null
        ? `Queued ${preview.generate_count} block${preview.generate_count === 1 ? '' : 's'}; keeping ${preview.preserve_count} recording${preview.preserve_count === 1 ? '' : 's'}.`
        : '';
    await load();
  }

  onMount(() =>
    invalidationBus.subscribe((change) => {
      if (
        active &&
        (invalidates(change, 'generation', session.id) ||
          invalidates(change, 'workflow', session.id))
      )
        void loadSpeechPlan();
    })
  );

  const hasAttachedCaptions = $derived(
    session.workflow_kind === 'media_edit' &&
      Boolean(
        snapshot?.stages.some(
          (stage) => stage.key === 'transcribe' && !stage.included
        ) ||
        snapshot?.sources.some((source) =>
          /\.(srt|vtt|txt)$/i.test(source.filename)
        )
      )
  );

  let outcome = $derived(initialOutcome);

  let error = $state('');

  let sourceDialog = $state(false);

  let AddSourceDialogComponent = $state<typeof AddSourceDialog | null>(null);

  let sourceMessage = $state('');

  let pendingRun = $state<{ stage: Stage; impact: StageRerunImpact } | null>(
    null
  );

  let pendingSettingsMismatch = $state<{
    stage: Stage;
    mismatches: StageSettingsMismatch['mismatches'];
  } | null>(null);

  const historyLoading = $derived(workflowStore.historyLoading);

  let optimizationReviewArtifactId = $state('');

  let TextOptimizationReviewComponent = $state<
    typeof TextOptimizationReview | null
  >(null);

  let preview = $state<PreviewableArtifact | null>(null);

  let ArtifactPreviewComponent = $state<typeof ArtifactPreview | null>(null);

  let forkCheckpoint = $state<{
    stage: 'correction' | 'translation';
    artifactId: string;
  } | null>(null);

  let SessionForkDialogComponent = $state<typeof SessionForkDialog | null>(
    null
  );

  let stageSettings = $state<Record<string, Record<string, unknown>>>({});

  let documentOptimizationEnabled = $state(false);

  let optimizationTiming = $state<'document' | 'generation'>('generation');

  let pdfSource = $state<{ id: string; filename: string } | null>(null);

  let PdfEditorComponent = $state<typeof PdfEditor | null>(null);

  let reviewArtifactId = $state('');

  let SubtitleReviewComponent = $state<typeof SubtitleReview | null>(null);

  let disposed = false;

  let workflowTour = $state(false);

  const workflowTourSteps = [
    {
      section: 'Workflow',
      title: 'Stages are independent',
      body: 'Run any ready card on its own. Its latest artifact, settings, and status stay attached to that stage.'
    },
    {
      section: 'Workflow',
      title: 'The outcome composes the pipeline',
      body: 'Customize Workflow chooses meaningful transformations and deliverables. Run Now remains available on every ready transformation.'
    },
    {
      section: 'Review',
      title: 'Preview before synthesis',
      body: 'Subtitle comparison aligns transcription, correction, and translation, including split and merged lineage. Saving creates a reviewed revision.'
    },
    {
      section: 'Export',
      title: 'Export does not require dubbing',
      body: 'Subtitle-only exports preserve source audio. When dubbing exists, choose source, mixed, or dubbing-only audio and soft or burned subtitles.'
    }
  ];

  async function load(options: { initial?: boolean } = {}) {
    try {
      const next = await workflowStore.load(!(options.initial ?? false));
      await loadSpeechPlan();
      const speechOptimization = next?.stages.find(
        (stage) => stage.key === 'optimize_tts'
      );
      if (speechOptimization) {
        optimizationTiming =
          speechOptimization.optimization_timing ?? 'generation';
        documentOptimizationEnabled = Boolean(
          speechOptimization.enabled && optimizationTiming === 'document'
        );
      }
    } catch {
      // WorkflowStore displays this load error and clears it on recovery.
    }
  }

  function defaultReviewArtifactId() {
    const preferredStages = [
      'optimize_tts',
      'translate',
      'correct',
      'transcribe'
    ];
    for (const key of preferredStages) {
      const artifact = snapshot?.stages.find(
        (stage) => stage.key === key
      )?.artifact;
      if (
        artifact &&
        ['transcription', 'correction', 'translation'].includes(
          artifact.raw_role ?? artifact.role
        )
      )
        return artifact.id;
      if (
        artifact?.kind === 'srt' &&
        (artifact.raw_role ?? artifact.role) === 'tts_optimized'
      )
        return artifact.id;
    }
    return '';
  }

  async function run(
    stage: Stage,
    confirmed = false,
    reuseStages: string[] | null = null
  ) {
    if (stage.key === 'edit_media') {
      location.href = `/sessions/${session.id}/edit`;
      return;
    }
    if (stage.key === 'preview') {
      const artifactId = defaultReviewArtifactId();
      if (artifactId) await openSubtitleReview(artifactId);
      return;
    }
    if (stage.key === 'export') {
      location.href = `/sessions/${session.id}/output`;
      return;
    }
    if (stage.key === 'generate_audio' && workspaceMode === 'review') {
      await generateSelectedPlan();
      return;
    }
    if (stage.key === 'generate_audio') {
      const serviceProblem = await generationServiceProblem();
      if (serviceProblem) {
        error = serviceProblem;
        return;
      }
    }
    if (!confirmed && stage.artifact && (stage.artifacts?.length ?? 0) > 0) {
      try {
        const impact = await sessionApi.stageImpact(session.id, stage.key);
        pendingRun = { stage, impact };
      } catch (caught) {
        error = errorMessage(caught);
      }
      return;
    }
    if (stage.key === 'generate_audio' && reuseStages === null) {
      try {
        const preflight = await sessionApi.stageSettingsMismatches(
          session.id,
          stage.key
        );
        if ((preflight?.mismatches ?? []).length) {
          pendingSettingsMismatch = { stage, mismatches: preflight.mismatches };
          return;
        }
      } catch {
        /* the settings check is advisory; continue with the run */
      }
    }
    error = '';
    try {
      const routeKey =
        stage.key === 'optimize_tts' && documentOptimizationEnabled
          ? 'optimize_document'
          : stage.key;
      const body =
        stage.key === 'generate_audio'
          ? {
              ...(stageSettings[stage.key] ?? {}),
              stage_settings: stageSettings,
              ...(reuseStages?.length ? { reuse_stages: reuseStages } : {})
            }
          : (stageSettings[stage.key] ?? {});
      await sessionApi.runStage(session.id, routeKey, body);
      await load();
    } catch (caught) {
      error = errorMessage(caught);
    }
  }

  async function sourceAdded(message: string) {
    sourceMessage = message;
    await load({ initial: false });
  }

  async function openPdfEditor(source: { id: string; filename: string }) {
    PdfEditorComponent ??= (await import('./PdfEditor.svelte')).default;
    pdfSource = source;
  }

  async function openSourceDialog() {
    AddSourceDialogComponent ??= (await import('./AddSourceDialog.svelte'))
      .default;
    sourceDialog = true;
  }

  async function openSubtitleReview(artifactId: string) {
    SubtitleReviewComponent ??= (await import('./SubtitleReview.svelte'))
      .default;
    reviewArtifactId = artifactId;
  }

  async function openOptimizationReview(artifactId: string) {
    TextOptimizationReviewComponent ??= (
      await import('./TextOptimizationReview.svelte')
    ).default;
    optimizationReviewArtifactId = artifactId;
  }

  async function openArtifactPreview(artifact: PreviewableArtifact) {
    ArtifactPreviewComponent ??= (await import('./ArtifactPreview.svelte'))
      .default;
    preview = artifact;
  }

  async function chooseStageArtifact(stage: Stage, artifactId: string) {
    if (!artifactId || artifactId === stage.selected_artifact_id) return;
    error = '';
    try {
      await sessionApi.selectStageArtifact(
        session.id,
        stage.key,
        stage.selection_revision ?? 0,
        artifactId
      );
      await load({ initial: false });
    } catch (caught) {
      error = errorMessage(caught);
    }
  }

  let inputBusy = $state(false);

  const inputsLocked = $derived(
    inputBusy ||
      Boolean(snapshot?.stages.some((stage) => stage.status === 'running'))
  );

  const sourceCardStage = $derived(
    snapshot?.stages.find((stage) =>
      ['transcribe', 'prepare_text', 'edit_media'].includes(stage.key)
    )?.key
  );

  const timedInputs = $derived(
    ['voiceover', 'subtitles', 'media_edit'].includes(session.workflow_kind)
  );

  const documentSpeechOptimization = $derived(
    Boolean(outcome.value.transformations?.llm_tts_document_optimization)
  );

  function outputHasInputPicker(key: string) {
    const consumers =
      key === 'transcribe' || key === 'edit_media'
        ? ['correct', 'translate', 'generate_audio']
        : key === 'correct'
          ? ['translate', 'generate_audio']
          : key === 'translate' || key === 'optimize_tts'
            ? ['generate_audio']
            : [];
    return (
      timedInputs &&
      Boolean(snapshot?.stages.some((stage) => consumers.includes(stage.key)))
    );
  }

  function sourceStageKey(role: string) {
    if (role === 'source')
      return session.workflow_kind === 'media_edit'
        ? 'edit_media'
        : 'transcribe';
    return (
      {
        correction: 'correct',
        translation: 'translate',
        optimized: 'optimize_tts',
        prepared: 'prepare_text'
      } as Record<string, string>
    )[role];
  }

  function inputStage(role: string) {
    return snapshot?.stages.find((stage) => stage.key === sourceStageKey(role));
  }

  const sourceChoices = $derived([
    {
      value: 'source',
      label:
        session.workflow_kind === 'media_edit'
          ? 'Edited subtitles'
          : 'Source subtitles'
    },
    { value: 'correction', label: 'Correction' },
    { value: 'translation', label: 'Translation' }
  ]);

  function inputRole(consumer: string) {
    if (consumer === 'correct') return 'source';
    if (consumer === 'translate')
      return outcome.value.inputs.translation ?? 'correction';
    if (consumer === 'speech_plan' && documentSpeechOptimization)
      return 'optimized';
    if (session.workflow_kind === 'audiobook') return 'prepared';
    return outcome.value.inputs.generation ?? 'translation';
  }

  function choicesForInput(consumer: string) {
    if (consumer === 'speech_plan' && documentSpeechOptimization)
      return [{ value: 'optimized', label: 'Optimized speech text' }];
    if (session.workflow_kind === 'audiobook')
      return [{ value: 'prepared', label: 'Prepared text' }];
    return sourceChoices.slice(
      0,
      consumer === 'correct' ? 1 : consumer === 'translate' ? 2 : 3
    );
  }

  async function changeInputRole(consumer: string, role: string) {
    const key = consumer === 'translate' ? 'translation' : 'generation';
    if (inputsLocked || outcome.value.inputs[key] === role) return;
    inputBusy = true;
    error = '';
    try {
      outcome = await sessionApi.updateOutcome(session.id, outcome.revision, {
        ...outcome.value,
        inputs: { ...outcome.value.inputs, [key]: role }
      });
      onupdated(session);
      await load({ initial: false });
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      inputBusy = false;
    }
  }

  async function chooseInputVersion(producer: Stage | undefined, id: string) {
    if (!producer || inputsLocked) return;
    inputBusy = true;
    try {
      await chooseStageArtifact(producer, id);
    } finally {
      inputBusy = false;
    }
  }

  async function previewVersion(artifact: StageArtifact) {
    const role = artifact.raw_role ?? artifact.role;
    if (role === 'tts_optimized' && artifact.kind === 'json')
      return openOptimizationReview(artifact.id);
    if (
      [
        'transcription',
        'correction',
        'translation',
        'tts_optimized',
        'media_edit_subtitles'
      ].includes(role)
    )
      return openSubtitleReview(artifact.id);
    return openArtifactPreview({
      ...artifact,
      role,
      relative_path: artifact.relative_path ?? artifact.path
    });
  }

  async function clearStageArtifact(stage: Stage) {
    if (
      !stage.selected_artifact_id ||
      !confirm(
        `Clear the selected ${stage.title.toLowerCase()} result? Dependent stage selections will also be cleared, but every artifact remains in history.`
      )
    )
      return;
    error = '';
    try {
      await sessionApi.selectStageArtifact(
        session.id,
        stage.key,
        stage.selection_revision ?? 0,
        null
      );
      await load({ initial: false });
    } catch (caught) {
      error = errorMessage(caught);
    }
  }

  async function deleteStageArtifact(stage: Stage, artifact: StageArtifact) {
    if (artifact.id === stage.selected_artifact_id) return;
    if (
      !confirm(
        `Move ${stage.title.toLowerCase()} version ${artifact.version} to trash? ` +
          'Its managed file will be retained. Results derived from it must be trashed first.'
      )
    )
      return;
    error = '';
    try {
      await sessionApi.trashStageArtifact(session.id, stage.key, artifact.id);
      await load({ initial: false });
    } catch (caught) {
      error = errorMessage(caught);
    }
  }

  async function forkStage(stage: Stage) {
    const forkStage =
      stage.key === 'correct'
        ? 'correction'
        : stage.key === 'translate'
          ? 'translation'
          : null;
    if (!forkStage || !stage.selected_artifact_id) return;
    SessionForkDialogComponent ??= (await import('./SessionForkDialog.svelte'))
      .default;
    forkCheckpoint = {
      stage: forkStage,
      artifactId: stage.selected_artifact_id
    };
  }

  async function loadMoreStageArtifacts(stage: Stage) {
    error = '';
    try {
      await workflowStore.loadStageHistory(stage.key);
    } catch (caught) {
      if (!disposed) error = errorMessage(caught);
    }
  }

  async function cancel(stage: Stage) {
    if (!stage.job_id) return;
    try {
      await sessionApi.cancelJob(stage.job_id);
      await load();
    } catch (caught) {
      error = errorMessage(caught);
    }
  }

  async function resume(stage: Stage) {
    if (!stage.agent_run_id || !stage.resumable) {
      await run(stage);
      return;
    }
    error = '';
    try {
      await sessionApi.resumeAgentRun(stage.agent_run_id);
      await load();
    } catch (caught) {
      error = errorMessage(caught);
    }
  }

  async function persistSection(
    section: string,
    value: Record<string, unknown>,
    base?: SettingsPayload
  ) {
    const stored = base ?? (await sessionApi.settings(session.id, section));
    if (section === 'stt') {
      return sessionApi.patchSettings(
        session.id,
        section,
        stored.revision,
        value
      );
    }
    return sessionApi.saveSettings(session.id, section, stored.revision, {
      ...stored.override,
      ...value
    });
  }

  async function updateOutcomeTransformations(
    changes: Record<string, boolean>,
    expectedOutcome: OutcomePlan = outcome
  ) {
    const current = expectedOutcome;
    const value = {
      ...current.value,
      transformations: {
        ...(current.value.transformations ?? {}),
        ...changes
      }
    };
    outcome = await sessionApi.updateOutcome(
      session.id,
      current.revision,
      value
    );
    onupdated(session);
  }

  async function toggleSpeechOptimization(enabled: boolean) {
    error = '';
    const documentEnabled = enabled && optimizationTiming === 'document';
    const generationEnabled = enabled && optimizationTiming === 'generation';
    try {
      await persistSection('text', {
        llm_tts_optimization: generationEnabled,
        llm_processing_enabled: generationEnabled,
        llm_tts_document_optimization: documentEnabled
      });
      await updateOutcomeTransformations({
        llm_tts_optimization: generationEnabled,
        llm_tts_document_optimization: documentEnabled
      });
      documentOptimizationEnabled = documentEnabled;
      await load();
    } catch (caught) {
      error = errorMessage(caught);
    }
  }

  async function previewArtifact(stage: Stage) {
    if (!stage.artifact) return;
    const role = stage.artifact.raw_role ?? stage.artifact.role;
    if (role === 'tts_optimized' && stage.artifact.kind === 'json') {
      await openOptimizationReview(stage.artifact.id);
      return;
    }
    if (
      ['transcription', 'correction', 'translation', 'tts_optimized'].includes(
        role
      )
    ) {
      await openSubtitleReview(stage.artifact.id);
      return;
    }
    await openArtifactPreview({
      ...stage.artifact,
      role,
      relative_path: stage.artifact.relative_path ?? stage.artifact.path
    });
  }

  async function generateAutomatically() {
    const stage = snapshot?.stages.find(
      (item) => item.key === 'generate_audio'
    );
    if (!stage) return;
    const serviceProblem = await generationServiceProblem();
    if (serviceProblem) {
      error = serviceProblem;
      return;
    }
    try {
      const preflight = await sessionApi.stageSettingsMismatches(
        session.id,
        'generate_audio'
      );
      const mismatches = preflight?.mismatches ?? [];
      if (mismatches.length) {
        const names = mismatches
          .map((item) => item.stage.replaceAll('_', ' '))
          .join(' and ');
        sourceMessage = `Generating with the selected text. Keeping the selected ${names} results; their settings or source history differ from the current setup. Use Review mode to inspect the differences or refresh the text first.`;
        await run(
          stage,
          true,
          mismatches.map((item) => item.stage)
        );
        return;
      }
    } catch {
      /* the settings check is advisory; continue with the run */
    }
    await run(stage);
  }

  onMount(async () => {
    if (manageMode)
      workspaceMode =
        localStorage.getItem(`pandrator:workspace-mode:${session.id}`) ===
        'automatic'
          ? 'automatic'
          : 'review';
    if (scope !== 'language') await load({ initial: true });
    if (initialSettingsStage) {
      const stage = workflowStore.snapshot?.stages.find(
        (item) => item.key === initialSettingsStage
      );
      if (stage) await openSettings(stage);
    }
  });

  $effect(() => {
    if (scope === 'language' && active)
      void untrack(() => load({ initial: true }));
  });

  $effect(() => {
    if (manageMode && typeof localStorage !== 'undefined')
      localStorage.setItem(
        `pandrator:workspace-mode:${session.id}`,
        workspaceMode
      );
  });

  $effect(() => {
    if (
      !active ||
      !snapshot?.stages.some((stage) => stage.status === 'running')
    )
      return;
    if (appState.eventsHealthy) return;
    const timer = window.setTimeout(() => load({ initial: false }), 5000);
    return () => window.clearTimeout(timer);
  });

  let stageSettingsDialog = $state<{
    openSettings: (stage: Stage) => Promise<void>;
    generationServiceProblem: () => Promise<string>;
  }>();

  async function openSettings(stage: Stage) {
    await stageSettingsDialog?.openSettings(stage);
  }

  async function generationServiceProblem() {
    return (
      (await stageSettingsDialog?.generationServiceProblem()) ??
      'Generation settings are loading.'
    );
  }

  onDestroy(() => {
    disposed = true;
  });
</script>

<div class="min-w-0 max-w-full overflow-x-hidden">
  {#if scope !== 'language'}
    <header class="mb-6 flex flex-wrap items-end justify-between gap-6">
      <div>
        {#if session.workflow_kind !== 'subtitles'}<div
            class="inline-flex rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] p-1"
            aria-label="Workspace mode"
          >
            <button
              onclick={() => (workspaceMode = 'review')}
              aria-pressed={workspaceMode === 'review'}
              class:mode-active={workspaceMode === 'review'}
              class="mode-choice">Review each stage</button
            ><button
              onclick={() => (workspaceMode = 'automatic')}
              aria-pressed={workspaceMode === 'automatic'}
              class:mode-active={workspaceMode === 'automatic'}
              class="mode-choice">Automatic workflow</button
            >
          </div>{/if}
      </div>
      <div class="flex flex-wrap gap-2">
        <button
          onclick={() => (workflowTour = true)}
          class="lift flex items-center gap-2 rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] px-4 py-3 text-sm font-semibold"
          ><Sparkles size={17} /> Tour</button
        >{#if snapshot?.sources.find((item) => item.filename
            .toLowerCase()
            .endsWith('.pdf'))}{@const availablePdf = snapshot.sources.find(
            (item) => item.filename.toLowerCase().endsWith('.pdf')
          )!}<button
            onclick={() => openPdfEditor(availablePdf)}
            class="lift flex items-center gap-3 rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] px-4 py-3 text-sm font-semibold"
            ><Crop size={18} /> Edit PDF</button
          >{/if}
      </div>
    </header>
  {/if}
  {#if sourceMessage}<div
      class="mb-5 rounded-xl bg-[var(--accent-soft)] px-4 py-3 text-sm"
    >
      {sourceMessage}
    </div>{/if}
  {#if scope !== 'source' && session.workflow_kind !== 'subtitles' && workspaceMode === 'automatic'}
    <section
      class="surface mb-6 flex flex-col gap-4 rounded-3xl border border-[var(--accent)]/25 p-5 sm:flex-row sm:items-center sm:p-6"
    >
      <div
        class="grid size-11 shrink-0 place-items-center rounded-2xl bg-[var(--accent-soft)] text-[var(--accent)]"
      >
        <Sparkles size={21} />
      </div>
      <div class="min-w-0 flex-1">
        <h2 class="font-semibold">Generate reviewable audio segments</h2>
        <p class="muted mt-1 text-sm leading-relaxed">
          Pandrator keeps your selected text, prepares any missing steps, and
          generates segment takes with the current speech settings. It stops
          there: reviewing takes, RVC conversion, assembly, export, and video
          synchronization remain manual.
        </p>
      </div>
      <button
        onclick={generateAutomatically}
        disabled={!snapshot?.sources.length ||
          snapshot?.stages.find((item) => item.key === 'generate_audio')
            ?.status === 'running'}
        class="flex shrink-0 items-center gap-2 rounded-xl bg-[var(--accent)] px-5 py-3 text-sm font-semibold text-white disabled:opacity-40"
        ><Play size={17} /> Generate audio segments</button
      >
    </section>
  {/if}
  {#if scope !== 'language'}
    <details
      class="mb-5 rounded-xl border border-[var(--line)] px-4 py-3 text-sm"
      data-testid="session-workflow-help"
    >
      <summary class="cursor-pointer font-semibold"
        >Workflow steps &amp; help</summary
      >
      <p class="muted mt-3">
        {#if session.workflow_kind !== 'subtitles' && workspaceMode === 'automatic'}
          Choosing this mode does not start a job. Use Generate audio segments
          to prepare missing steps and record audio. Review and export remain
          separate.
        {:else}
          Run a step, review its result, then continue. A later step becomes
          available when its input is ready.
        {/if}
      </p>
      {#if outcome?.pipeline?.length}
        <ol
          class="mt-3 flex flex-wrap items-center gap-2"
          aria-label="Workflow steps"
        >
          {#each outcome.pipeline as stage, index}
            <li class="flex items-center gap-2">
              <span
                class="rounded-lg bg-[var(--accent-soft)] px-3 py-2 text-xs font-semibold"
                >{stage.title}</span
              >
              {#if index < outcome.pipeline.length - 1}<ChevronRight
                  class="muted"
                  size={14}
                  aria-hidden="true"
                />{/if}
            </li>
          {/each}
        </ol>
      {/if}
    </details>
  {/if}

  {#if error || workflowStore.error}<div
      class="mb-5 flex items-start gap-3 rounded-xl border border-red-400/40 bg-red-500/10 px-4 py-3 text-sm"
    >
      <CircleAlert class="mt-0.5 shrink-0" size={17} /><span
        >{error || workflowStore.error}</span
      >
    </div>{/if}

  {#snippet stageInput(consumer: string)}
    {@const role = inputRole(consumer)}
    {@const producer = inputStage(role)}
    <StageInputPicker
      label={consumer === 'speech_plan'
        ? 'Prepare plan from'
        : 'Use input from'}
      choices={choicesForInput(consumer)}
      value={role}
      stage={producer}
      disabled={inputsLocked}
      loadingMore={Boolean(producer && historyLoading[producer.key])}
      onchange={(value) => changeInputRole(consumer, value)}
      onselect={(id) => chooseInputVersion(producer, id)}
      onpreview={previewVersion}
      onloadmore={() => producer && loadMoreStageArtifacts(producer)}
    />
  {/snippet}
  {#snippet sessionSource()}
    <SessionSourceCard
      compact={Boolean(sourceCardStage)}
      sessionId={session.id}
      refreshKey={JSON.stringify([
        session.revision,
        snapshot?.stages.map((stage) => [
          stage.key,
          stage.status,
          stage.selected_artifact_id
        ])
      ])}
      oninitialsource={openSourceDialog}
      onchanged={sourceAdded}
    />
  {/snippet}
  {#if workflowStore.loading}
    <div class="surface grid min-h-64 place-items-center rounded-3xl">
      <LoaderCircle class="animate-spin text-[var(--accent)]" size={28} />
    </div>
  {:else if snapshot}
    <div class="space-y-4">
      {#if scope !== 'language' && !sourceCardStage}{@render sessionSource()}{/if}
      {#if scope === 'all' && session.workflow_kind !== 'audiobook' && !session.included_stages_json.includes('translate')}
        <TranslationVersions sessionId={session.id} plannedOnly />
      {/if}
      {#snippet voiceSetup()}
        {#if session.workflow_kind === 'audiobook' || (session.workflow_kind === 'voiceover' && session.included_stages_json.includes('generate_audio'))}
          <VoiceSetupCard
            bind:castPanel={audiobookCastPanel}
            navigationManaged={workspaceMode === 'review' &&
              Boolean(speechPlan?.selected_revision_id)}
            sessionId={session.id}
            plan={speechPlan}
            busy={planBusy || inputsLocked}
            onchanged={async () => {
              const [record, nextOutcome] = await Promise.all([
                sessionApi.get(session.id),
                sessionApi.outcome(session.id)
              ]);
              outcome = nextOutcome;
              onupdated(record);
              await load();
            }}
            onsettings={() => {
              const stage = snapshot?.stages.find(
                (item) => item.key === 'prepare_text'
              );
              if (stage) void openSettings(stage);
            }}
          />
        {/if}
      {/snippet}
      {#if scope === 'all' || (scope === 'language' && !visibleStages.some((stage) => stage.key === 'translate'))}{@render voiceSetup()}{/if}
      {#each visibleStages as stage (stage.key)}
        {#if stage.key === 'generate_audio' && workspaceMode === 'review'}
          {#if planNotice}<p class="muted text-sm" role="status">
              {planNotice}
            </p>{/if}
          <SpeechPlanCard
            externalCastPanel={audiobookCastPanel}
            showCasting={false}
            sessionId={session.id}
            plan={speechPlan}
            busy={planBusy}
            onprepare={() => planAction('prepare')}
            onselect={(id) => planAction('select', id)}
            onreview={() => planAction('review')}
            onsettings={() => (planSettingsOpen = true)}
            onperformancechange={() => void load()}
          >
            {#snippet inputControls()}{@render stageInput(
                'speech_plan'
              )}{/snippet}
          </SpeechPlanCard>
        {/if}
        <WorkflowStageCard
          {stage}
          sessionId={session.id}
          outputsOnly={outputHasInputPicker(stage.key)}
          onpreviewversion={previewVersion}
          {workspaceMode}
          runDisabled={stage.key === 'generate_audio' &&
            workspaceMode === 'review' &&
            (planBusy || !speechPlan?.can_generate)}
          runLabel={stage.key === 'generate_audio' && workspaceMode === 'review'
            ? 'Generate audio…'
            : session.workflow_kind === 'media_edit' &&
                stage.key === 'transcribe'
              ? hasAttachedCaptions
                ? 'Configure & align captions'
                : 'Configure transcription'
              : ''}
          optional={session.workflow_kind === 'media_edit' &&
            stage.key === 'transcribe' &&
            !stage.included}
          historyLoading={Boolean(historyLoading[stage.key])}
          onsettings={() => openSettings(stage)}
          ontoggle={toggleSpeechOptimization}
          onrun={() =>
            session.workflow_kind === 'media_edit' && stage.key === 'transcribe'
              ? openSettings(stage)
              : run(stage)}
          onresume={() => resume(stage)}
          oncancel={() => cancel(stage)}
          onselect={(artifactId) => chooseStageArtifact(stage, artifactId)}
          onpreview={() => void previewArtifact(stage)}
          onclear={() => clearStageArtifact(stage)}
          onfork={['correct', 'translate'].includes(stage.key) &&
          stage.selected_artifact_id
            ? () => forkStage(stage)
            : undefined}
          ondelete={(artifact) => deleteStageArtifact(stage, artifact)}
          onloadmore={() => loadMoreStageArtifacts(stage)}
        >
          {#snippet languageVersions()}
            {#if scope === 'all' && stage.key === 'translate' && stage.included && session.workflow_kind !== 'audiobook'}
              <TranslationVersions sessionId={session.id} />
            {/if}
          {/snippet}
          {#snippet inputControls()}
            {#if stage.key === sourceCardStage}{@render sessionSource()}{/if}
            {#if timedInputs && (['correct', 'translate'].includes(stage.key) || (stage.key === 'optimize_tts' && documentSpeechOptimization))}
              {@render stageInput(stage.key)}
            {:else if stage.key === 'generate_audio' && workspaceMode !== 'review'}
              {@render stageInput('speech_plan')}
            {/if}
            {#if stage.key === 'generate_audio' && workspaceMode === 'review'}
              <SpeechPlanPicker
                plan={speechPlan}
                disabled={planBusy || Boolean(speechPlan?.blocked_reason)}
                onselect={(id) => planAction('select', id)}
              />
              <p class="muted mt-2 text-xs">
                A new run uses this plan with the current voice and generation
                settings.
              </p>
              {#if speechPlan?.generation_blocked_reason}<p
                  class="mt-2 text-sm text-[var(--warning)]"
                  role="status"
                >
                  {speechPlan.generation_blocked_reason}
                </p>{/if}
              <div class="mt-2 flex flex-wrap gap-2">
                <button
                  class="btn btn-sm btn-secondary border border-[var(--line)]"
                  disabled={!speechPlan?.selected_revision_id}
                  onclick={() => openSpeechPlanEditor(session.id)}
                  >Review selected plan</button
                >
                <button
                  class="btn btn-sm btn-secondary border border-[var(--line)]"
                  disabled={planBusy ||
                    !speechPlan?.can_generate ||
                    (selectedSpeechPlan?.audio_reuse_checked !== false &&
                      !selectedSpeechPlan?.stale_segment_count)}
                  onclick={() => void generateSelectedPlan(true)}
                  >Refresh changed audio…</button
                >
              </div>
              {#if generationNotice}<p
                  class="mt-2 text-sm"
                  role="status"
                  data-testid="generation-start-notice"
                >
                  {generationNotice}
                </p>{/if}
            {/if}
          {/snippet}
        </WorkflowStageCard>
        {#if scope === 'language' && stage.key === 'translate'}{@render voiceSetup()}{/if}
      {/each}
    </div>
  {/if}
</div>

{#if sourceDialog && AddSourceDialogComponent}<AddSourceDialogComponent
    sessionId={session.id}
    onclose={() => (sourceDialog = false)}
    onadded={sourceAdded}
  />{/if}

<WorkflowRunDialogs
  {pendingRun}
  pendingMismatch={pendingSettingsMismatch}
  onclose={() => {
    pendingRun = null;
    pendingSettingsMismatch = null;
  }}
  onrerun={async (stage) => {
    pendingRun = null;
    await run(stage, true);
  }}
  onreuse={async (pending) => {
    pendingSettingsMismatch = null;
    await run(
      pending.stage,
      true,
      pending.mismatches.map((item) => item.stage)
    );
  }}
  onrefresh={async (pending) => {
    pendingSettingsMismatch = null;
    await run(pending.stage, true, []);
  }}
/>

{#if generationStartDialog}
  <GenerationStartDialog
    sessionId={session.id}
    planRevisionId={speechPlan?.selected_revision_id ?? null}
    initialMode={generationStartDialog.mode}
    onclose={() => (generationStartDialog = null)}
    onstarted={(run, preview) => void handleGenerationStarted(run, preview)}
  />
{/if}

{#if pdfSource && PdfEditorComponent}<PdfEditorComponent
    sessionId={session.id}
    source={pdfSource}
    onclose={() => (pdfSource = null)}
  />{/if}
{#if reviewArtifactId && SubtitleReviewComponent}<SubtitleReviewComponent
    sessionId={session.id}
    primaryArtifactId={reviewArtifactId}
    onclose={() => (reviewArtifactId = '')}
    onsaved={load}
  />{/if}
{#if preview && ArtifactPreviewComponent}<ArtifactPreviewComponent
    artifact={preview}
    onclose={() => (preview = null)}
  />{/if}
{#if forkCheckpoint && SessionForkDialogComponent}<SessionForkDialogComponent
    {session}
    stage={forkCheckpoint.stage}
    artifactId={forkCheckpoint.artifactId}
    onclose={() => (forkCheckpoint = null)}
  />{/if}
{#if optimizationReviewArtifactId && TextOptimizationReviewComponent}<TextOptimizationReviewComponent
    artifactId={optimizationReviewArtifactId}
    onclose={() => (optimizationReviewArtifactId = '')}
    onsaved={load}
  />{/if}
<GuidedTour
  tourId="workflow"
  steps={workflowTourSteps}
  bind:open={workflowTour}
/>

{#if planSettingsOpen}<SpeechPlanSettings
    sessionId={session.id}
    plan={speechPlan}
    onclose={() => (planSettingsOpen = false)}
    onsaved={loadSpeechPlan}
  />{/if}

<StageSettingsDialog
  bind:this={stageSettingsDialog}
  {session}
  {outcome}
  {workflowStore}
  bind:stageSettings
  bind:error
  onrefresh={load}
  onrun={run}
  ontransform={updateOutcomeTransformations}
/>

<style>
  .mode-choice {
    border-radius: 0.65rem;
    padding: 0.55rem 0.85rem;
    font-size: 0.75rem;
    font-weight: 700;
    color: var(--muted);
  }
  .mode-choice.mode-active {
    background: var(--action-bg);
    color: white;
    box-shadow: 0 4px 14px color-mix(in srgb, var(--accent) 24%, transparent);
  }
</style>
