<script lang="ts">
  import { modalFocus } from './modal-focus';
  import { LoaderCircle, RotateCcw, Save, X } from '@lucide/svelte';
  import ParameterLabel from './ParameterLabel.svelte';
  import TranscriptionStageSettings from './TranscriptionStageSettings.svelte';
  import SourcePassageSettings from './SourcePassageSettings.svelte';
  import GenerationStageSettings from './GenerationStageSettings.svelte';
  import SubtitleLimitsSummary from './SubtitleLimitsSummary.svelte';
  import {
    TtsSettingsSelection,
    GenerationSettingsActivity
  } from './tts-settings-selection.svelte';
  import { StageSettingsDraft } from './stage-settings-draft.svelte';
  import {
    sttLanguageProblem,
    qwenTimedLanguageProblem
  } from './stt-language-policy';
  import { preferredTtsService } from './tts-provider-policy';
  import { errorMessage } from './errors';
  import { jobApi, sessionApi } from './domain-api';
  import { TtsCatalogueState } from './tts-catalogue-state.svelte';
  import { speechRecognitionApi, voiceApi } from './admin-api';
  import type {
    OutcomePlan,
    RuntimeCapabilities,
    SessionRecord,
    SettingsPayload,
    SttCatalogue,
    SubtitleReviewCatalogItem,
    TtsService,
    VoiceRecord,
    XttsModel,
    WorkflowStage
  } from './api-models';
  import { appState } from './app-state.svelte';
  import { LANGUAGE_OPTIONS } from './settings-fields';
  import { onDestroy, untrack } from 'svelte';
  import { type WorkflowStore } from './workflow-store.svelte';
  import type SettingsModal from './SettingsModal.svelte';
  import type TtsServicesModal from './TtsServicesModal.svelte';
  import type VoiceLibraryModal from './VoiceLibraryModal.svelte';
  import { SourcePassageSettingsState } from './source-passage-settings.svelte';

  const draft = new StageSettingsDraft();

  let {
    session,
    outcome,
    workflowStore,
    stageSettings = $bindable({}),
    error = $bindable(''),
    onrefresh: load,
    onrun: run,
    ontransform: updateOutcomeTransformations
  }: {
    session: SessionRecord;
    outcome: OutcomePlan;
    workflowStore: WorkflowStore;
    stageSettings: Record<string, Record<string, unknown>>;
    error: string;
    onrefresh: (options?: { initial?: boolean }) => Promise<void>;
    onrun: (stage: Stage) => Promise<void>;
    ontransform: (
      changes: Record<string, boolean>,
      expectedOutcome: OutcomePlan
    ) => Promise<void>;
  } = $props();

  type Stage = WorkflowStage;

  const snapshot = $derived(workflowStore.snapshot);

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

  let capabilities = $state<RuntimeCapabilities>({});

  const speechCatalogues = new TtsCatalogueState();

  const ttsSelection = new TtsSettingsSelection(draft, speechCatalogues);

  const activity = new GenerationSettingsActivity();

  $effect(() => {
    // A changed target needs a fresh review before applying the switch.
    void draft.ttsService;
    void draft.ttsModel;
    void draft.voiceName;
    ttsSelection.ttsSwitchReviewed = false;
  });

  let sttCatalogue = $state<SttCatalogue>({
    services: [],
    profiles: [],
    value: {},
    revision: 0
  });

  let llmModels = $state<
    {
      value: string;
      label: string;
      isDefault: boolean;
      defaultReasoningEffort: string;
    }[]
  >([]);

  let settingsStage = $state<Stage | null>(null);

  let settingsLoading = $state(false);

  let settingsSaving = $state(false);

  let settingsMutation = 0;

  let settingsBases: Record<string, SettingsPayload> = {};

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

  const sectionDisplay = (section: string) =>
    (({ stt: 'STT', tts: 'TTS', rvc: 'RVC' }) as Record<string, string>)[
      section
    ] ?? section.replaceAll('_', ' ');

  let subtitleSettingsPayload = $state<SettingsPayload | null>(null);

  const sourcePassages = new SourcePassageSettingsState(
    untrack(() => session.id),
    () => load({ initial: false })
  );

  const passageSourceArtifactId = $derived(
    snapshot?.stages.find((stage) => stage.key === 'transcribe')
      ?.selected_artifact_id ??
      snapshot?.subtitle_source?.subtitle_artifact_id ??
      ''
  );

  $effect(() => {
    if (
      settingsStage &&
      ['transcribe', 'correct'].includes(settingsStage.key)
    ) {
      sourcePassages.setArtifact(passageSourceArtifactId);
    } else {
      sourcePassages.close();
    }
  });

  onDestroy(() => sourcePassages.close());

  let subtitleCatalogItems = $state<SubtitleReviewCatalogItem[]>([]);

  let settingsOpening = 0;

  let ttsSelectionRequest = 0;

  let speechRefreshRequest = 0;

  let disposed = false;

  let llmModelsLoaded = false;

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

  function subtitleSourceLabel(item: SubtitleReviewCatalogItem) {
    const stage =
      item.stage === 'correction' ? 'Corrected subtitles' : 'Transcription';
    const language = item.language ? ` · ${item.language}` : '';
    const state = item.state === 'current' ? '' : ' · earlier result';
    return `${stage} v${item.version}${language}${state}`;
  }

  const normalizeSttEngine = (value: unknown) => {
    const normalized = String(value ?? '').toLowerCase();
    if (['auto', 'automatic', 'parakeet_preferred'].includes(normalized))
      return 'auto';
    if (sttCatalogue.services.some((service) => service.id === normalized))
      return normalized;
    if (normalized.includes('azure') && normalized.includes('mai'))
      return 'azure_mai_transcribe_1_5';
    if (normalized.includes('qwen')) return 'qwen3';
    if (normalized.includes('moss')) return 'moss';
    return normalized.includes('parakeet') ? 'parakeet' : 'whisper';
  };

  const sttLanguageIssue = $derived(
    sttLanguageProblem(capabilities, draft.sttEngine, draft.originalLanguage) ||
      (draft.sttEngine === 'qwen3'
        ? qwenTimedLanguageProblem(capabilities, draft.originalLanguage)
        : '')
  );

  export async function generationServiceProblem() {
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
    configuredServiceId ||= draft.ttsService;
    const configured = speechCatalogues.catalogue.services.find((service) =>
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

  export async function openSettings(stage: Stage) {
    const opening = ++settingsOpening;
    error = '';
    settingsMutation++;
    settingsSaving = false;
    settingsBases = {};
    invalidateSpeechRequests();
    const isCurrent = () => !disposed && opening === settingsOpening;
    ttsSelection.ttsSwitchSource = null;
    ttsSelection.ttsSwitchReviewed = false;
    if (stage.key === 'export' && session.workflow_kind === 'audiobook') {
      await openFullSettings('output');
      return;
    }
    settingsStage = stage;
    const passageSettingsLoad = ['transcribe', 'correct'].includes(stage.key)
      ? sourcePassages.open(passageSourceArtifactId)
      : Promise.resolve();
    settingsLoading = true;
    stageMessage = '';
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
    try {
      await Promise.all(dependencies);
      if (!isCurrent()) return;
    } catch (caught) {
      if (!isCurrent()) return;
      settingsLoading = false;
      error = errorMessage(caught);
      return;
    }
    let saved = stageSettings[stage.key] ?? {};
    let storedSettings: SettingsPayload | null = null;
    try {
      const stored = await sessionApi.settings(
        session.id,
        stageSection(stage.key)
      );
      if (!isCurrent()) return;
      storedSettings = stored;
      saved = { ...saved, ...stored.effective };
      settingsBases[stageSection(stage.key)] = stored;
      stageSettings[stage.key] = saved;
    } catch {
      /* use stage-local values */
    }
    if (!isCurrent()) return;
    draft.targetLanguage = String(
      (stage.key === 'generate_audio'
        ? saved.language
        : saved.target_language) ??
        session.target_language ??
        (session.source_language === 'auto' ? 'en' : session.source_language) ??
        'en'
    );
    draft.originalLanguage = String(
      (stage.key === 'transcribe'
        ? saved.stt_language
        : saved.original_language) ??
        session.source_language ??
        'auto'
    );
    draft.model =
      String(
        saved.model_name ??
          saved.tts_optimization_model ??
          saved[`${stage.key}_model`] ??
          ''
      ).trim() || 'default';
    draft.reasoningEffort = String(saved.reasoning_effort ?? '');
    draft.webResearchEnabled = Boolean(saved.web_research_enabled ?? false);
    draft.webResearchModel = String(saved.web_research_model_name ?? '');
    draft.webResearchMode =
      String(saved.web_research_mode ?? 'global') === 'per_chunk'
        ? 'per_chunk'
        : 'global';
    draft.webResearchContextFraction = Number(
      saved.web_research_context_fraction ?? 0.8
    );
    draft.backend = String(saved.backend ?? saved.translation_backend ?? 'llm');
    if (stage.key === 'translate') {
      const selectedCorrection = snapshot?.stages.find(
        (item) => item.key === 'correct'
      )?.selected_artifact_id;
      const selectedTranscription = snapshot?.stages.find(
        (item) => item.key === 'transcribe'
      )?.selected_artifact_id;
      const requestedSource = String(saved.source_artifact_id ?? '');
      draft.translationSourceArtifactId =
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
      capabilities?.stt?.default_engine ?? 'auto'
    );
    draft.sttEngine = normalizeSttEngine(
      hasSavedSttModel
        ? (saved.stt_engine ?? saved.stt_backend)
        : preferredSttEngine
    );
    draft.qwenAsrModel = String(saved.qwen_asr_model ?? 'qwen3_asr_0_6b');
    draft.qwenChunkMode = String(saved.qwen_asr_chunk_mode ?? 'auto');
    draft.transcriptionVocalIsolation = String(
      saved.transcription_vocal_isolation ?? 'off'
    );
    const savedCaptionAlignmentMethod = String(
      saved.caption_alignment_method ?? 'ctc'
    );
    draft.captionAlignmentMethod = ['ctc', 'ctc_asr_fallback', 'asr'].includes(
      savedCaptionAlignmentMethod
    )
      ? (savedCaptionAlignmentMethod as 'ctc' | 'ctc_asr_fallback' | 'asr')
      : 'ctc';
    draft.captionAlignmentCtcModel = String(
      saved.caption_alignment_ctc_model ?? 'auto'
    );
    draft.captionAlignmentPaddingMs = Number(
      saved.caption_alignment_padding_ms ?? 2000
    );
    draft.captionAlignmentBatchSeconds = Number(
      saved.caption_alignment_batch_seconds ?? 30
    );
    draft.captionAlignmentMinConfidence = Number(
      saved.caption_alignment_min_confidence ?? 0.5
    );
    draft.captionAlignmentFallbackCoverage = Number(
      saved.caption_alignment_fallback_coverage ?? 0.9
    );
    draft.sttQuantization = String(
      hasSavedSttModel
        ? (saved.stt_model_quantization ??
            capabilities?.stt?.models?.[draft.sttEngine]?.precision ??
            'f16')
        : (capabilities?.stt?.default_model_quantization ?? 'f16')
    );
    draft.sttComputeBackend = String(saved.stt_compute_backend ?? 'auto');
    draft.sttDevice = Number(saved.stt_compute_device ?? 0);
    draft.sttThreads = Number(saved.stt_threads ?? 0);
    draft.sttChunkSeconds = Number(saved.stt_chunk_seconds ?? 0);
    draft.sttChunkOverlap = Number(saved.stt_chunk_overlap_seconds ?? 3);
    draft.sttHotwords = String(saved.stt_hotwords ?? '');
    draft.sttTranscribeStyle = String(
      saved.stt_transcribe_style ?? 'readability'
    );
    draft.sttLidBackend = String(saved.stt_lid_backend ?? 'whisper');
    draft.sttBeamSize = Number(saved.stt_beam_size ?? 1);
    draft.parakeetDecoder = String(saved.parakeet_decoder ?? 'tdt');
    draft.mossMaxChunkSeconds = Number(saved.moss_max_chunk_seconds ?? 120);
    draft.mossChunkOverlap = Number(saved.moss_chunk_overlap_seconds ?? 0);
    draft.mossVadEnabled = Boolean(saved.moss_vad_enabled ?? false);
    draft.mossCtcAlignmentEnabled = Boolean(
      saved.moss_ctc_alignment_enabled ?? true
    );
    draft.mossCtcAlignerModel = String(saved.moss_ctc_aligner_model ?? 'auto');
    draft.mossCtcPaddingSeconds = Number(saved.moss_ctc_padding_seconds ?? 0.5);
    draft.vadEnabled = Boolean(saved.crispasr_vad_enabled ?? true);
    draft.vadModel = String(saved.crispasr_vad_model ?? 'silero');
    draft.vadThreshold = Number(saved.crispasr_vad_threshold ?? 0.5);
    draft.vadMinSpeech = Number(saved.crispasr_vad_min_speech_ms ?? 250);
    draft.vadMinSilence = Number(saved.crispasr_vad_min_silence_ms ?? 800);
    draft.vadMaxSpeech = Number(saved.crispasr_vad_max_speech_seconds ?? 300);
    draft.vadSpeechPad = Number(saved.crispasr_vad_speech_pad_ms ?? 30);
    let subtitleSettings: Record<string, unknown> = {};
    subtitleSettingsPayload = null;
    try {
      const subtitlePayload = await sessionApi.settings(
        session.id,
        'subtitles'
      );
      if (!isCurrent()) return;
      subtitleSettingsPayload = subtitlePayload;
      settingsBases.subtitles = subtitlePayload;
      subtitleSettings = subtitleSettingsPayload.effective;
    } catch {
      /* Stage snapshots still work when the settings request fails. */
    }
    if (!isCurrent()) return;
    const legacyCustomSubtitleLimits =
      saved.subtitle_language_defaults == null &&
      ((saved.subtitle_max_chars_per_line != null &&
        Number(saved.subtitle_max_chars_per_line) !== 60) ||
        (saved.subtitle_max_cps != null &&
          Number(saved.subtitle_max_cps) !== 20));
    draft.subtitleLanguageDefaults = Boolean(
      saved.subtitle_language_defaults ??
      (legacyCustomSubtitleLimits
        ? false
        : (subtitleSettings.language_defaults ?? true))
    );
    draft.subtitleChars = Number(
      saved.subtitle_max_chars_per_line ??
        subtitleSettings.max_chars_per_line ??
        60
    );
    draft.subtitleLines = Number(
      saved.subtitle_max_lines ?? subtitleSettings.max_lines ?? 2
    );
    draft.subtitleMinDuration = Number(
      saved.subtitle_min_duration_ms ?? subtitleSettings.min_duration_ms ?? 833
    );
    draft.subtitleMaxDuration = Number(
      saved.subtitle_max_duration_ms ?? subtitleSettings.max_duration_ms ?? 7000
    );
    draft.subtitleCps = Number(
      saved.subtitle_max_cps ?? subtitleSettings.max_cps ?? 20
    );
    draft.subtitleMinGap = Number(
      saved.subtitle_min_gap_ms ?? subtitleSettings.min_gap_ms ?? 80
    );
    draft.subtitlePhraseGap = Number(
      saved.subtitle_phrase_gap_ms ?? subtitleSettings.phrase_gap_ms ?? 900
    );
    draft.subtitleHardGap = Number(
      saved.subtitle_hard_gap_ms ?? subtitleSettings.hard_gap_ms ?? 1500
    );
    draft.subtitleSentenceBoundaryThreshold = Number(
      saved.subtitle_sentence_boundary_threshold ??
        subtitleSettings.sentence_boundary_threshold ??
        0.25
    );
    await passageSettingsLoad;
    if (!isCurrent()) return;
    if (
      sourcePassages.settingsPayload &&
      ['transcribe', 'correct'].includes(stage.key)
    )
      settingsBases.source_passages = sourcePassages.settingsPayload;
    draft.correctionStyle =
      String(saved.correction_style ?? 'publishable') === 'faithful'
        ? 'faithful'
        : 'publishable';
    draft.instructions = String(saved.instructions ?? '');
    draft.optimizationPrompt = String(saved.combined_prompt ?? '');
    draft.optimizationConcurrent = Number(saved.llm_concurrent_calls ?? 1);
    const savedTimingContextMode = String(
      saved.timing_context_mode ??
        (saved.timing_context_enabled === false ? 'none' : 'full')
    );
    draft.timingContextMode = ['full', 'overlap_only', 'none'].includes(
      savedTimingContextMode
    )
      ? (savedTimingContextMode as 'full' | 'overlap_only' | 'none')
      : 'full';
    draft.timingContextGap = Number(
      saved.substantial_gap_ms ?? saved.timing_context_gap_ms ?? 2000
    );
    draft.correctionBatchCharLimit = Number(
      saved.char_limit ?? saved.llm_char ?? 6000
    );
    draft.correctionBatchSegmentLimit = Number(
      saved.max_segments_per_batch ?? saved.max_subtitles_per_call ?? 40
    );
    draft.contextBefore = Number(saved.context_before ?? 8);
    draft.contextAfter = Number(saved.context_after ?? 2);
    draft.preventSubtitleRemoval = Boolean(saved.no_remove_subtitles ?? false);
    draft.optimizationBatchSize = Number(saved.llm_tts_batch_size ?? 3);
    draft.documentOptimizationBatchSize = Number(
      saved.llm_tts_document_batch_size ?? 8
    );
    draft.speechOptimizationMode =
      String(saved.speech_optimization_mode ?? 'guarded') === 'flexible'
        ? 'flexible'
        : 'guarded';
    draft.speechAnnotationMode = ['dialogue', 'speakers'].includes(
      String(saved.llm_tts_annotation_mode)
    )
      ? (saved.llm_tts_annotation_mode as 'dialogue' | 'speakers')
      : 'off';
    draft.speechAnnotationOnly = Boolean(saved.llm_tts_annotation_only);
    draft.optimizationMultiStage = Boolean(saved.llm_multi_stage ?? false);
    draft.optimizationFirstPrompt = String(saved.first_prompt ?? '');
    draft.optimizationSecondPrompt = String(saved.second_prompt ?? '');
    draft.optimizationThirdPrompt = String(saved.third_prompt ?? '');
    draft.optimizationEnabled = Boolean(
      saved.llm_tts_optimization ??
      (stage.key === 'optimize_tts' ? stage.enabled : false) ??
      false
    );
    draft.documentOptimizationEnabled = Boolean(
      saved.llm_tts_document_optimization ??
      (stage.key === 'optimize_document' ? stage.enabled : false) ??
      false
    );
    draft.optimizationTiming = draft.documentOptimizationEnabled
      ? 'document'
      : 'generation';
    draft.agentic = Boolean(saved.agentic ?? false);
    draft.maxIterations = Number(saved.max_iterations ?? 53);
    draft.splitSentences = Boolean(saved.enable_sentence_splitting ?? true);
    draft.appendSentences = Boolean(saved.enable_sentence_appending ?? true);
    draft.maxSentenceLength = Number(saved.max_sentence_length ?? 200);
    draft.audiobookChunking =
      saved.audiobook_chunking === 'manual' ? 'manual' : 'model';
    draft.nemoNormalization = Boolean(saved.enable_nemo_normalization ?? true);
    draft.normalizeAllCaps = Boolean(saved.normalize_all_caps ?? true);
    draft.removeDiacritics = Boolean(saved.remove_diacritics ?? false);
    draft.removeQuotationMarks = Boolean(saved.remove_quotation_marks ?? false);
    const explicitlySelectedServiceId = String(
      storedSettings?.override?.tts_service ??
        storedSettings?.override?.service ??
        ''
    ).trim();
    const configuredServiceId = String(
      saved.tts_service ??
        saved.service ??
        speechCatalogues.catalogue.default_service ??
        'XTTS'
    );
    const configuredService = speechCatalogues.catalogue.services.find((item) =>
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
        : preferredTtsService(speechCatalogues.catalogue.services)) ??
      configuredService;
    draft.ttsService = String(activeService?.id ?? configuredServiceId);
    draft.ttsModel =
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
    draft.voiceName =
      activeService?.id === configuredService?.id
        ? String(saved.voice ?? saved.voice_name ?? '')
        : String(activeService?.default_voice ?? '');
    draft.generationPrompt = String(saved.generation_prompt ?? '');
    draft.ttsBatchSize = Number(saved.tts_batch_size ?? 10);
    draft.ttsConcurrentRequests = Number(saved.tts_concurrent_requests ?? 1);
    draft.subtitleMode = String(saved.subtitle_mode ?? 'soft');
    draft.subtitleSelection = String(saved.subtitle_selection ?? 'dual');
    draft.audioMode = String(
      saved.audio_mode ??
        (session.workflow_kind === 'voiceover' ? 'mixed' : 'preserve')
    );
    draft.exportMode = String(
      saved.export_mode ??
        (session.workflow_kind === 'subtitles' ? 'subtitles' : 'media')
    );
    if (
      session.workflow_kind === 'subtitles' &&
      !['subtitles', 'text'].includes(draft.exportMode)
    )
      draft.exportMode = 'subtitles';
    draft.subtitleFormat = String(saved.subtitle_format ?? 'srt');
    if (stage.key === 'generate_audio' && activeService)
      await discoverTtsService(activeService);
    if (!isCurrent()) return;
    if (stage.key === 'generate_audio' && ttsSelection.selectedTtsService) {
      draft.ttsService = String(
        ttsSelection.selectedTtsService.id ??
          ttsSelection.selectedTtsService.name
      );
      draft.ttsModel =
        draft.ttsModel ||
        String(
          ttsSelection.selectedTtsService.default_model ??
            ttsSelection.ttsModels[0] ??
            ''
        );
      draft.voiceName =
        draft.voiceName || String(ttsSelection.selectedTtsDefaultVoice ?? '');
      if (
        String(ttsSelection.selectedTtsService.id).toLowerCase() ===
        'kobold_qwen'
      ) {
        const catalogue = Array.from(
          ttsSelection.selectedTtsService.voice_catalogues?.[draft.ttsModel] ??
            []
        ).map((voice) => String(voice));
        const published = speechCatalogues.voices.flatMap((voice) => {
          const registration = voice?.metadata_json?.providers?.kobold_qwen;
          return registration?.status === 'ready' && registration?.voice_id
            ? [String(registration.voice_id)]
            : [];
        });
        const allowed =
          draft.ttsModel.toLowerCase() === 'voice cloning'
            ? [...catalogue, ...published]
            : catalogue;
        if (
          !allowed.some(
            (voice: string) =>
              voice.toLowerCase() === draft.voiceName.toLowerCase()
          )
        ) {
          draft.voiceName = String(
            ttsSelection.selectedTtsService.default_voices?.[draft.ttsModel] ??
              allowed[0] ??
              ''
          );
        }
      }
    }
    settingsLoading = false;
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

  async function persistDefaultSection(
    section: string,
    value: Record<string, unknown>,
    stored: SettingsPayload,
    sourceProviderId: string | undefined,
    completed: string[]
  ) {
    if (stored.global_revision == null)
      throw new Error(
        'Reload these settings before saving application defaults.'
      );
    await sessionApi.saveDefaults(section, stored.global_revision, {
      ...stored.global,
      ...value
    });
    completed.push(`${sectionDisplay(section)} defaults`);
    const cleaned = { ...(stored.override ?? {}) };
    for (const key of Object.keys(value)) delete cleaned[key];
    if (section === 'tts' && sourceProviderId) {
      for (const key of Object.keys(cleaned)) {
        if (
          key.startsWith(`${sourceProviderId}_`) ||
          ['reference_audio', 'reference_text'].includes(key)
        )
          delete cleaned[key];
      }
    }
    return sessionApi.saveSettings(
      session.id,
      section,
      stored.revision,
      cleaned
    );
  }

  async function clearSectionOverrides(
    section: string,
    keys: string[],
    stored: SettingsPayload
  ) {
    const cleaned = { ...(stored.override ?? {}) };
    for (const key of keys) delete cleaned[key];
    return sessionApi.saveSettings(
      session.id,
      section,
      stored.revision,
      cleaned
    );
  }

  function invalidateSpeechRequests() {
    speechCatalogues.invalidate();
    ttsSelectionRequest++;
    speechRefreshRequest++;
    activity.refreshingTtsServices = false;
  }

  function closeStageSettings() {
    settingsOpening++;
    settingsMutation++;
    settingsSaving = false;
    invalidateSpeechRequests();
    settingsLoading = false;
    settingsStage = null;
  }

  onDestroy(() => {
    disposed = true;
    speechCatalogues.dispose();
    closeStageSettings();
  });

  function captureTtsSelection() {
    const opening = settingsOpening;
    const request = ttsSelectionRequest;
    const selected = {
      service: draft.ttsService,
      model: draft.ttsModel,
      voice: draft.voiceName,
      language: draft.targetLanguage,
      prompt: draft.generationPrompt
    };
    return () =>
      !disposed &&
      opening === settingsOpening &&
      request === ttsSelectionRequest &&
      selected.service === draft.ttsService &&
      selected.model === draft.ttsModel &&
      selected.voice === draft.voiceName &&
      selected.language === draft.targetLanguage &&
      selected.prompt === draft.generationPrompt;
  }

  async function loadSpeechCatalogues(
    preserveSelection = false,
    force = false
  ) {
    let isCurrent = () => false;
    const selectionIsCurrent = captureTtsSelection();
    const previousService = draft.ttsService;
    const previousModel = draft.ttsModel;
    const previousVoice = draft.voiceName;
    try {
      const loaded = await speechCatalogues.load(force);
      if (!loaded) return;
      isCurrent = loaded.current;
      const services = loaded.catalogue;
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
      const preserveDraft = !selectionIsCurrent();
      const active =
        (preserveDraft
          ? catalogue.find((item) =>
              [item.id, item.name].some(
                (value) =>
                  String(value ?? '').toLowerCase() ===
                  draft.ttsService.toLowerCase()
              )
            )
          : null) ??
        (preserveSelection ? preserved : null) ??
        (configured?.available ? configured : preferredTtsService(catalogue)) ??
        configured ??
        catalogue[0];
      if (active && !preserveDraft) {
        draft.ttsService = String(active.id ?? active.name);
        draft.ttsModel =
          preserveSelection && preserved
            ? previousModel
            : draft.ttsModel ||
              String(active.default_model ?? active.models?.[0] ?? '');
        draft.voiceName =
          preserveSelection && preserved
            ? previousVoice
            : draft.voiceName ||
              String(
                active.default_voices_by_language?.[draft.ttsModel]?.[
                  draft.targetLanguage
                ] ??
                  active.default_voices?.[draft.ttsModel] ??
                  active.default_voice ??
                  ''
              );
      }
      if (active) await discoverTtsService(active);
      if (!speechCatalogues.finishLoad(loaded)) return;
      if (String(active?.id ?? '').toLowerCase() === 'xtts')
        await loadXttsModels();
    } catch (caught) {
      if (isCurrent()) speechCatalogues.error = errorMessage(caught);
    }
  }

  async function refreshSpeechServices() {
    const request = ++speechRefreshRequest;
    activity.refreshingTtsServices = true;
    error = '';
    try {
      await loadSpeechCatalogues(true, true);
    } finally {
      if (request === speechRefreshRequest)
        activity.refreshingTtsServices = false;
    }
  }

  const XTTS_MODEL_BUNDLE_FILENAMES = [
    'config.json',
    'model.pth',
    'speakers_xtts.pth',
    'vocab.json'
  ] as const;

  function chooseXttsModelFiles(files: FileList | null) {
    activity.xttsModelFiles = Array.from(files ?? []);
    activity.xttsModelUploadError = '';
    activity.xttsModelUploadMessage = '';
  }

  function xttsModelBundleError() {
    const modelId = activity.xttsModelId.trim();
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
    const names = activity.xttsModelFiles.map((file) => file.name);
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
    if (activity.xttsModelFiles.some((file) => file.size < 1))
      return 'Every XTTS bundle file must contain data.';
    return '';
  }

  async function uploadXttsModel() {
    if (activity.uploadingXttsModel) return;
    error = '';
    const validationError = xttsModelBundleError();
    if (validationError) {
      activity.xttsModelUploadError = validationError;
      activity.xttsModelUploadMessage = '';
      return;
    }
    activity.uploadingXttsModel = true;
    activity.xttsModelUploadProgress = 0;
    activity.xttsModelUploadPhase = 'transferring';
    activity.xttsModelUploadError = '';
    activity.xttsModelUploadMessage = 'Uploading the XTTS model…';
    try {
      const uploaded = await sessionApi.uploadXttsModel(
        activity.xttsModelId.trim(),
        activity.xttsModelFiles,
        (fraction) => {
          activity.xttsModelUploadProgress = Math.max(0, Math.min(1, fraction));
        },
        () => {
          activity.xttsModelUploadProgress = 1;
          activity.xttsModelUploadPhase = 'installing';
          activity.xttsModelUploadMessage =
            'Upload transferred; Pandrator is installing the model…';
        }
      );
      await loadSpeechCatalogues(true, true);
      await loadXttsModels();
      const xttsService = speechCatalogues.catalogue.services.find(
        (service) => String(service.id).toLowerCase() === 'xtts'
      );
      draft.ttsService = String(xttsService?.id ?? 'xtts');
      draft.ttsModel = uploaded.id;
      draft.voiceName = String(
        xttsService?.default_voices?.[uploaded.id] ??
          xttsService?.default_voice ??
          ''
      );
      activity.xttsModelId = '';
      activity.xttsModelFiles = [];
      activity.xttsModelUploadMessage = `Installed ${uploaded.id} (${(uploaded.bytes / (1024 * 1024)).toFixed(1)} MB) and selected it for this generation.`;
    } catch (caught) {
      activity.xttsModelUploadError = errorMessage(caught);
      activity.xttsModelUploadMessage = '';
    } finally {
      activity.uploadingXttsModel = false;
      activity.xttsModelUploadPhase = 'idle';
    }
  }

  async function loadXttsModels() {
    activity.xttsModelsLoading = true;
    try {
      const catalogue = await sessionApi.xttsModels();
      activity.xttsModels = catalogue.data ?? [];
      activity.xttsModelsLifecycleSupported = Boolean(
        catalogue.lifecycle_supported
      );
      const wrapperVersion = String(catalogue.wrapper?.version ?? '');
      const wrapperStatus = String(catalogue.wrapper?.status ?? '');
      const wrapperMessage = wrapperVersion
        ? `Connected XTTS wrapper ${wrapperVersion}${wrapperStatus ? ` (${wrapperStatus})` : ''}.`
        : '';
      activity.xttsModelsCompatibility = [
        catalogue.compatibility,
        wrapperMessage
      ]
        .filter(Boolean)
        .join(' ');
    } catch (caught) {
      activity.xttsModels = [];
      activity.xttsModelsLifecycleSupported = false;
      activity.xttsModelsCompatibility = `${errorMessage(caught)} Update or Repair XTTS in Pandrator Manager if model lifecycle controls are unavailable.`;
    } finally {
      activity.xttsModelsLoading = false;
    }
  }

  async function removeXttsModel(model: XttsModel) {
    if (!model.removable || activity.deletingXttsModelId) return;
    const isCurrent = model.id === draft.ttsModel;
    const question = isCurrent
      ? `“${model.id}” is selected for this generation. Remove it and switch this generation to a safe XTTS fallback?`
      : `Remove the local XTTS model “${model.id}”? This cannot be undone.`;
    if (!window.confirm(question)) return;
    activity.deletingXttsModelId = model.id;
    error = '';
    try {
      await sessionApi.deleteXttsModel(model.id);
      if (isCurrent) draft.ttsModel = '';
      await Promise.all([loadXttsModels(), loadSpeechCatalogues(false, true)]);
      const service = speechCatalogues.catalogue.services.find(
        (item) => String(item.id).toLowerCase() === 'xtts'
      );
      const safeFallback =
        service?.default_model ??
        service?.models?.find((candidate) => candidate !== model.id) ??
        activity.xttsModels.find((candidate) => candidate.is_default)?.id ??
        activity.xttsModels.find((candidate) => candidate.id !== model.id)
          ?.id ??
        '';
      if (
        isCurrent ||
        !activity.xttsModels.some(
          (candidate) => candidate.id === draft.ttsModel
        )
      ) {
        draft.ttsModel = String(safeFallback);
        chooseTtsModel(draft.ttsModel);
      }
      activity.xttsModelUploadMessage = `Removed ${model.id}${isCurrent && draft.ttsModel ? ` and selected ${draft.ttsModel}` : ''}.`;
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      activity.deletingXttsModelId = '';
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
    service: TtsService | undefined = ttsSelection.selectedTtsService
  ) {
    await speechCatalogues.discover(service);
  }

  async function chooseTtsService(value: string) {
    ttsSelectionRequest++;
    const previous = ttsSelection.selectedTtsService;
    const previousModel = draft.ttsModel;
    const previousVoice = draft.voiceName;
    const switching =
      previous?.catalogue_role === 'compatibility' &&
      value === previous.replacement_service_id;
    ttsSelection.ttsSwitchSource = switching ? previous : null;
    ttsSelection.ttsSwitchReviewed = false;
    draft.ttsService = value;
    const service = speechCatalogues.catalogue.services.find(
      (item) => String(item.id) === value
    );
    draft.ttsModel = String(
      service?.default_model ?? service?.models?.[0] ?? ''
    );
    const isCurrent = captureTtsSelection();
    await discoverTtsService(service);
    if (!isCurrent()) return;
    if (switching && previous) {
      const live =
        speechCatalogues.catalogue.services.find((item) => item.id === value) ??
        service;
      const candidates = (live?.model_catalog ?? []).filter(
        (item) =>
          item.family === previous.replacement_model_family &&
          live?.models?.includes(item.id)
      );
      const preferredMode = /prebuilt|customvoice/i.test(previousModel)
        ? 'prebuilt'
        : 'cloning';
      const candidate =
        candidates.find((item) => item.voice_mode === preferredMode) ??
        candidates[0];
      draft.ttsModel = candidate?.id ?? '';
      // Reuse only a voice already advertised by the target or linked there.
      const native = live?.voice_catalogues?.[draft.ttsModel] ?? [];
      const linked = speechCatalogues.voices.find(
        (voice) =>
          voice.metadata_json?.providers?.[previous.id]?.voice_id ===
          previousVoice
      )?.metadata_json?.providers?.[value];
      draft.voiceName =
        native.find(
          (voice) => voice.toLowerCase() === previousVoice.toLowerCase()
        ) ?? (linked?.status === 'ready' ? String(linked.voice_id ?? '') : '');
      draft.generationPrompt = '';
      return;
    }
    if (String(service?.id ?? '').toLowerCase() === 'xtts')
      await loadXttsModels();
    if (!isCurrent()) return;
    draft.voiceName =
      service?.model_voice_modes?.[draft.ttsModel] === 'optional_cloning'
        ? ''
        : String(
            service?.default_voices_by_language?.[draft.ttsModel]?.[
              draft.targetLanguage
            ] ??
              service?.default_voices?.[draft.ttsModel] ??
              service?.default_voice ??
              ''
          );
  }

  function chooseTtsModel(value: string) {
    ttsSelectionRequest++;
    draft.ttsModel = value;
    const service = ttsSelection.selectedTtsService;
    const modelVoices = service?.voice_catalogues?.[value] ?? [];
    draft.voiceName =
      service?.model_voice_modes?.[value] === 'optional_cloning'
        ? ''
        : String(
            service?.default_voices_by_language?.[value]?.[
              draft.targetLanguage
            ] ??
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
    draft.voiceName = providerVoiceId;
    voiceLibraryOpen = false;
    const isCurrent = captureTtsSelection();
    await loadSpeechCatalogues(true, true);
    if (isCurrent()) draft.voiceName = providerVoiceId;
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

  const selectedLlmModel = $derived(
    draft.model === 'default'
      ? (llmModels.find((item) => item.isDefault) ?? null)
      : (llmModels.find((item) => item.value === draft.model) ?? null)
  );

  async function useLibraryVoice(voice: VoiceRecord) {
    const registration =
      voice.metadata_json?.providers?.[ttsSelection.selectedTtsServiceId];
    if (registration?.status === 'ready' && registration.voice_id) {
      draft.voiceName = String(registration.voice_id);
      return;
    }
    if (
      Number(voice.available_sample_count ?? 0) < 1 ||
      (ttsSelection.selectedModelNeedsReviewedTranscript &&
        !voice.preferred_sample_transcript_reviewed)
    ) {
      await openVoiceLibrary(
        'references',
        ttsSelection.selectedTtsServiceId,
        voice.id
      );
      return;
    }
    if (
      ttsSelection.selectedTtsService?.available === false &&
      !ttsSelection.audioCppLinkedReferences
    ) {
      error =
        ttsSelection.selectedTtsService.availability_reason ||
        `Start the speech service before ${ttsSelection.audioCppLinkedReferences ? 'linking' : 'uploading'} a voice.`;
      return;
    }
    if (activity.publishingLibraryVoiceId) return;
    const serviceId = ttsSelection.selectedTtsServiceId;
    const serviceName = ttsSelection.selectedTtsService?.name ?? serviceId;
    const modelId = draft.ttsModel;
    const linked = ttsSelection.audioCppLinkedReferences;
    const opening = settingsOpening;
    const isCurrent = () => !disposed && opening === settingsOpening;
    const selectionIsCurrent = captureTtsSelection();
    activity.publishingLibraryVoiceId = voice.id;
    activity.voicePublishStatus = linked
      ? `Linking ${voice.name} to ${serviceName}…`
      : `Uploading ${voice.name} to ${serviceName}…`;
    error = '';
    try {
      const queued = await voiceApi.publish(
        voice.id,
        serviceId,
        voice.revision
      );
      let completed: Awaited<ReturnType<typeof waitForVoiceJob>> | undefined;
      let jobFailure: unknown;
      try {
        completed = await waitForVoiceJob(queued.id);
      } catch (caught) {
        jobFailure = caught;
      }
      if (!isCurrent()) return;
      // A canceled job can still have saved a provider copy. Read the current
      // registration rather than inferring readiness or revision from its result.
      const voices = await speechCatalogues.reloadVoices();
      if (!isCurrent()) return;
      if (!voices) {
        activity.voicePublishStatus =
          'The voice library changed. Refresh it to check the publication.';
        return;
      }
      if (jobFailure) throw jobFailure;
      if (
        completed?.result_json?.voice_id !== voice.id ||
        completed.result_json.service_id !== serviceId
      )
        throw new Error(
          'The publication result did not match the requested voice and service.'
        );
      const providerVoiceId = String(
        completed.result_json.provider_voice_id ?? ''
      );
      if (!providerVoiceId)
        throw new Error('The provider did not return a usable voice ID.');
      const registration = voices.find((item) => item.id === voice.id)
        ?.metadata_json?.providers?.[serviceId];
      if (
        registration?.status !== 'ready' ||
        registration.voice_id !== providerVoiceId
      ) {
        activity.voicePublishStatus = `${voice.name} needs a fresh provider registration. Review it in the Voice Library.`;
        return;
      }
      speechCatalogues.catalogue = {
        ...speechCatalogues.catalogue,
        services: speechCatalogues.catalogue.services.map((service) =>
          service.id === serviceId
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
                  [modelId]: Array.from(
                    new Set([
                      ...(service.voice_catalogues?.[modelId] ?? []),
                      providerVoiceId
                    ])
                  )
                }
              }
            : service
        )
      };
      if (selectionIsCurrent()) {
        draft.voiceName = providerVoiceId;
        activity.voicePublishStatus = linked
          ? `${voice.name} is linked and selected.`
          : `${voice.name} is ready and selected.`;
      } else {
        activity.voicePublishStatus = `${voice.name} is ${linked ? 'linked' : 'ready'} in ${serviceName}.`;
      }
    } catch (caught) {
      if (isCurrent()) {
        activity.voicePublishStatus = '';
        error = `Could not prepare ${voice.name}: ${errorMessage(caught)}`;
      }
    } finally {
      activity.publishingLibraryVoiceId = '';
      if (!isCurrent()) activity.voicePublishStatus = '';
    }
  }

  function stageSectionUpdates(
    key: string
  ): { section: string; value: Record<string, unknown> }[] {
    if (key === 'transcribe') {
      const updates: { section: string; value: Record<string, unknown> }[] = [
        {
          section: 'stt',
          value: Object.fromEntries(
            Object.entries(stageSettings[key]).filter(
              ([field]) =>
                field !== 'original_language' && !field.startsWith('subtitle_')
            )
          )
        },
        {
          section: 'subtitles',
          value: {
            language_defaults: draft.subtitleLanguageDefaults,
            max_chars_per_line: draft.subtitleChars,
            max_lines: draft.subtitleLines,
            min_duration_ms: draft.subtitleMinDuration,
            max_duration_ms: draft.subtitleMaxDuration,
            max_cps: draft.subtitleCps,
            min_gap_ms: draft.subtitleMinGap,
            phrase_gap_ms: draft.subtitlePhraseGap,
            hard_gap_ms: draft.subtitleHardGap,
            sentence_boundary_threshold: draft.subtitleSentenceBoundaryThreshold
          }
        }
      ];
      const passages = sourcePassages.update();
      if (passages) updates.push(passages);
      return updates;
    }
    if (key === 'correct') {
      const updates: { section: string; value: Record<string, unknown> }[] = [
        {
          section: 'correction',
          value: {
            enabled: true,
            model_name: draft.model === 'default' ? '' : draft.model,
            reasoning_effort: draft.reasoningEffort,
            correction_style: draft.correctionStyle,
            instructions: draft.instructions,
            llm_concurrent_calls: draft.optimizationConcurrent,
            char_limit: draft.correctionBatchCharLimit,
            max_segments_per_batch: draft.correctionBatchSegmentLimit,
            context_before: draft.contextBefore,
            context_after: draft.contextAfter,
            no_remove_subtitles: draft.preventSubtitleRemoval,
            timing_context_mode: draft.timingContextMode,
            substantial_gap_ms: draft.timingContextGap,
            web_research_enabled: draft.webResearchEnabled,
            web_research_model_name: draft.webResearchModel,
            web_research_mode: draft.webResearchMode,
            web_research_context_fraction: draft.webResearchContextFraction
          }
        }
      ];
      const passages = sourcePassages.update();
      if (passages) updates.push(passages);
      return updates;
    }
    if (key === 'translate')
      return [
        {
          section: 'translation',
          value: {
            enabled: true,
            backend: draft.backend,
            target_language: draft.targetLanguage,
            model_name: draft.model === 'default' ? '' : draft.model,
            reasoning_effort: draft.reasoningEffort,
            instructions: draft.instructions,
            source_artifact_id: draft.translationSourceArtifactId,
            llm_concurrent_calls: draft.optimizationConcurrent,
            char_limit: draft.correctionBatchCharLimit,
            max_segments_per_batch: draft.correctionBatchSegmentLimit,
            context_before: draft.contextBefore,
            context_after: draft.contextAfter,
            no_remove_subtitles: draft.preventSubtitleRemoval,
            timing_context_mode: draft.timingContextMode,
            substantial_gap_ms: draft.timingContextGap,
            web_research_enabled: draft.webResearchEnabled,
            web_research_model_name: draft.webResearchModel,
            web_research_mode: draft.webResearchMode,
            web_research_context_fraction: draft.webResearchContextFraction
          }
        }
      ];
    if (key === 'optimize_tts')
      return [
        {
          section: 'text',
          value: {
            llm_tts_optimization: draft.optimizationEnabled,
            llm_processing_enabled: draft.optimizationEnabled,
            llm_tts_document_optimization: draft.documentOptimizationEnabled,
            tts_optimization_model:
              draft.model === 'default' ? '' : draft.model,
            speech_optimization_mode: draft.speechOptimizationMode,
            llm_tts_annotation_mode: draft.speechAnnotationMode,
            llm_tts_annotation_only: draft.speechAnnotationOnly,
            llm_tts_batch_size: draft.optimizationBatchSize,
            llm_tts_document_batch_size: draft.documentOptimizationBatchSize,
            llm_concurrent_calls: draft.optimizationConcurrent,
            llm_multi_stage: draft.optimizationMultiStage,
            combined_prompt: draft.optimizationPrompt,
            first_prompt: draft.optimizationFirstPrompt,
            second_prompt: draft.optimizationSecondPrompt,
            third_prompt: draft.optimizationThirdPrompt
          }
        }
      ];
    if (key === 'optimize_document')
      return [
        {
          section: 'text',
          value: {
            llm_tts_document_optimization: draft.documentOptimizationEnabled,
            tts_optimization_model:
              draft.model === 'default' ? '' : draft.model,
            speech_optimization_mode: draft.speechOptimizationMode,
            llm_tts_annotation_mode: draft.speechAnnotationMode,
            llm_tts_annotation_only: draft.speechAnnotationOnly,
            llm_tts_document_batch_size: draft.documentOptimizationBatchSize,
            llm_concurrent_calls: draft.optimizationConcurrent,
            llm_multi_stage: draft.optimizationMultiStage,
            combined_prompt: draft.optimizationPrompt,
            first_prompt: draft.optimizationFirstPrompt,
            second_prompt: draft.optimizationSecondPrompt,
            third_prompt: draft.optimizationThirdPrompt
          }
        }
      ];
    return [{ section: stageSection(key), value: stageSettings[key] }];
  }

  async function revertStageToDefaults() {
    if (!settingsStage || settingsLoading || settingsSaving) return;
    const stage = settingsStage;
    const updates = stageSectionUpdates(stage.key);
    const bases = settingsBases;
    const opening = settingsOpening;
    const mutation = ++settingsMutation;
    const isCurrent = () =>
      !disposed && opening === settingsOpening && mutation === settingsMutation;
    const completed: string[] = [];
    settingsSaving = true;
    try {
      for (const update of updates) {
        if (!bases[update.section])
          throw new Error(
            'Reload these settings before reverting to defaults.'
          );
      }
      for (const update of updates) {
        const saved = await clearSectionOverrides(
          update.section,
          Object.keys(update.value),
          bases[update.section]
        );
        completed.push(sectionDisplay(update.section));
        if (isCurrent()) settingsBases[update.section] = saved;
      }
      if (!isCurrent()) return;
      const next = { ...stageSettings };
      delete next[stage.key];
      stageSettings = next;
      await openSettings(stage);
      if (
        !disposed &&
        settingsStage?.key === stage.key &&
        settingsOpening === opening + 1
      )
        stageMessage = 'Reverted to application defaults.';
    } catch (caught) {
      if (isCurrent())
        error = `${completed.length ? `Reverted ${completed.join(', ')}. Remaining changes were not saved. ` : ''}${errorMessage(caught)}`;
    } finally {
      if (isCurrent()) settingsSaving = false;
    }
  }

  function captureStageSettings(stage: Stage) {
    const key = stage.key;
    const common = {
      model_name: draft.model === 'default' ? '' : draft.model,
      [`${key}_model`]: draft.model
    };
    if (key === 'transcribe')
      stageSettings[key] = {
        stt_engine: draft.sttEngine,
        qwen_asr_model: draft.qwenAsrModel,
        qwen_asr_chunk_mode: draft.qwenChunkMode,
        transcription_vocal_isolation: draft.transcriptionVocalIsolation,
        caption_alignment_method: draft.captionAlignmentMethod,
        caption_alignment_ctc_model: draft.captionAlignmentCtcModel,
        caption_alignment_padding_ms: draft.captionAlignmentPaddingMs,
        caption_alignment_batch_seconds: draft.captionAlignmentBatchSeconds,
        caption_alignment_min_confidence: draft.captionAlignmentMinConfidence,
        caption_alignment_fallback_coverage:
          draft.captionAlignmentFallbackCoverage,
        stt_model_quantization: draft.sttQuantization,
        stt_compute_backend: draft.sttComputeBackend,
        stt_compute_device: draft.sttDevice,
        stt_language: draft.originalLanguage,
        stt_threads: draft.sttThreads,
        stt_chunk_seconds:
          draft.sttEngine === 'moss' ? 0 : draft.sttChunkSeconds,
        stt_chunk_overlap_seconds: draft.sttChunkOverlap,
        stt_hotwords: draft.sttHotwords,
        stt_transcribe_style: draft.sttTranscribeStyle,
        stt_lid_backend: draft.sttLidBackend,
        stt_beam_size: draft.sttBeamSize,
        parakeet_decoder: draft.parakeetDecoder,
        moss_max_chunk_seconds: draft.mossMaxChunkSeconds,
        moss_chunk_overlap_seconds: draft.mossChunkOverlap,
        moss_vad_enabled: draft.mossVadEnabled,
        moss_ctc_alignment_enabled: draft.mossCtcAlignmentEnabled,
        moss_ctc_aligner_model: draft.mossCtcAlignerModel,
        moss_ctc_padding_seconds: draft.mossCtcPaddingSeconds,
        crispasr_vad_enabled: draft.vadEnabled,
        crispasr_vad_model: draft.vadModel,
        crispasr_vad_threshold: draft.vadThreshold,
        crispasr_vad_min_speech_ms: draft.vadMinSpeech,
        crispasr_vad_min_silence_ms: draft.vadMinSilence,
        crispasr_vad_max_speech_seconds: draft.vadMaxSpeech,
        crispasr_vad_speech_pad_ms: draft.vadSpeechPad,
        subtitle_language_defaults: draft.subtitleLanguageDefaults,
        subtitle_max_chars_per_line: draft.subtitleChars,
        subtitle_max_lines: draft.subtitleLines,
        subtitle_min_duration_ms: draft.subtitleMinDuration,
        subtitle_max_duration_ms: draft.subtitleMaxDuration,
        subtitle_max_cps: draft.subtitleCps,
        subtitle_min_gap_ms: draft.subtitleMinGap,
        subtitle_phrase_gap_ms: draft.subtitlePhraseGap,
        subtitle_hard_gap_ms: draft.subtitleHardGap,
        subtitle_sentence_boundary_threshold:
          draft.subtitleSentenceBoundaryThreshold
      };
    else if (key === 'correct')
      stageSettings[key] = {
        ...common,
        reasoning_effort: draft.reasoningEffort,
        correction_style: draft.correctionStyle,
        instructions: draft.instructions,
        llm_concurrent_calls: draft.optimizationConcurrent,
        char_limit: draft.correctionBatchCharLimit,
        max_segments_per_batch: draft.correctionBatchSegmentLimit,
        context_before: draft.contextBefore,
        context_after: draft.contextAfter,
        no_remove_subtitles: draft.preventSubtitleRemoval,
        timing_context_mode: draft.timingContextMode,
        substantial_gap_ms: draft.timingContextGap,
        web_research_enabled: draft.webResearchEnabled,
        web_research_model_name: draft.webResearchModel,
        web_research_mode: draft.webResearchMode,
        web_research_context_fraction: draft.webResearchContextFraction
      };
    else if (key === 'translate')
      stageSettings[key] = {
        ...common,
        translation_backend: draft.backend,
        target_language: draft.targetLanguage,
        source_artifact_id: draft.translationSourceArtifactId,
        reasoning_effort: draft.reasoningEffort,
        instructions: draft.instructions,
        llm_concurrent_calls: draft.optimizationConcurrent,
        char_limit: draft.correctionBatchCharLimit,
        max_segments_per_batch: draft.correctionBatchSegmentLimit,
        context_before: draft.contextBefore,
        context_after: draft.contextAfter,
        no_remove_subtitles: draft.preventSubtitleRemoval,
        timing_context_mode: draft.timingContextMode,
        substantial_gap_ms: draft.timingContextGap,
        web_research_enabled: draft.webResearchEnabled,
        web_research_model_name: draft.webResearchModel,
        web_research_mode: draft.webResearchMode,
        web_research_context_fraction: draft.webResearchContextFraction
      };
    else if (key === 'optimize_tts') {
      const enabled = Boolean(stage.enabled);
      if (draft.speechAnnotationMode !== 'off')
        draft.optimizationTiming = 'document';
      draft.optimizationEnabled =
        enabled && draft.optimizationTiming === 'generation';
      draft.documentOptimizationEnabled =
        enabled && draft.optimizationTiming === 'document';
      stageSettings[key] = {
        ...common,
        llm_tts_optimization: draft.optimizationEnabled,
        llm_tts_document_optimization: draft.documentOptimizationEnabled,
        speech_optimization_mode: draft.speechOptimizationMode,
        llm_tts_annotation_mode: draft.speechAnnotationMode,
        llm_tts_annotation_only: draft.speechAnnotationOnly,
        llm_tts_batch_size:
          draft.optimizationTiming === 'document'
            ? draft.documentOptimizationBatchSize
            : draft.optimizationBatchSize,
        llm_tts_document_batch_size: draft.documentOptimizationBatchSize,
        combined_prompt: draft.optimizationPrompt,
        llm_concurrent_calls: draft.optimizationConcurrent,
        llm_multi_stage: draft.optimizationMultiStage,
        first_prompt: draft.optimizationFirstPrompt,
        second_prompt: draft.optimizationSecondPrompt,
        third_prompt: draft.optimizationThirdPrompt
      };
    } else if (key === 'optimize_document')
      stageSettings[key] = {
        ...common,
        llm_tts_document_optimization: draft.documentOptimizationEnabled,
        speech_optimization_mode: draft.speechOptimizationMode,
        llm_tts_annotation_mode: draft.speechAnnotationMode,
        llm_tts_annotation_only: draft.speechAnnotationOnly,
        llm_tts_document_batch_size: draft.documentOptimizationBatchSize,
        llm_tts_batch_size: draft.documentOptimizationBatchSize,
        combined_prompt: draft.optimizationPrompt,
        llm_concurrent_calls: draft.optimizationConcurrent,
        llm_multi_stage: draft.optimizationMultiStage,
        first_prompt: draft.optimizationFirstPrompt,
        second_prompt: draft.optimizationSecondPrompt,
        third_prompt: draft.optimizationThirdPrompt
      };
    else if (key === 'clean_source')
      stageSettings[key] = {
        ...common,
        agentic: draft.agentic,
        max_iterations: draft.maxIterations
      };
    else if (key === 'prepare_text')
      stageSettings[key] = {
        enable_sentence_splitting: draft.splitSentences,
        enable_sentence_appending: draft.appendSentences,
        max_sentence_length: draft.maxSentenceLength,
        audiobook_chunking: draft.audiobookChunking,
        enable_nemo_normalization: draft.nemoNormalization,
        normalize_all_caps: draft.normalizeAllCaps,
        remove_diacritics: draft.removeDiacritics,
        remove_quotation_marks: draft.removeQuotationMarks
      };
    else if (key === 'generate_audio')
      stageSettings[key] = {
        tts_service: draft.ttsService,
        service: draft.ttsService,
        model: draft.ttsModel,
        xtts_model: draft.ttsModel,
        ...(ttsSelection.ttsSwitchSource
          ? {
              provider_switch_reviewed: ttsSelection.ttsSwitchReviewed,
              speaker: draft.voiceName,
              audio_cpp_voice_ref: {},
              audio_cpp_reference_text: '',
              options: {}
            }
          : {}),
        voice: draft.voiceName,
        generation_prompt: draft.generationPrompt,
        tts_batch_size: draft.ttsBatchSize,
        tts_concurrent_requests: draft.ttsConcurrentRequests,
        language: draft.targetLanguage,
        target_language: draft.targetLanguage
      };
    else if (key === 'export')
      stageSettings[key] = {
        export_mode: draft.exportMode,
        subtitle_format: draft.subtitleFormat,
        subtitle_mode: draft.subtitleMode,
        subtitle_selection: draft.subtitleSelection,
        audio_mode: draft.audioMode,
        subtitle_language_defaults: draft.subtitleLanguageDefaults,
        subtitle_max_chars_per_line: draft.subtitleChars,
        subtitle_max_lines: draft.subtitleLines,
        subtitle_min_duration_ms: draft.subtitleMinDuration,
        subtitle_max_duration_ms: draft.subtitleMaxDuration,
        subtitle_max_cps: draft.subtitleCps,
        subtitle_min_gap_ms: draft.subtitleMinGap,
        subtitle_phrase_gap_ms: draft.subtitlePhraseGap,
        subtitle_hard_gap_ms: draft.subtitleHardGap,
        subtitle_sentence_boundary_threshold:
          draft.subtitleSentenceBoundaryThreshold
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

  async function saveSettings(
    mode: 'session' | 'defaults' = 'session',
    runAfterSave = false
  ) {
    if (!settingsStage || settingsLoading || settingsSaving) return;
    const stage = settingsStage;
    const key = settingsStage.key;
    if (
      key === 'transcribe' &&
      (!hasAttachedCaptions || draft.captionAlignmentMethod !== 'ctc') &&
      sttLanguageIssue
    ) {
      error = sttLanguageIssue;
      return;
    }
    if (
      key === 'generate_audio' &&
      ttsSelection.ttsSwitchSource &&
      (!ttsSelection.ttsSwitchReviewed ||
        !ttsSelection.ttsModels.includes(draft.ttsModel))
    ) {
      error =
        'Choose an audio.cpp model and voice, then review the provider switch before saving.';
      return;
    }
    if (
      key === 'generate_audio' &&
      ttsSelection.selectedModelVoiceMode === 'design' &&
      !draft.generationPrompt.trim()
    ) {
      error =
        'Describe the voice in Speech direction before generating with Qwen3 VoiceDesign.';
      return;
    }
    if (key === 'generate_audio' && activity.publishingLibraryVoiceId) {
      error = `Wait for the selected library voice to finish ${ttsSelection.audioCppLinkedReferences ? 'linking' : 'uploading'}.`;
      return;
    }
    if (key === 'generate_audio' && ttsSelection.ttsLanguageIssue) {
      error = ttsSelection.ttsLanguageIssue;
      return;
    }
    if (key === 'generate_audio' && !ttsSelection.selectedTtsServiceAvailable) {
      error =
        ttsSelection.selectedTtsService?.availability_reason ||
        'Choose an available TTS service before saving generation settings.';
      return;
    }
    if (
      key === 'generate_audio' &&
      ttsSelection.showClonedVoices &&
      !ttsSelection.selectedModelAllowsReferenceFree &&
      (!draft.voiceName ||
        !ttsSelection.clonedVoiceIds.some(
          (voice) => voice.toLowerCase() === draft.voiceName.toLowerCase()
        ))
    ) {
      error = `Choose a ready cloned voice, or create and ${ttsSelection.audioCppLinkedReferences ? 'link' : 'upload'} one through the Voice Library.`;
      return;
    }
    if (
      mode === 'session' &&
      key === 'translate' &&
      !draft.translationSourceArtifactId
    ) {
      error = 'Choose the exact transcription or correction to translate.';
      return;
    }
    if (
      (key === 'transcribe' || key === 'correct') &&
      sourcePassages.customized &&
      !sourcePassages.valid
    ) {
      error = 'Correct the highlighted source-passage values before saving.';
      return;
    }
    captureStageSettings(settingsStage);
    const updates = stageSectionUpdates(key).map((update) => {
      if (mode !== 'defaults' || key !== 'translate') return update;
      const value = { ...update.value };
      delete value.source_artifact_id;
      return { ...update, value };
    });
    const submitted = updates.map((update) => ({
      ...update,
      value: structuredClone($state.snapshot(update.value))
    }));
    const bases = structuredClone($state.snapshot(settingsBases));
    const submittedOutcome = structuredClone($state.snapshot(outcome));
    const transformations: Record<string, boolean> | null =
      mode !== 'session'
        ? null
        : key === 'optimize_tts'
          ? {
              llm_tts_optimization: draft.optimizationEnabled,
              llm_tts_document_optimization: draft.documentOptimizationEnabled
            }
          : key === 'optimize_document'
            ? {
                llm_tts_document_optimization: draft.documentOptimizationEnabled
              }
            : null;
    const sourceProviderId = ttsSelection.ttsSwitchSource?.id;
    const opening = settingsOpening;
    const mutation = ++settingsMutation;
    const isCurrent = () =>
      !disposed && opening === settingsOpening && mutation === settingsMutation;
    const completed: string[] = [];
    settingsSaving = true;
    try {
      for (const update of submitted) {
        if (!bases[update.section])
          throw new Error('Reload these settings before saving changes.');
        if (
          mode === 'defaults' &&
          bases[update.section].global_revision == null
        )
          throw new Error(
            'Reload these settings before saving application defaults.'
          );
      }
      if (mode === 'defaults') {
        for (const update of submitted) {
          const saved = await persistDefaultSection(
            update.section,
            update.value,
            bases[update.section],
            sourceProviderId,
            completed
          );
          completed.push(`${sectionDisplay(update.section)} session settings`);
          if (isCurrent()) settingsBases[update.section] = saved;
        }
        if (isCurrent())
          stageMessage =
            'Saved as the application defaults for future sessions.';
      } else {
        for (const update of submitted) {
          const saved = await persistSection(
            update.section,
            update.value,
            bases[update.section]
          );
          completed.push(sectionDisplay(update.section));
          if (isCurrent()) settingsBases[update.section] = saved;
        }
      }
      if (transformations)
        await updateOutcomeTransformations(transformations, submittedOutcome);
      if (mode === 'session') {
        if (!isCurrent()) return;
        await load();
        if (!isCurrent()) return;
        closeStageSettings();
        if (runAfterSave) {
          const refreshed = workflowStore.snapshot?.stages.find(
            (item) => item.key === stage.key
          );
          await run(refreshed ?? stage);
        }
      }
    } catch (caught) {
      if (isCurrent())
        error = `${completed.length ? `Saved ${completed.join(', ')}. Remaining changes were not saved. ` : ''}${errorMessage(caught)}`;
    } finally {
      if (isCurrent()) settingsSaving = false;
    }
  }
</script>

{#if settingsStage && !fullSettingsSection}
  <div
    class="fixed inset-0 z-[60] grid place-items-center bg-black/35 p-5 backdrop-blur-sm"
    role="presentation"
    onclick={(event) =>
      event.target === event.currentTarget && closeStageSettings()}
  >
    <div
      use:modalFocus={{ onclose: () => closeStageSettings() }}
      class="surface flex max-h-[92vh] w-full max-w-xl flex-col overflow-hidden rounded-[1.7rem]"
      role="dialog"
      aria-modal="true"
      aria-labelledby="settings-title"
      aria-busy={settingsLoading || settingsSaving}
    >
      <div class="modal-scroll p-7">
        <div class="flex justify-between gap-5">
          <div>
            <h2 id="settings-title" class="mt-1 text-2xl font-semibold">
              {settingsStage.title}
            </h2>
          </div>
          <button
            onclick={() => closeStageSettings()}
            aria-label="Close stage settings"
            class="rounded-lg p-2"><X size={19} /></button
          >
        </div>
        {#if settingsLoading}<div
            role="status"
            class="mt-5 flex items-center gap-2 rounded-xl bg-[var(--accent-soft)] px-4 py-3 text-sm"
          >
            <LoaderCircle class="animate-spin" size={16} /> Loading available models
            and saved settings…
          </div>{/if}
        <fieldset
          disabled={settingsLoading || settingsSaving}
          class="mt-6 grid min-w-0 gap-5"
        >
          {#if settingsStage.key === 'correct' || (settingsStage.key === 'translate' && draft.backend === 'llm') || ['optimize_tts', 'optimize_document', 'clean_source'].includes(settingsStage.key)}<label
              class="text-sm font-semibold"
              >LLM model<select
                bind:value={draft.model}
                class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                ><option value="default">Application default</option
                >{#each llmModels as item}<option value={item.value}
                    >{item.label}{item.isDefault ? ' · default' : ''}</option
                  >{/each}</select
              ></label
            >{/if}
          {#if settingsStage.key === 'correct' || (settingsStage.key === 'translate' && draft.backend === 'llm')}
            <div
              class="rounded-xl border border-[var(--line)] bg-[var(--accent-soft)] p-4"
            >
              <label class="text-sm font-semibold"
                >Reasoning level<select
                  bind:value={draft.reasoningEffort}
                  class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                  ><option value="">Use model default</option><option
                    value="minimal">Minimal · fastest</option
                  ><option value="low">Low · economical</option><option
                    value="medium">Medium · balanced</option
                  ><option value="high">High · strongest</option></select
                ></label
              >
              <p class="muted mt-2 text-xs leading-relaxed">
                Higher reasoning can improve difficult passages, but usually
                adds latency and may add billed reasoning tokens. {#if draft.reasoningEffort}This
                  overrides the model default for this stage.{:else if selectedLlmModel?.defaultReasoningEffort}The
                  selected model currently defaults to
                  <strong>{selectedLlmModel.defaultReasoningEffort}</strong
                  >.{:else}The model or provider chooses the level.{/if}
                Availability depends on the selected model.
              </p>
            </div>
          {/if}
          {#if settingsStage.key === 'correct' || (settingsStage.key === 'translate' && draft.backend === 'llm') || ['optimize_tts', 'optimize_document'].includes(settingsStage.key)}
            <div class="rounded-xl border border-[var(--line)] p-4">
              <label class="text-sm font-semibold"
                >Concurrent LLM requests<input
                  type="number"
                  min="1"
                  max="16"
                  bind:value={draft.optimizationConcurrent}
                  class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                /></label
              >
              <p class="muted mt-2 text-xs leading-relaxed">
                1 is the quality-first default. Higher values process
                independent requests in parallel for speed.
                {#if settingsStage.key === 'correct'}Parallel correction cannot
                  include the preceding corrected batch.{:else if settingsStage.key === 'translate'}Parallel
                  translation cannot include the preceding translation or
                  glossary terms discovered by sibling batches.{:else}Units
                  inside one optimization request share context. Parallel
                  requests do not carry discoveries between them.{/if}
              </p>
            </div>
          {/if}
          {#if settingsStage.key === 'correct' || (settingsStage.key === 'translate' && draft.backend === 'llm')}
            <div class="rounded-xl border border-[var(--line)] p-4">
              <label class="block text-sm font-semibold"
                >Cue timing context<select
                  bind:value={draft.timingContextMode}
                  class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                >
                  <option value="full">Full timing · best quality</option>
                  <option value="overlap_only"
                    >Overlap only · fewer tokens</option
                  >
                  <option value="none">No timing context</option>
                </select><span
                  class="muted mt-2 block text-xs font-normal leading-relaxed"
                  >Full timing includes each cue interval and its preceding gap
                  or overlap exactly once. Overlap-only is a useful compromise
                  for simultaneous speech and ASR seam detection. None excludes
                  every timing field.</span
                ></label
              >
              {#if draft.timingContextMode === 'full'}<label
                  class="mt-4 block text-xs font-semibold"
                  ><ParameterLabel
                    section={settingsStage.key === 'correct'
                      ? 'correction'
                      : 'translation'}
                    name="substantial_gap_ms"
                    label="Model context gap (ms)"
                    compact
                  /><input
                    type="number"
                    min="0"
                    max="10000"
                    step="100"
                    bind:value={draft.timingContextGap}
                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                  /><span class="muted mt-1 block font-normal"
                    >The model is asked to preserve a rhetorical boundary at or
                    above this gap, and Pandrator prefers it when forming model
                    batches. It does not merge or split subtitle cues.</span
                  ></label
                >{/if}
            </div>
          {/if}
          {#if settingsStage.key === 'correct' || (settingsStage.key === 'translate' && draft.backend === 'llm')}
            <div class="rounded-xl border border-[var(--line)] p-4">
              <div class="grid grid-cols-2 gap-3">
                <label class="text-xs font-semibold"
                  >Maximum batch characters<input
                    type="number"
                    min="1"
                    max="100000"
                    step="100"
                    bind:value={draft.correctionBatchCharLimit}
                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                  /></label
                ><label class="text-xs font-semibold"
                  >Maximum cues per batch<input
                    type="number"
                    min="1"
                    max="500"
                    bind:value={draft.correctionBatchSegmentLimit}
                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                  /></label
                >
              </div>
              <p class="muted mt-2 text-xs leading-relaxed">
                Pandrator stops at whichever limit is reached first and prefers
                a sentence, speaker, or configured model-context gap. The
                quality-first defaults are 6,000 characters and 40 cues.
              </p>
              {#if settingsStage.key === 'correct' || draft.backend === 'llm'}
                <div class="mt-4 grid grid-cols-2 gap-3">
                  <label class="text-xs font-semibold"
                    >Previous output cues<input
                      type="number"
                      min="0"
                      max="20"
                      bind:value={draft.contextBefore}
                      class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                    /></label
                  ><label class="text-xs font-semibold"
                    >Following source cues<input
                      type="number"
                      min="0"
                      max="20"
                      bind:value={draft.contextAfter}
                      class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                    /></label
                  >
                </div>
                <p class="muted mt-2 text-xs leading-relaxed">
                  Boundary context improves names, sentence continuity, and
                  punctuation without making those cues editable. Sequential
                  mode can use corrected or translated output from the previous
                  batch; parallel mode cannot.
                </p>
                <label class="mt-4 flex items-start gap-3 text-sm font-semibold"
                  ><input
                    type="checkbox"
                    bind:checked={draft.preventSubtitleRemoval}
                    class="mt-1 accent-[var(--accent)]"
                  /><span
                    >Prevent cue removal<span
                      class="muted mt-1 block text-xs font-normal leading-relaxed"
                      >Every source cue must survive correction or translation.
                      Enable this when omissions would be worse than preserving
                      an uncertain filler or ASR artifact.</span
                    ></span
                  ></label
                >
              {/if}
            </div>
          {/if}
          {#if settingsStage.key === 'correct' || (settingsStage.key === 'translate' && draft.backend === 'llm')}
            <fieldset class="rounded-xl border border-[var(--line)] p-4">
              <legend class="px-1 text-sm font-semibold">Web research</legend>
              <label class="flex items-start gap-3 text-sm font-semibold">
                <input
                  type="checkbox"
                  bind:checked={draft.webResearchEnabled}
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
              {#if draft.webResearchEnabled}
                <div class="mt-4 grid gap-4">
                  <label class="text-xs font-semibold">
                    Researcher model
                    <select
                      bind:value={draft.webResearchModel}
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
                      bind:value={draft.webResearchMode}
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
                      draft.webResearchContextFraction * 100
                    )}%)
                    <input
                      type="range"
                      min="0.1"
                      max="0.8"
                      step="0.05"
                      bind:value={draft.webResearchContextFraction}
                      class="mt-2 w-full accent-[var(--accent)]"
                    />
                  </label>
                  <p class="muted text-xs leading-relaxed">
                    {#if draft.webResearchMode === 'global'}The researcher
                      receives up to this share of its context in deterministic
                      batches, then the consolidated evidence is reused by every
                      request.{:else}Per-chunk research runs as a sequential
                      prepass so each chunk can refine the accumulated evidence.
                      Transformation requests may still run concurrently after
                      that prepass.{/if}
                  </p>
                </div>
              {/if}
            </fieldset>
          {/if}
          {#if settingsStage.key === 'transcribe'}
            <TranscriptionStageSettings
              {draft}
              workflowKind={session.workflow_kind}
              {hasAttachedCaptions}
              {capabilities}
              {sttCatalogue}
              {sttLanguageIssue}
              {subtitleSettingsPayload}
            />
          {/if}
          {#if settingsStage.key === 'transcribe' || settingsStage.key === 'correct'}
            <SourcePassageSettings state={sourcePassages} />
          {/if}
          {#if settingsStage.key === 'correct'}<label
              class="text-sm font-semibold"
              ><ParameterLabel
                section="correction"
                name="correction_style"
                label="Correction approach"
              /><select
                bind:value={draft.correctionStyle}
                class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                ><option value="publishable"
                  >Publication-ready · remove disfluencies</option
                ><option value="faithful"
                  >Transcript-faithful · preserve delivery</option
                ></select
              ></label
            ><label class="text-sm font-semibold"
              >Additional correction guidance<textarea
                bind:value={draft.instructions}
                rows="4"
                class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
              ></textarea><span
                class="muted mt-1 block text-xs font-normal leading-relaxed"
                >These instructions and the approach above are sent to the
                model. Cue size and silence controls are applied separately by
                Pandrator.</span
              ></label
            >{/if}
          {#if settingsStage.key === 'translate'}<label
              class="text-sm font-semibold"
              >Translate from<select
                bind:value={draft.translationSourceArtifactId}
                required
                class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                >{#if !draft.translationSourceArtifactId}<option
                    value=""
                    disabled>Choose subtitle input</option
                  >{/if}{#each subtitleCatalogItems as item (item.artifact_id)}<option
                    value={item.artifact_id}>{subtitleSourceLabel(item)}</option
                  >{/each}</select
              ><span
                class="muted mt-1 block text-xs font-normal leading-relaxed"
                >Choose the exact transcription or corrected revision. A
                correction no longer silently replaces your selected
                transcription.</span
              ></label
            ><label class="text-sm font-semibold"
              >Translation backend<select
                bind:value={draft.backend}
                class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                ><option value="llm">LLM</option><option value="deepl"
                  >DeepL</option
                ></select
              ></label
            ><label class="text-sm font-semibold"
              >Target language<select
                bind:value={draft.targetLanguage}
                class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                >{#each LANGUAGE_OPTIONS.filter((item) => item.value !== 'auto') as item}<option
                    value={item.value}>{item.label}</option
                  >{/each}</select
              ></label
            >{#if draft.backend === 'llm'}<label class="text-sm font-semibold"
                >Translation guidance<textarea
                  bind:value={draft.instructions}
                  rows="3"
                  class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                ></textarea><span
                  class="muted mt-1 block text-xs font-normal leading-relaxed"
                  >The model creates natural display subtitles. Optional speech
                  optimization remains a separate, reviewable layer for
                  voiceover.</span
                ></label
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
                    bind:group={draft.optimizationTiming}
                    value="document"
                    class="mt-1 accent-[var(--accent)]"
                  /><span
                    ><strong class="block"
                      >Before generation · reviewable revision</strong
                    ><span class="muted mt-1 block text-xs"
                      >Process the document's existing narration units, create
                      an editable before-and-after artifact, and review it
                      before TTS.</span
                    ></span
                  ></label
                ><label
                  class="flex items-start gap-3 rounded-xl bg-[var(--accent-soft)] p-3 text-sm"
                  ><input
                    type="radio"
                    bind:group={draft.optimizationTiming}
                    value="generation"
                    disabled={draft.speechAnnotationMode !== 'off'}
                    class="mt-1 accent-[var(--accent)]"
                  /><span
                    ><strong class="block"
                      >During plan preparation · final speech units</strong
                    ><span class="muted mt-1 block text-xs"
                      >Optimize the final speech blocks before reviewing
                      speakers and delivery. Generation uses the accepted
                      wording.</span
                    ></span
                  ></label
                >
              </div>
            </fieldset>
            <p class="muted text-xs">
              Speaker identification and delivery directions have separate
              analysis controls in the prepared speech plan. They keep spoken
              words unchanged.
            </p>
            {#if draft.speechAnnotationMode !== 'off'}<details
                class="rounded-xl border border-[var(--line)] p-3"
              >
                <summary class="cursor-pointer text-sm"
                  >Existing combined text and speaker preparation</summary
                >
                <p class="muted my-2 text-xs">
                  This session already has recognition enabled in its text step.
                  These settings are preserved for compatibility. Use the
                  separate speech-plan analysis for new speaker passes.
                </p>
                <fieldset
                  class="speech-controls rounded-xl border border-[var(--line)] p-4 space-y-3"
                >
                  <legend class="px-1 text-sm font-semibold"
                    >Dialogue and character recognition</legend
                  >
                  <label class="block text-sm"
                    >Annotation level
                    <select
                      class="input mt-1 w-full"
                      bind:value={draft.speechAnnotationMode}
                      onchange={() => {
                        if (draft.speechAnnotationMode !== 'off')
                          draft.optimizationTiming = 'document';
                      }}
                    >
                      <option value="off">Preserve supplied markup</option>
                      <option value="dialogue"
                        >Recognize dialogue and turn boundaries</option
                      >
                      <option value="speakers"
                        >Recognize dialogue and identify characters</option
                      >
                    </select>
                  </label>
                  <label class="flex items-start gap-2 text-sm"
                    ><input
                      type="checkbox"
                      bind:checked={draft.speechAnnotationOnly}
                    />Annotate only — keep every spoken word unchanged</label
                  >
                  <p class="muted text-xs">
                    Dialogue annotations preserve integral segments and avoid
                    treating each dialogue line as a long paragraph pause.
                    Character proposals use the session dictionary. Review
                    structure and casting before generation; emotional
                    directions remain optional.
                  </p>
                </fieldset>
              </details>{/if}
            <label class="text-sm font-semibold"
              >Speech-planning policy<select
                bind:value={draft.speechOptimizationMode}
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
                >Units per model request{#if draft.optimizationTiming === 'document'}<input
                    type="number"
                    min="1"
                    max="64"
                    bind:value={draft.documentOptimizationBatchSize}
                    class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                  />{:else}<input
                    type="number"
                    min="1"
                    max="64"
                    bind:value={draft.optimizationBatchSize}
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
                bind:checked={draft.agentic}
                class="mt-1 size-4 accent-[var(--accent)]"
              /><span
                ><span class="block text-sm font-semibold"
                  >Agentic review loop</span
                ><span class="muted mt-1 block text-xs"
                  >Runs focused metadata, navigation, boilerplate,
                  repeated-element, and chapter passes. Provider costs may
                  apply.</span
                ></span
              ></label
            >{#if draft.agentic}<label class="text-sm font-semibold"
                >Maximum LLM turns<input
                  type="number"
                  min="5"
                  max="500"
                  bind:value={draft.maxIterations}
                  class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                /></label
              >{/if}{/if}
          {#if settingsStage.key === 'prepare_text'}<div
              class="rounded-xl border border-[var(--line)] bg-[var(--accent-soft)] p-4"
            >
              <div class="text-sm font-semibold">
                Model-aware narration length
              </div>
              <p class="muted mt-1 text-xs leading-relaxed">
                Pack complete sentences up to the selected model’s narration
                budget, leaving headroom for generation. Paragraph and chapter
                breaks remain intact. Changes apply when preparing new
                narration.
              </p>
            </div>
            <div class="grid gap-3 sm:grid-cols-2">
              <label class="text-sm font-semibold sm:col-span-2"
                >Segment length policy<select
                  bind:value={draft.audiobookChunking}
                  class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                  ><option value="model"
                    >Automatic · selected model with headroom</option
                  ><option value="manual">Custom character limit</option
                  ></select
                ></label
              >
              <label
                class="flex items-center gap-3 rounded-xl border border-[var(--line)] p-3 text-sm font-semibold"
                ><input
                  type="checkbox"
                  bind:checked={draft.splitSentences}
                  class="size-4 accent-[var(--accent)]"
                /> Split long sentences</label
              ><label
                class="flex items-center gap-3 rounded-xl border border-[var(--line)] p-3 text-sm font-semibold"
                ><input
                  type="checkbox"
                  bind:checked={draft.appendSentences}
                  class="size-4 accent-[var(--accent)]"
                /> Join short sentences</label
              ><label class="text-sm font-semibold"
                >Custom maximum characters<input
                  type="number"
                  min="20"
                  max="8192"
                  disabled={draft.audiobookChunking !== 'manual'}
                  bind:value={draft.maxSentenceLength}
                  class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                /></label
              ><label
                class="flex items-center gap-3 rounded-xl border border-[var(--line)] p-3 text-sm font-semibold"
                ><input
                  type="checkbox"
                  bind:checked={draft.nemoNormalization}
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
                    bind:checked={draft.normalizeAllCaps}
                    class="size-4 accent-[var(--accent)]"
                  /> Normalize all-caps text</label
                ><label class="flex items-center gap-3 text-sm"
                  ><input
                    type="checkbox"
                    bind:checked={draft.removeDiacritics}
                    class="size-4 accent-[var(--accent)]"
                  /> Remove diacritics</label
                ><label class="flex items-center gap-3 text-sm"
                  ><input
                    type="checkbox"
                    bind:checked={draft.removeQuotationMarks}
                    class="size-4 accent-[var(--accent)]"
                  /> Remove quotation marks</label
                >
              </div>
            </details>{/if}
          {#if settingsStage.key === 'generate_audio'}
            <GenerationStageSettings
              {draft}
              {ttsSelection}
              {activity}
              {speechCatalogues}
              actions={{
                chooseTtsService,
                refreshSpeechServices,
                openTtsServices,
                chooseTtsModel,
                loadXttsModels,
                removeXttsModel,
                chooseXttsModelFiles,
                uploadXttsModel,
                openVoiceLibrary,
                useLibraryVoice
              }}
            />
          {/if}
          {#if settingsStage.key === 'export'}
            <label class="text-sm font-semibold"
              ><ParameterLabel
                section="output"
                name="export_mode"
                label="Export target"
              /><select
                bind:value={draft.exportMode}
                class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                >{#if session.workflow_kind !== 'subtitles'}<option
                    value="media">Rendered video / media</option
                  >{/if}<option value="subtitles">Subtitle file</option><option
                  value="text">Concatenated plain text</option
                ></select
              ></label
            >
            {#if draft.exportMode === 'media'}
              <label class="text-sm font-semibold"
                ><ParameterLabel
                  section="output"
                  name="audio_mode"
                  label="Audio"
                /><select
                  bind:value={draft.audioMode}
                  class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                  ><option value="mixed"
                    >Mix source and dubbing (recommended)</option
                  ><option value="preserve">Preserve source audio</option
                  ><option value="dubbing_only">Dubbing only</option></select
                ></label
              ><label class="text-sm font-semibold"
                ><ParameterLabel
                  section="output"
                  name="subtitle_mode"
                  label="Subtitles"
                /><select
                  bind:value={draft.subtitleMode}
                  class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                  ><option value="none">None</option><option value="soft"
                    >Injected soft tracks</option
                  ><option value="burned">Burned subtitles</option></select
                ></label
              >
            {:else if draft.exportMode === 'subtitles'}
              <label class="text-sm font-semibold"
                ><ParameterLabel
                  section="output"
                  name="subtitle_format"
                  label="Subtitle format"
                /><select
                  bind:value={draft.subtitleFormat}
                  class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
                  ><option value="srt">SubRip (.srt)</option><option value="vtt"
                    >WebVTT (.vtt)</option
                  ></select
                ></label
              >
            {:else}<p
                class="muted rounded-xl bg-[var(--accent-soft)] p-3 text-xs"
              >
                Cue timestamps and numbering are removed and the selected
                subtitle text is joined into one plain-text document.
              </p>{/if}
            {#if draft.exportMode !== 'media' || draft.subtitleMode !== 'none'}<label
                class="text-sm font-semibold"
                ><ParameterLabel
                  section="output"
                  name="subtitle_selection"
                  label="Subtitle tracks"
                /><select
                  bind:value={draft.subtitleSelection}
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
              <label class="mt-3 flex items-center gap-2 text-xs font-semibold">
                <input
                  type="checkbox"
                  bind:checked={draft.subtitleLanguageDefaults}
                />
                Automatic language limits
              </label>
              <SubtitleLimitsSummary
                automatic={draft.subtitleLanguageDefaults}
                profiles={subtitleSettingsPayload?.subtitle_automatic_profiles}
                chars={draft.subtitleChars}
                cps={draft.subtitleCps}
                lines={draft.subtitleLines}
              />
              <div class="mt-3 grid grid-cols-2 gap-3">
                {#if !draft.subtitleLanguageDefaults}<label
                    class="text-xs font-semibold"
                    ><ParameterLabel
                      section="subtitles"
                      name="max_chars_per_line"
                      label="Display units / line"
                      compact
                    /><input
                      type="number"
                      min="8"
                      max="100"
                      bind:value={draft.subtitleChars}
                      class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                    /></label
                  >{/if}<label class="text-xs font-semibold"
                  ><ParameterLabel
                    section="subtitles"
                    name="max_lines"
                    label="Lines"
                    compact
                  /><input
                    type="number"
                    min="1"
                    max="3"
                    bind:value={draft.subtitleLines}
                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                  /></label
                ><label class="text-xs font-semibold"
                  ><ParameterLabel
                    section="subtitles"
                    name="min_duration_ms"
                    label="Minimum duration (ms)"
                    compact
                  /><input
                    type="number"
                    min="250"
                    bind:value={draft.subtitleMinDuration}
                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                  /></label
                ><label class="text-xs font-semibold"
                  ><ParameterLabel
                    section="subtitles"
                    name="max_duration_ms"
                    label="Maximum duration (ms)"
                    compact
                  /><input
                    type="number"
                    min="1000"
                    bind:value={draft.subtitleMaxDuration}
                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                  /></label
                >{#if !draft.subtitleLanguageDefaults}<label
                    class="text-xs font-semibold"
                    ><ParameterLabel
                      section="subtitles"
                      name="max_cps"
                      label="Reading-speed target (units/second)"
                      compact
                    /><input
                      type="number"
                      min="1"
                      max="40"
                      step="0.5"
                      bind:value={draft.subtitleCps}
                      class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                    /></label
                  >{/if}<label class="text-xs font-semibold"
                  ><ParameterLabel
                    section="subtitles"
                    name="min_gap_ms"
                    label="Minimum cue gap (ms)"
                    compact
                  /><input
                    type="number"
                    min="0"
                    max="500"
                    bind:value={draft.subtitleMinGap}
                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                  /></label
                ><label class="text-xs font-semibold"
                  ><ParameterLabel
                    section="subtitles"
                    name="phrase_gap_ms"
                    label="Subtitle grouping gap (ms)"
                    compact
                  /><input
                    type="number"
                    min="100"
                    max="3000"
                    bind:value={draft.subtitlePhraseGap}
                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                  /></label
                ><label class="text-xs font-semibold"
                  ><ParameterLabel
                    section="subtitles"
                    name="hard_gap_ms"
                    label="Hard silence boundary (ms)"
                    compact
                  /><input
                    type="number"
                    min="250"
                    max="5000"
                    bind:value={draft.subtitleHardGap}
                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                  /></label
                ><label class="text-xs font-semibold"
                  ><ParameterLabel
                    section="subtitles"
                    name="sentence_boundary_threshold"
                    label="Sentence boundary threshold"
                    compact
                  /><input
                    type="number"
                    min="0.01"
                    max="0.99"
                    step="0.01"
                    bind:value={draft.subtitleSentenceBoundaryThreshold}
                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
                  /></label
                >
              </div>
            </div>
          {/if}
        </fieldset>
        {#if stageMessage}<p
            role="status"
            class="mt-5 rounded-xl bg-[var(--accent-soft)] p-3 text-xs"
          >
            {stageMessage}
          </p>{/if}
        {#if error}<p
            role="alert"
            class="mt-5 rounded-xl border border-red-400/40 bg-red-500/10 p-3 text-xs"
          >
            {error}
          </p>{/if}
        <div class="mt-7 flex flex-wrap justify-end gap-3">
          <button
            onclick={openFullSettingsFromStage}
            disabled={settingsLoading || settingsSaving}
            class="mr-auto rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold"
            >All {sectionDisplay(stageSection(settingsStage.key))} settings</button
          ><button
            onclick={revertStageToDefaults}
            disabled={settingsLoading || settingsSaving}
            class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold"
            ><RotateCcw size={15} /> Revert to defaults</button
          ><button
            onclick={() => saveSettings('defaults')}
            disabled={settingsLoading ||
              settingsSaving ||
              Boolean(activity.publishingLibraryVoiceId) ||
              ((settingsStage.key === 'transcribe' ||
                settingsStage.key === 'correct') &&
                sourcePassages.customized &&
                !sourcePassages.valid) ||
              (settingsStage.key === 'generate_audio' &&
                (!ttsSelection.selectedTtsServiceAvailable ||
                  ttsSelection.selectedVoiceLanguageMismatch ||
                  ttsSelection.invalidTtsConcurrency ||
                  (Boolean(ttsSelection.ttsSwitchSource) &&
                    (!ttsSelection.ttsSwitchReviewed ||
                      !ttsSelection.ttsModels.includes(draft.ttsModel)))))}
            class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold disabled:opacity-40"
            ><Save size={15} /> Save as defaults</button
          ><button
            onclick={() => closeStageSettings()}
            class="rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold"
            >Cancel</button
          ><button
            onclick={() =>
              saveSettings(
                'session',
                session.workflow_kind === 'media_edit' &&
                  settingsStage?.key === 'transcribe' &&
                  settingsStage.status !== 'running'
              )}
            disabled={settingsLoading ||
              settingsSaving ||
              Boolean(activity.publishingLibraryVoiceId) ||
              ((settingsStage.key === 'transcribe' ||
                settingsStage.key === 'correct') &&
                sourcePassages.customized &&
                !sourcePassages.valid) ||
              (settingsStage.key === 'translate' &&
                !draft.translationSourceArtifactId) ||
              (settingsStage.key === 'generate_audio' &&
                (!ttsSelection.selectedTtsServiceAvailable ||
                  ttsSelection.selectedVoiceLanguageMismatch ||
                  ttsSelection.invalidTtsConcurrency ||
                  (Boolean(ttsSelection.ttsSwitchSource) &&
                    (!ttsSelection.ttsSwitchReviewed ||
                      !ttsSelection.ttsModels.includes(draft.ttsModel)))))}
            class="rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-40"
            >{session.workflow_kind === 'media_edit' &&
            settingsStage.key === 'transcribe' &&
            settingsStage.status !== 'running'
              ? hasAttachedCaptions
                ? 'Save & align captions'
                : 'Save & transcribe'
              : 'Save settings'}</button
          >
        </div>
      </div>
    </div>
  </div>
{/if}

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
