<script lang="ts">
  import { errorMessage } from './errors';
  import {
    ArrowLeft,
    ChevronRight,
    CheckCircle2,
    CircleAlert,
    CloudUpload,
    Crop,
    LoaderCircle,
    Library,
    Link2,
    Play,
    Plus,
    RefreshCw,
    RotateCcw,
    Save,
    Sparkles,
    X
  } from '@lucide/svelte';
  import { jobApi, sessionApi } from './domain-api';
  import { speechRecognitionApi, voiceApi } from './admin-api';
  import type {
    OutcomePlan,
    RuntimeCapabilities,
    SessionRecord,
    SettingsPayload,
    StageRerunImpact,
    StageSettingsMismatch,
    SttCatalogue,
    SubtitleReviewCatalogItem,
    TtsCatalogue,
    TtsService,
    VoiceRecord,
    XttsModel,
    WorkflowStage
  } from './api-models';
  import { appState } from './app-state.svelte';
  import SubtitleReview from './SubtitleReview.svelte';
  import GuidedTour from './GuidedTour.svelte';
  import ArtifactPreview from './ArtifactPreview.svelte';
  import TextOptimizationReview from './TextOptimizationReview.svelte';
  import AddSourceDialog from './AddSourceDialog.svelte';
  import WorkflowStageCard from './WorkflowStageCard.svelte';
  import WorkflowRunDialogs from './WorkflowRunDialogs.svelte';
  import SessionForkDialog from './SessionForkDialog.svelte';
  import type { PreviewableArtifact } from './artifact-display';
  import { LANGUAGE_OPTIONS } from './settings-fields';
  import { describeVoice, languagesForService } from './voice-catalog';
  import { onMount } from 'svelte';
  import { type WorkflowStore } from './workflow-store.svelte';
  import type PdfEditor from './PdfEditor.svelte';
  import type SettingsModal from './SettingsModal.svelte';
  import type TtsServicesModal from './TtsServicesModal.svelte';
  import type VoiceLibraryModal from './VoiceLibraryModal.svelte';
  import { modalFocus } from './modal-focus';

  type Stage = WorkflowStage;

  let {
    session,
    outcome: initialOutcome,
    workflowStore,
    onback,
    onupdated
  }: {
    session: SessionRecord;
    outcome: OutcomePlan;
    workflowStore: WorkflowStore;
    onback: () => void;
    onupdated: (session: SessionRecord) => void;
  } = $props();
  const snapshot = $derived(workflowStore.snapshot);
  let outcome = $derived(initialOutcome);
  let capabilities = $state<RuntimeCapabilities>({});
  let ttsCatalogue = $state<TtsCatalogue>({ services: [] });
  let sttCatalogue = $state<SttCatalogue>({
    services: [],
    profiles: [],
    value: {},
    revision: 0
  });
  let libraryVoices = $state<VoiceRecord[]>([]);
  let llmModels = $state<
    {
      value: string;
      label: string;
      isDefault: boolean;
      defaultReasoningEffort: string;
    }[]
  >([]);
  let error = $state('');
  let sourceDialog = $state(false);
  let sourceMessage = $state('');
  let pendingRun = $state<{ stage: Stage; impact: StageRerunImpact } | null>(
    null
  );
  let pendingSettingsMismatch = $state<{
    stage: Stage;
    mismatches: StageSettingsMismatch['mismatches'];
  } | null>(null);
  let historyLoading = $state<Record<string, boolean>>({});
  let settingsStage = $state<Stage | null>(null);
  let stageMessage = $state('');
  let fullSettingsSection = $state('');
  let fullSettingsDraft = $state<Record<string, unknown> | null>(null);
  let SettingsModalComponent = $state<typeof SettingsModal | null>(null);
  let ttsServicesOpen = $state(false);
  let TtsServicesModalComponent = $state<typeof TtsServicesModal | null>(null);
  let voiceLibraryOpen = $state(false);
  let VoiceLibraryModalComponent = $state<typeof VoiceLibraryModal | null>(
    null
  );
  let voiceLibraryView = $state<'references' | 'prebuilt'>('references');
  let voiceLibraryService = $state('');
  let voiceLibraryInitialVoice = $state('');
  let publishingLibraryVoiceId = $state('');
  let voicePublishStatus = $state('');
  let optimizationReviewArtifactId = $state('');
  let workspaceMode = $state<'review' | 'automatic'>('review');
  let preview = $state<PreviewableArtifact | null>(null);
  let forkCheckpoint = $state<{
    stage: 'correction' | 'translation';
    artifactId: string;
  } | null>(null);
  const sectionDisplay = (section: string) =>
    (({ stt: 'STT', tts: 'TTS', rvc: 'RVC' }) as Record<string, string>)[
      section
    ] ?? section.replaceAll('_', ' ');
  let stageSettings = $state<Record<string, Record<string, unknown>>>({});
  let targetLanguage = $state('en');
  let originalLanguage = $state('auto');
  let model = $state('default');
  let reasoningEffort = $state('');
  let backend = $state('llm');
  let sttEngine = $state('whisper');
  let sttQuantization = $state('f16');
  let sttComputeBackend = $state('auto');
  let sttDevice = $state(0);
  let sttThreads = $state(0);
  let sttChunkSeconds = $state(0);
  let sttChunkOverlap = $state(3);
  let sttHotwords = $state('');
  let sttTranscribeStyle = $state('readability');
  let sttLidBackend = $state('whisper');
  let sttBeamSize = $state(1);
  let parakeetDecoder = $state('tdt');
  let mossMaxChunkSeconds = $state(120);
  let mossChunkOverlap = $state(0);
  let mossVadEnabled = $state(false);
  let mossCtcAlignmentEnabled = $state(true);
  let mossCtcPaddingSeconds = $state(0.5);
  let vadEnabled = $state(true);
  let vadModel = $state('silero');
  let vadThreshold = $state(0.5);
  let vadMinSpeech = $state(250);
  let vadMinSilence = $state(800);
  let vadMaxSpeech = $state(300);
  let vadSpeechPad = $state(30);
  let subtitleChars = $state(48);
  let subtitleLines = $state(2);
  let subtitleMinDuration = $state(833);
  let subtitleMaxDuration = $state(7000);
  let subtitleCps = $state(20);
  let subtitleMinGap = $state(80);
  let subtitlePhraseGap = $state(600);
  let subtitleHardGap = $state(1500);
  let subtitleSentenceBoundaryThreshold = $state(0.25);
  let instructions = $state('');
  let optimizationPrompt = $state('');
  let optimizationConcurrent = $state(1);
  let timingContextMode = $state<'full' | 'overlap_only' | 'none'>('full');
  let timingContextGap = $state(2000);
  let correctionBatchCharLimit = $state(6000);
  let correctionBatchSegmentLimit = $state(40);
  let contextBefore = $state(8);
  let contextAfter = $state(2);
  let preventSubtitleRemoval = $state(false);
  let optimizationBatchSize = $state(3);
  let documentOptimizationBatchSize = $state(8);
  let speechOptimizationMode = $state<'guarded' | 'flexible'>('guarded');
  let optimizationMultiStage = $state(false);
  let optimizationFirstPrompt = $state('');
  let optimizationSecondPrompt = $state('');
  let optimizationThirdPrompt = $state('');
  let optimizationEnabled = $state(false);
  let documentOptimizationEnabled = $state(false);
  let optimizationTiming = $state<'document' | 'generation'>('generation');
  let agentic = $state(false);
  let maxIterations = $state(53);
  let splitSentences = $state(true);
  let appendSentences = $state(true);
  let maxSentenceLength = $state(200);
  let nemoNormalization = $state(true);
  let normalizeAllCaps = $state(true);
  let removeDiacritics = $state(false);
  let removeQuotationMarks = $state(false);
  let ttsService = $state('XTTS');
  let ttsModel = $state('');
  let voiceName = $state('');
  let generationPrompt = $state('');
  let ttsBatchSize = $state(10);
  let speechBlockMinChars = $state(10);
  let speechBlockMaxChars = $state(220);
  let speechBlockMergeThreshold = $state(250);
  let speechBlockContinuationThreshold = $state(3000);
  let speechBlockMaxInternalGap = $state(1800);
  let subtitleMode = $state('soft');
  let subtitleSelection = $state('dual');
  let audioMode = $state('mixed');
  let exportMode = $state('media');
  let subtitleFormat = $state('srt');
  let pdfSource = $state<{ id: string; filename: string } | null>(null);
  let PdfEditorComponent = $state<typeof PdfEditor | null>(null);
  let reviewArtifactId = $state('');
  let subtitleCatalogItems = $state<SubtitleReviewCatalogItem[]>([]);
  let translationSourceArtifactId = $state('');
  let webResearchEnabled = $state(false);
  let webResearchModel = $state('');
  let webResearchMode = $state<'global' | 'per_chunk'>('global');
  let webResearchContextFraction = $state(0.8);
  let refreshingTtsServices = $state(false);
  let xttsModelId = $state('');
  let xttsModelFiles = $state<File[]>([]);
  let uploadingXttsModel = $state(false);
  let xttsModelUploadProgress = $state(0);
  let xttsModelUploadPhase = $state<'idle' | 'transferring' | 'installing'>(
    'idle'
  );
  let xttsModelUploadError = $state('');
  let xttsModelUploadMessage = $state('');
  let xttsModels = $state<XttsModel[]>([]);
  let xttsModelsLoading = $state(false);
  let xttsModelsLifecycleSupported = $state(false);
  let xttsModelsCompatibility = $state('');
  let deletingXttsModelId = $state('');
  let speechCataloguesLoaded = false;
  let llmModelsLoaded = false;
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
      const speechOptimization = next?.stages.find(
        (stage) => stage.key === 'optimize_tts'
      );
      if (speechOptimization) {
        optimizationTiming =
          speechOptimization.optimization_timing ?? 'generation';
        documentOptimizationEnabled = Boolean(
          speechOptimization.enabled && optimizationTiming === 'document'
        );
        optimizationEnabled = Boolean(
          speechOptimization.enabled && optimizationTiming === 'generation'
        );
      }
    } catch (caught) {
      error = errorMessage(caught);
    }
  }

  async function loadCapabilities() {
    if (Object.keys(appState.capabilities).length) {
      capabilities = appState.capabilities;
      return;
    }
    try {
      await appState.refreshCapabilities();
      capabilities = appState.capabilities;
    } catch {
      capabilities = {};
    }
  }

  async function loadSttCatalogue() {
    try {
      sttCatalogue = await speechRecognitionApi.catalogue();
    } catch {
      sttCatalogue = { services: [], profiles: [], value: {}, revision: 0 };
    }
  }

  async function loadSubtitleCatalog() {
    try {
      const response = await sessionApi.subtitleCatalog(session.id);
      subtitleCatalogItems = response.items.filter((item) =>
        ['transcription', 'correction'].includes(item.stage)
      );
    } catch (caught) {
      error = errorMessage(caught);
      subtitleCatalogItems = [];
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

  function subtitleSourceLabel(item: SubtitleReviewCatalogItem) {
    const stage =
      item.stage === 'correction' ? 'Corrected subtitles' : 'Transcription';
    const language = item.language ? ` · ${item.language}` : '';
    const state = item.state === 'current' ? '' : ' · earlier result';
    return `${stage} v${item.version}${language}${state}`;
  }

  const supportsSttCompute = (name: string) =>
    name === 'auto' ||
    (capabilities?.stt?.compute_backends ?? []).includes(name);
  const normalizeSttEngine = (value: unknown) => {
    const normalized = String(value ?? '').toLowerCase();
    if (normalized.includes('azure') && normalized.includes('mai'))
      return 'azure_mai_transcribe_1_5';
    if (normalized.includes('moss')) return 'moss';
    return normalized.includes('parakeet') ? 'parakeet' : 'whisper';
  };
  const isCloudStt = (engine: string) =>
    sttCatalogue.services.some(
      (service) =>
        service.id.replaceAll('-', '_') === engine.replaceAll('-', '_')
    );
  const sttOptionLabel = (engineId: string, label: string, timing: string) => {
    const info = capabilities?.stt?.models?.[engineId] ?? {};
    const readiness = info.default
      ? 'default'
      : info.installed
        ? 'ready'
        : 'downloads on first use';
    return `${label} · ${timing} · ${readiness}`;
  };

  async function generationServiceProblem() {
    let configuredServiceId = String(
      stageSettings.generate_audio?.tts_service ??
        stageSettings.generate_audio?.service ??
        ''
    ).trim();
    if (!configuredServiceId) {
      try {
        const stored = await sessionApi.settings(session.id, 'tts');
        configuredServiceId = String(
          stored.effective?.tts_service ?? stored.effective?.service ?? ''
        ).trim();
      } catch {
        /* the catalogue check below still catches a missing selection */
      }
    }
    await loadSpeechCatalogues();
    configuredServiceId ||= ttsService;
    const configured = ttsCatalogue.services.find((service) =>
      [service.id, service.name].some(
        (value) =>
          String(value ?? '').toLowerCase() ===
          configuredServiceId.toLowerCase()
      )
    );
    if (configured?.available === true) return '';
    return (
      configured?.availability_reason ||
      (configured
        ? `${configured.name} is unavailable. Refresh service availability or choose an available provider in Generation settings.`
        : 'Choose an available TTS service in Generation settings before starting audio generation.')
    );
  }

  async function run(
    stage: Stage,
    confirmed = false,
    reuseStages: string[] = []
  ) {
    if (stage.key === 'preview') {
      reviewArtifactId = defaultReviewArtifactId();
      return;
    }
    if (stage.key === 'export') {
      location.href = `/sessions/${session.id}/output`;
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
    if (!confirmed && stage.key === 'generate_audio') {
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
              ...(reuseStages.length ? { reuse_stages: reuseStages } : {})
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

  function forkStage(stage: Stage) {
    const forkStage =
      stage.key === 'correct'
        ? 'correction'
        : stage.key === 'translate'
          ? 'translation'
          : null;
    if (!forkStage || !stage.selected_artifact_id) return;
    forkCheckpoint = {
      stage: forkStage,
      artifactId: stage.selected_artifact_id
    };
  }

  async function loadMoreStageArtifacts(stage: Stage) {
    const beforeVersion = stage.artifact_history_next_before_version;
    if (!beforeVersion || historyLoading[stage.key]) return;
    historyLoading[stage.key] = true;
    error = '';
    try {
      const history = await sessionApi.stageArtifacts(
        session.id,
        stage.key,
        beforeVersion
      );
      const merged = new Map(
        [...(stage.artifacts ?? []), ...history.items].map((artifact) => [
          artifact.id,
          artifact
        ])
      );
      stage.artifacts = [...merged.values()].sort(
        (left, right) => right.version - left.version
      );
      stage.artifact_history_total =
        history.total || stage.artifact_history_total;
      stage.artifact_history_has_more = history.has_more;
      stage.artifact_history_next_before_version = history.next_before_version;
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      historyLoading[stage.key] = false;
    }
  }

  const stageSection = (key: string) =>
    ({
      transcribe: 'stt',
      correct: 'correction',
      translate: 'translation',
      optimize_document: 'text',
      optimize_tts: 'text',
      clean_source: 'source_cleaning',
      prepare_text: 'text',
      generate_audio: 'tts',
      export: 'output'
    })[key] ?? 'text';

  async function openSettings(stage: Stage) {
    if (stage.key === 'export' && session.workflow_kind === 'audiobook') {
      await openFullSettings('output');
      return;
    }
    const dependencies: Promise<void>[] = [];
    if (stage.key === 'transcribe')
      dependencies.push(loadCapabilities(), loadSttCatalogue());
    if (
      [
        'correct',
        'translate',
        'optimize_tts',
        'optimize_document',
        'clean_source'
      ].includes(stage.key)
    ) {
      dependencies.push(loadLlmModels());
    }
    if (stage.key === 'translate') dependencies.push(loadSubtitleCatalog());
    if (stage.key === 'generate_audio')
      dependencies.push(loadSpeechCatalogues());
    await Promise.all(dependencies);
    settingsStage = stage;
    stageMessage = '';
    let saved = stageSettings[stage.key] ?? {};
    let storedSettings: SettingsPayload | null = null;
    try {
      const stored = await sessionApi.settings(
        session.id,
        stageSection(stage.key)
      );
      storedSettings = stored;
      saved = { ...stored.effective, ...saved };
      stageSettings[stage.key] = saved;
    } catch {
      /* use stage-local values */
    }
    targetLanguage = String(
      (stage.key === 'generate_audio'
        ? saved.language
        : saved.target_language) ??
        session.target_language ??
        (session.source_language === 'auto' ? 'en' : session.source_language) ??
        'en'
    );
    originalLanguage = String(
      saved.original_language ?? session.source_language ?? 'auto'
    );
    model =
      String(
        saved.model_name ??
          saved.tts_optimization_model ??
          saved[`${stage.key}_model`] ??
          ''
      ).trim() || 'default';
    reasoningEffort = String(saved.reasoning_effort ?? '');
    webResearchEnabled = Boolean(saved.web_research_enabled ?? false);
    webResearchModel = String(saved.web_research_model_name ?? '');
    webResearchMode =
      String(saved.web_research_mode ?? 'global') === 'per_chunk'
        ? 'per_chunk'
        : 'global';
    webResearchContextFraction = Number(
      saved.web_research_context_fraction ?? 0.8
    );
    backend = String(saved.backend ?? saved.translation_backend ?? 'llm');
    if (stage.key === 'translate') {
      const selectedCorrection = snapshot?.stages.find(
        (item) => item.key === 'correct'
      )?.selected_artifact_id;
      const selectedTranscription = snapshot?.stages.find(
        (item) => item.key === 'transcribe'
      )?.selected_artifact_id;
      const requestedSource = String(saved.source_artifact_id ?? '');
      translationSourceArtifactId =
        [requestedSource, selectedCorrection, selectedTranscription].find(
          (artifactId) =>
            artifactId &&
            subtitleCatalogItems.some((item) => item.artifact_id === artifactId)
        ) ?? '';
    }
    const hasSavedSttModel = Boolean(
      storedSettings?.override?.stt_engine ||
      storedSettings?.global?.stt_engine ||
      stageSettings[stage.key]?.stt_engine
    );
    const preferredSttEngine = String(
      capabilities?.stt?.default_engine ?? 'whisper'
    );
    sttEngine = normalizeSttEngine(
      hasSavedSttModel
        ? (saved.stt_engine ?? saved.stt_backend)
        : preferredSttEngine
    );
    sttQuantization = String(
      hasSavedSttModel
        ? (saved.stt_model_quantization ??
            capabilities?.stt?.models?.[sttEngine]?.precision ??
            'f16')
        : (capabilities?.stt?.default_model_quantization ?? 'f16')
    );
    sttComputeBackend = String(saved.stt_compute_backend ?? 'auto');
    sttDevice = Number(saved.stt_compute_device ?? 0);
    sttThreads = Number(saved.stt_threads ?? 0);
    sttChunkSeconds = Number(saved.stt_chunk_seconds ?? 0);
    sttChunkOverlap = Number(saved.stt_chunk_overlap_seconds ?? 3);
    sttHotwords = String(saved.stt_hotwords ?? '');
    sttTranscribeStyle = String(saved.stt_transcribe_style ?? 'readability');
    sttLidBackend = String(saved.stt_lid_backend ?? 'whisper');
    sttBeamSize = Number(saved.stt_beam_size ?? 1);
    parakeetDecoder = String(saved.parakeet_decoder ?? 'tdt');
    mossMaxChunkSeconds = Number(saved.moss_max_chunk_seconds ?? 120);
    mossChunkOverlap = Number(saved.moss_chunk_overlap_seconds ?? 0);
    mossVadEnabled = Boolean(saved.moss_vad_enabled ?? false);
    mossCtcAlignmentEnabled = Boolean(saved.moss_ctc_alignment_enabled ?? true);
    mossCtcPaddingSeconds = Number(saved.moss_ctc_padding_seconds ?? 0.5);
    vadEnabled = Boolean(saved.crispasr_vad_enabled ?? true);
    vadModel = String(saved.crispasr_vad_model ?? 'silero');
    vadThreshold = Number(saved.crispasr_vad_threshold ?? 0.5);
    vadMinSpeech = Number(saved.crispasr_vad_min_speech_ms ?? 250);
    vadMinSilence = Number(saved.crispasr_vad_min_silence_ms ?? 800);
    vadMaxSpeech = Number(saved.crispasr_vad_max_speech_seconds ?? 300);
    vadSpeechPad = Number(saved.crispasr_vad_speech_pad_ms ?? 30);
    subtitleChars = Number(saved.subtitle_max_chars_per_line ?? 48);
    subtitleLines = Number(saved.subtitle_max_lines ?? 2);
    subtitleMinDuration = Number(saved.subtitle_min_duration_ms ?? 833);
    subtitleMaxDuration = Number(saved.subtitle_max_duration_ms ?? 7000);
    subtitleCps = Number(saved.subtitle_max_cps ?? 20);
    subtitleMinGap = Number(saved.subtitle_min_gap_ms ?? 80);
    subtitlePhraseGap = Number(saved.subtitle_phrase_gap_ms ?? 600);
    subtitleHardGap = Number(saved.subtitle_hard_gap_ms ?? 1500);
    subtitleSentenceBoundaryThreshold = Number(
      saved.subtitle_sentence_boundary_threshold ?? 0.25
    );
    instructions = String(saved.instructions ?? '');
    optimizationPrompt = String(saved.combined_prompt ?? '');
    optimizationConcurrent = Number(saved.llm_concurrent_calls ?? 1);
    const savedTimingContextMode = String(
      saved.timing_context_mode ??
        (saved.timing_context_enabled === false ? 'none' : 'full')
    );
    timingContextMode = ['full', 'overlap_only', 'none'].includes(
      savedTimingContextMode
    )
      ? (savedTimingContextMode as 'full' | 'overlap_only' | 'none')
      : 'full';
    timingContextGap = Number(
      saved.substantial_gap_ms ?? saved.timing_context_gap_ms ?? 2000
    );
    correctionBatchCharLimit = Number(
      saved.char_limit ?? saved.llm_char ?? 6000
    );
    correctionBatchSegmentLimit = Number(
      saved.max_segments_per_batch ?? saved.max_subtitles_per_call ?? 40
    );
    contextBefore = Number(saved.context_before ?? 8);
    contextAfter = Number(saved.context_after ?? 2);
    preventSubtitleRemoval = Boolean(saved.no_remove_subtitles ?? false);
    optimizationBatchSize = Number(saved.llm_tts_batch_size ?? 3);
    documentOptimizationBatchSize = Number(
      saved.llm_tts_document_batch_size ?? 8
    );
    speechOptimizationMode =
      String(saved.speech_optimization_mode ?? 'guarded') === 'flexible'
        ? 'flexible'
        : 'guarded';
    optimizationMultiStage = Boolean(saved.llm_multi_stage ?? false);
    optimizationFirstPrompt = String(saved.first_prompt ?? '');
    optimizationSecondPrompt = String(saved.second_prompt ?? '');
    optimizationThirdPrompt = String(saved.third_prompt ?? '');
    optimizationEnabled = Boolean(
      saved.llm_tts_optimization ??
      (stage.key === 'optimize_tts' ? stage.enabled : false) ??
      false
    );
    documentOptimizationEnabled = Boolean(
      saved.llm_tts_document_optimization ??
      (stage.key === 'optimize_document' ? stage.enabled : false) ??
      false
    );
    optimizationTiming = documentOptimizationEnabled
      ? 'document'
      : 'generation';
    agentic = Boolean(saved.agentic ?? false);
    maxIterations = Number(saved.max_iterations ?? 53);
    splitSentences = Boolean(saved.enable_sentence_splitting ?? true);
    appendSentences = Boolean(saved.enable_sentence_appending ?? true);
    maxSentenceLength = Number(saved.max_sentence_length ?? 200);
    nemoNormalization = Boolean(saved.enable_nemo_normalization ?? true);
    normalizeAllCaps = Boolean(saved.normalize_all_caps ?? true);
    removeDiacritics = Boolean(saved.remove_diacritics ?? false);
    removeQuotationMarks = Boolean(saved.remove_quotation_marks ?? false);
    const explicitlySelectedServiceId = String(
      storedSettings?.override?.tts_service ??
        storedSettings?.override?.service ??
        ''
    ).trim();
    const configuredServiceId = String(
      saved.tts_service ??
        saved.service ??
        ttsCatalogue.default_service ??
        'XTTS'
    );
    const configuredService = ttsCatalogue.services.find((item) =>
      [item.id, item.name].some(
        (value) =>
          String(value ?? '').toLowerCase() ===
          configuredServiceId.toLowerCase()
      )
    );
    const activeService =
      (explicitlySelectedServiceId ? configuredService : null) ??
      (configuredService?.available
        ? configuredService
        : ttsCatalogue.services.find((item) => item.available)) ??
      configuredService;
    ttsService = String(activeService?.id ?? configuredServiceId);
    ttsModel =
      activeService?.id === configuredService?.id
        ? String(
            saved.model ??
              saved.xtts_model ??
              activeService?.default_model ??
              ''
          )
        : String(
            activeService?.default_model ?? activeService?.models?.[0] ?? ''
          );
    voiceName =
      activeService?.id === configuredService?.id
        ? String(saved.voice ?? saved.voice_name ?? '')
        : String(activeService?.default_voice ?? '');
    generationPrompt = String(saved.generation_prompt ?? '');
    ttsBatchSize = Number(saved.tts_batch_size ?? 10);
    speechBlockMinChars = Number(saved.speech_block_min_chars ?? 10);
    speechBlockMaxChars = Number(saved.speech_block_max_chars ?? 220);
    speechBlockMergeThreshold = Number(
      saved.speech_block_merge_threshold ?? 250
    );
    speechBlockContinuationThreshold = Number(
      saved.speech_block_continuation_threshold_ms ?? 3000
    );
    speechBlockMaxInternalGap = Number(
      saved.speech_block_max_internal_gap_ms ?? 1800
    );
    subtitleMode = String(saved.subtitle_mode ?? 'soft');
    subtitleSelection = String(saved.subtitle_selection ?? 'dual');
    audioMode = String(
      saved.audio_mode ??
        (session.workflow_kind === 'voiceover' ? 'mixed' : 'preserve')
    );
    exportMode = String(
      saved.export_mode ??
        (session.workflow_kind === 'subtitles' ? 'subtitles' : 'media')
    );
    if (
      session.workflow_kind === 'subtitles' &&
      !['subtitles', 'text'].includes(exportMode)
    )
      exportMode = 'subtitles';
    subtitleFormat = String(saved.subtitle_format ?? 'srt');
    if (stage.key === 'generate_audio' && activeService)
      await discoverTtsService(activeService);
    if (stage.key === 'generate_audio' && selectedTtsService) {
      ttsService = String(selectedTtsService.id ?? selectedTtsService.name);
      ttsModel =
        ttsModel ||
        String(selectedTtsService.default_model ?? ttsModels[0] ?? '');
      voiceName = voiceName || String(selectedTtsDefaultVoice ?? '');
      if (String(selectedTtsService.id).toLowerCase() === 'kobold_qwen') {
        const catalogue = Array.from(
          selectedTtsService.voice_catalogues?.[ttsModel] ?? []
        ).map((voice) => String(voice));
        const published = libraryVoices.flatMap((voice) => {
          const registration = voice?.metadata_json?.providers?.kobold_qwen;
          return registration?.status === 'ready' && registration?.voice_id
            ? [String(registration.voice_id)]
            : [];
        });
        const allowed =
          ttsModel.toLowerCase() === 'voice cloning'
            ? [...catalogue, ...published]
            : catalogue;
        if (
          !allowed.some(
            (voice: string) => voice.toLowerCase() === voiceName.toLowerCase()
          )
        ) {
          voiceName = String(
            selectedTtsService.default_voices?.[ttsModel] ?? allowed[0] ?? ''
          );
        }
      }
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
    value: Record<string, unknown>
  ) {
    const stored = await sessionApi.settings(session.id, section);
    return sessionApi.saveSettings(session.id, section, stored.revision, {
      ...stored.override,
      ...value
    });
  }

  async function persistDefaultSection(
    section: string,
    value: Record<string, unknown>
  ) {
    const defaults = await sessionApi.defaults(section);
    await sessionApi.saveDefaults(section, defaults.revision, {
      ...defaults.value,
      ...value
    });
    const stored = await sessionApi.settings(session.id, section);
    const cleaned = { ...(stored.override ?? {}) };
    for (const key of Object.keys(value)) delete cleaned[key];
    await sessionApi.saveSettings(
      session.id,
      section,
      stored.revision,
      cleaned
    );
  }

  async function clearSectionOverrides(section: string, keys: string[]) {
    const stored = await sessionApi.settings(session.id, section);
    const cleaned = { ...(stored.override ?? {}) };
    for (const key of keys) delete cleaned[key];
    await sessionApi.saveSettings(
      session.id,
      section,
      stored.revision,
      cleaned
    );
  }

  async function loadSpeechCatalogues(
    preserveSelection = false,
    force = false
  ) {
    if (speechCataloguesLoaded && !force) return;
    const previousService = ttsService;
    const previousModel = ttsModel;
    const previousVoice = voiceName;
    try {
      const [services, voices] = await Promise.all([
        sessionApi.ttsCatalogue(true),
        sessionApi.voices()
      ]);
      ttsCatalogue = services;
      libraryVoices = voices.items ?? [];
      const catalogue = services.services ?? [];
      const configured = catalogue.find((item) =>
        [item.id, item.name].some(
          (value) =>
            String(value ?? '').toLowerCase() ===
            String(services.default_service ?? '').toLowerCase()
        )
      );
      const preserved = catalogue.find((item) =>
        [item.id, item.name].some(
          (value) =>
            String(value ?? '').toLowerCase() === previousService.toLowerCase()
        )
      );
      const active =
        (preserveSelection ? preserved : null) ??
        (configured?.available
          ? configured
          : catalogue.find((item) => item.available)) ??
        configured ??
        catalogue[0];
      if (active) {
        ttsService = String(active.id ?? active.name);
        ttsModel =
          preserveSelection && preserved
            ? previousModel
            : ttsModel ||
              String(active.default_model ?? active.models?.[0] ?? '');
        voiceName =
          preserveSelection && preserved
            ? previousVoice
            : voiceName ||
              String(
                active.default_voices_by_language?.[ttsModel]?.[
                  targetLanguage
                ] ??
                  active.default_voices?.[ttsModel] ??
                  active.default_voice ??
                  ''
              );
        await discoverTtsService(active);
      }
      speechCataloguesLoaded = true;
      if (String(active?.id ?? '').toLowerCase() === 'xtts')
        await loadXttsModels();
    } catch {
      ttsCatalogue = { services: [] };
      libraryVoices = [];
    }
  }

  async function refreshSpeechServices() {
    refreshingTtsServices = true;
    error = '';
    try {
      await loadSpeechCatalogues(true, true);
    } finally {
      refreshingTtsServices = false;
    }
  }

  const XTTS_MODEL_BUNDLE_FILENAMES = [
    'config.json',
    'model.pth',
    'speakers_xtts.pth',
    'vocab.json'
  ] as const;
  function chooseXttsModelFiles(files: FileList | null) {
    xttsModelFiles = Array.from(files ?? []);
    xttsModelUploadError = '';
    xttsModelUploadMessage = '';
  }

  function xttsModelBundleError() {
    const modelId = xttsModelId.trim();
    if (
      !modelId ||
      modelId.length > 512 ||
      modelId.startsWith('/') ||
      modelId.includes('\\') ||
      modelId
        .split('/')
        .some(
          (part) =>
            !part || part === '.' || part === '..' || part.startsWith('.')
        )
    )
      return 'Use a relative model ID such as custom/my-narrator-v1. Slashes may organize models, but empty, hidden, or traversal path parts are not allowed.';
    const names = xttsModelFiles.map((file) => file.name);
    const expected = new Set(XTTS_MODEL_BUNDLE_FILENAMES);
    if (
      names.length !== XTTS_MODEL_BUNDLE_FILENAMES.length ||
      names.some(
        (name) =>
          !expected.has(name as (typeof XTTS_MODEL_BUNDLE_FILENAMES)[number])
      ) ||
      new Set(names).size !== names.length
    )
      return 'Choose exactly config.json, model.pth, speakers_xtts.pth, and vocab.json from one flat XTTS model bundle. Training folders and incomplete checkpoints cannot be uploaded.';
    if (xttsModelFiles.some((file) => file.size < 1))
      return 'Every XTTS bundle file must contain data.';
    return '';
  }

  async function uploadXttsModel() {
    if (uploadingXttsModel) return;
    error = '';
    const validationError = xttsModelBundleError();
    if (validationError) {
      xttsModelUploadError = validationError;
      xttsModelUploadMessage = '';
      return;
    }
    uploadingXttsModel = true;
    xttsModelUploadProgress = 0;
    xttsModelUploadPhase = 'transferring';
    xttsModelUploadError = '';
    xttsModelUploadMessage = 'Uploading the XTTS model…';
    try {
      const uploaded = await sessionApi.uploadXttsModel(
        xttsModelId.trim(),
        xttsModelFiles,
        (fraction) => {
          xttsModelUploadProgress = Math.max(0, Math.min(1, fraction));
        },
        () => {
          xttsModelUploadProgress = 1;
          xttsModelUploadPhase = 'installing';
          xttsModelUploadMessage =
            'Upload transferred; Pandrator is installing the model…';
        }
      );
      await loadSpeechCatalogues(true, true);
      await loadXttsModels();
      const xttsService = ttsCatalogue.services.find(
        (service) => String(service.id).toLowerCase() === 'xtts'
      );
      ttsService = String(xttsService?.id ?? 'xtts');
      ttsModel = uploaded.id;
      voiceName = String(
        xttsService?.default_voices?.[uploaded.id] ??
          xttsService?.default_voice ??
          ''
      );
      xttsModelId = '';
      xttsModelFiles = [];
      xttsModelUploadMessage = `Installed ${uploaded.id} (${(uploaded.bytes / (1024 * 1024)).toFixed(1)} MB) and selected it for this generation.`;
    } catch (caught) {
      xttsModelUploadError = errorMessage(caught);
      xttsModelUploadMessage = '';
    } finally {
      uploadingXttsModel = false;
      xttsModelUploadPhase = 'idle';
    }
  }

  async function loadXttsModels() {
    xttsModelsLoading = true;
    try {
      const catalogue = await sessionApi.xttsModels();
      xttsModels = catalogue.data ?? [];
      xttsModelsLifecycleSupported = Boolean(catalogue.lifecycle_supported);
      const wrapperVersion = String(catalogue.wrapper?.version ?? '');
      const wrapperStatus = String(catalogue.wrapper?.status ?? '');
      const wrapperMessage = wrapperVersion
        ? `Connected XTTS wrapper ${wrapperVersion}${wrapperStatus ? ` (${wrapperStatus})` : ''}.`
        : '';
      xttsModelsCompatibility = [catalogue.compatibility, wrapperMessage]
        .filter(Boolean)
        .join(' ');
    } catch (caught) {
      xttsModels = [];
      xttsModelsLifecycleSupported = false;
      xttsModelsCompatibility = `${errorMessage(caught)} Update or Repair XTTS in Pandrator Manager if model lifecycle controls are unavailable.`;
    } finally {
      xttsModelsLoading = false;
    }
  }

  async function removeXttsModel(model: XttsModel) {
    if (!model.removable || deletingXttsModelId) return;
    const isCurrent = model.id === ttsModel;
    const question = isCurrent
      ? `“${model.id}” is selected for this generation. Remove it and switch this generation to a safe XTTS fallback?`
      : `Remove the local XTTS model “${model.id}”? This cannot be undone.`;
    if (!window.confirm(question)) return;
    deletingXttsModelId = model.id;
    error = '';
    try {
      await sessionApi.deleteXttsModel(model.id);
      if (isCurrent) ttsModel = '';
      await Promise.all([loadXttsModels(), loadSpeechCatalogues(false, true)]);
      const service = ttsCatalogue.services.find(
        (item) => String(item.id).toLowerCase() === 'xtts'
      );
      const safeFallback =
        service?.default_model ??
        service?.models?.find((candidate) => candidate !== model.id) ??
        xttsModels.find((candidate) => candidate.is_default)?.id ??
        xttsModels.find((candidate) => candidate.id !== model.id)?.id ??
        '';
      if (
        isCurrent ||
        !xttsModels.some((candidate) => candidate.id === ttsModel)
      ) {
        ttsModel = String(safeFallback);
        chooseTtsModel(ttsModel);
      }
      xttsModelUploadMessage = `Removed ${model.id}${isCurrent && ttsModel ? ` and selected ${ttsModel}` : ''}.`;
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      deletingXttsModelId = '';
    }
  }

  async function loadLlmModels(force = false) {
    if (llmModelsLoaded && !force) return;
    try {
      const providerPayload = await sessionApi.providers();
      const enabled = providerPayload.items.filter(
        (provider) => provider.enabled
      );
      const groups = await Promise.all(
        enabled.map(async (provider) => ({
          provider,
          models: (await sessionApi.providerModels(provider.id)).items
        }))
      );
      llmModels = groups.flatMap(({ provider, models }) =>
        models
          .filter((item) => item.is_active)
          .map((item) => {
            const custom =
              Boolean(provider.options_json?.is_custom) ||
              !['openai', 'gemini', 'anthropic'].includes(
                provider.provider_key
              );
            const providerId = custom
              ? provider.options_json?.provider_id || provider.id
              : provider.options_json?.provider_id || provider.provider_key;
            return {
              value: custom
                ? `custom:${providerId}/${item.model_id}`
                : `${provider.provider_key}/${item.model_id}`,
              label: `${provider.label} · ${item.model_id}`,
              isDefault: Boolean(item.is_default),
              defaultReasoningEffort: String(
                item.default_reasoning_effort ?? ''
              )
            };
          })
      );
      llmModelsLoaded = true;
    } catch {
      llmModels = [];
    }
  }

  async function discoverTtsService(
    service: TtsService | undefined = selectedTtsService
  ) {
    if (!service?.api_base) return;
    try {
      const discovered = await sessionApi.discoverTts(
        service.api_base,
        service.id
      );
      if (!discovered?.success) return;
      const services = ttsCatalogue.services.map((item) =>
        item.id === service.id
          ? {
              ...item,
              models: Array.from(
                new Set([...(discovered.models ?? []), ...(item.models ?? [])])
              ),
              voices: Array.from(
                new Set([...(discovered.voices ?? []), ...(item.voices ?? [])])
              ),
              live_voices: Array.from(new Set(discovered.voices ?? [])),
              online: true,
              available: true
            }
          : item
      );
      ttsCatalogue = { ...ttsCatalogue, services };
    } catch {
      /* A reachable service may not expose catalogue routes. */
    }
  }

  async function chooseTtsService(value: string) {
    ttsService = value;
    const service = ttsCatalogue.services.find(
      (item) => String(item.id) === value
    );
    ttsModel = String(service?.default_model ?? service?.models?.[0] ?? '');
    await discoverTtsService(service);
    if (String(service?.id ?? '').toLowerCase() === 'xtts')
      await loadXttsModels();
    voiceName = String(
      service?.default_voices_by_language?.[ttsModel]?.[targetLanguage] ??
        service?.default_voices?.[ttsModel] ??
        service?.default_voice ??
        ''
    );
  }

  function chooseTtsModel(value: string) {
    ttsModel = value;
    const service = selectedTtsService;
    const modelVoices = service?.voice_catalogues?.[value] ?? [];
    voiceName = String(
      service?.default_voices_by_language?.[value]?.[targetLanguage] ??
        service?.default_voices?.[value] ??
        modelVoices[0] ??
        ''
    );
  }

  async function openFullSettings(
    section: string,
    initialOverride: Record<string, unknown> | null = null
  ) {
    SettingsModalComponent ??= (await import('./SettingsModal.svelte')).default;
    fullSettingsDraft = initialOverride ? { ...initialOverride } : null;
    fullSettingsSection = section;
  }

  async function openTtsServices() {
    TtsServicesModalComponent ??= (await import('./TtsServicesModal.svelte'))
      .default;
    ttsServicesOpen = true;
  }

  async function openVoiceLibrary(
    view: 'references' | 'prebuilt',
    serviceId = '',
    voiceId = ''
  ) {
    VoiceLibraryModalComponent ??= (await import('./VoiceLibraryModal.svelte'))
      .default;
    voiceLibraryView = view;
    voiceLibraryService = serviceId;
    voiceLibraryInitialVoice = voiceId;
    voiceLibraryOpen = true;
  }

  async function usePublishedVoice(providerVoiceId: string) {
    voiceName = providerVoiceId;
    voiceLibraryOpen = false;
    await loadSpeechCatalogues(true, true);
    voiceName = providerVoiceId;
  }

  async function waitForVoiceJob(id: string) {
    for (let attempt = 0; attempt < 240; attempt += 1) {
      const job = await jobApi.get(id);
      if (job.status === 'succeeded') return job;
      if (['failed', 'canceled', 'interrupted'].includes(job.status))
        throw new Error(job.error_message || `Voice upload ${job.status}.`);
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    throw new Error(
      'The voice upload is still running. Check Activity & logs.'
    );
  }

  const selectedTtsService = $derived(
    ttsCatalogue.services.find((item) =>
      [item.id, item.name]
        .map((value) => String(value ?? '').toLowerCase())
        .includes(ttsService.toLowerCase())
    )
  );
  const selectedTtsServiceAvailable = $derived(
    selectedTtsService?.available === true
  );
  const compareLabels = (left: string, right: string) =>
    left.localeCompare(right, undefined, { sensitivity: 'base' });
  const serviceLabel = (service: TtsService) =>
    String(service.name || service.id);
  const compareServices = (left: TtsService, right: TtsService) =>
    compareLabels(serviceLabel(left), serviceLabel(right)) ||
    compareLabels(left.id, right.id);
  const availableTtsServices = $derived(
    [...ttsCatalogue.services]
      .filter((service) => service.available === true)
      .sort(compareServices)
  );
  const unavailableTtsServices = $derived(
    [...ttsCatalogue.services]
      .filter((service) => service.available !== true)
      .sort(compareServices)
  );
  const ttsModels = $derived(selectedTtsService?.models ?? []);
  const selectedLlmModel = $derived(
    model === 'default'
      ? (llmModels.find((item) => item.isDefault) ?? null)
      : (llmModels.find((item) => item.value === model) ?? null)
  );
  const selectedTtsDefaultVoice = $derived(
    selectedTtsService?.default_voices_by_language?.[ttsModel]?.[
      targetLanguage
    ] ??
      selectedTtsService?.default_voices?.[ttsModel] ??
      selectedTtsService?.default_voice ??
      ''
  );
  const selectedTtsServiceId = $derived(
    String(selectedTtsService?.id ?? ttsService)
      .trim()
      .toLowerCase()
      .replaceAll('-', '_')
      .replaceAll(' ', '_')
  );
  const audioCppLinkedReferences = $derived(
    selectedTtsService?.adapter === 'audio_cpp'
  );
  const selectedModelNeedsReviewedTranscript = $derived(
    selectedTtsService?.voice_reference_text === 'required' ||
      (audioCppLinkedReferences && ttsModel.toLowerCase().includes('omnivoice'))
  );
  const supportsXttsModelUpload = $derived(
    selectedTtsServiceId === 'xtts' &&
      Boolean(selectedTtsService?.supports_model_upload)
  );
  const ttsModelAcquisitionHint = $derived(
    selectedTtsServiceId === 'kobold_qwen'
      ? 'A Qwen voice family that was not prepared initially downloads automatically on first use. Model size and precision are configured in Speech services → Local.'
      : selectedTtsServiceId === 'chatterbox'
        ? 'Chatterbox downloads the selected model automatically on first use. The first generation can therefore take several minutes.'
        : selectedTtsServiceId === 'fishs2'
          ? 'Fish uses one S2 Pro model. Choose its quantization in Speech services → Local; the selected file is downloaded automatically when that configuration is applied.'
          : ''
  );
  const generationPromptModels = $derived(
    Array.from(selectedTtsService?.generation_prompt_models ?? []).map(
      (model) => String(model).toLowerCase()
    )
  );
  const supportsGenerationPrompt = $derived(
    generationPromptModels.includes(ttsModel.toLowerCase())
  );
  const supportsBatchSynthesis = $derived(
    Boolean(
      selectedTtsService?.supports_batch_synthesis &&
      selectedTtsService?.batch_synthesis?.streaming &&
      ['ndjson-v1', 'pandrator-ordered-serial-v1'].includes(
        selectedTtsService?.batch_synthesis?.protocol ?? ''
      )
    )
  );
  const maximumTtsBatchSize = $derived(
    Number(selectedTtsService?.batch_synthesis?.max_batch_size ?? 32)
  );
  const selectedModelVoiceMode = $derived(
    selectedTtsService?.model_voice_modes?.[ttsModel] ?? ''
  );
  const selectedModelUsesReferences = $derived(
    ['cloning', 'hybrid'].includes(selectedModelVoiceMode) ||
      (selectedTtsServiceId === 'kobold_qwen' &&
        ttsModel.toLowerCase() === 'voice cloning')
  );
  const selectedModelIsCloningOnly = $derived(
    selectedModelVoiceMode === 'cloning' ||
      (selectedTtsServiceId === 'kobold_qwen' &&
        ttsModel.toLowerCase() === 'voice cloning')
  );
  const supportsCloningVoices = $derived(
    Boolean(selectedTtsService?.supports_voice_cloning)
  );
  const supportsPrebuiltVoices = $derived(
    Boolean(
      selectedTtsService?.supports_prebuilt_voices &&
      !selectedModelIsCloningOnly
    )
  );
  const selectedModelVoiceIds = $derived(
    selectedTtsService?.voice_catalogues?.[ttsModel] ??
      (supportsPrebuiltVoices ? (selectedTtsService?.voices ?? []) : [])
  );
  const ttsVoiceDescriptors = $derived(
    Array.from(new Set(selectedModelVoiceIds)).map((voice) =>
      describeVoice(
        String(selectedTtsService?.id ?? ttsService),
        String(voice),
        selectedTtsService?.voice_metadata?.[`${ttsModel}:${String(voice)}`]
      )
    )
  );
  const ttsLanguages = $derived(
    languagesForService(
      String(selectedTtsService?.id ?? ttsService),
      ttsVoiceDescriptors,
      {
        modelId: ttsModel,
        modelCatalog: selectedTtsService?.model_catalog
      }
    )
  );
  const filteredPrebuiltVoices = $derived(
    ttsVoiceDescriptors.filter(
      (voice) => !voice.languageCode || voice.languageCode === targetLanguage
    )
  );
  const publishedProviderVoices = $derived(
    libraryVoices.flatMap((voice) => {
      const registration =
        voice?.metadata_json?.providers?.[selectedTtsServiceId];
      return registration?.status === 'ready' && registration?.voice_id
        ? [String(registration.voice_id)]
        : [];
    })
  );
  const managedProviderVoiceIds = $derived(
    new Set(
      libraryVoices
        .map((voice) =>
          String(
            voice.metadata_json?.providers?.[selectedTtsServiceId]?.voice_id ??
              ''
          ).toLowerCase()
        )
        .filter(Boolean)
    )
  );
  const readyManagedProviderVoiceIds = $derived(
    new Set(publishedProviderVoices.map((voice) => voice.toLowerCase()))
  );
  const prebuiltVoiceIds = $derived(
    new Set(
      Array.from(
        selectedTtsService?.voice_catalogues?.['Prebuilt Voices'] ?? []
      ).map((voice) => String(voice).toLowerCase())
    )
  );
  const clonedVoiceIds = $derived(
    Array.from(
      new Set(
        [
          ...(selectedModelUsesReferences ? selectedModelVoiceIds : []),
          ...(selectedTtsService?.live_voices ?? []),
          ...publishedProviderVoices,
          ...(!selectedTtsService?.supports_prebuilt_voices
            ? (selectedTtsService?.voices ?? [])
            : [])
        ]
          .map((voice) => String(voice))
          .filter(
            (voice) =>
              voice &&
              !prebuiltVoiceIds.has(voice.toLowerCase()) &&
              (!managedProviderVoiceIds.has(voice.toLowerCase()) ||
                readyManagedProviderVoiceIds.has(voice.toLowerCase()))
          )
      )
    )
  );
  const clonedVoiceDescriptors = $derived(
    clonedVoiceIds.map((voice) => {
      const managed = libraryVoices.find(
        (item) =>
          String(
            item.metadata_json?.providers?.[selectedTtsServiceId]?.voice_id ??
              ''
          ).toLowerCase() === voice.toLowerCase()
      );
      const descriptor = describeVoice(selectedTtsServiceId, voice);
      return {
        ...descriptor,
        name: managed?.name ?? descriptor.name,
        managedLanguage: managed?.language
      };
    })
  );
  type VoiceLanguageGroup = {
    key: string;
    label: string;
    unspecified: boolean;
    voices: (typeof clonedVoiceDescriptors)[number][];
  };
  const normalizeVoiceLanguage = (value: string | null | undefined) => {
    const raw = String(value ?? '').trim();
    if (!raw) return null;
    const normalized = raw
      .replaceAll(/\s+/g, '')
      .replaceAll('_', '-')
      .toLowerCase();
    const option = LANGUAGE_OPTIONS.find((item) => {
      const optionValue = String(item.value)
        .replaceAll(/\s+/g, '')
        .replaceAll('_', '-')
        .toLowerCase();
      const optionLabel = item.label.replaceAll(/\s+/g, '').toLowerCase();
      return optionValue === normalized || optionLabel === normalized;
    });
    if (option)
      return {
        key: String(option.value),
        label: option.label,
        unspecified: false
      };
    const baseCode = normalized.split('-')[0];
    const baseOption = LANGUAGE_OPTIONS.find(
      (item) => String(item.value).toLowerCase() === baseCode
    );
    if (baseOption)
      return {
        key: String(baseOption.value),
        label: baseOption.label,
        unspecified: false
      };
    return { key: normalized, label: raw, unspecified: false };
  };
  const targetVoiceLanguage = $derived(
    normalizeVoiceLanguage(targetLanguage)?.key ?? targetLanguage
  );
  const clonedVoiceGroups = $derived.by(() => {
    const groups = new Map<string, VoiceLanguageGroup>();
    for (const voice of clonedVoiceDescriptors) {
      const language = normalizeVoiceLanguage(voice.managedLanguage) ??
        normalizeVoiceLanguage(voice.languageCode) ?? {
          key: 'unspecified',
          label: 'Multilingual / language not set',
          unspecified: true
        };
      const existing = groups.get(language.key) ?? { ...language, voices: [] };
      existing.voices.push(voice);
      groups.set(language.key, existing);
    }
    return [...groups.values()]
      .map((group) => ({
        ...group,
        voices: [...group.voices].sort(
          (left, right) =>
            compareLabels(left.name, right.name) ||
            compareLabels(left.id, right.id)
        )
      }))
      .sort(
        (left, right) =>
          Number(left.key !== targetVoiceLanguage) -
            Number(right.key !== targetVoiceLanguage) ||
          Number(left.unspecified) - Number(right.unspecified) ||
          compareLabels(left.label, right.label) ||
          compareLabels(left.key, right.key)
      );
  });
  const showClonedVoices = $derived(
    Boolean(
      supportsCloningVoices &&
      (!selectedTtsService?.supports_prebuilt_voices ||
        selectedModelUsesReferences)
    )
  );
  const localVoiceChoices = $derived(
    libraryVoices
      .map((voice) => {
        const registration =
          voice.metadata_json?.providers?.[selectedTtsServiceId];
        const hasSample = Number(voice.available_sample_count ?? 0) > 0;
        const needsTranscript = Boolean(
          selectedModelNeedsReviewedTranscript &&
          !voice.preferred_sample_transcript_reviewed
        );
        return { voice, registration, hasSample, needsTranscript };
      })
      .sort((left, right) => {
        const leftLanguage = left.voice.language === targetLanguage ? 0 : 1;
        const rightLanguage = right.voice.language === targetLanguage ? 0 : 1;
        return (
          leftLanguage - rightLanguage ||
          left.voice.name.localeCompare(right.voice.name)
        );
      })
  );

  async function useLibraryVoice(voice: VoiceRecord) {
    const registration = voice.metadata_json?.providers?.[selectedTtsServiceId];
    if (registration?.status === 'ready' && registration.voice_id) {
      voiceName = String(registration.voice_id);
      return;
    }
    if (
      Number(voice.available_sample_count ?? 0) < 1 ||
      (selectedModelNeedsReviewedTranscript &&
        !voice.preferred_sample_transcript_reviewed)
    ) {
      await openVoiceLibrary('references', selectedTtsServiceId, voice.id);
      return;
    }
    if (selectedTtsService?.available === false && !audioCppLinkedReferences) {
      error =
        selectedTtsService.availability_reason ||
        `Start the speech service before ${audioCppLinkedReferences ? 'linking' : 'uploading'} a voice.`;
      return;
    }
    if (publishingLibraryVoiceId) return;
    publishingLibraryVoiceId = voice.id;
    voicePublishStatus = audioCppLinkedReferences
      ? `Linking ${voice.name} to ${selectedTtsService?.name ?? selectedTtsServiceId}…`
      : `Uploading ${voice.name} to ${selectedTtsService?.name ?? selectedTtsServiceId}…`;
    error = '';
    try {
      const queued = await voiceApi.publish(
        voice.id,
        selectedTtsServiceId,
        voice.revision
      );
      const completed = await waitForVoiceJob(queued.id);
      const providerVoiceId = String(
        completed.result_json?.provider_voice_id ?? ''
      );
      if (!providerVoiceId)
        throw new Error('The provider did not return a usable voice ID.');
      const nextRevision = Number(
        completed.result_json?.voice_revision ?? voice.revision + 1
      );
      libraryVoices = libraryVoices.map((item) =>
        item.id === voice.id
          ? {
              ...item,
              revision: nextRevision,
              metadata_json: {
                ...(item.metadata_json ?? {}),
                providers: {
                  ...(item.metadata_json?.providers ?? {}),
                  [selectedTtsServiceId]: {
                    ...(item.metadata_json?.providers?.[selectedTtsServiceId] ??
                      {}),
                    voice_id: providerVoiceId,
                    status: 'ready',
                    managed_by: 'pandrator',
                    ...(audioCppLinkedReferences
                      ? {
                          resource_kind: 'linked_reference',
                          protocol: 'pandrator-linked-voices-v1'
                        }
                      : {})
                  }
                }
              }
            }
          : item
      );
      ttsCatalogue = {
        ...ttsCatalogue,
        services: ttsCatalogue.services.map((service) =>
          service.id === selectedTtsService?.id
            ? {
                ...service,
                voices: Array.from(
                  new Set([...(service.voices ?? []), providerVoiceId])
                ),
                live_voices: Array.from(
                  new Set([...(service.live_voices ?? []), providerVoiceId])
                ),
                voice_catalogues: {
                  ...(service.voice_catalogues ?? {}),
                  [ttsModel]: Array.from(
                    new Set([
                      ...(service.voice_catalogues?.[ttsModel] ?? []),
                      providerVoiceId
                    ])
                  )
                }
              }
            : service
        )
      };
      voiceName = providerVoiceId;
      voicePublishStatus = audioCppLinkedReferences
        ? `${voice.name} is linked and selected.`
        : `${voice.name} is ready and selected.`;
    } catch (caught) {
      voicePublishStatus = '';
      error = `Could not prepare ${voice.name}: ${errorMessage(caught)}`;
    } finally {
      publishingLibraryVoiceId = '';
    }
  }

  async function updateOutcomeTransformations(
    changes: Record<string, boolean>
  ) {
    const current = outcome ?? (await sessionApi.outcome(session.id));
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
      optimizationEnabled = generationEnabled;
      documentOptimizationEnabled = documentEnabled;
      await load();
    } catch (caught) {
      error = errorMessage(caught);
    }
  }

  function previewArtifact(stage: Stage) {
    if (!stage.artifact) return;
    const role = stage.artifact.raw_role ?? stage.artifact.role;
    if (role === 'tts_optimized' && stage.artifact.kind === 'json') {
      optimizationReviewArtifactId = stage.artifact.id;
      return;
    }
    if (
      ['transcription', 'correction', 'translation', 'tts_optimized'].includes(
        role
      )
    ) {
      reviewArtifactId = stage.artifact.id;
      return;
    }
    preview = {
      ...stage.artifact,
      role,
      relative_path: stage.artifact.relative_path ?? stage.artifact.path
    };
  }

  function stageSectionUpdates(
    key: string
  ): { section: string; value: Record<string, unknown> }[] {
    if (key === 'transcribe')
      return [
        { section: 'stt', value: stageSettings[key] },
        {
          section: 'subtitles',
          value: {
            max_chars_per_line: subtitleChars,
            max_lines: subtitleLines,
            min_duration_ms: subtitleMinDuration,
            max_duration_ms: subtitleMaxDuration,
            max_cps: subtitleCps,
            min_gap_ms: subtitleMinGap,
            phrase_gap_ms: subtitlePhraseGap,
            hard_gap_ms: subtitleHardGap,
            sentence_boundary_threshold: subtitleSentenceBoundaryThreshold
          }
        }
      ];
    if (key === 'correct')
      return [
        {
          section: 'correction',
          value: {
            enabled: true,
            model_name: model === 'default' ? '' : model,
            reasoning_effort: reasoningEffort,
            instructions,
            llm_concurrent_calls: optimizationConcurrent,
            char_limit: correctionBatchCharLimit,
            max_segments_per_batch: correctionBatchSegmentLimit,
            context_before: contextBefore,
            context_after: contextAfter,
            no_remove_subtitles: preventSubtitleRemoval,
            timing_context_mode: timingContextMode,
            substantial_gap_ms: timingContextGap,
            web_research_enabled: webResearchEnabled,
            web_research_model_name: webResearchModel,
            web_research_mode: webResearchMode,
            web_research_context_fraction: webResearchContextFraction
          }
        }
      ];
    if (key === 'translate')
      return [
        {
          section: 'translation',
          value: {
            enabled: true,
            backend,
            target_language: targetLanguage,
            model_name: model === 'default' ? '' : model,
            reasoning_effort: reasoningEffort,
            instructions,
            source_artifact_id: translationSourceArtifactId,
            llm_concurrent_calls: optimizationConcurrent,
            char_limit: correctionBatchCharLimit,
            max_segments_per_batch: correctionBatchSegmentLimit,
            context_before: contextBefore,
            context_after: contextAfter,
            no_remove_subtitles: preventSubtitleRemoval,
            timing_context_mode: timingContextMode,
            substantial_gap_ms: timingContextGap,
            web_research_enabled: webResearchEnabled,
            web_research_model_name: webResearchModel,
            web_research_mode: webResearchMode,
            web_research_context_fraction: webResearchContextFraction
          }
        }
      ];
    if (key === 'optimize_tts')
      return [
        {
          section: 'text',
          value: {
            llm_tts_optimization: optimizationEnabled,
            llm_processing_enabled: optimizationEnabled,
            llm_tts_document_optimization: documentOptimizationEnabled,
            tts_optimization_model: model === 'default' ? '' : model,
            speech_optimization_mode: speechOptimizationMode,
            llm_tts_batch_size: optimizationBatchSize,
            llm_tts_document_batch_size: documentOptimizationBatchSize,
            llm_concurrent_calls: optimizationConcurrent,
            llm_multi_stage: optimizationMultiStage,
            combined_prompt: optimizationPrompt,
            first_prompt: optimizationFirstPrompt,
            second_prompt: optimizationSecondPrompt,
            third_prompt: optimizationThirdPrompt
          }
        }
      ];
    if (key === 'optimize_document')
      return [
        {
          section: 'text',
          value: {
            llm_tts_document_optimization: documentOptimizationEnabled,
            tts_optimization_model: model === 'default' ? '' : model,
            speech_optimization_mode: speechOptimizationMode,
            llm_tts_document_batch_size: documentOptimizationBatchSize,
            llm_concurrent_calls: optimizationConcurrent,
            llm_multi_stage: optimizationMultiStage,
            combined_prompt: optimizationPrompt,
            first_prompt: optimizationFirstPrompt,
            second_prompt: optimizationSecondPrompt,
            third_prompt: optimizationThirdPrompt
          }
        }
      ];
    return [{ section: stageSection(key), value: stageSettings[key] }];
  }

  async function revertStageToDefaults() {
    if (!settingsStage) return;
    const stage = settingsStage;
    const updates = stageSectionUpdates(stage.key);
    try {
      for (const update of updates)
        await clearSectionOverrides(update.section, Object.keys(update.value));
      const next = { ...stageSettings };
      delete next[stage.key];
      stageSettings = next;
      await openSettings(stage);
      stageMessage = 'Reverted to application defaults.';
    } catch (caught) {
      error = errorMessage(caught);
    }
  }

  function captureStageSettings(stage: Stage) {
    const key = stage.key;
    const common = {
      model_name: model === 'default' ? '' : model,
      [`${key}_model`]: model
    };
    if (key === 'transcribe')
      stageSettings[key] = {
        stt_engine: sttEngine,
        stt_backend: sttEngine,
        stt_model_quantization: sttQuantization,
        stt_compute_backend: sttComputeBackend,
        stt_compute_device: sttDevice,
        stt_language: sttEngine === 'moss' ? 'auto' : originalLanguage,
        original_language: sttEngine === 'moss' ? 'auto' : originalLanguage,
        stt_threads: sttThreads,
        stt_chunk_seconds: sttEngine === 'moss' ? 0 : sttChunkSeconds,
        stt_chunk_overlap_seconds: sttChunkOverlap,
        stt_hotwords: sttHotwords,
        stt_transcribe_style: sttTranscribeStyle,
        stt_lid_backend: sttLidBackend,
        stt_beam_size: sttBeamSize,
        parakeet_decoder: parakeetDecoder,
        moss_max_chunk_seconds: mossMaxChunkSeconds,
        moss_chunk_overlap_seconds: mossChunkOverlap,
        moss_vad_enabled: mossVadEnabled,
        moss_ctc_alignment_enabled: mossCtcAlignmentEnabled,
        moss_ctc_aligner_model: 'auto',
        moss_ctc_padding_seconds: mossCtcPaddingSeconds,
        crispasr_vad_enabled: vadEnabled,
        crispasr_vad_model: vadModel,
        crispasr_vad_threshold: vadThreshold,
        crispasr_vad_min_speech_ms: vadMinSpeech,
        crispasr_vad_min_silence_ms: vadMinSilence,
        crispasr_vad_max_speech_seconds: vadMaxSpeech,
        crispasr_vad_speech_pad_ms: vadSpeechPad,
        subtitle_max_chars_per_line: subtitleChars,
        subtitle_max_lines: subtitleLines,
        subtitle_min_duration_ms: subtitleMinDuration,
        subtitle_max_duration_ms: subtitleMaxDuration,
        subtitle_max_cps: subtitleCps,
        subtitle_min_gap_ms: subtitleMinGap,
        subtitle_phrase_gap_ms: subtitlePhraseGap,
        subtitle_hard_gap_ms: subtitleHardGap,
        subtitle_sentence_boundary_threshold: subtitleSentenceBoundaryThreshold
      };
    else if (key === 'correct')
      stageSettings[key] = {
        ...common,
        reasoning_effort: reasoningEffort,
        instructions,
        llm_concurrent_calls: optimizationConcurrent,
        char_limit: correctionBatchCharLimit,
        max_segments_per_batch: correctionBatchSegmentLimit,
        context_before: contextBefore,
        context_after: contextAfter,
        no_remove_subtitles: preventSubtitleRemoval,
        timing_context_mode: timingContextMode,
        substantial_gap_ms: timingContextGap,
        web_research_enabled: webResearchEnabled,
        web_research_model_name: webResearchModel,
        web_research_mode: webResearchMode,
        web_research_context_fraction: webResearchContextFraction
      };
    else if (key === 'translate')
      stageSettings[key] = {
        ...common,
        translation_backend: backend,
        target_language: targetLanguage,
        source_artifact_id: translationSourceArtifactId,
        reasoning_effort: reasoningEffort,
        instructions,
        llm_concurrent_calls: optimizationConcurrent,
        char_limit: correctionBatchCharLimit,
        max_segments_per_batch: correctionBatchSegmentLimit,
        context_before: contextBefore,
        context_after: contextAfter,
        no_remove_subtitles: preventSubtitleRemoval,
        timing_context_mode: timingContextMode,
        substantial_gap_ms: timingContextGap,
        web_research_enabled: webResearchEnabled,
        web_research_model_name: webResearchModel,
        web_research_mode: webResearchMode,
        web_research_context_fraction: webResearchContextFraction
      };
    else if (key === 'optimize_tts') {
      const enabled = Boolean(stage.enabled);
      optimizationEnabled = enabled && optimizationTiming === 'generation';
      documentOptimizationEnabled =
        enabled && optimizationTiming === 'document';
      stageSettings[key] = {
        ...common,
        llm_tts_optimization: optimizationEnabled,
        llm_tts_document_optimization: documentOptimizationEnabled,
        speech_optimization_mode: speechOptimizationMode,
        llm_tts_batch_size:
          optimizationTiming === 'document'
            ? documentOptimizationBatchSize
            : optimizationBatchSize,
        llm_tts_document_batch_size: documentOptimizationBatchSize,
        combined_prompt: optimizationPrompt,
        llm_concurrent_calls: optimizationConcurrent,
        llm_multi_stage: optimizationMultiStage,
        first_prompt: optimizationFirstPrompt,
        second_prompt: optimizationSecondPrompt,
        third_prompt: optimizationThirdPrompt
      };
    } else if (key === 'optimize_document')
      stageSettings[key] = {
        ...common,
        llm_tts_document_optimization: documentOptimizationEnabled,
        speech_optimization_mode: speechOptimizationMode,
        llm_tts_document_batch_size: documentOptimizationBatchSize,
        llm_tts_batch_size: documentOptimizationBatchSize,
        combined_prompt: optimizationPrompt,
        llm_concurrent_calls: optimizationConcurrent,
        llm_multi_stage: optimizationMultiStage,
        first_prompt: optimizationFirstPrompt,
        second_prompt: optimizationSecondPrompt,
        third_prompt: optimizationThirdPrompt
      };
    else if (key === 'clean_source')
      stageSettings[key] = {
        ...common,
        agentic,
        max_iterations: maxIterations
      };
    else if (key === 'prepare_text')
      stageSettings[key] = {
        enable_sentence_splitting: splitSentences,
        enable_sentence_appending: appendSentences,
        max_sentence_length: maxSentenceLength,
        enable_nemo_normalization: nemoNormalization,
        normalize_all_caps: normalizeAllCaps,
        remove_diacritics: removeDiacritics,
        remove_quotation_marks: removeQuotationMarks
      };
    else if (key === 'generate_audio')
      stageSettings[key] = {
        tts_service: ttsService,
        service: ttsService,
        model: ttsModel,
        xtts_model: ttsModel,
        voice: voiceName,
        generation_prompt: generationPrompt,
        tts_batch_size: ttsBatchSize,
        language: targetLanguage,
        target_language: targetLanguage,
        speech_block_min_chars: speechBlockMinChars,
        speech_block_max_chars: speechBlockMaxChars,
        speech_block_merge_threshold: speechBlockMergeThreshold,
        speech_block_continuation_threshold_ms:
          speechBlockContinuationThreshold,
        speech_block_max_internal_gap_ms: speechBlockMaxInternalGap
      };
    else if (key === 'export')
      stageSettings[key] = {
        export_mode: exportMode,
        subtitle_format: subtitleFormat,
        subtitle_mode: subtitleMode,
        subtitle_selection: subtitleSelection,
        audio_mode: audioMode,
        subtitle_max_chars_per_line: subtitleChars,
        subtitle_max_lines: subtitleLines,
        subtitle_min_duration_ms: subtitleMinDuration,
        subtitle_max_duration_ms: subtitleMaxDuration,
        subtitle_max_cps: subtitleCps,
        subtitle_min_gap_ms: subtitleMinGap,
        subtitle_phrase_gap_ms: subtitlePhraseGap,
        subtitle_hard_gap_ms: subtitleHardGap,
        subtitle_sentence_boundary_threshold: subtitleSentenceBoundaryThreshold
      };
    else stageSettings[key] = common;
  }

  async function openFullSettingsFromStage() {
    if (!settingsStage) return;
    const stage = settingsStage;
    captureStageSettings(stage);
    const section = stageSection(stage.key);
    const draft = stageSectionUpdates(stage.key).find(
      (update) => update.section === section
    )?.value;
    await openFullSettings(section, draft ?? stageSettings[stage.key]);
  }

  async function syncStageAfterFullSettings(payload: SettingsPayload) {
    if (!settingsStage) return;
    const stage = settingsStage;
    stageSettings[stage.key] = {
      ...(stageSettings[stage.key] ?? {}),
      ...(payload.effective ?? {})
    };
    await openSettings(stage);
  }

  function closeFullSettings() {
    fullSettingsSection = '';
    fullSettingsDraft = null;
  }

  async function saveSettings(mode: 'session' | 'defaults' = 'session') {
    if (!settingsStage) return;
    const key = settingsStage.key;
    if (key === 'generate_audio' && publishingLibraryVoiceId) {
      error = `Wait for the selected library voice to finish ${audioCppLinkedReferences ? 'linking' : 'uploading'}.`;
      return;
    }
    if (key === 'generate_audio' && !selectedTtsServiceAvailable) {
      error =
        selectedTtsService?.availability_reason ||
        'Choose an available TTS service before saving generation settings.';
      return;
    }
    if (
      key === 'generate_audio' &&
      showClonedVoices &&
      (!voiceName ||
        !clonedVoiceIds.some(
          (voice) => voice.toLowerCase() === voiceName.toLowerCase()
        ))
    ) {
      error = `Choose a ready cloned voice, or create and ${audioCppLinkedReferences ? 'link' : 'upload'} one through the Voice Library.`;
      return;
    }
    if (
      mode === 'session' &&
      key === 'translate' &&
      !translationSourceArtifactId
    ) {
      error = 'Choose the exact transcription or correction to translate.';
      return;
    }
    captureStageSettings(settingsStage);
    const updates = stageSectionUpdates(key).map((update) => {
      if (mode !== 'defaults' || key !== 'translate') return update;
      const value = { ...update.value };
      delete value.source_artifact_id;
      return { ...update, value };
    });
    try {
      if (mode === 'defaults') {
        for (const update of updates)
          await persistDefaultSection(update.section, update.value);
        stageMessage = 'Saved as the application defaults for future sessions.';
      } else {
        for (const update of updates)
          await persistSection(update.section, update.value);
      }
      if (mode === 'session' && key === 'optimize_tts') {
        await updateOutcomeTransformations({
          llm_tts_optimization: optimizationEnabled,
          llm_tts_document_optimization: documentOptimizationEnabled
        });
      } else if (mode === 'session' && key === 'optimize_document') {
        await updateOutcomeTransformations({
          llm_tts_document_optimization: documentOptimizationEnabled
        });
      }
      if (mode === 'session') {
        await load();
        settingsStage = null;
      }
    } catch (caught) {
      error = errorMessage(caught);
    }
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
        sourceMessage = `Rerunning stale ${names} before audio generation. Use Review mode if you want to keep selected prerequisite outputs instead.`;
        await run(stage, true);
        return;
      }
    } catch {
      /* the settings check is advisory; continue with the run */
    }
    await run(stage);
  }

  onMount(async () => {
    workspaceMode =
      localStorage.getItem(`pandrator:workspace-mode:${session.id}`) ===
      'automatic'
        ? 'automatic'
        : 'review';
    await load({ initial: true });
  });
  $effect(() => {
    if (typeof localStorage !== 'undefined')
      localStorage.setItem(
        `pandrator:workspace-mode:${session.id}`,
        workspaceMode
      );
  });
  $effect(() => {
    if (!snapshot?.stages.some((stage) => stage.status === 'running')) return;
    if (appState.eventsHealthy) return;
    const timer = window.setTimeout(() => load({ initial: false }), 5000);
    return () => window.clearTimeout(timer);
  });
</script>

<div class="mx-auto max-w-6xl">
  <button
    onclick={onback}
    class="muted mb-4 flex items-center gap-2 text-sm font-semibold"
    ><ArrowLeft size={16} /> Sessions</button
  >
  <header class="mb-6 flex flex-wrap items-end justify-between gap-6">
    <div>
      <div class="eyebrow mb-2">Resolved outcome</div>
      <p class="muted max-w-2xl">
        {session.workflow_kind === 'subtitles'
          ? 'Transcribe, refine, translate, and export subtitle documents. Voice generation and rendered video remain available by converting this workspace to voiceover.'
          : 'Choose how much control you want while keeping the same settings, artifacts, and review history.'}
      </p>
      {#if session.workflow_kind !== 'subtitles'}<div
          class="mt-4 inline-flex rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] p-1"
          aria-label="Workspace mode"
        >
          <button
            onclick={() => (workspaceMode = 'review')}
            class:mode-active={workspaceMode === 'review'}
            class="mode-choice">Review each stage</button
          ><button
            onclick={() => (workspaceMode = 'automatic')}
            class:mode-active={workspaceMode === 'automatic'}
            class="mode-choice">Generate automatically</button
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
        >{/if}<button
        onclick={() => (sourceDialog = true)}
        class="lift flex items-center gap-3 rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] px-4 py-3 text-sm font-semibold"
        ><Plus size={18} /> Add source</button
      >
    </div>
  </header>
  {#if sourceMessage}<div
      class="mb-5 rounded-xl bg-[var(--accent-soft)] px-4 py-3 text-sm"
    >
      {sourceMessage}
    </div>{/if}
  {#if session.workflow_kind !== 'subtitles' && workspaceMode === 'automatic'}
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
          Pandrator runs the enabled missing or stale prerequisites in order,
          then generates segment takes. It stops there: reviewing takes, RVC
          conversion, assembly, export, and video synchronization remain manual.
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
  {:else}
    <div
      class="mb-6 rounded-2xl border border-[var(--line)] bg-[var(--paper-strong)] px-4 py-3 text-sm"
    >
      <strong>Review mode:</strong>
      <span class="muted"
        >run each ready transformation, inspect its artifact, and proceed when
        satisfied. Downstream cards unlock only when their selected prerequisite
        exists.</span
      >
    </div>
  {/if}
  {#if outcome?.pipeline}<div
      class="mb-6 flex flex-wrap items-center gap-2 rounded-2xl border border-[var(--line)] bg-[var(--paper-strong)] p-4"
    >
      {#each outcome.pipeline as stage, index}<span
          class="rounded-lg bg-[var(--accent-soft)] px-3 py-2 text-xs font-semibold"
          >{stage.title}</span
        >{#if index < outcome.pipeline.length - 1}<ChevronRight
            class="muted"
            size={14}
          />{/if}{/each}
    </div>{/if}

  {#if error}<div
      class="mb-5 flex items-start gap-3 rounded-xl border border-red-400/40 bg-red-500/10 px-4 py-3 text-sm"
    >
      <CircleAlert class="mt-0.5 shrink-0" size={17} /><span>{error}</span>
    </div>{/if}

  {#if workflowStore.loading}
    <div class="surface grid min-h-64 place-items-center rounded-3xl">
      <LoaderCircle class="animate-spin text-[var(--accent)]" size={28} />
    </div>
  {:else if snapshot}
    <div class="space-y-4">
      {#each snapshot.stages as stage}
        <WorkflowStageCard
          {stage}
          {workspaceMode}
          historyLoading={Boolean(historyLoading[stage.key])}
          onsettings={() => openSettings(stage)}
          ontoggle={toggleSpeechOptimization}
          onrun={() => run(stage)}
          onresume={() => resume(stage)}
          oncancel={() => cancel(stage)}
          onselect={(artifactId) => chooseStageArtifact(stage, artifactId)}
          onpreview={() => previewArtifact(stage)}
          onclear={() => clearStageArtifact(stage)}
          onfork={['correct', 'translate'].includes(stage.key) &&
          stage.selected_artifact_id
            ? () => forkStage(stage)
            : undefined}
          onloadmore={() => loadMoreStageArtifacts(stage)}
        />
      {/each}
    </div>
  {/if}
</div>

{#if sourceDialog}<AddSourceDialog
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
    await run(pending.stage, true);
  }}
/>

{#if settingsStage && !fullSettingsSection}
  <div
    class="fixed inset-0 z-50 grid place-items-center bg-black/35 p-5 backdrop-blur-sm"
    role="presentation"
    onclick={(event) =>
      event.target === event.currentTarget && (settingsStage = null)}
  >
    <div
      use:modalFocus={{ onclose: () => (settingsStage = null) }}
      class="surface max-h-[92vh] w-full max-w-xl overflow-y-auto rounded-[1.7rem] p-7"
      role="dialog"
      aria-modal="true"
      aria-labelledby="settings-title"
    >
      <div class="flex justify-between gap-5">
        <div>
          <div class="eyebrow">Stage settings</div>
          <h2 id="settings-title" class="mt-1 text-2xl font-semibold">
            {settingsStage.title}
          </h2>
        </div>
        <button
          onclick={() => (settingsStage = null)}
          aria-label="Close stage settings"
          class="rounded-lg p-2"><X size={19} /></button
        >
      </div>
      <div class="mt-6 grid gap-5">
        {#if settingsStage.key === 'correct' || (settingsStage.key === 'translate' && backend === 'llm') || ['optimize_tts', 'optimize_document', 'clean_source'].includes(settingsStage.key)}<label
            class="text-sm font-semibold"
            >LLM model<select
              bind:value={model}
              class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
              ><option value="default">Application default</option
              >{#each llmModels as item}<option value={item.value}
                  >{item.label}{item.isDefault ? ' · default' : ''}</option
                >{/each}</select
            ></label
          >{/if}
        {#if settingsStage.key === 'correct' || (settingsStage.key === 'translate' && backend === 'llm')}
          <div
            class="rounded-xl border border-[var(--line)] bg-[var(--accent-soft)] p-4"
          >
            <label class="text-sm font-semibold"
              >Reasoning level<select
                bind:value={reasoningEffort}
                class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                ><option value="">Use model default</option><option
                  value="minimal">Minimal · fastest</option
                ><option value="low">Low · economical</option><option
                  value="medium">Medium · balanced</option
                ><option value="high">High · strongest</option></select
              ></label
            >
            <p class="muted mt-2 text-xs leading-relaxed">
              Higher reasoning can improve difficult passages, but usually adds
              latency and may add billed reasoning tokens. {#if reasoningEffort}This
                overrides the model default for this stage.{:else if selectedLlmModel?.defaultReasoningEffort}The
                selected model currently defaults to
                <strong>{selectedLlmModel.defaultReasoningEffort}</strong
                >.{:else}The model or provider chooses the level.{/if}
              Availability depends on the selected model.
            </p>
          </div>
        {/if}
        {#if settingsStage.key === 'correct' || (settingsStage.key === 'translate' && backend === 'llm') || ['optimize_tts', 'optimize_document'].includes(settingsStage.key)}
          <div class="rounded-xl border border-[var(--line)] p-4">
            <label class="text-sm font-semibold"
              >Concurrent LLM requests<input
                type="number"
                min="1"
                max="16"
                bind:value={optimizationConcurrent}
                class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
              /></label
            >
            <p class="muted mt-2 text-xs leading-relaxed">
              1 is the quality-first default. Higher values process independent
              requests in parallel for speed.
              {#if settingsStage.key === 'correct'}Parallel correction cannot
                include the preceding corrected batch.{:else if settingsStage.key === 'translate'}Parallel
                translation cannot include the preceding translation or glossary
                terms discovered by sibling batches.{:else}Units inside one
                optimization request share context. Parallel requests do not
                carry discoveries between them.{/if}
            </p>
          </div>
        {/if}
        {#if settingsStage.key === 'correct' || (settingsStage.key === 'translate' && backend === 'llm')}
          <div class="rounded-xl border border-[var(--line)] p-4">
            <label class="block text-sm font-semibold"
              >Cue timing context<select
                bind:value={timingContextMode}
                class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
              >
                <option value="full">Full timing · best quality</option>
                <option value="overlap_only">Overlap only · fewer tokens</option
                >
                <option value="none">No timing context</option>
              </select><span
                class="muted mt-2 block text-xs font-normal leading-relaxed"
                >Full timing includes each cue interval and its preceding gap or
                overlap exactly once. Overlap-only is a useful compromise for
                simultaneous speech and ASR seam detection. None excludes every
                timing field.</span
              ></label
            >
            {#if timingContextMode === 'full'}<label
                class="mt-4 block text-xs font-semibold"
                >Substantial audible pause (ms)<input
                  type="number"
                  min="0"
                  max="10000"
                  step="100"
                  bind:value={timingContextGap}
                  class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                /><span class="muted mt-1 block font-normal"
                  >The model is asked to preserve a rhetorical boundary at or
                  above this gap.</span
                ></label
              >{/if}
          </div>
        {/if}
        {#if settingsStage.key === 'correct' || (settingsStage.key === 'translate' && backend === 'llm')}
          <div class="rounded-xl border border-[var(--line)] p-4">
            <div class="grid grid-cols-2 gap-3">
              <label class="text-xs font-semibold"
                >Maximum batch characters<input
                  type="number"
                  min="1"
                  max="100000"
                  step="100"
                  bind:value={correctionBatchCharLimit}
                  class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                /></label
              ><label class="text-xs font-semibold"
                >Maximum cues per batch<input
                  type="number"
                  min="1"
                  max="500"
                  bind:value={correctionBatchSegmentLimit}
                  class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                /></label
              >
            </div>
            <p class="muted mt-2 text-xs leading-relaxed">
              Pandrator stops at whichever limit is reached first and prefers a
              sentence or speaker boundary. The quality-first defaults are 6,000
              characters and 40 cues.
            </p>
            {#if settingsStage.key === 'correct' || backend === 'llm'}
              <div class="mt-4 grid grid-cols-2 gap-3">
                <label class="text-xs font-semibold"
                  >Previous output cues<input
                    type="number"
                    min="0"
                    max="20"
                    bind:value={contextBefore}
                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                  /></label
                ><label class="text-xs font-semibold"
                  >Following source cues<input
                    type="number"
                    min="0"
                    max="20"
                    bind:value={contextAfter}
                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                  /></label
                >
              </div>
              <p class="muted mt-2 text-xs leading-relaxed">
                Boundary context improves names, sentence continuity, and
                punctuation without making those cues editable. Sequential mode
                can use corrected or translated output from the previous batch;
                parallel mode cannot.
              </p>
              <label class="mt-4 flex items-start gap-3 text-sm font-semibold"
                ><input
                  type="checkbox"
                  bind:checked={preventSubtitleRemoval}
                  class="mt-1 accent-[var(--accent)]"
                /><span
                  >Prevent cue removal<span
                    class="muted mt-1 block text-xs font-normal leading-relaxed"
                    >Every source cue must survive correction or translation.
                    Enable this when omissions would be worse than preserving an
                    uncertain filler or ASR artifact.</span
                  ></span
                ></label
              >
            {/if}
          </div>
        {/if}
        {#if settingsStage.key === 'correct' || (settingsStage.key === 'translate' && backend === 'llm')}
          <fieldset class="rounded-xl border border-[var(--line)] p-4">
            <legend class="px-1 text-sm font-semibold">Web research</legend>
            <label class="flex items-start gap-3 text-sm font-semibold">
              <input
                type="checkbox"
                bind:checked={webResearchEnabled}
                class="mt-1 accent-[var(--accent)]"
              />
              <span>
                Ground uncertain terms before processing
                <span
                  class="muted mt-1 block text-xs font-normal leading-relaxed"
                  >Research evidence is kept separately from the editable
                  glossary and attached to the resulting artifact.</span
                >
              </span>
            </label>
            {#if webResearchEnabled}
              <div class="mt-4 grid gap-4">
                <label class="text-xs font-semibold">
                  Researcher model
                  <select
                    bind:value={webResearchModel}
                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                  >
                    <option value="">Use the task model</option>
                    {#each llmModels as item}
                      <option value={item.value}>{item.label}</option>
                    {/each}
                  </select>
                </label>
                <label class="text-xs font-semibold">
                  Research mode
                  <select
                    bind:value={webResearchMode}
                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                  >
                    <option value="global"
                      >Research once for the full document</option
                    >
                    <option value="per_chunk"
                      >Research each chunk and compound findings</option
                    >
                  </select>
                </label>
                <label class="text-xs font-semibold">
                  Maximum researcher context ({Math.round(
                    webResearchContextFraction * 100
                  )}%)
                  <input
                    type="range"
                    min="0.1"
                    max="0.8"
                    step="0.05"
                    bind:value={webResearchContextFraction}
                    class="mt-2 w-full accent-[var(--accent)]"
                  />
                </label>
                <p class="muted text-xs leading-relaxed">
                  {#if webResearchMode === 'global'}The researcher receives up
                    to this share of its context in deterministic batches, then
                    the consolidated evidence is reused by every request.{:else}Per-chunk
                    research runs as a sequential prepass so each chunk can
                    refine the accumulated evidence. Transformation requests may
                    still run concurrently after that prepass.{/if}
                </p>
              </div>
            {/if}
          </fieldset>
        {/if}
        {#if settingsStage.key === 'transcribe'}
          <label class="text-sm font-semibold"
            >Recognition model<select
              bind:value={sttEngine}
              onchange={() =>
                (sttQuantization = String(
                  capabilities?.stt?.models?.[sttEngine]?.precision ?? 'f16'
                ))}
              class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
              ><option value="whisper"
                >{sttOptionLabel(
                  'whisper',
                  'Whisper large-v3',
                  'DTW timestamps'
                )}</option
              ><option value="parakeet"
                >{sttOptionLabel(
                  'parakeet',
                  'Parakeet TDT 0.6B v3',
                  'native timestamps'
                )}</option
              ><option value="moss"
                >{sttOptionLabel(
                  'moss',
                  'MOSS Transcribe-Diarize 0.9B',
                  'native speakers + CTC words'
                )}</option
              >{#each sttCatalogue.services as service}<option
                  value={service.id}
                  >{service.name} · cloud word timestamps</option
                >{/each}
              ></select
            ><span class="muted mt-1 block text-xs"
              >{isCloudStt(sttEngine)
                ? 'The selected connection runs remotely; audio is sent to its configured provider.'
                : 'CrispASR downloads a model the first time you use it; the installer-selected model is the default.'}</span
            ></label
          >
          {#if ttsModelAcquisitionHint}
            <p class="muted -mt-2 text-xs leading-relaxed">
              {ttsModelAcquisitionHint}
            </p>
          {/if}
          {#if isCloudStt(sttEngine)}
            <div
              class="rounded-xl border border-[var(--line)] bg-[var(--accent-soft)] p-4"
            >
              <div class="text-sm font-semibold">
                Remote timed transcription
              </div>
              <p class="muted mt-1 text-xs leading-relaxed">
                Pandrator sends the normalized WAV to this provider and accepts
                the result only when it includes genuine word-level spans.
                Diarization is not available for this profile.
              </p>
              <a
                href="/providers?tab=speech&service=stt"
                class="mt-3 inline-flex text-xs font-semibold text-[var(--accent)]"
                >Manage recognition connection</a
              >
            </div>
            <div class="grid gap-3 sm:grid-cols-2">
              <label class="text-sm font-semibold"
                >Source language<select
                  bind:value={originalLanguage}
                  class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                  >{#each LANGUAGE_OPTIONS as item}<option value={item.value}
                      >{item.label}</option
                    >{/each}</select
                ></label
              ><label class="text-sm font-semibold"
                >Transcript style<select
                  bind:value={sttTranscribeStyle}
                  class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                  ><option value="readability">Readable transcript</option
                  ><option value="verbatim">Verbatim · preserve fillers</option
                  ></select
                ></label
              >
            </div>
            <label class="text-sm font-semibold"
              >Phrase hints<textarea
                rows="2"
                bind:value={sttHotwords}
                placeholder="Names and terminology, comma-separated"
                class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
              ></textarea><span class="muted mt-1 block text-xs font-normal"
                >Sent as the provider's phrase list; useful for names and
                specialist terms.</span
              ></label
            >
          {:else}
            <label class="text-sm font-semibold"
              >Model precision<select
                bind:value={sttQuantization}
                class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                ><option value="f16">Full F16</option
                >{#if sttEngine === 'whisper'}<option value="q5_0"
                    >Q5_0 · 1.08 GB</option
                  >{:else if sttEngine === 'parakeet'}<option value="q8_0"
                    >Q8_0 · 745 MB</option
                  ><option value="q5_0">Q5_0 · 541 MB</option><option
                    value="q4_k">Q4_K · 489 MB</option
                  >{:else}<option value="q8_0">Q8_0 · recommended</option
                  ><option value="q4_k">Q4_K</option>{/if}</select
              ><span class="muted mt-1 block text-xs"
                >F16 maximizes fidelity; quantized files reduce download and
                memory use.</span
              ></label
            >
            <div class="grid gap-3 sm:grid-cols-[1fr_7rem]">
              <label class="text-sm font-semibold"
                >Compute backend<select
                  bind:value={sttComputeBackend}
                  class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                  ><option value="auto">Automatic</option><option
                    value="cpu"
                    disabled={!supportsSttCompute('cpu')}>CPU</option
                  ><option value="cuda" disabled={!supportsSttCompute('cuda')}
                    >CUDA</option
                  ><option
                    value="vulkan"
                    disabled={!supportsSttCompute('vulkan')}>Vulkan</option
                  ><option value="metal" disabled={!supportsSttCompute('metal')}
                    >Metal</option
                  ></select
                ><span class="muted mt-1 block text-xs"
                  >Only backends compiled into the installed CrispASR runtime
                  can be forced.</span
                ></label
              ><label class="text-sm font-semibold"
                >Device<input
                  type="number"
                  min="0"
                  disabled={['auto', 'cpu'].includes(sttComputeBackend)}
                  bind:value={sttDevice}
                  class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal disabled:opacity-40"
                /></label
              >
            </div>
            {#if sttEngine === 'moss'}
              <div
                class="rounded-xl border border-[var(--line)] bg-[var(--accent-soft)] p-4"
              >
                <div class="text-sm font-semibold">
                  Native speaker turns with local CTC timing
                </div>
                <p class="muted mt-1 text-xs leading-relaxed">
                  MOSS detects the language and speaker changes. Each turn is
                  then aligned separately with Canary CTC and a small acoustic
                  margin, avoiding long-recording alignment drift.
                </p>
                <div class="mt-3 grid gap-3 sm:grid-cols-2">
                  <label class="flex items-center gap-3 text-xs font-semibold"
                    ><input
                      type="checkbox"
                      bind:checked={mossCtcAlignmentEnabled}
                      class="size-4 accent-[var(--accent)]"
                    /> Word-level CTC alignment</label
                  ><label class="text-xs font-semibold"
                    >CTC padding (s)<input
                      type="number"
                      min="0"
                      max="2"
                      step="0.1"
                      disabled={!mossCtcAlignmentEnabled}
                      bind:value={mossCtcPaddingSeconds}
                      class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal disabled:opacity-40"
                    /></label
                  >
                </div>
              </div>
            {:else}
              <div class="grid gap-3 sm:grid-cols-2">
                <label class="text-sm font-semibold"
                  >Source language<select
                    bind:value={originalLanguage}
                    class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                    >{#each LANGUAGE_OPTIONS as item}<option value={item.value}
                        >{item.label}</option
                      >{/each}</select
                  ></label
                ><label class="text-sm font-semibold"
                  >Language detector<select
                    bind:value={sttLidBackend}
                    class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                    ><option value="whisper">Whisper tiny</option><option
                      value="ecapa">ECAPA (recommended)</option
                    ><option value="silero">Silero</option><option value="off"
                      >Off</option
                    ></select
                  ></label
                >
              </div>
            {/if}
            {#if sttEngine === 'moss'}<label
                class="flex items-start gap-3 text-sm font-semibold"
                ><input
                  type="checkbox"
                  bind:checked={mossVadEnabled}
                  class="mt-0.5 size-4 accent-[var(--accent)]"
                />
                <span
                  >Voice activity detection<span
                    class="muted mt-1 block text-xs font-normal"
                    >Off by default so native speaker tracking keeps the longest
                    context. The normal chunker still seeks low-energy cut
                    points.</span
                  ></span
                ></label
              >{:else}<label
                class="flex items-center gap-3 text-sm font-semibold"
                ><input
                  type="checkbox"
                  bind:checked={vadEnabled}
                  class="size-4 accent-[var(--accent)]"
                /> Voice activity detection</label
              >{/if}
            {#if sttEngine === 'moss' ? mossVadEnabled : vadEnabled}<div
                class="grid grid-cols-2 gap-3"
              >
                <label class="text-xs font-semibold"
                  >VAD model<select
                    bind:value={vadModel}
                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                    ><option value="silero">Silero · general purpose</option
                    ><option value="firered">FireRedVAD · robust</option><option
                      value="marblenet">MarbleNet · compact</option
                    ><option value="whisper-vad"
                      >Whisper VAD · experimental</option
                    ></select
                  ></label
                ><label class="text-xs font-semibold"
                  >VAD threshold<span
                    class="mt-1 grid min-h-10 grid-cols-[1fr_2.5rem] items-center gap-2 rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3"
                    ><input
                      type="range"
                      min="0"
                      max="1"
                      step="0.05"
                      bind:value={vadThreshold}
                      class="w-full accent-[var(--accent)]"
                    /><output class="text-right text-xs font-bold"
                      >{Number(vadThreshold).toFixed(2)}</output
                    ></span
                  ></label
                ><label class="text-xs font-semibold"
                  >Minimum speech (ms)<input
                    type="number"
                    min="0"
                    bind:value={vadMinSpeech}
                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                  /></label
                ><label class="text-xs font-semibold"
                  >Minimum silence (ms)<input
                    type="number"
                    min="0"
                    bind:value={vadMinSilence}
                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                  /></label
                ><label class="text-xs font-semibold"
                  >Maximum speech (s)<input
                    type="number"
                    min="1"
                    bind:value={vadMaxSpeech}
                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                  /></label
                ><label class="text-xs font-semibold"
                  >Speech padding (ms)<input
                    type="number"
                    min="0"
                    bind:value={vadSpeechPad}
                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                  /></label
                >
              </div>{/if}
            <details class="rounded-xl border border-[var(--line)] p-4">
              <summary class="cursor-pointer text-sm font-semibold"
                >Decoder and long-form controls</summary
              >
              <div class="mt-4 grid grid-cols-2 gap-3">
                <label class="text-xs font-semibold"
                  >Threads (0 = automatic)<input
                    type="number"
                    min="0"
                    bind:value={sttThreads}
                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                  /></label
                ><label class="text-xs font-semibold"
                  >Beam size<input
                    type="number"
                    min="1"
                    max="16"
                    bind:value={sttBeamSize}
                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                  /></label
                >{#if sttEngine === 'parakeet'}<label
                    class="text-xs font-semibold"
                    >Parakeet decoder<select
                      bind:value={parakeetDecoder}
                      class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                      ><option value="tdt">TDT greedy / beam</option><option
                        value="maes">MAES beam</option
                      ><option value="ctc">CTC greedy</option></select
                    ></label
                  >{/if}{#if sttEngine === 'moss'}<label
                    class="text-xs font-semibold"
                    >Maximum MOSS context (s)<input
                      type="number"
                      min="30"
                      max="120"
                      step="1"
                      bind:value={mossMaxChunkSeconds}
                      class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                    /></label
                  >{:else}<label class="text-xs font-semibold"
                    >Forced chunk size (s, 0 = default)<input
                      type="number"
                      min="0"
                      step="1"
                      bind:value={sttChunkSeconds}
                      class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                    /></label
                  >{/if}{#if sttEngine === 'moss'}<label
                    class="text-xs font-semibold"
                    >MOSS chunk overlap (s)<input
                      type="number"
                      min="0"
                      step="0.5"
                      bind:value={mossChunkOverlap}
                      class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                    /><span class="muted mt-1 block font-normal"
                      >0 prevents duplicated speech and conflicting speaker IDs
                      at chunk seams.</span
                    ></label
                  >{:else}<label class="text-xs font-semibold"
                    >Chunk overlap (s)<input
                      type="number"
                      min="0"
                      step="0.5"
                      bind:value={sttChunkOverlap}
                      class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                    /></label
                  >{/if}
                ><label class="col-span-2 text-xs font-semibold"
                  >Hotwords<textarea
                    rows="2"
                    bind:value={sttHotwords}
                    placeholder="Names and terminology, comma-separated"
                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                  ></textarea></label
                >
              </div>
              {#if sttEngine === 'moss'}<p class="muted mt-3 text-xs">
                  Pandrator uses the longest safe MOSS window, then lets
                  CrispASR seek the lowest-energy point near its limit. Speaker
                  IDs remain local to a chunk; speaker-change boundaries are
                  preserved.
                </p>{:else}<p class="muted mt-3 text-xs">
                  Parakeet normally preserves full context and handles long
                  recordings internally. Force chunking only for constrained
                  systems or diagnostics.
                </p>{/if}
            </details>
          {/if}
          <div class="rounded-xl border border-[var(--line)] p-4">
            <div class="text-sm font-semibold">
              Readable subtitle composition
            </div>
            <p class="muted mt-1 text-xs">
              Independent from speech blocks and TTS segmentation. Defaults
              allow 48 characters per line for meetings while retaining
              two-line, 20 CPS and 0.833–7 second delivery guidance.
            </p>
            <div class="mt-3 grid grid-cols-2 gap-3">
              <label class="text-xs font-semibold"
                >Characters / line<input
                  type="number"
                  min="20"
                  max="100"
                  bind:value={subtitleChars}
                  class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                /></label
              ><label class="text-xs font-semibold"
                >Lines<input
                  type="number"
                  min="1"
                  max="3"
                  bind:value={subtitleLines}
                  class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                /></label
              ><label class="text-xs font-semibold"
                >Minimum duration (ms)<input
                  type="number"
                  min="250"
                  bind:value={subtitleMinDuration}
                  class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                /></label
              ><label class="text-xs font-semibold"
                >Maximum duration (ms)<input
                  type="number"
                  min="1000"
                  bind:value={subtitleMaxDuration}
                  class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                /></label
              ><label class="text-xs font-semibold"
                >Characters / second<input
                  type="number"
                  min="5"
                  max="40"
                  step="0.5"
                  bind:value={subtitleCps}
                  class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                /></label
              ><label class="text-xs font-semibold"
                >Minimum cue gap (ms)<input
                  type="number"
                  min="0"
                  max="500"
                  bind:value={subtitleMinGap}
                  class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                /></label
              ><label class="text-xs font-semibold"
                >Phrase-break silence (ms)<input
                  type="number"
                  min="100"
                  max="3000"
                  bind:value={subtitlePhraseGap}
                  class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                /></label
              ><label class="text-xs font-semibold"
                >Hard silence boundary (ms)<input
                  type="number"
                  min="250"
                  max="5000"
                  bind:value={subtitleHardGap}
                  class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                /></label
              ><label class="text-xs font-semibold"
                >Sentence boundary threshold<input
                  type="number"
                  min="0.01"
                  max="0.99"
                  step="0.01"
                  bind:value={subtitleSentenceBoundaryThreshold}
                  class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                /></label
              >
            </div>
          </div>
        {/if}
        {#if settingsStage.key === 'correct'}<label
            class="text-sm font-semibold"
            >Correction guidance<textarea
              bind:value={instructions}
              rows="4"
              class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
            ></textarea></label
          >{/if}
        {#if settingsStage.key === 'translate'}<label
            class="text-sm font-semibold"
            >Translate from<select
              bind:value={translationSourceArtifactId}
              required
              class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
              >{#if !translationSourceArtifactId}<option value="" disabled
                  >Choose subtitle input</option
                >{/if}{#each subtitleCatalogItems as item (item.artifact_id)}<option
                  value={item.artifact_id}>{subtitleSourceLabel(item)}</option
                >{/each}</select
            ><span class="muted mt-1 block text-xs font-normal leading-relaxed"
              >Choose the exact transcription or corrected revision. A
              correction no longer silently replaces your selected
              transcription.</span
            ></label
          ><label class="text-sm font-semibold"
            >Translation backend<select
              bind:value={backend}
              class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
              ><option value="llm">LLM</option><option value="deepl"
                >DeepL</option
              ></select
            ></label
          ><label class="text-sm font-semibold"
            >Target language<select
              bind:value={targetLanguage}
              class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
              >{#each LANGUAGE_OPTIONS.filter((item) => item.value !== 'auto') as item}<option
                  value={item.value}>{item.label}</option
                >{/each}</select
            ></label
          >{#if backend === 'llm'}<label class="text-sm font-semibold"
              >Translation guidance<textarea
                bind:value={instructions}
                rows="3"
                class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
              ></textarea></label
            >{/if}{/if}
        {#if settingsStage.key === 'optimize_tts'}
          <fieldset class="rounded-xl border border-[var(--line)] p-4">
            <legend class="px-1 text-sm font-semibold"
              >When should optimization run?</legend
            >
            <div class="mt-2 grid gap-2">
              <label
                class="flex items-start gap-3 rounded-xl bg-[var(--accent-soft)] p-3 text-sm"
                ><input
                  type="radio"
                  bind:group={optimizationTiming}
                  value="document"
                  class="mt-1 accent-[var(--accent)]"
                /><span
                  ><strong class="block"
                    >Before generation · reviewable revision</strong
                  ><span class="muted mt-1 block text-xs"
                    >Process the document's existing narration units, create an
                    editable before-and-after artifact, and review it before
                    TTS.</span
                  ></span
                ></label
              ><label
                class="flex items-start gap-3 rounded-xl bg-[var(--accent-soft)] p-3 text-sm"
                ><input
                  type="radio"
                  bind:group={optimizationTiming}
                  value="generation"
                  class="mt-1 accent-[var(--accent)]"
                /><span
                  ><strong class="block"
                    >During generation · final speech units</strong
                  ><span class="muted mt-1 block text-xs"
                    >Optimize the final synthesis units as generation begins and
                    compare each result in the generation drawer.</span
                  ></span
                ></label
              >
            </div>
          </fieldset>
          <label class="text-sm font-semibold"
            >Speech-planning policy<select
              bind:value={speechOptimizationMode}
              class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
            >
              <option value="guarded">Guarded · safest</option>
              <option value="flexible">Flexible · contextual rewrite</option>
            </select><span
              class="muted mt-2 block text-xs font-normal leading-relaxed"
              >Guarded changes only validated speech candidates. Flexible may
              revise phrasing but must preserve protected text and meaning.</span
            ></label
          >
          <div>
            <label class="text-sm font-semibold"
              >Units per model request{#if optimizationTiming === 'document'}<input
                  type="number"
                  min="1"
                  max="64"
                  bind:value={documentOptimizationBatchSize}
                  class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                />{:else}<input
                  type="number"
                  min="1"
                  max="64"
                  bind:value={optimizationBatchSize}
                  class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                />{/if}</label
            >
            <p class="muted mt-2 text-xs leading-relaxed">
              Use 1 for small local models. Larger values reduce request
              overhead and provide neighboring context; every unit is still
              validated and stored independently.
            </p>
          </div>
        {/if}
        {#if settingsStage.key === 'clean_source'}<label
            class="flex items-start gap-3 rounded-xl border border-[var(--line)] p-4"
            ><input
              type="checkbox"
              bind:checked={agentic}
              class="mt-1 size-4 accent-[var(--accent)]"
            /><span
              ><span class="block text-sm font-semibold"
                >Agentic review loop</span
              ><span class="muted mt-1 block text-xs"
                >Runs focused metadata, navigation, boilerplate,
                repeated-element, and chapter passes. Provider costs may apply.</span
              ></span
            ></label
          >{#if agentic}<label class="text-sm font-semibold"
              >Maximum LLM turns<input
                type="number"
                min="5"
                max="500"
                bind:value={maxIterations}
                class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
              /></label
            >{/if}{/if}
        {#if settingsStage.key === 'prepare_text'}<div
            class="rounded-xl border border-[var(--line)] bg-[var(--accent-soft)] p-4"
          >
            <div class="text-sm font-semibold">
              Provider-independent segmentation
            </div>
            <p class="muted mt-1 text-xs leading-relaxed">
              These controls create editable narration units and pauses. Voice,
              model, and synthesis controls are selected later in Generate
              audio.
            </p>
          </div>
          <div class="grid gap-3 sm:grid-cols-2">
            <label
              class="flex items-center gap-3 rounded-xl border border-[var(--line)] p-3 text-sm font-semibold"
              ><input
                type="checkbox"
                bind:checked={splitSentences}
                class="size-4 accent-[var(--accent)]"
              /> Split long sentences</label
            ><label
              class="flex items-center gap-3 rounded-xl border border-[var(--line)] p-3 text-sm font-semibold"
              ><input
                type="checkbox"
                bind:checked={appendSentences}
                class="size-4 accent-[var(--accent)]"
              /> Join short sentences</label
            ><label class="text-sm font-semibold"
              >Maximum segment length<input
                type="number"
                min="20"
                max="2000"
                bind:value={maxSentenceLength}
                class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
              /></label
            ><label
              class="flex items-center gap-3 rounded-xl border border-[var(--line)] p-3 text-sm font-semibold"
              ><input
                type="checkbox"
                bind:checked={nemoNormalization}
                class="size-4 accent-[var(--accent)]"
              /> Deterministic normalization</label
            >
          </div>
          <details class="rounded-xl border border-[var(--line)] p-4">
            <summary class="cursor-pointer text-sm font-semibold"
              >Advanced text cleanup</summary
            >
            <div class="mt-4 grid gap-3 sm:grid-cols-2">
              <label class="flex items-center gap-3 text-sm"
                ><input
                  type="checkbox"
                  bind:checked={normalizeAllCaps}
                  class="size-4 accent-[var(--accent)]"
                /> Normalize all-caps text</label
              ><label class="flex items-center gap-3 text-sm"
                ><input
                  type="checkbox"
                  bind:checked={removeDiacritics}
                  class="size-4 accent-[var(--accent)]"
                /> Remove diacritics</label
              ><label class="flex items-center gap-3 text-sm"
                ><input
                  type="checkbox"
                  bind:checked={removeQuotationMarks}
                  class="size-4 accent-[var(--accent)]"
                /> Remove quotation marks</label
              >
            </div>
          </details>{/if}
        {#if settingsStage.key === 'generate_audio'}
          <div class="grid gap-2">
            <div class="flex items-center justify-between gap-3">
              <span class="text-sm font-semibold">TTS service</span><button
                type="button"
                onclick={refreshSpeechServices}
                disabled={refreshingTtsServices}
                class="flex items-center gap-2 rounded-lg border border-[var(--line)] px-3 py-2 text-xs font-semibold disabled:opacity-50"
                ><RefreshCw
                  size={14}
                  class={refreshingTtsServices ? 'animate-spin' : ''}
                /> Refresh service availability</button
              >
            </div>
            <select
              value={ttsService}
              onchange={(event) => chooseTtsService(event.currentTarget.value)}
              class="w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
              aria-label="TTS service"
              >{#if availableTtsServices.length}<optgroup label="Available"
                  >{#each availableTtsServices as service}<option
                      value={service.id}>{service.name} · available</option
                    >{/each}</optgroup
                >{/if}{#if unavailableTtsServices.length}<optgroup
                  label="Unavailable"
                  >{#each unavailableTtsServices as service}<option
                      value={service.id}
                      disabled
                      class="text-[var(--muted)]"
                      >{service.name} · unavailable</option
                    >{/each}</optgroup
                >{/if}</select
            >{#if selectedTtsService && !selectedTtsServiceAvailable}<span
                class="text-xs font-semibold text-red-500"
                role="status"
                >{selectedTtsService.availability_reason ||
                  `${selectedTtsService.name} is unavailable. Refresh availability or choose another provider.`}</span
              >{/if}<span class="muted text-xs"
              >Available means Pandrator can use the provider. Cloud providers
              can be available without a local process.</span
            >
          </div>
          <div class="flex flex-wrap items-center justify-between gap-3">
            <p class="muted text-xs">
              The preferred available service is selected automatically. Your
              selected unavailable service remains visible until it is ready.
            </p>
            <button
              type="button"
              onclick={openTtsServices}
              class="text-xs font-semibold text-[var(--accent)]"
              >Manage services</button
            >
          </div>
          <label class="text-sm font-semibold"
            >{selectedTtsServiceId === 'kobold_qwen'
              ? 'Voice type'
              : 'Model'}<select
              value={ttsModel}
              onchange={(event) => chooseTtsModel(event.currentTarget.value)}
              class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
              aria-label="TTS model"
              >{#each ttsModels as item}<option value={item}>{item}</option
                >{/each}</select
            ></label
          >
          {#if supportsXttsModelUpload}
            <section
              class="rounded-xl border border-[var(--line)] bg-[var(--accent-soft)] p-4"
              aria-labelledby="xtts-model-upload-title"
            >
              <div class="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h3
                    id="xtts-model-upload-title"
                    class="text-sm font-semibold"
                  >
                    XTTS model management
                  </h3>
                  <p class="muted mt-1 max-w-xl text-xs leading-relaxed">
                    Select any listed model for this generation. Local complete
                    bundles can be removed here; the built-in XTTS model is
                    protected. Add a fine-tuned model with the four-file bundle
                    below—Pandrator installs it in stable user data.
                  </p>
                </div>
                <button
                  type="button"
                  onclick={loadXttsModels}
                  disabled={xttsModelsLoading}
                  class="flex items-center gap-2 rounded-lg border border-[var(--line)] px-3 py-2 text-xs font-semibold disabled:opacity-50"
                  ><RefreshCw
                    size={14}
                    class={xttsModelsLoading ? 'animate-spin' : ''}
                  /> Refresh models</button
                >
              </div>
              {#if xttsModelsCompatibility}<p
                  class="mt-3 rounded-lg border border-amber-300 bg-amber-50 p-3 text-xs text-amber-950"
                  role="status"
                >
                  {xttsModelsCompatibility}
                </p>{/if}
              {#if xttsModels.length}<div
                  class="mt-3 overflow-hidden rounded-lg border border-[var(--line)] bg-[var(--paper)]"
                >
                  {#each xttsModels as model}<div
                      class="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--line)] p-3 last:border-b-0"
                    >
                      <div class="min-w-0">
                        <button
                          type="button"
                          onclick={() => chooseTtsModel(model.id)}
                          class="max-w-full truncate text-left text-xs font-semibold text-[var(--accent)]"
                          aria-label={`Select XTTS model ${model.id}`}
                          >{model.id}</button
                        >
                        <p class="muted mt-1 text-xs">
                          {model.is_default
                            ? 'Built-in protected model'
                            : model.removable
                              ? 'Local model bundle'
                              : model.lifecycle_supported
                                ? 'Local model (not removable)'
                                : 'Model lifecycle requires an XTTS update'}
                        </p>
                        {#if model.id === ttsModel}<span
                            class="mt-1 inline-block rounded bg-[var(--accent-soft)] px-2 py-0.5 text-[11px] font-semibold"
                            >Selected for this generation</span
                          >{/if}
                      </div>
                      <div class="flex items-center gap-2">
                        <button
                          type="button"
                          onclick={() => chooseTtsModel(model.id)}
                          class="rounded-lg border border-[var(--line)] px-3 py-2 text-xs font-semibold"
                          >Select</button
                        >{#if model.removable && xttsModelsLifecycleSupported}<button
                            type="button"
                            onclick={() => removeXttsModel(model)}
                            disabled={Boolean(deletingXttsModelId)}
                            class="rounded-lg border border-red-300 px-3 py-2 text-xs font-semibold text-red-700 disabled:opacity-50"
                            >{deletingXttsModelId === model.id
                              ? 'Removing…'
                              : 'Remove'}</button
                          >{:else if !model.is_default}<span
                            class="muted text-xs">Removal unavailable</span
                          >{/if}
                      </div>
                    </div>{/each}
                </div>{/if}
              <div class="mt-3 grid gap-3 sm:grid-cols-2">
                <label class="text-xs font-semibold"
                  >Model ID<input
                    bind:value={xttsModelId}
                    placeholder="custom/my-narrator-v1"
                    disabled={uploadingXttsModel}
                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-sm font-normal"
                  /></label
                ><label class="text-xs font-semibold"
                  >Bundle files<input
                    type="file"
                    multiple
                    accept=".json,.pth"
                    disabled={uploadingXttsModel}
                    onchange={(event) =>
                      chooseXttsModelFiles(event.currentTarget.files)}
                    class="mt-1 block w-full text-sm font-normal"
                  /></label
                >
              </div>
              <p class="muted mt-3 text-xs">
                Required: <code>config.json</code>, <code>model.pth</code>,
                <code>speakers_xtts.pth</code>, and <code>vocab.json</code>.
                {#if xttsModelFiles.length}
                  Selected: {xttsModelFiles
                    .map((file) => file.name)
                    .join(', ')}.
                {/if}
              </p>
              <div class="mt-3 flex flex-wrap items-center gap-3">
                <button
                  type="button"
                  onclick={uploadXttsModel}
                  disabled={uploadingXttsModel || !selectedTtsServiceAvailable}
                  class="flex items-center gap-2 rounded-lg bg-[var(--accent)] px-3 py-2 text-xs font-semibold text-white disabled:opacity-50"
                  >{#if uploadingXttsModel}<LoaderCircle
                      class="animate-spin"
                      size={14}
                    /> Uploading model…{:else}<CloudUpload size={14} /> Upload and
                    select{/if}</button
                >
                {#if xttsModelUploadError}<p
                    class="basis-full rounded-lg border border-red-300 bg-red-50 p-3 text-xs text-red-800"
                    role="alert"
                  >
                    {xttsModelUploadError}
                  </p>{/if}
                {#if xttsModelUploadMessage && !uploadingXttsModel}<span
                    class="text-xs"
                    role="status"
                    aria-live="polite">{xttsModelUploadMessage}</span
                  >{/if}
              </div>
              {#if uploadingXttsModel}<div class="mt-3">
                  <div
                    class="mb-1.5 flex items-center justify-between gap-3 text-xs"
                  >
                    <span class="muted"
                      >{xttsModelUploadPhase === 'installing'
                        ? 'Upload transferred; Pandrator is installing the model…'
                        : 'Uploading the XTTS model…'}</span
                    ><span class="muted tabular-nums"
                      >{Math.round(xttsModelUploadProgress * 100)}%</span
                    >
                  </div>
                  <progress
                    class="h-2 w-full overflow-hidden rounded-full accent-[var(--accent)]"
                    max="1"
                    value={xttsModelUploadProgress}
                    aria-label="XTTS model upload progress"
                    >{Math.round(xttsModelUploadProgress * 100)}%</progress
                  >
                </div>{/if}
            </section>
          {/if}
          <label class="text-sm font-semibold"
            >Speech language<select
              bind:value={targetLanguage}
              class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
              >{#each ttsLanguages.length ? ttsLanguages : LANGUAGE_OPTIONS.filter((item) => item.value !== 'auto') as item}<option
                  value={item.value}>{item.label}</option
                >{/each}</select
            ></label
          >
          {#if supportsPrebuiltVoices || showClonedVoices}
            <label class="text-sm font-semibold"
              >Voice<select
                bind:value={voiceName}
                class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                >{#if !showClonedVoices}<option value="">Service default</option
                  >{/if}{#if supportsPrebuiltVoices}<optgroup
                    label={`${LANGUAGE_OPTIONS.find((item) => item.value === targetLanguage)?.label ?? targetLanguage} · pre-built voices`}
                    >{#each filteredPrebuiltVoices as voice}<option
                        value={voice.id}
                        >{voice.name}{voice.gender
                          ? ` · ${voice.gender}`
                          : ''}</option
                      >{/each}</optgroup
                  >{/if}{#if showClonedVoices}{#each clonedVoiceGroups as group}
                    <optgroup label={group.label}
                      >{#each group.voices as voice}<option value={voice.id}
                          >{voice.name}</option
                        >{/each}</optgroup
                    >{/each}{/if}</select
              ></label
            >
            <div class="flex flex-wrap items-center justify-between gap-3">
              <p class="muted text-xs">
                {showClonedVoices
                  ? audioCppLinkedReferences
                    ? 'Linked local voices can be selected above. Qwen benefits from a reviewed transcript; OmniVoice requires one.'
                    : 'Provider-ready voices can be selected above. Local voices can be prepared in one click below.'
                  : 'Only voices supported by the selected model are shown.'}
              </p>
              {#if showClonedVoices}<button
                  type="button"
                  onclick={() =>
                    openVoiceLibrary('references', selectedTtsServiceId)}
                  class="flex items-center gap-1.5 text-xs font-semibold text-[var(--accent)]"
                  ><Library size={14} /> Manage Voice Library</button
                >{:else}<button
                  type="button"
                  onclick={() => openVoiceLibrary('prebuilt')}
                  class="flex items-center gap-1.5 text-xs font-semibold text-[var(--accent)]"
                  ><Library size={14} /> Browse pre-built voices</button
                >{/if}
            </div>
            {#if showClonedVoices}
              <section
                class="rounded-2xl border border-[var(--line)] bg-[var(--paper)] p-4"
              >
                <div class="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h4 class="text-sm font-semibold">
                      Available from your Voice Library
                    </h4>
                    <p class="muted mt-1 text-xs">
                      {audioCppLinkedReferences
                        ? 'Choose a local voice; Pandrator links its newest sample without making a provider-side copy, then selects it automatically.'
                        : 'Choose a local voice; Pandrator uploads or refreshes only that voice, then selects it automatically.'}
                    </p>
                  </div>
                  <button
                    type="button"
                    onclick={() =>
                      openVoiceLibrary('references', selectedTtsServiceId)}
                    class="text-xs font-semibold text-[var(--accent)]"
                    >Add a new voice</button
                  >
                </div>
                {#if localVoiceChoices.length}
                  <div class="mt-3 grid gap-2 sm:grid-cols-2">
                    {#each localVoiceChoices as choice}
                      {@const ready =
                        choice.registration?.status === 'ready' &&
                        Boolean(choice.registration.voice_id)}
                      {@const preparing =
                        publishingLibraryVoiceId === choice.voice.id}
                      <article
                        class="flex min-w-0 items-center gap-3 rounded-xl border border-[var(--line)] px-3 py-3"
                      >
                        <div class="min-w-0 flex-1">
                          <div class="truncate text-sm font-semibold">
                            {choice.voice.name}
                          </div>
                          <div class="muted mt-0.5 text-xs">
                            {choice.voice.language || 'Language not set'} · {ready
                              ? audioCppLinkedReferences
                                ? 'linked to newest sample'
                                : 'ready in provider'
                              : choice.registration?.status === 'stale'
                                ? audioCppLinkedReferences
                                  ? 'link needs refresh'
                                  : 'provider copy needs update'
                                : !choice.hasSample
                                  ? 'sample needed'
                                  : choice.needsTranscript
                                    ? 'reviewed transcript needed'
                                    : audioCppLinkedReferences
                                      ? 'ready to link'
                                      : 'ready to upload'}
                          </div>
                        </div>
                        <button
                          type="button"
                          onclick={() => useLibraryVoice(choice.voice)}
                          disabled={preparing ||
                            (Boolean(publishingLibraryVoiceId) && !preparing) ||
                            (selectedTtsService?.available === false &&
                              !audioCppLinkedReferences &&
                              !ready &&
                              choice.hasSample &&
                              !choice.needsTranscript)}
                          class:btn-primary={!ready}
                          class="btn shrink-0 disabled:opacity-40"
                        >
                          {#if preparing}<LoaderCircle
                              size={15}
                              class="animate-spin"
                            />{:else if ready}<CheckCircle2
                              size={15}
                            />{:else if audioCppLinkedReferences}<Link2
                              size={15}
                            />{:else}<CloudUpload size={15} />{/if}
                          {preparing
                            ? audioCppLinkedReferences
                              ? 'Linking…'
                              : 'Uploading…'
                            : ready
                              ? voiceName === choice.registration?.voice_id
                                ? 'Selected'
                                : 'Use'
                              : !choice.hasSample
                                ? 'Add sample'
                                : choice.needsTranscript
                                  ? 'Review text'
                                  : choice.registration?.status === 'stale'
                                    ? audioCppLinkedReferences
                                      ? 'Refresh & use'
                                      : 'Update & use'
                                    : audioCppLinkedReferences
                                      ? 'Link & use'
                                      : 'Upload & use'}
                        </button>
                      </article>
                    {/each}
                  </div>
                {:else}
                  <p
                    class="muted mt-3 rounded-xl border border-dashed border-[var(--line)] p-4 text-sm"
                  >
                    No local voices yet. Add one once, then reuse it across
                    compatible speech services.
                  </p>
                {/if}
                {#if voicePublishStatus}<p
                    class="mt-3 text-xs font-semibold text-[var(--accent)]"
                    role="status"
                  >
                    {voicePublishStatus}
                  </p>{/if}
              </section>
            {/if}
          {/if}
          {#if supportsGenerationPrompt}
            <label class="text-sm font-semibold"
              >Speech direction<textarea
                bind:value={generationPrompt}
                rows="4"
                placeholder="For example: Warm, intimate narration with measured pacing and subtle excitement."
                class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
              ></textarea><span class="muted mt-2 block text-xs"
                >Sent with every segment as performance guidance. It does not
                rewrite the transcript and should not be spoken aloud.</span
              ></label
            >
          {:else if generationPromptModels.length}
            <p class="muted rounded-xl bg-[var(--accent-soft)] p-3 text-xs">
              {ttsModel || 'This model'} does not accept speech-direction prompts.
              Choose an instruction-capable model to add one.
            </p>
          {/if}
          {#if supportsBatchSynthesis}
            <div class="rounded-xl border border-[var(--line)] p-4">
              <div class="text-sm font-semibold">
                Streaming generation batches
              </div>
              <p class="muted mt-1 text-xs leading-relaxed">
                Keep the speech engine continuously occupied while completed
                segments become playable one by one. Use 1 to disable batching.
              </p>
              <label class="mt-3 block text-xs font-semibold"
                >Segments per batch<input
                  type="number"
                  min="1"
                  max={maximumTtsBatchSize}
                  bind:value={ttsBatchSize}
                  class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                /></label
              >
            </div>
          {/if}
          {#if session.workflow_kind !== 'audiobook'}<div
              class="rounded-xl border border-[var(--line)] p-4"
            >
              <div class="text-sm font-semibold">Speech blocks for dubbing</div>
              <p class="muted mt-1 text-xs">
                Pandrator first reconstructs unfinished same-speaker sentences,
                then splits at balanced linguistic boundaries and optionally
                packs nearby complete utterances. These TTS chunks are
                independent from the final subtitle layout.
              </p>
              <div class="mt-3 grid grid-cols-2 gap-3">
                <label class="text-xs font-semibold"
                  >Preferred minimum split size<input
                    type="number"
                    min="1"
                    bind:value={speechBlockMinChars}
                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                  /></label
                ><label class="text-xs font-semibold"
                  >Maximum characters<input
                    type="number"
                    min="1"
                    bind:value={speechBlockMaxChars}
                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                  /></label
                ><label class="text-xs font-semibold"
                  >Merge gap (ms)<input
                    type="number"
                    min="0"
                    bind:value={speechBlockMergeThreshold}
                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                  /></label
                ><label class="text-xs font-semibold"
                  >Unfinished-sentence pause (ms)<input
                    type="number"
                    min="0"
                    bind:value={speechBlockContinuationThreshold}
                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                  /></label
                ><label class="text-xs font-semibold"
                  >Maximum silence inside a TTS chunk (ms)<input
                    type="number"
                    min="0"
                    bind:value={speechBlockMaxInternalGap}
                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                  /></label
                >
              </div>
            </div>{/if}
        {/if}
        {#if settingsStage.key === 'export'}
          <label class="text-sm font-semibold"
            >Export target<select
              bind:value={exportMode}
              class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
              >{#if session.workflow_kind !== 'subtitles'}<option value="media"
                  >Rendered video / media</option
                >{/if}<option value="subtitles">Subtitle file</option><option
                value="text">Concatenated plain text</option
              ></select
            ></label
          >
          {#if exportMode === 'media'}
            <label class="text-sm font-semibold"
              >Audio<select
                bind:value={audioMode}
                class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                ><option value="mixed"
                  >Mix source and dubbing (recommended)</option
                ><option value="preserve">Preserve source audio</option><option
                  value="dubbing_only">Dubbing only</option
                ></select
              ></label
            ><label class="text-sm font-semibold"
              >Subtitles<select
                bind:value={subtitleMode}
                class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                ><option value="none">None</option><option value="soft"
                  >Injected soft tracks</option
                ><option value="burned">Burned subtitles</option></select
              ></label
            >
          {:else if exportMode === 'subtitles'}
            <label class="text-sm font-semibold"
              >Subtitle format<select
                bind:value={subtitleFormat}
                class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                ><option value="srt">SubRip (.srt)</option><option value="vtt"
                  >WebVTT (.vtt)</option
                ></select
              ></label
            >
          {:else}<p
              class="muted rounded-xl bg-[var(--accent-soft)] p-3 text-xs"
            >
              Cue timestamps and numbering are removed and the selected subtitle
              text is joined into one plain-text document.
            </p>{/if}
          {#if exportMode !== 'media' || subtitleMode !== 'none'}<label
              class="text-sm font-semibold"
              >Subtitle tracks<select
                bind:value={subtitleSelection}
                class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                ><option value="source">Source / corrected</option><option
                  value="translation">Translation</option
                ><option value="dual">Source and translation</option></select
              ></label
            >{/if}
          <div class="rounded-xl border border-[var(--line)] p-4">
            <div class="text-sm font-semibold">Final subtitle layout</div>
            <p class="muted mt-1 text-xs">
              Applied only to derived export subtitles; source and reviewed
              revisions remain unchanged.
            </p>
            <div class="mt-3 grid grid-cols-2 gap-3">
              <label class="text-xs font-semibold"
                >Characters / line<input
                  type="number"
                  min="20"
                  max="100"
                  bind:value={subtitleChars}
                  class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                /></label
              ><label class="text-xs font-semibold"
                >Lines<input
                  type="number"
                  min="1"
                  max="3"
                  bind:value={subtitleLines}
                  class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                /></label
              ><label class="text-xs font-semibold"
                >Minimum duration (ms)<input
                  type="number"
                  min="250"
                  bind:value={subtitleMinDuration}
                  class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                /></label
              ><label class="text-xs font-semibold"
                >Maximum duration (ms)<input
                  type="number"
                  min="1000"
                  bind:value={subtitleMaxDuration}
                  class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                /></label
              ><label class="text-xs font-semibold"
                >Characters / second<input
                  type="number"
                  min="5"
                  max="40"
                  step="0.5"
                  bind:value={subtitleCps}
                  class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                /></label
              ><label class="text-xs font-semibold"
                >Minimum cue gap (ms)<input
                  type="number"
                  min="0"
                  max="500"
                  bind:value={subtitleMinGap}
                  class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                /></label
              ><label class="text-xs font-semibold"
                >Phrase-break silence (ms)<input
                  type="number"
                  min="100"
                  max="3000"
                  bind:value={subtitlePhraseGap}
                  class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                /></label
              ><label class="text-xs font-semibold"
                >Hard silence boundary (ms)<input
                  type="number"
                  min="250"
                  max="5000"
                  bind:value={subtitleHardGap}
                  class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                /></label
              ><label class="text-xs font-semibold"
                >Sentence boundary threshold<input
                  type="number"
                  min="0.01"
                  max="0.99"
                  step="0.01"
                  bind:value={subtitleSentenceBoundaryThreshold}
                  class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                /></label
              >
            </div>
          </div>
        {/if}
      </div>
      {#if stageMessage}<p
          role="status"
          class="mt-5 rounded-xl bg-[var(--accent-soft)] p-3 text-xs"
        >
          {stageMessage}
        </p>{/if}
      <div class="mt-7 flex flex-wrap justify-end gap-3">
        <button
          onclick={openFullSettingsFromStage}
          class="mr-auto rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold"
          >All {sectionDisplay(stageSection(settingsStage.key))} settings</button
        ><button
          onclick={revertStageToDefaults}
          class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold"
          ><RotateCcw size={15} /> Revert to defaults</button
        ><button
          onclick={() => saveSettings('defaults')}
          disabled={Boolean(publishingLibraryVoiceId) ||
            (settingsStage.key === 'generate_audio' &&
              !selectedTtsServiceAvailable)}
          class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold disabled:opacity-40"
          ><Save size={15} /> Save as defaults</button
        ><button
          onclick={() => (settingsStage = null)}
          class="rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold"
          >Cancel</button
        ><button
          onclick={() => saveSettings('session')}
          disabled={Boolean(publishingLibraryVoiceId) ||
            (settingsStage.key === 'translate' &&
              !translationSourceArtifactId) ||
            (settingsStage.key === 'generate_audio' &&
              !selectedTtsServiceAvailable)}
          class="rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-40"
          >Save settings</button
        >
      </div>
    </div>
  </div>
{/if}

{#if pdfSource && PdfEditorComponent}<PdfEditorComponent
    sessionId={session.id}
    source={pdfSource}
    onclose={() => (pdfSource = null)}
  />{/if}
{#if reviewArtifactId}<SubtitleReview
    sessionId={session.id}
    primaryArtifactId={reviewArtifactId}
    sourceAudioArtifactId={snapshot?.sources[0]?.id}
    onclose={() => (reviewArtifactId = '')}
    onsaved={load}
  />{/if}
{#if preview}<ArtifactPreview
    artifact={preview}
    onclose={() => (preview = null)}
  />{/if}
{#if forkCheckpoint}<SessionForkDialog
    {session}
    stage={forkCheckpoint.stage}
    artifactId={forkCheckpoint.artifactId}
    onclose={() => (forkCheckpoint = null)}
  />{/if}
{#if optimizationReviewArtifactId}<TextOptimizationReview
    artifactId={optimizationReviewArtifactId}
    onclose={() => (optimizationReviewArtifactId = '')}
    onsaved={load}
  />{/if}
{#if fullSettingsSection && SettingsModalComponent}<SettingsModalComponent
    sessionId={session.id}
    section={fullSettingsSection}
    title={`${sectionDisplay(fullSettingsSection)} settings`}
    description="These settings are saved as session overrides and inherited by future runs."
    initialOverride={fullSettingsDraft ?? {}}
    onpersisted={syncStageAfterFullSettings}
    onclose={closeFullSettings}
  />{/if}
{#if ttsServicesOpen && TtsServicesModalComponent}<TtsServicesModalComponent
    onclose={async () => {
      ttsServicesOpen = false;
      await loadSpeechCatalogues(true, true);
    }}
  />{/if}
{#if voiceLibraryOpen && VoiceLibraryModalComponent}<VoiceLibraryModalComponent
    initialView={voiceLibraryView}
    initialService={voiceLibraryService}
    initialVoice={voiceLibraryInitialVoice}
    onvoicepublished={usePublishedVoice}
    onclose={async () => {
      voiceLibraryOpen = false;
      await loadSpeechCatalogues(true, true);
    }}
  />{/if}
<GuidedTour
  tourId="workflow"
  steps={workflowTourSteps}
  bind:open={workflowTour}
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
