<script lang="ts">
  import GenerationRunHistory from './GenerationRunHistory.svelte';
  import {
    generationHistoryVersion,
    generationHistoryStatus,
    visibleGenerationRuns
  } from './generation-history';
  import { selectableTtsServices } from './tts-provider-policy';
  import { errorMessage } from './errors';
  import PassageSplitDialog from './PassageSplitDialog.svelte';
  import PassageActionsPopover from './PassageActionsPopover.svelte';
  import SpeechAnnotationPopover from './SpeechAnnotationPopover.svelte';
  import PerformancePanel from './PerformancePanel.svelte';
  import type { SpeechSelection, SpeechPreview } from './speech-annotations';
  import SpeechSelectionEditor from './SpeechSelectionEditor.svelte';
  import { modalFocus } from './modal-focus';
  import {
    readPassageVisibility,
    savePassageVisibility,
    type PassageBoundary,
    type PassageTextLayer
  } from './passage-structure';
  import {
    GenerationEditQueue,
    collectStaleSegmentIds
  } from './generation-edit-queue';
  import {
    ChevronDown,
    ChevronUp,
    BookOpenText,
    Download,
    Eye,
    ListMusic,
    Maximize2,
    Minimize2,
    Pause,
    Play,
    RefreshCw,
    Search,
    Settings,
    Sparkles,
    Square,
    Trash2,
    Undo2,
    WandSparkles,
    X
  } from '@lucide/svelte';
  import { onMount, tick, untrack } from 'svelte';
  import { appState } from './app-state.svelte';
  import {
    generationApi,
    sessionApi,
    type GenerationSegmentChanges
  } from './domain-api';
  import type {
    AudioTake,
    GenerationRun,
    GenerationSegment,
    SpeechPlan,
    TtsCatalogue,
    VoiceRecord
  } from './api-models';
  import {
    GenerationStore,
    SEGMENT_FILTER_OPTIONS,
    type GenerationLoadResult,
    type SegmentFilter
  } from './generation-store.svelte';
  import TtsServicesModal from './TtsServicesModal.svelte';
  import AudioPlayer from './AudioPlayer.svelte';
  import GenerationSegmentTable from './GenerationSegmentTable.svelte';
  import GenerationReadingView from './GenerationReadingView.svelte';
  import SpeechPlanReviewDialog from './SpeechPlanReviewDialog.svelte';
  import SpeechPlanHistory from './SpeechPlanHistory.svelte';
  import GenerationPlanReviewBar from './GenerationPlanReviewBar.svelte';
  import GenerationStartDialog from './GenerationStartDialog.svelte';
  import type { GenerationStartMode } from './generation-start';
  import {
    sessionFlowAction,
    notifySessionFlowChange,
    subscribeSpeechPlanEditor
  } from './session-flow';
  import SearchReplaceBar from './SearchReplaceBar.svelte';
  import type { TextReplacement, TextSearchMatch } from './search-replace';
  import { LANGUAGE_OPTIONS } from './settings-fields';
  import {
    describeVoice,
    languagesForService,
    type VoiceDescriptor
  } from './voice-catalog';
  import {
    getTtsCompactCatalogue,
    getVoiceLibrary
  } from './tts-catalogue-cache';
  import type {
    ComparisonDecisionRow,
    PlayableTake,
    ReadingBlock
  } from './generation-view-models';
  import { GenerationPlaybackController } from './generation-playback.svelte';
  const hasArtifact = (take: AudioTake): take is PlayableTake =>
    Boolean(take.artifact_id);

  let {
    sessionId,
    workflowKind = 'audiobook'
  }: {
    sessionId: string;
    workflowKind?: string;
  } = $props();
  const isVoiceover = $derived(workflowKind === 'voiceover');
  const generationStore = new GenerationStore(untrack(() => sessionId));
  let mode = $state<'collapsed' | 'half' | 'full'>('collapsed');
  let headerHeight = $state(64);
  const payload = $derived(generationStore.payload);
  const run = $derived(generationStore.activeRun);
  const runs = $derived(visibleGenerationRuns(generationStore.runs));
  const takeNumbers = $derived.by(() => {
    const numbers = new Map<string, number>();
    for (const item of payload.items) {
      [...item.takes]
        .sort((left, right) =>
          String(left.created_at ?? '').localeCompare(
            String(right.created_at ?? '')
          )
        )
        .forEach((take, index) => numbers.set(take.id, index + 1));
    }
    return numbers;
  });
  const latestFailure = $derived.by(() => {
    const latest = generationStore.runs.find(
      (item) => !item.early_repair_parent_run_id
    );
    if (
      latest?.status !== 'failed' ||
      ['queued', 'running', 'pausing', 'cancel_requested'].includes(
        run?.status ?? ''
      )
    )
      return null;
    const owner = runs.find(
      (item) => item.id === latest.output_generation_run_id
    );
    if (
      owner?.updated_at &&
      latest.updated_at &&
      owner.updated_at > latest.updated_at
    )
      return null;
    return latest;
  });
  let selectedRunId = $state('');
  let selectedRunVersionId = $state('');
  // Editing creates a new plan; its settings source survives leaving history.
  let settingsSourceRunId = $state('');
  const settingsSourceRun = $derived(
    generationStore.runs.find((item) => item.id === settingsSourceRunId) ?? null
  );
  const pendingGeneration = $derived.by(() => {
    const messages: Record<string, string> = {};
    for (const queued of generationStore.runs) {
      if (
        queued.status !== 'queued' ||
        queued.plan_revision_id !== payload.plan_revision_id
      )
        continue;
      const ids = queued.queued_segment_ids ?? [];
      const message = queued.waiting_for_job
        ? queued.waiting_for_job.kind.startsWith('export')
          ? 'Regeneration queued — waiting for export'
          : 'Generation queued — waiting for other session work'
        : 'Generation queued — waiting for a worker';
      for (const item of payload.items) {
        if (!ids.length || ids.includes(item.id)) messages[item.id] = message;
      }
    }
    return messages;
  });
  const assembly = $derived(generationStore.assembly);
  let filter = $state<SegmentFilter>('all');
  let error = $state('');
  let loading = $state(false);
  let generationStartDialog = $state<{ mode: GenerationStartMode } | null>(
    null
  );
  let timer: number | undefined;
  let startedRunReconciliation: AbortController | undefined;
  let selectedRow = $state('');
  let previewSegmentId = $state('');
  let selectedRows = $state<string[]>([]);
  let selectionAnchor = $state('');
  let viewMode = $state<'segments' | 'reading'>('segments');
  let segmentScrollRoot = $state<HTMLDivElement | null>(null);
  let segmentTable = $state<{
    scrollToSegment: (
      id: string,
      options?: {
        align?: 'center' | 'nearest' | 'start' | 'end';
        focus?: string | boolean;
      }
    ) => Promise<boolean>;
  }>();

  async function revealSegment(
    id: string,
    align: 'center' | 'nearest' = 'nearest'
  ) {
    await tick();
    if (viewMode === 'segments' && segmentTable) {
      return segmentTable.scrollToSegment(id, { align });
    }
    const row = document.querySelector<HTMLElement>(
      `[data-segment-id="${CSS.escape(id)}"]`
    );
    row?.scrollIntoView({ block: align, behavior: 'auto' });
    return Boolean(row);
  }
  let textMode = $state<'display' | 'speech'>('display');
  let displayMenuOpen = $state(false);
  let showPassageBoundaries = $state(readPassageVisibility());
  let showSpeechAnnotations = $state(true);
  let speechPreviews = $state<Record<string, SpeechPreview>>({});
  let speechSelection = $state<SpeechSelection | null>(null);
  function editSelectedSpeech(selection: SpeechSelection) {
    if (selectedRunId || !payload.plan_revision_id || pendingSegmentUpdates)
      return;
    speechTarget = null;
    passageActions = null;
    speechSelection = selection;
  }
  type SpeechTarget = {
    item: GenerationSegment;
    offset: number;
    anchor: HTMLButtonElement;
    activate: boolean;
    revisionId: string;
    runId: string;
  };
  let speechTarget = $state<SpeechTarget | null>(null);
  let directionsTarget = $state<SpeechTarget | null>(null);
  let directionsPanel = $state<{ requestClose: (next: () => void) => void }>();
  function closeDirections() {
    if (directionsPanel)
      directionsPanel.requestClose(() => {
        directionsTarget = null;
      });
    else directionsTarget = null;
  }
  function inspectSpeech(
    item: GenerationSegment,
    offset: number,
    anchor: HTMLButtonElement,
    activate: boolean
  ) {
    const revisionId = payload.plan_revision_id;
    if (!revisionId) return;
    if (speechSelection) return;
    passageActions = null;
    speechTarget = {
      item,
      offset,
      anchor,
      activate:
        activate ||
        (speechTarget?.anchor === anchor && speechTarget.activate) ||
        false,
      revisionId,
      runId: selectedRunVersionId || selectedRunId
    };
  }
  let showRepairHistory = $state(false);
  type PassageTarget = {
    item: GenerationSegment;
    layer: PassageTextLayer;
    boundary: PassageBoundary;
    planRevisionId: string | null;
  };
  let passagePreview = $state<PassageTarget | null>(null);
  let passageActions = $state<
    | (PassageTarget & {
        anchor: HTMLButtonElement;
        activate: boolean;
      })
    | null
  >(null);

  function passageTargetDisabledReason(target: PassageTarget | null) {
    if (!target) return '';
    if (selectedRunId)
      return 'History is read-only. Select the active mix to split.';
    if (pendingSegmentUpdates > 0)
      return 'Wait for the current text edit to finish saving.';
    if (payload.plan_revision_id !== target.planRevisionId)
      return 'The plan changed. Close this preview and inspect the boundary again.';
    const current = payload.items.find((item) => item.id === target.item.id);
    if (
      !current ||
      current.revision !== target.item.revision ||
      !current.passage_structure?.layers[target.layer].boundaries.some(
        (boundary) => boundary.id === target.boundary.id
      )
    )
      return 'The text or passage mapping changed. Close this preview and inspect it again.';
    return error || '';
  }
  const passageDisabledReason = $derived(
    passageTargetDisabledReason(passagePreview)
  );

  function inspectPassage(
    item: GenerationSegment,
    layer: PassageTextLayer,
    boundary: PassageBoundary,
    anchor?: HTMLButtonElement,
    activate = false
  ) {
    if (passagePreview) return;
    const target = {
      item,
      layer,
      boundary,
      planRevisionId: payload.plan_revision_id
    };
    if (anchor) passageActions = { ...target, anchor, activate };
    else passagePreview = target;
  }

  function previewPassageAction() {
    if (!passageActions) return;
    passagePreview = passageActions;
    passageActions = null;
  }

  async function splitPassageBoundary(target = passagePreview) {
    await editQueue.settledIds([]).catch((caught) => {
      error = errorMessage(caught);
    });
    if (
      !target ||
      passageTargetDisabledReason(target) ||
      topologyBusy ||
      !target.boundary.split_allowed ||
      target.boundary.split_blocked_reason
    )
      return;
    const succeeded = await reviseSpeechBlocks({
      action: 'split',
      segment_id: target.item.id,
      text_layer: target.layer,
      cursor: target.boundary.offset,
      passage_boundary_id: target.boundary.id
    });
    if (succeeded) passagePreview = null;
  }

  function quickSplitPassage() {
    const target = passageActions;
    if (!target) return;
    if (!target.boundary.natural) return previewPassageAction();
    passageActions = null;
    void splitPassageBoundary(target);
  }
  let settingsMenuOpen = $state(false);
  let regenerateMenuOpen = $state(false);
  let rvcModels = $state<string[]>([]);
  let rvcModel = $state('');
  let rvcPitch = $state(0);
  let rvcF0 = $state('rmvpe');
  let rvcIndexRate = $state(0.3);
  let showRvc = $state(false);
  let ttsServicesOpen = $state(false);
  let comparisonItem = $state<GenerationSegment | null>(null);
  let comparisonText = $state('');
  let regenerateAfterReview = $state(true);
  let comparisonDiff = $state(false);
  let searchLoading = $state(false);
  let searchQuery = $state('');
  let searchOptions = $state({ matchCase: false, wholeWord: false });
  // The search panel is hidden by default behind a toolbar magnifier toggle.
  // While hidden, searchQuery stays empty so no filter affects the visible
  // list and no full-plan scan runs.
  let searchOpen = $state(false);
  // Voiceover-only search/replace target. Audiobook keeps following textMode.
  let searchScope = $state<'cue' | 'tts'>('cue');
  let searchItems = $state<
    Pick<
      GenerationSegment,
      | 'id'
      | 'ordinal'
      | 'revision'
      | 'text'
      | 'optimized_text'
      | 'search_matches'
    >[]
  >([]);
  let searchController: AbortController | undefined;
  // Effective searched/replaced field. Voiceover uses the explicit scope
  // selector (Cue text -> text, TTS text -> spoken); other workflows keep
  // following the display text layer. Matches the backend text_field contract.
  const searchField = $derived<'text' | 'spoken'>(
    isVoiceover
      ? searchScope === 'tts'
        ? 'spoken'
        : 'text'
      : textMode === 'speech'
        ? 'spoken'
        : 'text'
  );
  const searchParams = $derived.by(() => {
    const params = new URLSearchParams();
    if (searchQuery) {
      params.set('q', searchQuery);
      params.set('match_case', String(searchOptions.matchCase));
      params.set('whole_word', String(searchOptions.wholeWord));
      params.set('text_field', searchField);
    }
    return params;
  });
  let speechOptionsLoading = $state(false);
  let topologyBusy = $state(false);
  let supportingOptionsLoaded = false;
  let ttsSettings = $state<Record<string, unknown>>({});
  let ttsCatalogue = $state<TtsCatalogue>({ services: [] });
  let libraryVoices = $state<VoiceRecord[]>([]);
  let alternateOpen = $state(false);
  let pendingSegmentUpdates = $state(0);
  const editQueue = new GenerationEditQueue();
  const savedEditRows = new Map<string, GenerationSegment>();
  let regenerationNotice = $state('');
  let alternateSegmentIds = $state<string[]>([]);
  let alternateTts = $state<Record<string, unknown>>({});
  let alternateRvc = $state<Record<string, unknown>>({ enabled: false });

  const normalizeId = (value: unknown) =>
    String(value ?? '')
      .trim()
      .toLowerCase()
      .replaceAll('-', '_');
  const progressPercent = (value: unknown) => {
    const numeric = Number(value ?? 0);
    return Math.round(
      Math.max(0, Math.min(1, Number.isFinite(numeric) ? numeric : 0)) * 100
    );
  };
  const inheritedTtsSettings = $derived.by(() => {
    const saved = settingsSourceRun?.settings_snapshot?.tts;
    return saved && typeof saved === 'object'
      ? (saved as Record<string, unknown>)
      : ttsSettings;
  });
  const selectedTtsService = $derived.by(() => {
    const configured = String(
      inheritedTtsSettings.service ??
        inheritedTtsSettings.tts_service ??
        ttsCatalogue.default_service ??
        ''
    );
    return (
      ttsCatalogue.services.find((service) =>
        [service.id, service.name].some(
          (value) => normalizeId(value) === normalizeId(configured)
        )
      ) ?? null
    );
  });
  const selectedTtsModel = $derived(
    String(
      inheritedTtsSettings.model ||
        inheritedTtsSettings.xtts_model ||
        selectedTtsService?.default_model ||
        ''
    )
  );
  const inheritedLanguage = $derived(
    String(
      inheritedTtsSettings.language ||
        inheritedTtsSettings.target_language ||
        'en'
    )
  );
  const selectedTtsServiceId = $derived(
    normalizeId(
      selectedTtsService?.id ??
        inheritedTtsSettings.service ??
        inheritedTtsSettings.tts_service
    )
  );
  const modelVoiceDescriptors = $derived.by(() => {
    if (!selectedTtsService) return [] as VoiceDescriptor[];
    const service = selectedTtsService;
    const catalogue = Array.from(
      service.voice_catalogues?.[selectedTtsModel] ?? []
    ).map(String);
    const modelUsesReferences =
      ['cloning', 'hybrid', 'optional_cloning'].includes(
        service.model_voice_modes?.[selectedTtsModel] ?? ''
      ) ||
      (selectedTtsServiceId === 'kobold_qwen' &&
        selectedTtsModel.toLowerCase() === 'voice cloning');
    const providerVoicesAllowed =
      !service.supports_prebuilt_voices || modelUsesReferences;
    const published = providerVoicesAllowed
      ? libraryVoices.flatMap((voice) => {
          const registration =
            voice?.metadata_json?.providers?.[selectedTtsServiceId];
          return registration?.status === 'ready' && registration?.voice_id
            ? [String(registration.voice_id)]
            : [];
        })
      : [];
    const managedRegistrations = new Map(
      libraryVoices.flatMap((voice) => {
        const registration =
          voice.metadata_json?.providers?.[selectedTtsServiceId];
        return registration?.voice_id
          ? [
              [
                String(registration.voice_id).toLowerCase(),
                { registration, name: voice.name }
              ] as const
            ]
          : [];
      })
    );
    const live = providerVoicesAllowed
      ? Array.from(service.live_voices ?? [])
          .map(String)
          .filter((voice) => {
            const managed = managedRegistrations.get(voice.toLowerCase());
            return !managed || managed.registration.status === 'ready';
          })
      : [];
    const configured = catalogue.length
      ? catalogue
      : Array.from(service.voices ?? []).map(String);
    const defaultVoice = String(
      service.default_voices_by_language?.[selectedTtsModel]?.[
        inheritedLanguage
      ] ??
        service.default_voices?.[selectedTtsModel] ??
        service.default_voice ??
        ''
    );
    return Array.from(
      new Set(
        [...configured, ...live, ...published, defaultVoice].filter(Boolean)
      )
    ).map((voice) => {
      const managed = managedRegistrations.get(voice.toLowerCase());
      return {
        ...describeVoice(
          String(service.id ?? inheritedTtsSettings.service ?? ''),
          voice,
          service.voice_metadata?.[`${selectedTtsModel}:${voice}`]
        ),
        name:
          managed?.name ??
          describeVoice(
            String(service.id ?? inheritedTtsSettings.service ?? ''),
            voice,
            service.voice_metadata?.[`${selectedTtsModel}:${voice}`]
          ).name
      };
    });
  });
  const supportedSpeechLanguages = $derived.by(() => {
    const discovered = languagesForService(
      String(selectedTtsService?.id ?? inheritedTtsSettings.service ?? ''),
      modelVoiceDescriptors,
      {
        modelId: selectedTtsModel,
        modelCatalog: selectedTtsService?.model_catalog
      }
    );
    return discovered.length
      ? discovered
      : LANGUAGE_OPTIONS.filter((item) => item.value !== 'auto');
  });
  const inheritedVoice = $derived(
    String(
      inheritedTtsSettings.voice ||
        inheritedTtsSettings.speaker ||
        selectedTtsService?.default_voices_by_language?.[selectedTtsModel]?.[
          inheritedLanguage
        ] ||
        selectedTtsService?.default_voices?.[selectedTtsModel] ||
        selectedTtsService?.default_voice ||
        ''
    )
  );
  const alternateService = $derived.by(() => {
    const configured = String(
      alternateTts.service ?? alternateTts.tts_service ?? ''
    );
    return (
      ttsCatalogue.services.find((service) =>
        [service.id, service.name].some(
          (value) => normalizeId(value) === normalizeId(configured)
        )
      ) ?? null
    );
  });
  const alternateModels = $derived.by(() => {
    const service = alternateService;
    if (!service) return [] as string[];
    return Array.from(
      new Set([
        ...(service.models ?? []),
        ...(service.model_catalog ?? []).map((model) => String(model.id ?? '')),
        String(service.default_model ?? '')
      ])
    ).filter(Boolean);
  });
  function alternateUsesManagedReferences(
    service: TtsCatalogue['services'][number],
    model: string
  ) {
    const mode = service.model_voice_modes?.[model];
    if (mode) {
      return ['cloning', 'hybrid', 'optional_cloning'].includes(mode);
    }
    return Boolean(
      service.supports_voice_cloning && !service.supports_prebuilt_voices
    );
  }
  function alternateVoiceIds(
    service: TtsCatalogue['services'][number],
    model: string,
    language: string
  ) {
    const catalogue = Array.from(service.voice_catalogues?.[model] ?? []).map(
      String
    );
    const configured = catalogue.length
      ? catalogue
      : Array.from(service.voices ?? []).map(String);
    const managed = alternateUsesManagedReferences(service, model)
      ? libraryVoices.flatMap((voice) => {
          const registration =
            voice.metadata_json?.providers?.[normalizeId(service.id)];
          return registration?.status === 'ready' && registration?.voice_id
            ? [String(registration.voice_id)]
            : [];
        })
      : [];
    return Array.from(
      new Set([
        ...configured,
        ...(service.live_voices ?? []).map(String),
        ...managed,
        String(service.default_voices_by_language?.[model]?.[language] ?? ''),
        String(service.default_voices?.[model] ?? service.default_voice ?? '')
      ])
    ).filter(Boolean);
  }
  function preferredAlternateVoice(
    service: TtsCatalogue['services'][number],
    model: string,
    language: string
  ) {
    const preferred = String(
      service.default_voices_by_language?.[model]?.[language] ??
        service.default_voices?.[model] ??
        service.default_voice ??
        ''
    );
    return (
      alternateVoiceIds(service, model, language).find(
        (voice) => normalizeId(voice) === normalizeId(preferred)
      ) ?? ''
    );
  }
  function compatibleAlternateVoice(
    service: TtsCatalogue['services'][number],
    model: string,
    language: string,
    voice: string
  ) {
    const candidate = alternateVoiceIds(service, model, language).find(
      (item) => normalizeId(item) === normalizeId(voice)
    );
    if (!candidate) return false;
    const descriptor = describeVoice(
      service.id,
      candidate,
      service.voice_metadata?.[`${model}:${candidate}`]
    );
    return (
      !descriptor.languageCode ||
      normalizeId(descriptor.languageCode) === normalizeId(language)
    );
  }
  function setAlternateVoiceFor(
    service: TtsCatalogue['services'][number],
    model: string,
    language: string,
    current: string
  ) {
    return compatibleAlternateVoice(service, model, language, current)
      ? current
      : preferredAlternateVoice(service, model, language);
  }
  const alternateVoices = $derived.by(() => {
    const service = alternateService;
    if (!service) return [] as VoiceDescriptor[];
    const serviceId = normalizeId(service.id);
    const model = String(alternateTts.model ?? alternateTts.xtts_model ?? '');
    const language = String(
      alternateTts.language ?? alternateTts.target_language ?? ''
    );
    const managed = alternateUsesManagedReferences(service, model)
      ? libraryVoices.flatMap((voice) => {
          const registration = voice.metadata_json?.providers?.[serviceId];
          return registration?.status === 'ready' && registration?.voice_id
            ? [[String(registration.voice_id), voice.name] as const]
            : [];
        })
      : [];
    const names = new Map(
      managed.map(([id, name]) => [id.toLowerCase(), name])
    );
    return Array.from(new Set(alternateVoiceIds(service, model, language)))
      .filter(Boolean)
      .map((voice) => {
        const descriptor = describeVoice(
          service.id,
          voice,
          service.voice_metadata?.[`${model}:${voice}`]
        );
        return {
          ...descriptor,
          name: names.get(voice.toLowerCase()) ?? descriptor.name
        };
      });
  });
  const alternateLanguages = $derived.by(() => {
    const model = String(alternateTts.model ?? alternateTts.xtts_model ?? '');
    const discovered = languagesForService(
      String(alternateService?.id ?? ''),
      alternateVoices,
      {
        modelId: model,
        modelCatalog: alternateService?.model_catalog
      }
    );
    return discovered.length
      ? discovered
      : LANGUAGE_OPTIONS.filter((item) => item.value !== 'auto');
  });
  const alternateIsChatterbox = $derived(
    normalizeId(alternateService?.id) === 'chatterbox'
  );
  const alternateCanStart = $derived(
    alternateSegmentIds.length > 0 &&
      Boolean(alternateService) &&
      alternateService?.online !== false &&
      (alternateModels.length === 0 ||
        Boolean(String(alternateTts.model ?? ''))) &&
      (!alternateRvc.enabled || Boolean(String(alternateRvc.model ?? '')))
  );

  const marked = $derived(
    payload.items.filter((item) => item.marked).map((item) => item.id)
  );
  const selectedSegmentIds = $derived(
    payload.items
      .filter((item) => selectedRows.includes(item.id))
      .map((item) => item.id)
  );
  const editableTexts = $derived(
    searchItems.map((item) =>
      String(
        searchField === 'spoken' ? item.optimized_text || item.text : item.text
      )
    )
  );
  const activeFilterLabel = $derived(
    SEGMENT_FILTER_OPTIONS.find((option) => option.value === filter)?.label ??
      'All segments'
  );
  const searchScopeLabel = $derived(
    filter === 'all'
      ? 'generation segments'
      : `${activeFilterLabel.toLowerCase()} segments`
  );
  const selectedRun = $derived(
    generationHistoryVersion(
      generationStore.runs,
      selectedRunId,
      selectedRunVersionId
    )
  );
  const selectedHistoryRun = $derived(
    runs.find((item) => item.id === selectedRunId) ?? null
  );
  const repairHistoryRun = $derived(selectedHistoryRun ?? runs[0] ?? null);
  const comparisonPlan = $derived<SpeechPlan>(
    comparisonItem?.speech_plan ?? {}
  );
  const topologyDisabled = $derived(
    topologyBusy || Boolean(selectedRunId) || !payload.plan_revision_id
  );
  const boundaryRiskCount = $derived(payload.boundary_flag_count ?? 0);
  const topologyUndoLabel = $derived(
    payload.operation_json?.action === 'restore'
      ? 'Redo the reverted speech-block edit'
      : 'Undo the latest speech-block edit'
  );
  const comparisonDecisionRows = $derived.by(() => {
    const decisions = new Map(
      (comparisonPlan?.decisions ?? [])
        .filter((item) => item && item.span_id)
        .map((item) => [String(item.span_id), item])
    );
    const rows: ComparisonDecisionRow[] = (
      comparisonPlan?.candidates ?? []
    ).map((candidate) => ({
      id: candidate.id,
      written: candidate.text,
      task: candidate.task,
      signals: candidate.signals ?? [],
      ...(decisions.get(String(candidate.id)) ?? {
        action: 'unchanged',
        confidence: ''
      })
    }));
    for (const [index, discovery] of (
      comparisonPlan?.discoveries ?? []
    ).entries()) {
      rows.push({
        id: `discovery-${index}`,
        written: discovery.source_text ?? '',
        task: 'discovery',
        signals: ['model discovery'],
        ...discovery
      });
    }
    return rows;
  });
  const selectedAssembly = $derived(
    selectedRun?.assembly ??
      (!selectedRun && !assembly?.generation_run_id ? assembly : null)
  );
  const selectedRunUsage = $derived(
    selectedHistoryRun?.timing_repair?.usage ?? selectedRun?.usage
  );
  const selectedRunCost = $derived.by(() => {
    const value = selectedRunUsage?.total_cost_usd;
    if (value == null) return 'Cost unavailable';
    return `$${Number(value).toFixed(Number(value) < 0.01 ? 6 : 4)}`;
  });
  const readingBlocks = $derived.by(() => {
    const blocks: ReadingBlock[] = [];
    for (const item of payload.items) {
      const standalone = ['heading', 'chapter_marker'].includes(item.node_kind);
      if (standalone) {
        blocks.push({
          key: `standalone-${item.id}`,
          kind: item.node_kind,
          items: [item],
          closed: true
        });
        continue;
      }
      let paragraph = blocks.at(-1);
      if (!paragraph || paragraph.kind !== 'paragraph' || paragraph.closed) {
        paragraph = {
          key: `paragraph-${item.id}`,
          kind: 'paragraph',
          items: []
        };
        blocks.push(paragraph);
      }
      paragraph.items.push(item);
      paragraph.closed = Boolean(item.paragraph_break_after);
    }
    return blocks;
  });

  function languageLabel(value: string) {
    return (
      supportedSpeechLanguages.find(
        (item) => normalizeId(item.value) === normalizeId(value)
      )?.label ??
      LANGUAGE_OPTIONS.find(
        (item) => normalizeId(item.value) === normalizeId(value)
      )?.label ??
      value
    );
  }

  function languageOptionsFor(item: GenerationSegment) {
    const options = [...supportedSpeechLanguages];
    const current = String(item.language ?? '').trim();
    if (
      current &&
      !options.some(
        (option) => normalizeId(option.value) === normalizeId(current)
      )
    ) {
      options.push({
        value: current,
        label: `${current} · unavailable for this model`
      });
    }
    return options;
  }

  function voiceLabel(voice: VoiceDescriptor) {
    return [voice.name, voice.gender, voice.languageCode ? voice.language : '']
      .filter(Boolean)
      .join(' · ');
  }

  function voiceOptionsFor(item: GenerationSegment) {
    const effectiveLanguage = String(item.language || inheritedLanguage);
    const options = modelVoiceDescriptors.filter(
      (voice) =>
        !voice.languageCode ||
        normalizeId(voice.languageCode) === normalizeId(effectiveLanguage)
    );
    const current = String(item.voice ?? '').trim();
    if (
      current &&
      !options.some((voice) => normalizeId(voice.id) === normalizeId(current))
    ) {
      options.push({
        id: current,
        name: current,
        languageCode: '',
        language: 'Unavailable for the current model or language',
        gender: ''
      });
    }
    return options;
  }

  async function changeSegmentLanguage(
    item: GenerationSegment,
    selected: string
  ) {
    const language = selected || null;
    const effectiveLanguage = String(language || inheritedLanguage);
    const currentVoice = String(item.voice ?? '').trim();
    const descriptor = modelVoiceDescriptors.find(
      (voice) => normalizeId(voice.id) === normalizeId(currentVoice)
    );
    const voiceIsIncompatible = Boolean(
      descriptor?.languageCode &&
      normalizeId(descriptor.languageCode) !== normalizeId(effectiveLanguage)
    );
    await patchSegment(item, {
      language,
      ...(voiceIsIncompatible ? { voice: null } : {})
    });
  }

  async function loadSpeechOptions() {
    speechOptionsLoading = true;
    try {
      const [settings, services, voices] = await Promise.all([
        sessionApi.settings(sessionId, 'tts'),
        getTtsCompactCatalogue(true),
        getVoiceLibrary()
      ]);
      ttsSettings = settings.effective ?? {};
      ttsCatalogue = services;
      libraryVoices = voices.items ?? [];

      const service = services.services.find((candidate) =>
        [candidate.id, candidate.name].some(
          (value) =>
            normalizeId(value) ===
            normalizeId(
              ttsSettings.service ??
                ttsSettings.tts_service ??
                services.default_service
            )
        )
      );
      if (service?.api_base && service.online !== false) {
        try {
          const discovered = await sessionApi.discoverTts(
            service.api_base,
            service.id
          );
          if (discovered?.success) {
            const refreshed = ttsCatalogue.services.map((candidate) =>
              candidate.id === service.id
                ? {
                    ...candidate,
                    models: Array.from(
                      new Set([
                        ...(candidate.models ?? []),
                        ...(discovered.models ?? [])
                      ])
                    ),
                    voices: Array.from(
                      new Set([
                        ...(candidate.voices ?? []),
                        ...(discovered.voices ?? [])
                      ])
                    ),
                    live_voices: Array.from(new Set(discovered.voices ?? [])),
                    online: true
                  }
                : candidate
            );
            ttsCatalogue = { ...ttsCatalogue, services: refreshed };
          }
        } catch {
          // The saved catalogue remains useful when a backend has no discovery route.
        }
      }
    } catch {
      ttsSettings = {};
      ttsCatalogue = { services: [] };
      libraryVoices = [];
    } finally {
      speechOptionsLoading = false;
    }
  }

  async function load(reset = true, preserveLoaded = reset) {
    try {
      applyLoadResult(
        await generationStore.load({
          filter,
          selectedRunId,
          selectedRunVersionId,
          reset,
          preserveLoaded,
          search: searchParams
        })
      );
    } catch (caught) {
      error = errorMessage(caught);
    }
  }

  function expandIfCollapsed() {
    if (mode === 'collapsed') mode = 'full';
  }

  function applyLoadResult(result: GenerationLoadResult) {
    selectedRunId = result.selectedRunId;
    if (result.shouldExpand) expandIfCollapsed();
  }

  async function patchSegment(
    item: GenerationSegment,
    changes: GenerationSegmentChanges
  ) {
    try {
      return await editQueue.save(item.id, async (currentId) => {
        const current =
          savedEditRows.get(currentId) ??
          (currentId !== item.id
            ? payload.items.find((row) => row.id === currentId)
            : item);
        if (!current)
          throw new Error(
            'The segment changed. Refresh before saving this edit.'
          );
        const updated = await performSegmentPatch(current, changes);
        if (!updated)
          throw new Error(error || 'The segment edit could not be saved.');
        return updated;
      });
    } catch (caught) {
      error = errorMessage(caught);
    }
  }

  async function performSegmentPatch(
    item: GenerationSegment,
    changes: GenerationSegmentChanges
  ) {
    pendingSegmentUpdates += 1;
    regenerateMenuOpen = false;
    try {
      const previousRows = payload.items;
      if (selectedRun) settingsSourceRunId = selectedRun.id;
      const independentChanges =
        'text' in changes && !('optimized_text' in changes)
          ? { ...changes, optimized_text: item.optimized_text ?? item.text }
          : changes;
      const updated = await generationStore.updateSegment(
        item,
        independentChanges
      );
      savedEditRows.set(updated.id, { ...item, ...updated });
      // Record the ID mapping directly from the mutation response so an
      // aborted post-save reload cannot drop it; the ordinal rematch below
      // only adds sibling mappings and refreshes saved rows.
      if (
        updated.previous_segment_id &&
        updated.previous_segment_id !== updated.id
      ) {
        editQueue.recordReplacements([
          [updated.previous_segment_id, updated.id]
        ]);
      }
      if (updated.id !== item.id) {
        selectedRunId = '';
        selectedRow = '';
        selectedRows = [];
        filter = 'all';
        await load(true, true);
        const byOrdinal = new Map(
          payload.items.map((row) => [row.ordinal, row])
        );
        for (const old of previousRows) {
          const current = byOrdinal.get(old.ordinal);
          if (
            current &&
            JSON.stringify(current.source_segment_ids) ===
              JSON.stringify(old.source_segment_ids)
          ) {
            editQueue.recordReplacements([[old.id, current.id]]);
            savedEditRows.set(current.id, current);
          }
        }
      }
      if (
        'node_kind' in changes ||
        'silence_after_ms' in changes ||
        'removed' in changes
      )
        await refreshAssembly();
      notifySessionFlowChange(sessionId);
      return updated;
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      pendingSegmentUpdates -= 1;
    }
  }

  function openOptimizationReview(item: GenerationSegment) {
    comparisonItem = item;
    comparisonText = String(
      item.optimized_text ??
        activeTake(item)?.synthesized_text ??
        item.text ??
        ''
    );
    comparisonDiff = false;
  }

  async function saveOptimizationReview() {
    if (!comparisonItem || !comparisonText.trim()) return;
    const item = comparisonItem;
    const updated = await patchSegment(item, {
      optimized_text: comparisonText.trim()
    });
    if (!updated) return;
    comparisonItem = null;
    if (regenerateAfterReview) await start('regenerate', [updated.id]);
  }

  async function reviseSpeechBlocks(operation: {
    action: 'split' | 'merge' | 'restore';
    segment_id?: string;
    left_segment_id?: string;
    right_segment_id?: string;
    cursor?: number;
    text_layer?: 'display' | 'speech';
    passage_boundary_id?: string;
    target_revision_id?: string;
  }) {
    if (!payload.plan_revision_id || topologyBusy || selectedRunId) return;
    topologyBusy = true;
    error = '';
    stopPlayback();
    try {
      await generationApi.reviseSpeechBlocks(
        sessionId,
        payload.plan_revision_id,
        operation
      );
      selectedRow = '';
      selectedRows = [];
      selectionAnchor = '';
      notifySessionFlowChange(sessionId);
      await load(true, false);
      await refreshAssembly();
      return true;
    } catch (caught) {
      error = errorMessage(caught);
      await load(true, true);
    } finally {
      topologyBusy = false;
    }
  }

  function splitSpeechBlock(
    item: GenerationSegment,
    textLayer: 'display' | 'speech',
    cursor: number
  ) {
    return reviseSpeechBlocks({
      action: 'split',
      segment_id: item.id,
      cursor,
      text_layer: textLayer
    });
  }

  function mergeSpeechBlocks(
    left: GenerationSegment,
    right: GenerationSegment
  ) {
    return reviseSpeechBlocks({
      action: 'merge',
      left_segment_id: left.id,
      right_segment_id: right.id
    });
  }

  function undoSpeechBlockRevision() {
    if (!payload.parent_revision_id) return;
    return reviseSpeechBlocks({
      action: 'restore',
      target_revision_id: payload.parent_revision_id
    });
  }

  async function refreshAssembly() {
    try {
      await generationStore.refreshAssembly();
    } catch {
      generationStore.setAssembly(null);
    }
  }

  async function selectTake(item: GenerationSegment, takeId: string) {
    await generationStore.selectTake(item, takeId);
    // Take selection is a current mix edit, never a request to preview the
    // run that originally produced the selected take.
    selectedRunId = '';
    await load(true, false);
  }

  function waitForStartedRunReconciliation(
    signal: AbortSignal
  ): Promise<boolean> {
    return new Promise((resolve) => {
      if (signal.aborted) {
        resolve(false);
        return;
      }
      const handle = window.setTimeout(() => {
        signal.removeEventListener('abort', abort);
        resolve(true);
      }, 1_500);
      const abort = () => {
        window.clearTimeout(handle);
        resolve(false);
      };
      signal.addEventListener('abort', abort, { once: true });
    });
  }

  async function reconcileStartedRun() {
    startedRunReconciliation?.abort();
    const controller = new AbortController();
    startedRunReconciliation = controller;
    try {
      if (!(await waitForStartedRunReconciliation(controller.signal))) return;
      if (controller.signal.aborted) return;
      // A terminal event can arrive immediately after the first running
      // snapshot. Do one prompt authoritative refresh so status-filtered rows
      // return without waiting for the long-lived SSE safety interval.
      await load(true, true);
    } finally {
      if (startedRunReconciliation === controller)
        startedRunReconciliation = undefined;
    }
  }

  async function regenerateAllStale() {
    regenerateMenuOpen = false;
    loading = true;
    error = '';
    regenerationNotice = '';
    try {
      await editQueue.settledIds([]);
      const revisionId = payload.plan_revision_id;
      if (!revisionId || selectedRunId)
        throw new Error('Select the active speech plan first.');
      const ids = await collectStaleSegmentIds(revisionId, (query) =>
        generationApi.segments(sessionId, query)
      );
      if (payload.plan_revision_id !== revisionId || selectedRunId) {
        throw new Error(
          'The speech plan changed. Refresh before regenerating stale segments.'
        );
      }
      if (!ids.length) {
        regenerationNotice =
          'No stale segments in the active plan. Ungenerated segments were left alone.';
        return;
      }
      await start('regenerate', ids, {}, false, revisionId);
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      loading = false;
    }
  }

  async function start(
    operation: 'generate' | 'regenerate' | 'rvc' = 'generate',
    ids: string[] = [],
    selectedSegmentOverride: Record<string, unknown> = {},
    staleOnly = false,
    pinnedRevisionId: string | null = null
  ) {
    if (operation === 'rvc' && !rvcModel) {
      showRvc = true;
      expandIfCollapsed();
      error = 'Choose an RVC model before converting audio.';
      return;
    }
    loading = true;
    error = '';
    regenerationNotice = '';
    try {
      if (selectedRun) settingsSourceRunId = selectedRun.id;
      ids = await editQueue.settledIds(ids);
      if (
        pinnedRevisionId &&
        (payload.plan_revision_id !== pinnedRevisionId || selectedRunId)
      ) {
        throw new Error(
          'The speech plan changed. Refresh before regenerating stale segments.'
        );
      }
      if (operation === 'regenerate' && !ids.length) {
        throw new Error(
          'Choose stale, selected, or marked segments to regenerate.'
        );
      }
      const run_override =
        operation === 'rvc'
          ? {
              rvc: {
                enabled: true,
                model: rvcModel,
                rvc_model: rvcModel,
                pitch: rvcPitch,
                f0_method: rvcF0,
                index_rate: rvcIndexRate,
                source_run_id: selectedRun?.id || null
              }
            }
          : {};
      const started = await generationApi.start(
        sessionId,
        operation,
        ids,
        ids.length && operation !== 'rvc' ? selectedRun?.id || null : null,
        run_override,
        selectedSegmentOverride,
        selectedRunId && ids.length ? null : payload.plan_revision_id || null,
        staleOnly,
        settingsSourceRunId || null
      );
      generationStore.upsertRun(started);
      if (operation === 'regenerate') {
        regenerationNotice = started.resume_source_on_completion
          ? 'Replacement generation queued after the current block. The main run resumes automatically.'
          : 'Replacement generation requested. New takes will appear in the active mix.';
      }
      if (operation === 'rvc') showRvc = false;
      // New generation should be visible in the live Active mix while it
      // runs; history remains an explicit comparison view.
      selectedRunId = '';
      expandIfCollapsed();
      await load();
      void reconcileStartedRun();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      loading = false;
    }
  }

  async function settlePlanReviewEdits() {
    const inspectedRevision = payload.plan_revision_id;
    await editQueue.settledIds([]);
    await tick();
    if (
      !inspectedRevision ||
      selectedRunId ||
      payload.plan_revision_id !== inspectedRevision ||
      pendingSegmentUpdates > 0 ||
      topologyBusy
    ) {
      throw new Error(
        'The plan is still changing. Inspect the updated plan before marking it reviewed.'
      );
    }
  }

  async function openGenerationStart(mode: GenerationStartMode) {
    if (selectedRun) settingsSourceRunId = selectedRun.id;
    try {
      await editQueue.settledIds([]);
    } catch (caught) {
      error = errorMessage(caught);
      return;
    }
    if (!payload.plan_revision_id) {
      error = 'Select the active speech plan first.';
      return;
    }
    error = '';
    generationStartDialog = { mode };
  }

  async function handleGenerationStarted(
    run: GenerationRun,
    preview: { generate_count: number; preserve_count: number } | null
  ) {
    generationStartDialog = null;
    generationStore.upsertRun(run);
    if (preview !== null)
      regenerationNotice = `Queued ${preview.generate_count} block${preview.generate_count === 1 ? '' : 's'}; keeping ${preview.preserve_count} recording${preview.preserve_count === 1 ? '' : 's'}.`;
    // New generation is visible in the live Active mix while it runs;
    // history remains an explicit comparison view.
    selectedRunId = '';
    expandIfCollapsed();
    await load();
    void reconcileStartedRun();
  }

  function sourceSettingsForAlternate() {
    const snapshot =
      (selectedRun ?? settingsSourceRun)?.settings_snapshot ?? {};
    const sourceTts =
      snapshot.tts && typeof snapshot.tts === 'object'
        ? (snapshot.tts as Record<string, unknown>)
        : ttsSettings;
    const sourceRvc =
      snapshot.rvc && typeof snapshot.rvc === 'object'
        ? (snapshot.rvc as Record<string, unknown>)
        : {};
    return { sourceTts, sourceRvc };
  }

  function openAlternateRegeneration(ids: string[]) {
    if (!ids.length) return;
    const { sourceTts, sourceRvc } = sourceSettingsForAlternate();
    const service = String(
      sourceTts.service ??
        sourceTts.tts_service ??
        ttsSettings.service ??
        ttsCatalogue.default_service ??
        ''
    );
    const model = String(
      sourceTts.model ?? sourceTts.xtts_model ?? selectedTtsModel ?? ''
    );
    const voice = String(
      sourceTts.voice ?? sourceTts.speaker ?? inheritedVoice ?? ''
    );
    const language = String(
      sourceTts.language ??
        sourceTts.target_language ??
        inheritedLanguage ??
        'en'
    );
    const matchingService = ttsCatalogue.services.find((item) =>
      [item.id, item.name].some(
        (value) => normalizeId(value) === normalizeId(service)
      )
    );
    const compatibleVoice = matchingService
      ? setAlternateVoiceFor(matchingService, model, language, voice)
      : voice;
    alternateSegmentIds = [...new Set(ids)];
    alternateTts = {
      service,
      tts_service: service,
      model,
      voice: compatibleVoice,
      speaker: compatibleVoice,
      language,
      target_language: language,
      generation_prompt: String(sourceTts.generation_prompt ?? ''),
      chatterbox_exaggeration: Number(sourceTts.chatterbox_exaggeration ?? 0.5),
      chatterbox_cfg_weight: Number(sourceTts.chatterbox_cfg_weight ?? 0.5)
    };
    alternateRvc = {
      enabled: Boolean(sourceRvc.enabled),
      model: String(sourceRvc.model ?? sourceRvc.rvc_model ?? ''),
      rvc_model: String(sourceRvc.model ?? sourceRvc.rvc_model ?? ''),
      pitch: Number(sourceRvc.pitch ?? 0),
      f0_method: String(sourceRvc.f0_method ?? 'rmvpe'),
      index_rate: Number(sourceRvc.index_rate ?? 0.3)
    };
    alternateOpen = true;
  }

  async function submitAlternateRegeneration() {
    if (!alternateCanStart) return;
    const voice = String(alternateTts.voice ?? '').trim();
    const language = String(alternateTts.language ?? '').trim();
    const tts = {
      ...alternateTts,
      voice: voice || null,
      speaker: voice || null,
      language: language || null,
      target_language: language || null
    };
    const rvc = {
      ...alternateRvc,
      enabled: Boolean(alternateRvc.enabled),
      model: String(alternateRvc.model ?? '').trim(),
      rvc_model: String(alternateRvc.model ?? '').trim()
    };
    alternateOpen = false;
    await start('regenerate', alternateSegmentIds, { tts, rvc });
  }

  async function loadRvc() {
    try {
      const result = await generationApi.rvcModels();
      rvcModels = result.items ?? [];
      rvcModel ||= rvcModels[0] ?? '';
    } catch {
      rvcModels = [];
    }
  }

  async function loadSupportingOptions() {
    if (supportingOptionsLoaded) return;
    supportingOptionsLoaded = true;
    await Promise.all([loadRvc(), loadSpeechOptions()]);
  }

  async function action(name: 'pause' | 'resume' | 'cancel') {
    if (!run) return;
    generationStore.upsertRun(await generationApi.runAction(run.id, name));
    await load();
  }

  async function assemble() {
    loading = true;
    error = '';
    try {
      generationStore.setAssembly(
        await generationApi.createAssembly(sessionId, selectedRun?.id || null)
      );
      await load();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      loading = false;
    }
  }

  function activeTake(item: GenerationSegment): PlayableTake | undefined {
    if (selectedRun) {
      const sequences = new Map(
        generationStore.runs.map((item) => [
          item.id,
          Number(item.sequence_number || 0)
        ])
      );
      const targetSequence = Number(selectedRun.sequence_number || 0);
      const candidates = (item.takes ?? [])
        .filter(
          (take): take is PlayableTake =>
            hasArtifact(take) &&
            ['completed', 'stale'].includes(take.status) &&
            Boolean(take.generation_run_id) &&
            Number(
              sequences.get(take.generation_run_id ?? '') ??
                Number.POSITIVE_INFINITY
            ) <= targetSequence
        )
        .sort(
          (left, right) =>
            Number(sequences.get(right.generation_run_id ?? '') ?? 0) -
              Number(sequences.get(left.generation_run_id ?? '') ?? 0) ||
            String(right.created_at).localeCompare(String(left.created_at))
        );
      if (candidates.length) return candidates[0];
      return (
        item.takes?.find(
          (take): take is PlayableTake =>
            !take.generation_run_id && take.is_active && hasArtifact(take)
        ) ??
        item.takes?.find(
          (take): take is PlayableTake =>
            !take.generation_run_id && hasArtifact(take)
        )
      );
    }
    return (
      item.takes?.find(
        (take): take is PlayableTake => take.is_active && hasArtifact(take)
      ) ??
      item.takes?.find(
        (take): take is PlayableTake =>
          !take.generation_run_id && hasArtifact(take)
      )
    );
  }

  const playback = new GenerationPlaybackController({
    getItems: () => payload.items,
    getNextCursor: () => payload.next_cursor,
    loadMore: async () => {
      await load(false);
    },
    getTake: activeTake,
    onSelect: (id) => {
      selectedRow = id;
    },
    onError: (message) => {
      error = message;
    }
  });
  const playlistActive = $derived(playback.active);
  const playlistPaused = $derived(playback.paused);
  const activePlayingId = $derived(playback.activePlayingId);

  function stopPlayback() {
    playback.stop();
  }

  function togglePlaylistPlayback() {
    // Starting the playlist takes over audibility: drop any mounted row
    // preview so the previous take stops with its unmounted element.
    if (!playback.active) previewSegmentId = '';
    playback.toggle(selectedRow);
  }

  function playOnly(item: GenerationSegment) {
    // Controller playback (keyboard shortcut, reading view, popovers) also
    // takes over: unmount the row preview so it stops.
    previewSegmentId = '';
    return playback.playOnly(item);
  }

  function requestSegmentPreview(item: GenerationSegment) {
    // Row preview takes over: stop playlist playback, then mount only this
    // row's player. Switching rows unmounts the previous player, and its
    // onDestroy pauses the old audio element.
    playback.stop();
    previewSegmentId = item.id;
  }

  function takeLabel(take: AudioTake) {
    const owner = generationStore.runs.find(
      (item) => item.id === take.generation_run_id
    );
    const task = generationStore.runs.find(
      (item) => item.id === take.generation_task_run_id
    );
    const tts = task?.settings_snapshot?.tts as
      Record<string, unknown> | undefined;
    const alternate = tts
      ? [...new Set([tts.service, tts.model, tts.voice].filter(Boolean))].join(
          ' · '
        )
      : '';
    const label =
      alternate && owner
        ? `${owner.label.split(':')[0]}: ${alternate}`
        : (owner?.label ?? 'Legacy');
    return `${label} · Take ${takeNumbers.get(take.id) ?? 1} · ${String(take.kind || 'audio').toUpperCase()}`;
  }

  function outputRunBusy(runId: string) {
    return generationStore.runs.some(
      (item) =>
        (item.id === runId ||
          item.output_generation_run_id === runId ||
          item.early_repair_parent_run_id === runId) &&
        ['queued', 'running', 'pausing', 'cancel_requested'].includes(
          item.status
        )
    );
  }

  function verificationTitle(take: AudioTake) {
    const verification = take?.audio_verification;
    if (!verification) return '';
    const issues = (verification.issues ?? [])
      .map((item) => String(item.message || item.code))
      .filter(Boolean);
    const metrics = verification.metrics ?? {};
    const measurements = [
      metrics.rms_dbfs != null
        ? `RMS ${Number(metrics.rms_dbfs).toFixed(1)} dBFS`
        : '',
      metrics.peak_dbfs != null
        ? `peak ${Number(metrics.peak_dbfs).toFixed(1)} dBFS`
        : '',
      metrics.tail_rms_dbfs != null
        ? `tail ${Number(metrics.tail_rms_dbfs).toFixed(1)} dBFS`
        : ''
    ]
      .filter(Boolean)
      .join(', ');
    return [...issues, measurements].filter(Boolean).join(' ');
  }

  async function deleteSelectedRun() {
    if (!selectedHistoryRun || outputRunBusy(selectedHistoryRun.id)) return;
    if (
      !window.confirm(
        `Delete ${selectedHistoryRun.label}, including its audio takes, assembled audio, timing repairs, and regeneration history? This cannot be undone.`
      )
    )
      return;
    loading = true;
    error = '';
    try {
      await generationApi.deleteRun(selectedHistoryRun.id);
      generationStore.removeRun(selectedHistoryRun.id);
      settingsSourceRunId = '';
      selectedRunId = '';
      await load(true, false);
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      loading = false;
    }
  }

  function readingSegmentText(item: GenerationSegment) {
    const speech = activeTake(item)?.synthesized_text || item.optimized_text;
    return String(textMode === 'speech' && speech ? speech : item.text || '')
      .replace(/\s+/g, ' ')
      .trim();
  }

  function changeSearch(
    query: string,
    options: { matchCase: boolean; wholeWord: boolean }
  ) {
    searchQuery = query;
    searchOptions = options;
  }

  async function searchAllMatches(controller: AbortController) {
    searchItems = [];
    if (!searchQuery) {
      searchLoading = false;
      await load(true, false);
      return;
    }
    // The full-plan scan below only runs for a live query. A hidden panel
    // always clears its query on close, so it never triggers this path.
    if (!searchOpen) {
      searchLoading = false;
      await load(true, false);
      return;
    }
    searchLoading = true;
    const query = new URLSearchParams(searchParams);
    query.set('limit', '250');
    query.set(
      'fields',
      'id,ordinal,revision,text,optimized_text,search_matches'
    );
    if (selectedRun) query.set('generation_run_id', selectedRun.id);
    if (filter === 'marked') query.set('marked', 'true');
    else if (filter === 'boundary_flags') query.set('boundary_flags', 'true');
    else if (filter === 'verification_issues')
      query.set('verification', 'issues');
    else if (filter !== 'all') query.set('status', filter);
    try {
      const items: typeof searchItems = [];
      let revision: string | null | undefined;
      do {
        const page = await generationApi.segments(
          sessionId,
          query,
          controller.signal
        );
        if (controller.signal.aborted) return;
        if (revision !== undefined && revision !== page.plan_revision_id)
          throw new Error(
            'The speech plan changed during search. Search again.'
          );
        revision = page.plan_revision_id;
        if (revision) query.set('plan_revision_id', revision);
        items.push(...page.items);
        if (items.length > 20000)
          throw new Error(
            'More than 20,000 segments match. Narrow the search before replacing.'
          );
        if (page.next_cursor == null) break;
        query.set('cursor', String(page.next_cursor));
      } while (!controller.signal.aborted);
      await load(true, false);
      if (!controller.signal.aborted) searchItems = items;
    } catch (caught) {
      if (!controller.signal.aborted) error = errorMessage(caught);
    } finally {
      if (!controller.signal.aborted) searchLoading = false;
    }
  }

  async function changeSelectedRun(event: Event) {
    selectedRunId = (event.currentTarget as HTMLSelectElement).value;
    selectedRunVersionId = '';
    settingsSourceRunId = selectedRunId;
    selectedRow = '';
    selectedRows = [];
    selectionAnchor = '';
    stopPlayback();
    await load(true, false);
    if (selectedRun) settingsSourceRunId = selectedRun.id;
  }

  async function selectRepairVersion(versionId: string) {
    if (!repairHistoryRun) return;
    selectedRunId = repairHistoryRun.id;
    settingsSourceRunId = versionId;
    selectedRunVersionId = versionId;
    selectedRow = '';
    selectedRows = [];
    selectionAnchor = '';
    stopPlayback();
    await load(true, false);
  }

  // Adopt an edit-copy (R1 -> R2) returned by a bulk save: remap
  // selection/search state to the new segment IDs, record the mapping for
  // later ID resolution, and reload the authoritative plan for takes
  // freshness (the store pre-adopts rows/pin synchronously). A post-save load
  // that was aborted or superseded by a delayed stale response must not
  // silently count as success: re-check the adopted revision, retry once,
  // then surface instead of proceeding on stale state.
  async function adoptEditCopy(
    updated: GenerationSegment[]
  ): Promise<string | null> {
    const idMap = new Map<string, string>();
    let copiedRevision: string | null = null;
    for (const item of updated) {
      const previous = item.previous_segment_id;
      if (typeof previous === 'string' && previous && previous !== item.id)
        idMap.set(previous, item.id);
      if (typeof item.plan_revision_id === 'string' && item.plan_revision_id)
        copiedRevision = item.plan_revision_id;
    }
    if (
      idMap.size > 0 ||
      (copiedRevision && copiedRevision !== payload.plan_revision_id)
    ) {
      const remap = (id: string) => idMap.get(id) ?? id;
      editQueue.recordReplacements(idMap);
      selectedRows = selectedRows.map(remap);
      if (selectedRow) selectedRow = remap(selectedRow);
      if (selectionAnchor) selectionAnchor = remap(selectionAnchor);
      const freshById = new Map(updated.map((item) => [item.id, item]));
      searchItems = searchItems.map((item) => {
        const fresh = freshById.get(idMap.get(item.id) ?? item.id);
        return fresh
          ? { ...item, ...fresh, id: idMap.get(item.id) ?? item.id }
          : item;
      });
      await load(true, true);
      if (copiedRevision && payload.plan_revision_id !== copiedRevision) {
        await load(true, true);
      }
      if (copiedRevision && payload.plan_revision_id !== copiedRevision) {
        throw new Error(
          'The speech plan changed during save. Search again before regenerating.'
        );
      }
    }
    return copiedRevision;
  }

  async function applySearchReplacements(updates: TextReplacement[]) {
    error = '';
    try {
      if (selectedRun) settingsSourceRunId = selectedRun.id;
      const changes = updates.flatMap((update) => {
        const item = searchItems[update.index];
        if (!item || update.text === editableTexts[update.index]) return [];
        if (!update.text.trim())
          throw new Error(
            'Replacement would leave a generation segment blank. Remove that segment instead.'
          );
        // Display edits preserve effective speech; the speech layer is explicit.
        return [
          {
            segment: item,
            changes:
              searchField === 'spoken'
                ? { optimized_text: update.text }
                : {
                    text: update.text,
                    optimized_text: item.optimized_text ?? item.text
                  }
          }
        ];
      });
      if (!changes.length) return;
      const updated = await generationStore.updateSegments(changes);
      await adoptEditCopy(updated);
      await refreshAssembly();
      searchController?.abort();
      searchController = new AbortController();
      await searchAllMatches(searchController);
    } catch (caught) {
      const message = errorMessage(caught);
      searchController?.abort();
      searchController = new AbortController();
      await searchAllMatches(searchController);
      error = message;
      throw new Error(message, { cause: caught });
    }
  }

  async function navigateSearchMatch(match: TextSearchMatch) {
    const item = searchItems[match.itemIndex];
    if (!item || searchLoading) return;
    if (!payload.items.some((row) => row.id === item.id)) {
      applyLoadResult(
        await generationStore.load({
          filter,
          selectedRunId,
          selectedRunVersionId,
          search: searchParams,
          reset: true,
          preserveLoaded: false,
          startOrdinal: item.ordinal
        })
      );
    }
    if (viewMode !== 'segments') viewMode = 'segments';
    await revealSegment(item.id, 'center');
    const itemIndex = payload.items.findIndex((row) => row.id === item.id);
    // Match offsets address the searched field, so prefer the textarea that
    // renders it. When the display layer shows the other field, fall back to
    // scrolling to the row without applying foreign offsets.
    const scopePrefix =
      searchField === 'spoken' ? 'Spoken override' : 'Script text';
    const scopedField = document.querySelector<HTMLTextAreaElement>(
      `[data-generation-search-index="${itemIndex}"][aria-label^="${scopePrefix}"]`
    );
    const field =
      scopedField ??
      document.querySelector<HTMLTextAreaElement>(
        `[data-generation-search-index="${itemIndex}"]`
      );
    field?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    field?.focus({ preventScroll: true });
    if (scopedField) field?.setSelectionRange(match.start, match.end);
  }

  async function openSearch() {
    searchOpen = true;
    await tick();
    const field = document.querySelector<HTMLInputElement>(
      '#generation-search-panel input[aria-label^="Find in"]'
    );
    field?.focus({ preventScroll: true });
    field?.select();
  }

  function closeSearch() {
    if (!searchOpen) return;
    searchOpen = false;
    searchController?.abort();
    searchController = undefined;
    searchItems = [];
    searchLoading = false;
    // Clearing the query lifts the list filter so no hidden filter survives.
    // Segment texts and the pending replacement draft are untouched.
    if (searchQuery) searchQuery = '';
    requestAnimationFrame(() => {
      document
        .querySelector<HTMLButtonElement>('[data-generation-search-toggle]')
        ?.focus();
    });
  }

  function selectSegment(
    item: GenerationSegment,
    event?: MouseEvent | KeyboardEvent
  ) {
    const toggle = Boolean(event && (event.ctrlKey || event.metaKey));
    const extend = Boolean(event?.shiftKey && selectionAnchor);
    if (extend) {
      const anchorIndex = payload.items.findIndex(
        (candidate) => candidate.id === selectionAnchor
      );
      const itemIndex = payload.items.findIndex(
        (candidate) => candidate.id === item.id
      );
      if (anchorIndex >= 0 && itemIndex >= 0) {
        const first = Math.min(anchorIndex, itemIndex);
        const last = Math.max(anchorIndex, itemIndex);
        const range = payload.items
          .slice(first, last + 1)
          .map((candidate) => candidate.id);
        selectedRows = toggle
          ? Array.from(new Set([...selectedRows, ...range]))
          : range;
      } else {
        selectedRows = [item.id];
        selectionAnchor = item.id;
      }
    } else if (toggle) {
      selectedRows = selectedRows.includes(item.id)
        ? selectedRows.filter((id) => id !== item.id)
        : [...selectedRows, item.id];
      selectionAnchor = item.id;
    } else {
      selectedRows = [item.id];
      selectionAnchor = item.id;
    }
    selectedRow = selectedRows.includes(item.id)
      ? item.id
      : (selectedRows.at(-1) ?? '');
  }

  function activateReadingSegment(
    event: MouseEvent | KeyboardEvent,
    item: GenerationSegment
  ) {
    selectSegment(item, event);
    if (
      !event.ctrlKey &&
      !event.metaKey &&
      !event.shiftKey &&
      activeTake(item) &&
      !item.removed
    )
      void playOnly(item);
  }

  function activateReadingSegmentFromKeyboard(
    event: KeyboardEvent,
    item: GenerationSegment
  ) {
    if (!['Enter', ' '].includes(event.key)) return;
    event.preventDefault();
    activateReadingSegment(event, item);
  }

  function onGlobalDrawerKeydown(event: KeyboardEvent) {
    if (mode === 'collapsed' || event.defaultPrevented || event.isComposing)
      return;
    // Search is reachable from an editor too, but never steal shortcuts from
    // a modal or intercept browser shortcuts while this drawer is collapsed.
    if (
      (event.ctrlKey || event.metaKey) &&
      !event.altKey &&
      !event.shiftKey &&
      event.key.toLowerCase() === 'k'
    ) {
      if (
        alternateOpen ||
        ttsServicesOpen ||
        comparisonItem ||
        passagePreview ||
        Array.from(
          document.querySelectorAll('[role="dialog"], dialog[open]')
        ).some(
          (dialog) =>
            dialog.getClientRects().length > 0 &&
            getComputedStyle(dialog).visibility !== 'hidden'
        )
      )
        return;
      event.preventDefault();
      displayMenuOpen = false;
      settingsMenuOpen = false;
      regenerateMenuOpen = false;
      void openSearch();
      return;
    }
    const target = event.target as HTMLElement | null;
    if (
      target &&
      (target.tagName === 'INPUT' ||
        target.tagName === 'TEXTAREA' ||
        target.tagName === 'SELECT' ||
        target.isContentEditable)
    ) {
      return;
    }
    if (event.key === 'Escape') {
      displayMenuOpen = false;
      settingsMenuOpen = false;
      regenerateMenuOpen = false;
      return;
    }
    const index = payload.items.findIndex((item) => item.id === selectedRow);
    // Native buttons and links (including their descendants, e.g. icons)
    // keep their own Enter/Space activation: the row shortcuts below must
    // not steal the keypress or cancel the native click. This matters for
    // the deferred row preview, whose focus moves to the mounted player's
    // transport button.
    const onNativeControl = Boolean(target?.closest('button, a'));
    if (event.key === 'ArrowDown') {
      const nextIndex = Math.min(
        payload.items.length - 1,
        Math.max(0, index + 1)
      );
      const item = payload.items[nextIndex];
      if (item) {
        selectSegment(item, event);
        void revealSegment(item.id);
      }
      event.preventDefault();
    } else if (event.key === 'ArrowUp') {
      const prevIndex = Math.max(0, index < 0 ? 0 : index - 1);
      const item = payload.items[prevIndex];
      if (item) {
        selectSegment(item, event);
        void revealSegment(item.id);
      }
      event.preventDefault();
    } else if (event.key === ' ' && index >= 0 && !onNativeControl) {
      const item = payload.items[index];
      patchSegment(item, {
        marked: !item.marked
      });
      event.preventDefault();
    } else if (event.key === 'Delete' && index >= 0) {
      const item = payload.items[index];
      patchSegment(item, {
        removed: !item.removed
      });
      event.preventDefault();
    } else if (event.key === 'Enter' && index >= 0 && !onNativeControl) {
      const item = payload.items[index];
      if (activeTake(item) && !item.removed) {
        void playOnly(item);
        event.preventDefault();
      }
    }
  }

  onMount(() => {
    const openPlan = (requestedSessionId: string) => {
      if (requestedSessionId !== sessionId) return;
      selectedRunId = '';
      filter = 'all';
      mode = 'full';
      void load(true, false);
    };
    const disconnectPlanEditor = subscribeSpeechPlanEditor(openPlan);
    const disconnect = generationStore.connect(
      () => ({
        filter,
        selectedRunId,
        selectedRunVersionId,
        search: searchParams
      }),
      applyLoadResult
    );
    return () => {
      disconnect();
      disconnectPlanEditor();
      if (timer) window.clearTimeout(timer);
      searchController?.abort();
      startedRunReconciliation?.abort();
      stopPlayback();
    };
  });
  $effect(() => {
    if (mode !== 'collapsed') void untrack(loadSupportingOptions);
  });
  $effect(() => {
    void payload.plan_revision_id;
    void selectedRunId;
    void selectedRunVersionId;
    speechPreviews = {};
    speechTarget = null;
    speechSelection = null;
  });
  let lastPreviewScope = '';
  $effect(() => {
    // Preview reset lives apart from the annotation clearing above: every
    // payload replacement (including take adoptions and reloads) re-fires
    // effects that read through the payload object, even when the revision
    // ID is unchanged. Compare the previous/current primitive scope so a
    // take switch keeps its mounted player instead of unmounting it.
    const scope = `${payload.plan_revision_id ?? ''}|${selectedRunId}|${selectedRunVersionId}`;
    if (scope !== untrack(() => lastPreviewScope)) {
      lastPreviewScope = scope;
      previewSegmentId = '';
    }
  });
  $effect(() => {
    void filter;
    void selectedRunId;
    void selectedRunVersionId;
    void textMode;
    void searchQuery;
    void searchOptions;
    void searchOpen;
    void searchScope;
    void searchField;
    searchLoading = Boolean(searchQuery);
    const controller = new AbortController();
    searchController = controller;
    const timeout = window.setTimeout(
      () => void searchAllMatches(controller),
      searchQuery ? 250 : 0
    );
    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  });
  $effect(() => {
    if (timer) clearTimeout(timer);
    const active =
      (run &&
        [
          'queued',
          'running',
          'pausing',
          'pause_requested',
          'cancel_requested'
        ].includes(run.status)) ||
      (selectedAssembly &&
        ['queued', 'running'].includes(selectedAssembly.status));
    if (active) {
      // Events remain the primary update path, but a stream can look healthy
      // while a terminal event is lost during reconnect. Reconcile active work
      // periodically so filtered segments cannot remain stale indefinitely.
      timer = window.setTimeout(
        () => load(true, true),
        appState.eventsHealthy ? 10_000 : 2_500
      );
    }
  });
</script>

<svelte:window onkeydown={onGlobalDrawerKeydown} />

{#if displayMenuOpen || settingsMenuOpen || regenerateMenuOpen}
  <button
    type="button"
    class="fixed inset-0 z-40 cursor-default bg-transparent"
    onclick={() => {
      displayMenuOpen = false;
      settingsMenuOpen = false;
      regenerateMenuOpen = false;
    }}
    tabindex="-1"
    aria-label="Close menu"
  ></button>
{/if}

{#if payload.total > 0 || payload.plan_revision_id || run || searchQuery || filter !== 'all'}
  <div
    aria-hidden="true"
    style={`height:${mode === 'collapsed' ? headerHeight + 16 : 80}px`}
  ></div>
  <aside
    data-generation-layout={mode}
    class:full={mode === 'full'}
    class:half={mode === 'half'}
    class="generation-drawer fixed inset-x-3 bottom-3 z-50 overflow-hidden rounded-2xl md:left-[calc(var(--sidebar-offset,5rem)+.35rem)] md:right-[.35rem]"
  >
    <header
      bind:clientHeight={headerHeight}
      class:border-b={mode !== 'collapsed'}
      class="generation-header flex shrink-0 flex-wrap items-center gap-3 border-[var(--line)] px-4 py-3 lg:flex-nowrap"
    >
      <button
        onclick={() => (mode = mode === 'collapsed' ? 'full' : 'collapsed')}
        aria-expanded={mode !== 'collapsed'}
        class="drawer-toggle flex min-h-11 items-center gap-2 font-semibold"
      >
        {#if mode === 'collapsed'}<ChevronUp size={17} />{:else}<ChevronDown
            size={17}
          />{/if}
        Generation
      </button>
      <span
        class="drawer-summary muted min-w-0 text-xs lg:flex-1 lg:truncate"
        title={`${payload.total} segments · ${selectedHistoryRun?.label ?? 'Active mix'}${selectedAssembly ? ` · output ${selectedAssembly.status}` : ''}`}
        >{payload.total} segments · {selectedHistoryRun?.label ??
          'Active mix'}{#if selectedAssembly}
          · output {selectedAssembly.status}{/if}</span
      >
      {#if selectedRunUsage?.commercial}<span class="cost-pill"
          >{selectedRunUsage.estimated ? 'Est.' : ''}
          {selectedRunCost}{selectedRunUsage.has_unpriced_usage
            ? ' + unpriced usage'
            : ''}</span
        >{/if}
      {#if run && ['queued', 'running', 'pausing', 'cancel_requested'].includes(run.status)}
        <div
          class="run-progress"
          role="progressbar"
          aria-label="Generation progress"
          aria-valuemin="0"
          aria-valuemax="100"
          aria-valuenow={progressPercent(run.progress)}
        >
          <span style={`width:${progressPercent(run.progress)}%`}></span>
        </div>
        <span class="muted text-[.65rem] tabular-nums"
          >{progressPercent(run.progress)}%</span
        >
        {#if run.progress_detail || run.status === 'queued'}
          <span
            class="muted max-w-64 truncate text-[.65rem]"
            title={run.progress_detail ?? ''}
            >{run.progress_detail ?? 'Waiting for an available worker'}</span
          >
        {/if}
      {/if}
      {#if selectedAssembly && ['queued', 'running'].includes(selectedAssembly.status)}
        <div
          class="run-progress"
          role="progressbar"
          aria-label="Output assembly progress"
          aria-valuemin="0"
          aria-valuemax="100"
          aria-valuenow={progressPercent(selectedAssembly.progress)}
        >
          <span style={`width:${progressPercent(selectedAssembly.progress)}%`}
          ></span>
        </div>
        <span class="muted text-[.65rem] tabular-nums"
          >{progressPercent(selectedAssembly.progress)}%</span
        >
        {#if selectedAssembly.progress_detail || selectedAssembly.status === 'queued'}
          <span
            class="muted max-w-64 truncate text-[.65rem]"
            title={selectedAssembly.progress_detail ?? ''}
            >{selectedAssembly.progress_detail ??
              'Waiting for an available worker'}</span
          >
        {/if}
      {/if}
      <div class="header-playback flex items-center gap-1">
        <button
          onclick={togglePlaylistPlayback}
          class:active={playlistActive}
          class="action"
          title={playlistActive
            ? playlistPaused
              ? 'Resume playlist'
              : 'Pause playlist'
            : 'Play playlist from the selected segment'}
          aria-label={playlistActive
            ? playlistPaused
              ? 'Resume playlist'
              : 'Pause playlist'
            : 'Play playlist'}
        >
          {#if playlistActive && !playlistPaused}<Pause size={14} />{:else}<Play
              size={14}
            />{/if}
          {playlistActive ? (playlistPaused ? 'Resume' : 'Pause') : 'Play'}
        </button>
        {#if playlistActive}
          <button
            onclick={stopPlayback}
            class="action icon-action"
            title="Stop playlist"
            aria-label="Stop playlist"><Square size={14} /></button
          >
        {/if}
      </div>
      {#if mode !== 'collapsed'}
        <button
          type="button"
          data-generation-search-toggle
          onclick={() => (searchOpen ? closeSearch() : openSearch())}
          class="action icon-action"
          class:active={searchOpen}
          title="Search and replace (Ctrl+K)"
          aria-label="Search and replace"
          aria-keyshortcuts="Control+k Meta+k"
          aria-expanded={searchOpen}
          aria-controls="generation-search-panel"
        >
          <Search size={14} />
        </button>
        <div class="dropdown-wrapper">
          <button
            onclick={() => {
              displayMenuOpen = !displayMenuOpen;
              settingsMenuOpen = false;
              regenerateMenuOpen = false;
            }}
            class="action icon-action"
            class:active={displayMenuOpen}
            title="Display options (view and text mode)"
            aria-label="Display options"
            aria-expanded={displayMenuOpen}
          >
            <Eye size={14} />
          </button>
          {#if displayMenuOpen}
            <div class="dropdown-menu left">
              <span class="dropdown-section-title">View Mode</span>
              <button
                type="button"
                class="dropdown-item"
                class:active={viewMode === 'segments'}
                onclick={() => {
                  viewMode = 'segments';
                  displayMenuOpen = false;
                }}
              >
                <ListMusic size={13} /> Segments table
              </button>
              <button
                type="button"
                class="dropdown-item"
                class:active={viewMode === 'reading'}
                onclick={() => {
                  viewMode = 'reading';
                  displayMenuOpen = false;
                }}
              >
                <BookOpenText size={13} /> Reading view
              </button>
              <div class="dropdown-divider"></div>
              <span class="dropdown-section-title">Text layer</span>
              <button
                type="button"
                class="dropdown-item"
                class:active={textMode === 'display'}
                onclick={() => {
                  textMode = 'display';
                  displayMenuOpen = false;
                }}
              >
                Script / subtitle text
              </button>
              <button
                type="button"
                class="dropdown-item"
                class:active={textMode === 'speech'}
                onclick={() => {
                  textMode = 'speech';
                  displayMenuOpen = false;
                }}
              >
                Spoken override (TTS only)
              </button>
              <div class="dropdown-divider"></div>
              <button
                type="button"
                class="dropdown-item"
                aria-pressed={showSpeechAnnotations}
                onclick={() => {
                  showSpeechAnnotations = !showSpeechAnnotations;
                  if (showSpeechAnnotations) showPassageBoundaries = false;
                  speechTarget = null;
                  displayMenuOpen = false;
                }}
                >{showSpeechAnnotations
                  ? 'Hide voices and delivery'
                  : 'Show voices and delivery'}</button
              >
              <button
                type="button"
                class="dropdown-item"
                aria-pressed={showPassageBoundaries}
                onclick={() => {
                  showPassageBoundaries = !showPassageBoundaries;
                  if (showPassageBoundaries) showSpeechAnnotations = false;
                  savePassageVisibility(showPassageBoundaries);
                  passageActions = null;
                  displayMenuOpen = false;
                }}
              >
                {showPassageBoundaries
                  ? 'Hide passage boundaries'
                  : 'Show passage boundaries'}
              </button>
              <button
                type="button"
                class="dropdown-item"
                aria-pressed={showRepairHistory}
                disabled={!repairHistoryRun?.timing_repair}
                title={repairHistoryRun?.timing_repair
                  ? 'Show or hide automatic split and timing-repair history'
                  : 'No timing-repair history is available for this audio view'}
                onclick={() => {
                  showRepairHistory = !showRepairHistory;
                  displayMenuOpen = false;
                }}
              >
                {showRepairHistory
                  ? 'Hide split / repair history'
                  : 'Show split / repair history'}
              </button>
            </div>
          {/if}
        </div>
        {#if boundaryRiskCount > 0}
          <button
            class="rounded-full border border-amber-500/25 bg-amber-500/12 px-2.5 py-1 text-[.62rem] font-semibold text-amber-700 hover:bg-amber-500/20"
            aria-pressed={filter === 'boundary_flags'}
            onclick={() => {
              filter = filter === 'boundary_flags' ? 'all' : 'boundary_flags';
              viewMode = 'segments';
              mode = 'full';
            }}
            title="Show only speech blocks with boundary review flags"
          >
            {boundaryRiskCount} block {boundaryRiskCount === 1
              ? 'flag'
              : 'flags'}
          </button>
        {/if}
        <SpeechPlanHistory
          {sessionId}
          activeRevisionId={selectedRunId
            ? null
            : payload.plan_revision_id || null}
          disabled={loading ||
            topologyBusy ||
            pendingSegmentUpdates > 0 ||
            ['queued', 'running', 'pausing', 'cancel_requested'].includes(
              run?.status ?? ''
            )}
          onselect={async (revisionId) => {
            await sessionFlowAction(sessionId, 'generation-plan/select', {
              revision_id: revisionId,
              expected_plan_revision_id: payload.plan_revision_id
            });
            selectedRunId = '';
            settingsSourceRunId = '';
            await load(true, false);
          }}
          onrestore={(revisionId) =>
            reviseSpeechBlocks({
              action: 'restore',
              target_revision_id: revisionId
            })}
          ongenerate={async (staleOnly) => {
            openGenerationStart(staleOnly ? 'refresh' : 'all');
          }}
        />
        {#if payload.parent_revision_id && !selectedRunId}
          <button
            type="button"
            onclick={undoSpeechBlockRevision}
            disabled={topologyBusy}
            class="action icon-action"
            title={topologyUndoLabel}
            aria-label={topologyUndoLabel}
          >
            <Undo2 size={14} />
          </button>
        {/if}
      {/if}
      <div class="drawer-actions ml-auto flex flex-wrap gap-2">
        {#if !run || ['completed', 'partial', 'failed', 'canceled', 'paused'].includes(run.status)}
          <button
            onclick={() => openGenerationStart('continue')}
            disabled={loading || topologyBusy || pendingSegmentUpdates > 0}
            class="action primary"
            title={settingsSourceRunId
              ? 'Generate using the selected run’s saved voice and settings'
              : 'Generate using the current voice and settings'}
            ><Play size={14} /> Generate audio…</button
          >
        {/if}
        {#if run?.status === 'paused'}
          <button
            onclick={() => action('resume')}
            class="action"
            title="Continue the paused run with its saved voice and settings"
            ><Play size={14} /> Resume previous run</button
          >
        {:else if run && ['queued', 'running'].includes(run.status)}
          <button
            onclick={() => action('pause')}
            class="action icon-action"
            title="Stop safely after the current segment"
            aria-label="Stop safely after the current segment"
            ><Pause size={14} /></button
          >
          <button
            onclick={() => action('cancel')}
            class="action icon-action text-red-500"
            title="Cancel generation run immediately"
            aria-label="Cancel generation run immediately"
            ><Square size={14} /></button
          >
        {/if}
        {#if mode !== 'collapsed'}
          <button
            onclick={() => (mode = mode === 'full' ? 'half' : 'full')}
            class="action icon-action drawer-height-toggle"
            title={mode === 'full' ? 'Use half height' : 'Use full height'}
            aria-label={mode === 'full' ? 'Use half height' : 'Use full height'}
            >{#if mode === 'full'}<Minimize2 size={14} />{:else}<Maximize2
                size={14}
              />{/if}</button
          >
        {/if}
      </div>
    </header>

    {#if mode !== 'collapsed'}
      <div class="flex min-h-0 min-w-0 flex-1 flex-col overflow-y-auto">
        <div
          class="flex flex-wrap items-center justify-end gap-2 border-b border-[var(--line)] p-3"
        >
          <label class="mr-auto flex items-center gap-2 text-xs font-semibold">
            Edit text
            <select
              aria-label="Text to edit"
              bind:value={textMode}
              class="mini"
            >
              <option value="display">Display / subtitles</option>
              <option value="speech">TTS speech</option>
            </select>
          </label>
          {#if settingsSourceRunId}
            <span class="muted text-xs" data-generation-settings-source>
              Settings from {settingsSourceRun?.label ?? 'selected run'}
            </span>
          {/if}
          {#if runs.length}
            <label
              class="run-picker flex items-center gap-2 text-xs font-semibold"
              >Audio view
              <select
                value={selectedRunId}
                onchange={changeSelectedRun}
                class="mini max-w-[22rem]"
              >
                <option value="">Active mix · current selections</option>
                {#each runs as item}<option value={item.id}
                    >History · {item.label} · {generationHistoryStatus(
                      item
                    )}</option
                  >{/each}
              </select>
            </label>
            <button
              onclick={deleteSelectedRun}
              disabled={loading ||
                !selectedRun ||
                outputRunBusy(selectedHistoryRun?.id ?? selectedRun.id)}
              class="action text-red-500"
              title={selectedRun
                ? 'Delete this run and its generated audio'
                : 'Choose a run from Audio view to delete it'}
              ><Trash2 size={14} /> Delete run</button
            >
            <span class="h-6 w-px bg-[var(--line)]"></span>
          {/if}
          <label class="flex items-center gap-2 text-xs font-semibold"
            >Show
            <select
              bind:value={filter}
              class="mini min-w-44"
              aria-label="Segments to display"
            >
              {#each SEGMENT_FILTER_OPTIONS as option}
                <option value={option.value}>{option.label}</option>
              {/each}
            </select>
          </label>
          <div class="dropdown-wrapper">
            <button
              onclick={() => {
                regenerateMenuOpen = !regenerateMenuOpen;
                displayMenuOpen = false;
                settingsMenuOpen = false;
              }}
              disabled={loading ||
                topologyBusy ||
                pendingSegmentUpdates > 0 ||
                (!payload.plan_revision_id &&
                  !selectedSegmentIds.length &&
                  !marked.length)}
              class="action icon-action"
              class:active={regenerateMenuOpen}
              title="Regenerate stale, selected, or marked takes"
              aria-label="Regeneration options"
              aria-expanded={regenerateMenuOpen}
            >
              <RefreshCw size={14} />
            </button>
            {#if regenerateMenuOpen}
              <div class="dropdown-menu">
                <span class="dropdown-section-title">Standard settings</span>
                {#if !selectedRunId}
                  <button
                    type="button"
                    class="dropdown-item"
                    onclick={regenerateAllStale}
                  >
                    <RefreshCw size={13} /> Regenerate all stale
                  </button>
                {/if}
                {#if selectedSegmentIds.length}
                  <button
                    type="button"
                    class="dropdown-item"
                    onclick={() => {
                      regenerateMenuOpen = false;
                      void start('regenerate', selectedSegmentIds);
                    }}
                  >
                    <RefreshCw size={13} />
                    Regenerate selected ({selectedSegmentIds.length})
                  </button>
                {/if}
                {#if marked.length}
                  <button
                    type="button"
                    class="dropdown-item"
                    onclick={() => {
                      regenerateMenuOpen = false;
                      void start('regenerate', marked);
                    }}
                  >
                    <RefreshCw size={13} />
                    Regenerate marked ({marked.length})
                  </button>
                {/if}
                <div class="dropdown-divider"></div>
                <button
                  type="button"
                  class="dropdown-item"
                  onclick={() => {
                    regenerateMenuOpen = false;
                    openAlternateRegeneration(
                      selectedSegmentIds.length ? selectedSegmentIds : marked
                    );
                  }}
                  disabled={!selectedSegmentIds.length && !marked.length}
                >
                  <WandSparkles size={13} />
                  Different settings / provider…
                </button>
              </div>
            {/if}
          </div>
          <button
            onclick={assemble}
            disabled={loading ||
              selectedRun?.status !== 'completed' ||
              selectedAssembly?.status === 'queued' ||
              selectedAssembly?.status === 'running'}
            class="action icon-action"
            title={selectedRun?.status !== 'completed'
              ? 'Generate every remaining segment before assembly'
              : selectedAssembly?.status === 'stale'
                ? 'Reassemble output'
                : 'Assemble output'}
            aria-label={selectedAssembly?.status === 'stale'
              ? 'Reassemble output'
              : 'Assemble output'}
          >
            <Sparkles size={14} />
          </button>
          <div class="dropdown-wrapper">
            <button
              onclick={() => {
                settingsMenuOpen = !settingsMenuOpen;
                displayMenuOpen = false;
                regenerateMenuOpen = false;
              }}
              class="action icon-action"
              class:active={settingsMenuOpen}
              title="Session generation settings & speech services"
              aria-label="Settings and speech services"
              aria-expanded={settingsMenuOpen}
            >
              <Settings size={14} />
            </button>
            {#if settingsMenuOpen}
              <div class="dropdown-menu">
                <a
                  href={`/sessions/${sessionId}/output`}
                  class="dropdown-item"
                  onclick={() => (settingsMenuOpen = false)}
                >
                  Output settings
                </a>
                <button
                  type="button"
                  class="dropdown-item"
                  onclick={() => {
                    ttsServicesOpen = true;
                    settingsMenuOpen = false;
                  }}
                >
                  Speech services & catalogue
                </button>
                <button
                  type="button"
                  class="dropdown-item"
                  onclick={() => {
                    showRvc = true;
                    settingsMenuOpen = false;
                  }}
                >
                  <WandSparkles size={13} />
                  RVC conversion…
                </button>
              </div>
            {/if}
          </div>
        </div>
        {#if showRepairHistory && repairHistoryRun?.timing_repair}
          <div class="border-b border-[var(--line)] p-3">
            <GenerationRunHistory
              run={repairHistoryRun}
              versionId={selectedRunId ? selectedRunVersionId : ''}
              activeMix={!selectedRunId}
              disabled={loading}
              onSelectVersion={selectRepairVersion}
            />
          </div>
        {/if}
        {#if searchOpen}
          <div
            id="generation-search-panel"
            class="border-b border-[var(--line)] px-3 py-2"
          >
            <SearchReplaceBar
              texts={editableTexts}
              onreplace={applySearchReplacements}
              onnavigate={navigateSearchMatch}
              disabled={searchLoading || loading}
              label={searchScopeLabel}
              onsearch={changeSearch}
              searching={searchLoading}
              showScopeSelector={isVoiceover}
              {searchScope}
              onScopeChange={(scope) => {
                searchScope = scope;
              }}
              onclose={closeSearch}
              autoFocus
              serverMatches={searchItems.flatMap((item, itemIndex) =>
                (item.search_matches ?? []).map((match) => ({
                  ...match,
                  itemIndex
                }))
              )}
            />
            {#if searchQuery}<p
                class="muted mt-1 px-1 text-[.65rem]"
                aria-live="polite"
              >
                {searchLoading
                  ? 'Searching the complete plan…'
                  : `${searchItems.length} matching segments across the complete plan · ${isVoiceover ? (searchField === 'spoken' ? 'TTS text' : 'Cue text') : searchField === 'spoken' ? 'spoken overrides' : 'script text'}`}
              </p>{/if}
          </div>
        {/if}

        {#if selectedAssembly?.status === 'completed' && selectedAssembly.artifact_id}
          <div
            class="flex flex-wrap items-center gap-3 border-b border-[var(--line)] bg-[var(--accent-soft)] px-4 py-2"
          >
            <strong class="text-xs">Assembled output</strong>
            <div class="min-w-64 flex-1">
              <AudioPlayer
                src={`/api/v1/artifacts/${selectedAssembly.artifact_id}/content`}
                label="Assembled output"
              />
            </div>
            <a
              class="action"
              download
              href={`/api/v1/artifacts/${selectedAssembly.artifact_id}/content`}
              ><Download size={14} /> Download</a
            >
          </div>
        {:else if selectedAssembly?.status === 'stale'}
          <div
            class="border-b border-amber-400/30 bg-amber-500/10 px-4 py-2 text-xs text-amber-700"
          >
            The output is out of date because segment order, chapter boundaries,
            silence, or selected takes changed. Reassemble to apply the changes.
          </div>
        {:else if selectedAssembly?.status === 'failed'}
          <div
            class="border-b border-red-400/30 bg-red-500/10 px-4 py-2 text-xs text-red-600"
          >
            Assembly failed: {selectedAssembly.error_message}
          </div>
        {/if}

        {#if run?.status === 'failed' || latestFailure}
          <div
            class="border-b border-red-400/30 bg-red-500/10 px-4 py-2 text-xs text-red-600"
          >
            Generation failed: {(run?.status === 'failed'
              ? run.error_message
              : latestFailure?.error_message) ||
              'Open Activity & logs for details, then retry.'}
            <p class="mt-2 text-[var(--ink)]">
              Completed recordings are still available. Continue unfinished
              audio keeps them and generates only missing, edited, or failed
              blocks.
            </p>
            <button
              type="button"
              class="action mt-2"
              disabled={loading || topologyBusy || pendingSegmentUpdates > 0}
              onclick={() => openGenerationStart('continue')}
              data-testid="generation-recovery-continue"
              >Continue unfinished audio…</button
            >
          </div>
        {/if}

        {#if error}<p class="p-3 text-sm text-red-500" role="alert">
            {error}
          </p>{/if}
        {#if run?.status === 'queued' && run.waiting_for_job}
          <p class="px-4 py-2 text-sm" role="status">
            Generation queued — waiting for {run.waiting_for_job.kind.startsWith(
              'export'
            )
              ? 'export'
              : 'other session work'}.
            {run.waiting_for_job.progress_detail ?? ''}
          </p>
        {/if}
        {#if regenerationNotice}<p
            class="p-3 text-sm text-[var(--muted)]"
            role="status"
          >
            {regenerationNotice}
          </p>{/if}

        {#if payload.plan_revision_id}
          {#key `${sessionId}:${payload.plan_revision_id}:${selectedRunId}`}
            <GenerationPlanReviewBar
              {sessionId}
              revisionId={payload.plan_revision_id}
              current={!selectedRunId && payload.is_active_revision !== false}
              refreshKey={payload}
              blocked={loading ||
                generationStore.status === 'loading' ||
                generationStore.status === 'stale' ||
                topologyBusy ||
                pendingSegmentUpdates > 0 ||
                ['queued', 'running', 'pausing', 'cancel_requested'].includes(
                  run?.status ?? ''
                )}
              beforeReview={settlePlanReviewEdits}
              onrefresh={() => load(true, false)}
            />
          {/key}
        {/if}
        {#if !selectedRunId && payload.items.length}
          <p class="muted border-b border-[var(--line)] px-4 py-2 text-xs">
            Select speech text to edit its speaker, voice or delivery.
            Underlines show speaker changes; double underlines indicate
            directions.
          </p>
        {/if}
        <div
          bind:this={segmentScrollRoot}
          class="min-h-[12rem] shrink-0 flex-1 overflow-auto"
        >
          {#if viewMode === 'segments'}
            <GenerationSegmentTable
              compactRows={workflowKind === 'audiobook'}
              bind:this={segmentTable}
              scrollRoot={segmentScrollRoot}
              activeSegmentId={activePlayingId}
              {showSpeechAnnotations}
              {speechPreviews}
              onspeech={inspectSpeech}
              onselection={selectedRunId ? undefined : editSelectedSpeech}
              {showPassageBoundaries}
              onpassage={inspectPassage}
              items={payload.items}
              {pendingGeneration}
              {selectedRows}
              {loading}
              {speechOptionsLoading}
              selectedTtsServiceName={selectedTtsService?.name}
              {selectedTtsModel}
              {inheritedVoice}
              {inheritedLanguage}
              {textMode}
              onselect={selectSegment}
              onpatch={patchSegment}
              onreview={openOptimizationReview}
              onvoices={voiceOptionsFor}
              onvoicelabel={voiceLabel}
              onlanguages={languageOptionsFor}
              onlanguagelabel={languageLabel}
              onlanguagechange={changeSegmentLanguage}
              onactivetake={activeTake}
              onselecttake={selectTake}
              ontakelabel={takeLabel}
              onverificationtitle={verificationTitle}
              onregenerate={(item) => start('regenerate', [item.id])}
              onregeneratewith={(item) => openAlternateRegeneration([item.id])}
              onmerge={mergeSpeechBlocks}
              onsplit={splitSpeechBlock}
              {topologyDisabled}
              {previewSegmentId}
              onpreviewrequest={requestSegmentPreview}
            />
          {:else}
            <GenerationReadingView
              {showSpeechAnnotations}
              {speechPreviews}
              onspeech={inspectSpeech}
              onselection={selectedRunId ? undefined : editSelectedSpeech}
              {showPassageBoundaries}
              onpassage={inspectPassage}
              blocks={readingBlocks}
              selectedRunLabel={selectedHistoryRun?.label ?? 'Active mix'}
              {textMode}
              loaded={payload.items.length}
              total={payload.total}
              {activePlayingId}
              {selectedRows}
              {loading}
              ontextmode={(value) => (textMode = value)}
              onactivate={activateReadingSegment}
              onactivatekeyboard={activateReadingSegmentFromKeyboard}
              ontext={readingSegmentText}
              onhasaudio={(item) => Boolean(activeTake(item))}
              onplay={playOnly}
              onregenerate={(item) => start('regenerate', [item.id])}
              onregeneratewith={(item) => openAlternateRegeneration([item.id])}
              onpatch={patchSegment}
              onmerge={mergeSpeechBlocks}
              {topologyDisabled}
            />
          {/if}
          {#if payload.next_cursor != null}<button
              onclick={() => load(false)}
              class="m-4 w-[calc(100%-2rem)] rounded-xl border border-[var(--line)] py-2 text-sm font-semibold"
              >Load more</button
            >{/if}
        </div>
      </div>
    {/if}
  </aside>
{/if}
{#if speechSelection && payload.plan_revision_id}
  {@const selected = speechSelection}
  {#key `${selected.item.id}:${selected.start}:${selected.end}`}
    <SpeechSelectionEditor
      {sessionId}
      revisionId={payload.plan_revision_id}
      selection={selected}
      onclose={() => {
        speechSelection = null;
        window.getSelection()?.removeAllRanges();
      }}
      onapplied={async () => {
        speechPreviews = {};
        await load(true, false);
        if (!payload.plan_revision_id) return;
        try {
          const preview = await generationApi.previewSpeech(
            sessionId,
            payload.plan_revision_id,
            selected.item.id
          );
          speechPreviews = { ...speechPreviews, [selected.item.id]: preview };
        } catch (caught) {
          error = errorMessage(caught);
        }
      }}
    />
  {/key}
{/if}
{#if speechTarget}
  {@const target = speechTarget}
  <SpeechAnnotationPopover
    {sessionId}
    revisionId={target.revisionId}
    segmentId={target.item.id}
    segmentNumber={target.item.ordinal + 1}
    runId={target.runId}
    offset={target.offset}
    anchor={target.anchor}
    activate={target.activate}
    canPlay={Boolean(activeTake(target.item)) && !target.item.removed}
    onclose={() => {
      speechTarget = null;
    }}
    onplay={() => void playOnly(target.item)}
    onloaded={(preview) => {
      speechPreviews = { ...speechPreviews, [target.item.id]: preview };
    }}
    onedit={() => {
      directionsTarget = target;
    }}
  />
{/if}
{#if directionsTarget}
  <div
    class="fixed inset-0 z-[100] grid place-items-center bg-black/45 p-3 backdrop-blur-sm"
  >
    <div
      class="w-full max-w-4xl max-h-[calc(100dvh-1.5rem)] overflow-y-auto rounded-2xl border border-[var(--line)] bg-[var(--paper-strong)] p-5 shadow-xl"
      role="dialog"
      aria-modal="true"
      aria-labelledby="drawer-direction-title"
      tabindex="-1"
      use:modalFocus={{ onclose: closeDirections }}
    >
      <div class="flex items-start justify-between gap-4">
        <div>
          <h2 id="drawer-direction-title" class="text-lg font-semibold">
            Speech directions
          </h2>
          <p class="muted text-sm">
            Review segment {directionsTarget.item.ordinal + 1} and adopt changes before
            regenerating.
          </p>
        </div>
        <button
          class="grid size-10 place-items-center rounded-lg border border-[var(--line)]"
          aria-label="Close speech directions"
          onclick={closeDirections}><X size={18} /></button
        >
      </div>
      <PerformancePanel
        bind:this={directionsPanel}
        {sessionId}
        revisionId={directionsTarget.revisionId}
        initialOpen={true}
        initialSegmentId={directionsTarget.item.id}
        initialOrdinal={directionsTarget.item.ordinal}
        showCasting={false}
        onchanged={() => {
          speechPreviews = {};
          void load(true, false);
        }}
      />
    </div>
  </div>
{/if}
{#if passageActions}
  <PassageActionsPopover
    anchor={passageActions.anchor}
    activate={passageActions.activate}
    boundary={passageActions.boundary}
    disabledReason={passageTargetDisabledReason(passageActions)}
    busy={topologyBusy}
    onclose={() => {
      passageActions = null;
    }}
    onpreview={previewPassageAction}
    onsplit={quickSplitPassage}
  />
{/if}
{#if passagePreview}
  <PassageSplitDialog
    item={passagePreview.item}
    layer={passagePreview.layer}
    boundary={passagePreview.boundary}
    disabledReason={passageDisabledReason}
    busy={topologyBusy}
    onclose={() => {
      passagePreview = null;
    }}
    onsplit={() => splitPassageBoundary()}
  />
{/if}
{#if showRvc}
  <div
    class="fixed inset-0 z-[80] grid place-items-center bg-black/40 p-5"
    role="presentation"
    onclick={(event) => {
      if (event.currentTarget === event.target) showRvc = false;
    }}
  >
    <div
      class="surface max-h-[92vh] w-full max-w-2xl overflow-y-auto rounded-3xl p-6"
      role="dialog"
      aria-modal="true"
      aria-labelledby="rvc-conversion-title"
    >
      <header class="flex items-start justify-between gap-4">
        <div>
          <h2 id="rvc-conversion-title" class="mt-1 text-xl font-semibold">
            RVC conversion
          </h2>
          <p class="muted mt-1 text-sm">
            Convert existing takes without changing their spoken text.
          </p>
        </div>
        <button class="action" onclick={() => (showRvc = false)}>Close</button>
      </header>
      <div class="mt-5 grid gap-4 sm:grid-cols-2">
        <label class="text-sm font-semibold"
          >Model
          <select bind:value={rvcModel} class="field mt-1 w-full"
            ><option value="">Choose a model</option
            >{#each rvcModels as item}<option value={item}>{item}</option
              >{/each}</select
          >
        </label>
        <label class="text-sm font-semibold"
          >Pitch
          <input
            type="number"
            min="-24"
            max="24"
            bind:value={rvcPitch}
            class="field mt-1 w-full"
          /></label
        >
        <label class="text-sm font-semibold"
          >Pitch detector
          <select bind:value={rvcF0} class="field mt-1 w-full"
            ><option value="rmvpe">RMVPE</option><option value="harvest"
              >Harvest</option
            ><option value="crepe">CREPE</option><option value="pm">PM</option
            ></select
          >
        </label>
        <label class="text-sm font-semibold"
          >Index rate
          <input
            type="number"
            min="0"
            max="1"
            step="0.05"
            bind:value={rvcIndexRate}
            class="field mt-1 w-full"
          /></label
        >
      </div>
      {#if !rvcModels.length}
        <p class="mt-4 text-sm text-red-500">
          No RVC models are available. Add one in RVC management first.
        </p>
      {/if}
      <footer class="mt-6 flex flex-wrap items-center justify-between gap-3">
        <a href="/rvc" class="action">Manage models</a>
        <div class="flex flex-wrap justify-end gap-2">
          <button
            onclick={() => start('rvc', selectedSegmentIds)}
            disabled={!selectedSegmentIds.length || !rvcModel}
            class="action">Selected ({selectedSegmentIds.length})</button
          >
          <button
            onclick={() => start('rvc', marked)}
            disabled={!marked.length || !rvcModel}
            class="action">Marked ({marked.length})</button
          >
          <button
            onclick={() => start('rvc', [])}
            disabled={!rvcModel}
            class="action primary">Convert all</button
          >
        </div>
      </footer>
    </div>
  </div>
{/if}
{#if alternateOpen}
  <div
    class="fixed inset-0 z-[80] grid place-items-center bg-black/40 p-5"
    role="presentation"
    onclick={(event) => {
      if (event.currentTarget === event.target) alternateOpen = false;
    }}
  >
    <div
      class="surface max-h-[92vh] w-full max-w-3xl overflow-y-auto rounded-3xl p-6"
      role="dialog"
      aria-modal="true"
      aria-labelledby="alternate-regeneration-title"
    >
      <header class="flex items-start justify-between gap-4">
        <div>
          <h2
            id="alternate-regeneration-title"
            class="mt-1 text-xl font-semibold"
          >
            Regenerate {alternateSegmentIds.length} selected segment{alternateSegmentIds.length ===
            1
              ? ''
              : 's'} with…
          </h2>
          <p class="muted mt-1 text-xs">
            Uses {selectedRun
              ? `History · ${selectedHistoryRun?.label ?? selectedRun.label}`
              : 'the current session settings'} as the source, then saves these choices
            with the replacement takes in the same output run. Previous takes remain
            available.
          </p>
        </div>
        <button
          class="action"
          aria-label="Close alternate regeneration"
          onclick={() => (alternateOpen = false)}>Close</button
        >
      </header>

      <div class="mt-5 grid gap-4 sm:grid-cols-2">
        <label class="text-sm font-semibold"
          >Speech service
          <select
            class="field mt-1 w-full"
            value={String(alternateTts.service ?? '')}
            onchange={(event) => {
              const service = event.currentTarget.value;
              const next = ttsCatalogue.services.find(
                (item) => item.id === service
              );
              const language = String(
                alternateTts.language ?? alternateTts.target_language ?? 'en'
              );
              const model = String(
                next?.default_model ??
                  next?.models?.[0] ??
                  next?.model_catalog?.[0]?.id ??
                  ''
              );
              const voice = next
                ? preferredAlternateVoice(next, model, language)
                : '';
              alternateTts = {
                ...alternateTts,
                service,
                tts_service: service,
                model,
                voice,
                speaker: voice
              };
            }}
          >
            <option value="">Choose a service</option>
            {#each selectableTtsServices(ttsCatalogue.services, alternateTts.service) as service}
              <option value={service.id} disabled={service.online === false}
                >{service.name ?? service.id}{service.online === false
                  ? ' · unavailable'
                  : ''}</option
              >
            {/each}
          </select>
        </label>
        <label class="text-sm font-semibold"
          >Model
          <select
            class="field mt-1 w-full"
            value={String(alternateTts.model ?? '')}
            disabled={!alternateService ||
              alternateService.online === false ||
              !alternateModels.length}
            onchange={(event) => {
              const model = event.currentTarget.value;
              const language = String(
                alternateTts.language ?? alternateTts.target_language ?? 'en'
              );
              const voice = alternateService
                ? setAlternateVoiceFor(
                    alternateService,
                    model,
                    language,
                    String(alternateTts.voice ?? '')
                  )
                : '';
              alternateTts = {
                ...alternateTts,
                model,
                voice,
                speaker: voice
              };
            }}
          >
            {#if !alternateModels.length}<option value=""
                >Service default</option
              >{/if}
            {#each alternateModels as model}<option value={model}
                >{model}</option
              >{/each}
          </select>
        </label>
        <label class="text-sm font-semibold"
          >Voice / managed reference
          <select
            class="field mt-1 w-full"
            value={String(alternateTts.voice ?? '')}
            disabled={!alternateService || alternateService.online === false}
            onchange={(event) => {
              const voice = event.currentTarget.value;
              alternateTts = { ...alternateTts, voice, speaker: voice };
            }}
          >
            <option value="">Service default</option>
            {#each alternateVoices as voice}<option value={voice.id}
                >{voiceLabel(voice)}</option
              >{/each}
          </select>
          <span class="muted mt-1 block text-[.67rem]"
            >Published voice references appear here when ready for this
            provider.</span
          >
        </label>
        <label class="text-sm font-semibold"
          >Language
          <select
            class="field mt-1 w-full"
            value={String(alternateTts.language ?? '')}
            disabled={!alternateService || alternateService.online === false}
            onchange={(event) => {
              const language = event.currentTarget.value;
              const voice = alternateService
                ? setAlternateVoiceFor(
                    alternateService,
                    String(alternateTts.model ?? ''),
                    language,
                    String(alternateTts.voice ?? '')
                  )
                : '';
              alternateTts = {
                ...alternateTts,
                language,
                target_language: language,
                voice,
                speaker: voice
              };
            }}
          >
            {#each alternateLanguages as language}<option value={language.value}
                >{language.label}</option
              >{/each}
          </select>
        </label>
      </div>

      <label class="mt-4 block text-sm font-semibold"
        >Generation prompt / instructions
        <textarea
          class="field mt-1 min-h-20 w-full"
          value={String(alternateTts.generation_prompt ?? '')}
          oninput={(event) => {
            alternateTts = {
              ...alternateTts,
              generation_prompt: event.currentTarget.value
            };
          }}
          placeholder="Provider-supported style or voice instructions"
        ></textarea>
      </label>

      {#if alternateIsChatterbox}
        <div
          class="mt-4 grid gap-4 rounded-xl bg-[var(--accent-soft)] p-4 sm:grid-cols-2"
        >
          <label class="text-sm font-semibold"
            >Chatterbox exaggeration
            <input
              class="field mt-1 w-full"
              type="number"
              min="0"
              max="2"
              step="0.05"
              value={Number(alternateTts.chatterbox_exaggeration ?? 0.5)}
              onchange={(event) =>
                (alternateTts = {
                  ...alternateTts,
                  chatterbox_exaggeration: Number(event.currentTarget.value)
                })}
            />
          </label>
          <label class="text-sm font-semibold"
            >Chatterbox CFG weight
            <input
              class="field mt-1 w-full"
              type="number"
              min="0"
              max="2"
              step="0.05"
              value={Number(alternateTts.chatterbox_cfg_weight ?? 0.5)}
              onchange={(event) =>
                (alternateTts = {
                  ...alternateTts,
                  chatterbox_cfg_weight: Number(event.currentTarget.value)
                })}
            />
          </label>
        </div>
      {/if}

      <div class="mt-4 rounded-xl border border-[var(--line)] p-4">
        <label class="flex items-center gap-2 text-sm font-semibold">
          <input
            type="checkbox"
            checked={Boolean(alternateRvc.enabled)}
            onchange={(event) =>
              (alternateRvc = {
                ...alternateRvc,
                enabled: event.currentTarget.checked
              })}
          />
          Convert the new take with RVC
        </label>
        {#if alternateRvc.enabled}
          <div class="mt-3 grid gap-3 sm:grid-cols-3">
            <label class="text-sm font-semibold"
              >RVC model
              <select
                class="field mt-1 w-full"
                value={String(alternateRvc.model ?? '')}
                onchange={(event) =>
                  (alternateRvc = {
                    ...alternateRvc,
                    model: event.currentTarget.value,
                    rvc_model: event.currentTarget.value
                  })}
              >
                <option value="">Choose a model</option>
                {#each rvcModels as model}<option value={model}>{model}</option
                  >{/each}
              </select>
            </label>
            <label class="text-sm font-semibold"
              >Pitch
              <input
                class="field mt-1 w-full"
                type="number"
                min="-24"
                max="24"
                value={Number(alternateRvc.pitch ?? 0)}
                onchange={(event) =>
                  (alternateRvc = {
                    ...alternateRvc,
                    pitch: Number(event.currentTarget.value)
                  })}
              />
            </label>
            <label class="text-sm font-semibold"
              >Index rate
              <input
                class="field mt-1 w-full"
                type="number"
                min="0"
                max="1"
                step="0.05"
                value={Number(alternateRvc.index_rate ?? 0.3)}
                onchange={(event) =>
                  (alternateRvc = {
                    ...alternateRvc,
                    index_rate: Number(event.currentTarget.value)
                  })}
              />
            </label>
          </div>
        {/if}
      </div>
      {#if alternateService?.online === false}<p
          class="mt-3 text-sm text-red-500"
        >
          This speech service is currently unavailable.
        </p>{/if}
      {#if alternateRvc.enabled && !rvcModels.length}<p
          class="mt-3 text-sm text-red-500"
        >
          No RVC models are available. Add one in RVC management first.
        </p>{/if}
      <footer class="mt-6 flex justify-end gap-3">
        <button class="action" onclick={() => (alternateOpen = false)}
          >Cancel</button
        >
        <button
          class="action primary"
          disabled={!alternateCanStart || loading}
          onclick={submitAlternateRegeneration}
          >Create alternate take{alternateSegmentIds.length === 1
            ? ''
            : 's'}</button
        >
      </footer>
    </div>
  </div>
{/if}
{#if ttsServicesOpen}<TtsServicesModal
    onclose={() => {
      ttsServicesOpen = false;
      loadSpeechOptions();
    }}
  />{/if}
{#if comparisonItem}
  <SpeechPlanReviewDialog
    item={comparisonItem}
    plan={comparisonPlan}
    decisionRows={comparisonDecisionRows}
    text={comparisonText}
    diff={comparisonDiff}
    regenerate={regenerateAfterReview}
    ontext={(value) => (comparisonText = value)}
    ontogglediff={() => (comparisonDiff = !comparisonDiff)}
    ontoggleregenerate={(value) => (regenerateAfterReview = value)}
    onclose={() => (comparisonItem = null)}
    onsave={saveOptimizationReview}
  />
{/if}
{#if generationStartDialog}
  <GenerationStartDialog
    {sessionId}
    planRevisionId={payload.plan_revision_id}
    initialMode={generationStartDialog.mode}
    {settingsSourceRunId}
    pausedRunLabel={run?.status === 'paused'
      ? (run.label ?? 'paused run')
      : null}
    onclose={() => (generationStartDialog = null)}
    onstarted={(started, preview) =>
      void handleGenerationStarted(started, preview)}
  />
{/if}

<style>
  .generation-drawer {
    display: flex;
    flex-direction: column;
    height: auto;
    min-height: 3.9rem;
    border: 1px solid var(--line);
    background: var(--paper-strong);
    box-shadow: var(--shadow);
    transition: height 0.18s ease;
  }
  .generation-drawer.half {
    height: min(52vh, 38rem);
  }
  .generation-drawer.full {
    height: calc(100vh - 1.5rem);
  }
  @media (width < 48rem) {
    .generation-header {
      gap: 0.5rem;
      padding: 0.5rem 0.75rem;
    }
    .drawer-summary {
      flex: 1 1 calc(100% - 10rem);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .generation-drawer:not(.half, .full) .header-playback {
      margin-left: 0;
    }
    .header-playback :global(button),
    .drawer-actions :global(button) {
      min-height: 2.75rem;
      min-width: 2.75rem;
    }
    .generation-drawer:is(.half, .full) {
      inset: 0;
      height: 100dvh;
      border-radius: 0;
    }
    .run-picker .mini {
      max-width: calc(100vw - 8rem);
    }
    .generation-drawer .drawer-height-toggle {
      display: none;
    }
  }
  .run-progress {
    height: 0.34rem;
    width: min(9rem, 18vw);
    overflow: hidden;
    border-radius: 999px;
    background: var(--line);
  }
  .run-progress span {
    display: block;
    height: 100%;
    border-radius: inherit;
    background: var(--accent);
    transition: width 0.2s ease;
  }
  .cost-pill {
    border: 1px solid var(--line);
    border-radius: 999px;
    background: var(--accent-soft);
    padding: 0.28rem 0.55rem;
    font-size: 0.65rem;
    font-weight: 700;
    color: var(--ink);
  }
  .action {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    border: 1px solid var(--line);
    border-radius: 0.55rem;
    padding: 0.4rem 0.6rem;
    font-size: 0.7rem;
    font-weight: 700;
  }
  .action.primary {
    background: var(--action-bg);
    color: white;
  }
  .action.primary:hover {
    background: var(--action-hover);
  }
  .action:disabled {
    opacity: 0.35;
  }
  .icon-action {
    padding: 0.42rem;
  }
  .dropdown-wrapper {
    position: relative;
    display: inline-flex;
  }
  .dropdown-menu {
    position: absolute;
    top: calc(100% + 4px);
    right: 0;
    z-index: 50;
    min-width: 12.5rem;
    border: 1px solid var(--line);
    border-radius: 0.65rem;
    background: var(--paper-strong);
    box-shadow: 0 8px 24px rgb(0 0 0 / 0.18);
    padding: 0.35rem;
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }
  .dropdown-menu.left {
    right: auto;
    left: 0;
  }
  .dropdown-item {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.4rem 0.6rem;
    border-radius: 0.45rem;
    font-size: 0.72rem;
    font-weight: 600;
    color: var(--ink);
    text-align: left;
    background: transparent;
    border: none;
    cursor: pointer;
    text-decoration: none;
  }
  .dropdown-item:hover,
  .dropdown-item:focus {
    background: var(--accent-soft);
  }
  .dropdown-item.active {
    background: var(--accent-soft);
    color: var(--accent);
    font-weight: 700;
  }
  .dropdown-section-title {
    padding: 0.25rem 0.5rem 0.15rem;
    font-size: 0.6rem;
    font-weight: 700;
    text-transform: uppercase;
    color: var(--muted);
    letter-spacing: 0.04em;
  }
  .dropdown-divider {
    height: 1px;
    background: var(--line);
    margin: 0.2rem 0;
  }
  .mini {
    border: 1px solid var(--line);
    border-radius: 0.45rem;
    background: var(--paper);
    padding: 0.3rem 0.45rem;
    font-size: 0.68rem;
  }
  @media (prefers-reduced-motion: reduce) {
    .generation-drawer {
      transition: none;
    }
  }
</style>
