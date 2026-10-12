<script lang="ts">
  import GenerationRunHistory from './GenerationRunHistory.svelte';
  import GenerationAudioVersionHistory from './GenerationAudioVersionHistory.svelte';
  import GenerationAlternateDialog, {
    type GenerationAlternateDraft
  } from './GenerationAlternateDialog.svelte';
  import {
    generationHistoryVersion,
    generationHistoryStatus,
    visibleGenerationRuns
  } from './generation-history';
  import { errorMessage } from './errors';
  import { useProjectInterface } from './project-interface.svelte';
  import { invalidationBus, invalidates } from './invalidation';
  import PassageSplitDialog from './PassageSplitDialog.svelte';
  import PassageActionsPopover from './PassageActionsPopover.svelte';
  import SpeechInspector from './SpeechInspector.svelte';
  import SpeechSelectionToolbar from './SpeechSelectionToolbar.svelte';
  import ProjectSoundControls from './ProjectSoundControls.svelte';
  import type { SoundAnchor } from './project-sounds-api';
  import {
    inspectorTargetKey,
    type SpeechInspectorTarget,
    type SpeechInspectorController
  } from './speech-inspector';
  import type { SpeechSelection, SpeechPreview } from './speech-annotations';
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
    BookOpenText,
    Download,
    CircleHelp,
    Eye,
    ListMusic,
    Pause,
    PencilLine,
    Play,
    RefreshCw,
    Search,
    Settings,
    Sparkles,
    Square,
    Trash2,
    Undo2,
    WandSparkles
  } from '@lucide/svelte';
  import { onDestroy, onMount, tick, untrack } from 'svelte';
  import { beforeNavigate, goto } from '$app/navigation';
  import { page } from '$app/state';
  import { appState } from './app-state.svelte';
  import { generationApi, type GenerationSegmentChanges } from './domain-api';
  import type {
    AudioTake,
    GenerationRun,
    GenerationSegment,
    SpeechPlan
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
  import SpeechEditingHelp from './SpeechEditingHelp.svelte';
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
  import { GenerationSpeechOptionsState } from './generation-speech-options.svelte';
  import { getTtsServiceDetail } from './tts-catalogue-cache';
  import type { SpeechCapabilities } from './speech-support';
  import type {
    ComparisonDecisionRow,
    PlayableTake,
    ReadingBlock
  } from './generation-view-models';
  import { useGenerationWorkspace } from './generation-workspace-state.svelte';
  import {
    selectedGenerationTake,
    type TakeSelection
  } from './generation-take-selection';

  let {
    sessionId,
    workflowKind = 'audiobook'
  }: {
    sessionId: string;
    workflowKind?: string;
  } = $props();
  const projectInterface = useProjectInterface();
  const simpleMode = $derived(projectInterface?.mode === 'simple');
  const isVoiceover = $derived(workflowKind === 'voiceover');
  const workspaceState = useGenerationWorkspace(untrack(() => sessionId));
  const preferences = workspaceState.preferences;
  const editingEntry = untrack(
    () =>
      page.url.searchParams.get('mode') === 'editing' ||
      page.url.searchParams.get('inspect') === 'speech'
  );
  let showAudioColumns = $state(preferences?.showAudioColumns ?? true);
  let showDirectionDetails = $state(preferences?.showDirectionDetails ?? false);
  let helpOpen = $state(false);
  let planMenuOpen = $state(false);
  let planHistory = $state<SpeechPlanHistory>();
  const generationStore = new GenerationStore(untrack(() => sessionId));
  const summaryStore = workspaceState.summary;
  let mode = $state<'collapsed' | 'half' | 'full'>('full');
  const payload = $derived(generationStore.payload);
  const run = $derived(generationStore.activeRun);
  const summary = $derived(summaryStore.value);
  const headerRun = $derived(mode === 'collapsed' ? summary?.active_run : run);
  const headerTotal = $derived(
    mode === 'collapsed' || generationStore.status === 'idle'
      ? (summary?.total ?? 0)
      : payload.total
  );
  const headerPlanId = $derived(
    payload.plan_revision_id ?? summary?.plan_revision_id
  );
  const runs = $derived(visibleGenerationRuns(generationStore.runs));
  const takeNumbers = $derived.by(() => {
    const numbers = new Map<string, number>();
    for (const item of payload.items) {
      [...item.takes]
        .filter((take) => !take.is_reused)
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
  let selectedRunId = $state(
    untrack(() => (editingEntry ? '' : (preferences?.selectedRunId ?? '')))
  );
  let selectedRunVersionId = $state(
    untrack(() =>
      editingEntry ? '' : (preferences?.selectedRunVersionId ?? '')
    )
  );
  let audioVersionSide = $state<'before' | 'after'>(
    preferences?.audioVersionSide ?? 'after'
  );
  // Editing creates a new plan; its settings source survives leaving history.
  let settingsSourceRunId = $state(preferences?.settingsSourceRunId ?? '');
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
          : 'Generation queued — waiting for other project work'
        : 'Generation queued — waiting for a worker';
      for (const item of payload.items) {
        if (!ids.length || ids.includes(item.id)) messages[item.id] = message;
      }
    }
    return messages;
  });
  const assembly = $derived(generationStore.assembly);
  let filter = $state<SegmentFilter>(preferences?.filter ?? 'all');
  let error = $state('');
  let loading = $state(false);
  let generationStartDialog = $state<{ mode: GenerationStartMode } | null>(
    null
  );
  let timer: number | undefined;
  let startedRunReconciliation: AbortController | undefined;
  let selectedRow = $state(preferences?.selectedRow ?? '');
  let previewSegmentId = $state('');
  let selectedRows = $state<string[]>([...(preferences?.selectedRows ?? [])]);
  let selectionAnchor = $state('');
  let viewMode = $state<'segments' | 'reading'>(
    preferences?.viewMode ?? 'segments'
  );
  let segmentScrollRoot = $state<HTMLDivElement | null>(null);
  let readingView = $state<{
    scrollToSegment: (id: string) => Promise<boolean>;
  }>();
  let nextPageSentinel = $state<HTMLElement>();
  let previousPageSentinel = $state<HTMLElement>();
  let viewportTimer: number | undefined;
  let restoredPosition = false;
  let lastVisibleAnchor = {
    id: preferences?.anchorId ?? '',
    ordinal: (preferences?.startOrdinal ?? 0) + 10
  };
  let segmentTable = $state<{
    editSegmentText: (
      id: string,
      range?: { start: number; end: number }
    ) => Promise<boolean>;
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
    align: 'center' | 'nearest' | 'start' = 'nearest'
  ) {
    await tick();
    if (viewMode === 'segments' && segmentTable) {
      return segmentTable.scrollToSegment(id, { align });
    }
    if (viewMode === 'reading' && readingView)
      return readingView.scrollToSegment(id);
    const row = document.querySelector<HTMLElement>(
      `[data-segment-id="${CSS.escape(id)}"]`
    );
    row?.scrollIntoView({ block: align, behavior: 'auto' });
    return Boolean(row);
  }
  let textMode = $state<'display' | 'speech'>(
    preferences?.textMode ?? 'speech'
  );
  let displayMenuOpen = $state(false);
  let showPassageBoundaries = $state(readPassageVisibility());
  let showSpeechAnnotations = $state(true);
  let speechPreviews = $state<Record<string, SpeechPreview>>({});
  let speechSelection = $state<SpeechSelection | null>(null);
  let inspectorTarget = $state<SpeechInspectorTarget | null>(null);
  let speechCapabilities = $state<SpeechCapabilities>();
  let inspector = $state<SpeechInspectorController>();
  let soundControls = $state<ProjectSoundControls>();
  let mixedAssemblyArtifactId = $state<string | null>(null);
  const returningSound = untrack(() => {
    const itemId = page.url.searchParams.get('sound_item');
    try {
      const anchor = JSON.parse(
        page.url.searchParams.get('sound_anchor') ?? 'null'
      ) as SoundAnchor | null;
      return itemId &&
        anchor &&
        [
          'before_segment',
          'after_segment',
          'source_time',
          'after_word',
          'next_pause'
        ].includes(anchor.kind)
        ? { itemId, anchor }
        : null;
    } catch {
      return null;
    }
  });
  let soundReturnOpened = false;
  $effect(() => {
    if (returningSound && soundControls && !simpleMode && !soundReturnOpened) {
      soundReturnOpened = true;
      soundControls.useSaved(returningSound.anchor, returningSound.itemId);
    }
  });
  let inspectorDirty = $state(false),
    inspectorPending = $state(false);
  let inspectorIntent = 0;
  onDestroy(() => {
    inspectorIntent++;
  });
  function editSelectedSpeech(selection: SpeechSelection) {
    inspectorIntent++;
    if (
      selectedRunId ||
      !payload.plan_revision_id ||
      pendingSegmentUpdates ||
      inspectorPending
    )
      return;
    passageActions = null;
    speechSelection = selection;
  }
  function closeInspector() {
    inspectorIntent++;
    const anchor = inspectorTarget?.anchor;
    const inlineItemId =
      inspectorTarget?.presentation === 'block-instruction'
        ? inspectorTarget.item.id
        : '';
    inspectorTarget = null;
    inspectorDirty = false;
    inspectorPending = false;
    if (anchor?.isConnected) anchor.focus({ preventScroll: true });
    else if (inlineItemId)
      void tick().then(() => {
        document
          .querySelector<HTMLButtonElement>(
            `[data-segment-id="${CSS.escape(inlineItemId)}"] .block-number`
          )
          ?.focus({ preventScroll: true });
      });
  }
  async function editBlockInstruction(
    item: GenerationSegment,
    anchor: HTMLTextAreaElement
  ) {
    if (!payload.plan_revision_id || selectedRunId || inspectorPending) return;
    const request = ++inspectorIntent;
    const rect = anchor.getBoundingClientRect();
    try {
      // Blur can save wording and clone the plan. Resolve its final ID before
      // asking for XML, rather than opening an editor for the old revision.
      const [currentId] = await editQueue.settledIds([item.id]);
      if (request !== inspectorIntent || selectedRunId || inspectorPending)
        return;
      const focused = document.activeElement;
      if (anchor.isConnected ? focused !== anchor : focused !== document.body)
        return;
      const current = payload.items.find((entry) => entry.id === currentId);
      const revisionId = payload.plan_revision_id;
      if (!current || !revisionId) return;
      openInspector({
        item: current,
        start: 0,
        end: Array.from(current.optimized_text ?? current.text).length,
        kind: 'segment',
        anchor,
        rect: { left: rect.left, top: rect.top, bottom: rect.bottom },
        revisionId,
        focus: 'instruction',
        presentation: 'block-instruction'
      });
    } catch (caught) {
      if (request === inspectorIntent) error = errorMessage(caught);
    }
  }
  function openInspector(target: SpeechInspectorTarget) {
    if (inspectorPending) return;
    inspectorIntent++;
    const next = { ...target };
    if (
      inspectorTarget &&
      inspectorTargetKey(next) === inspectorTargetKey(inspectorTarget)
    ) {
      const focus = () => {
        inspectorTarget = next;
        inspector?.focusSection(next.focus);
      };
      if (next.focus !== inspectorTarget.focus && inspector)
        inspector.requestClose(focus);
      else focus();
      return;
    }
    const show = () => {
      speechSelection = null;
      if (
        next.presentation === 'inline' ||
        next.presentation === 'block-instruction'
      ) {
        viewMode = 'segments';
        textMode = 'speech';
      }
      inspectorTarget = next;
      inspectorDirty = false;
      inspectorPending = false;
    };
    if (inspector) inspector.requestClose(show);
    else show();
  }
  function openSelectedInspector(
    selection: SpeechSelection,
    focus: SpeechInspectorTarget['focus']
  ) {
    const revisionId = payload.plan_revision_id;
    if (!revisionId) return;
    // Capture the target before dismissing its toolbar's reactive selection.
    openInspector({
      ...selection,
      revisionId,
      focus,
      presentation: 'selection'
    });
    speechSelection = null;
    window.getSelection()?.removeAllRanges();
  }
  async function speechChanged() {
    speechPreviews = {};
    soundControls?.refresh();
    await load(true, false);
    if (inspectorTarget) {
      const updated = payload.items.find(
        (item) => item.id === inspectorTarget?.item.id
      );
      if (updated) inspectorTarget = { ...inspectorTarget, item: updated };
    }
    await speechOptions.refreshSettings();
    await loadSpeakerNames();
  }
  function inspectSpeech(
    item: GenerationSegment,
    _offset: number,
    anchor: HTMLButtonElement,
    activate: boolean
  ) {
    const revisionId = payload.plan_revision_id;
    if (!revisionId || !activate || inspectorPending) return;
    passageActions = null;
    const rect = anchor.getBoundingClientRect();
    const end = Number(
      anchor.dataset.speechEnd ??
        Array.from(item.optimized_text ?? item.text).length
    );
    const selection = {
      item,
      start: 0,
      end,
      anchor,
      kind: 'segment' as const,
      rect: { left: rect.left, top: rect.top, bottom: rect.bottom }
    };
    if (anchor.hasAttribute('data-speech-xml')) {
      if (
        inspectorTarget?.presentation === 'inline' &&
        inspectorTarget.item.id === item.id
      )
        inspector?.requestClose(closeInspector);
      else
        openInspector({
          ...selection,
          revisionId,
          focus: 'xml',
          presentation: 'inline'
        });
      return;
    }
    if (anchor.hasAttribute('data-speech-block') && !selectedRunId) {
      editSelectedSpeech(selection);
      return;
    }
    openInspector({
      item,
      start: Number(anchor.dataset.speechStart ?? 0),
      end,
      anchor,
      rect: { left: rect.left, top: rect.top, bottom: rect.bottom },
      revisionId,
      runId: selectedRunVersionId || selectedRunId,
      focus: ['speaker', 'delivery', 'instruction', 'emotion'].includes(
        anchor.dataset.speechFocus ?? ''
      )
        ? (anchor.dataset.speechFocus as SpeechInspectorTarget['focus'])
        : anchor.dataset.speechDirection
          ? 'delivery'
          : projectMultiVoice
            ? 'speaker'
            : 'instruction',
      presentation: 'selection'
    });
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
  let searchQuery = $state(preferences?.searchQuery ?? '');
  let searchOptions = $state(
    preferences?.searchOptions ?? { matchCase: false, wholeWord: false }
  );
  // The search panel is hidden by default behind a toolbar magnifier toggle.
  // While hidden, searchQuery stays empty so no filter affects the visible
  // list and no full-plan scan runs.
  let searchOpen = $state(preferences?.searchOpen ?? false);
  // Voiceover-only search/replace target. Audiobook keeps following textMode.
  let searchScope = $state<'cue' | 'tts'>(preferences?.searchScope ?? 'cue');
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
    simpleMode
      ? 'spoken'
      : isVoiceover
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
  let topologyBusy = $state(false);
  let supportingOptionsScope = '';
  let alternateDraft = $state.raw<GenerationAlternateDraft | null>(null);
  let pendingSegmentUpdates = $state(0);
  const editQueue = new GenerationEditQueue();
  const savedEditRows = new Map<string, GenerationSegment>();
  let regenerationNotice = $state('');

  const normalizeId = (value: unknown) =>
    String(value ?? '')
      .trim()
      .toLowerCase()
      .replaceAll('-', '_');
  const speechOptions = new GenerationSpeechOptionsState(
    () => sessionId,
    normalizeId
  );
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
      : speechOptions.settings;
  });
  const selectedTtsService = $derived.by(() => {
    const configured = String(
      inheritedTtsSettings.service ??
        inheritedTtsSettings.tts_service ??
        speechOptions.catalogue.default_service ??
        ''
    );
    return (
      speechOptions.catalogue.services.find((service) =>
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
  const directionTtsSettings = $derived.by(() => {
    if (!selectedRunId)
      return Object.keys(speechOptions.settings).length
        ? speechOptions.settings
        : null;
    const historical = generationHistoryVersion(
      generationStore.runs,
      selectedRunId,
      selectedRunVersionId
    )?.settings_snapshot?.tts;
    return historical && typeof historical === 'object'
      ? (historical as Record<string, unknown>)
      : null;
  });
  $effect(() => {
    const settings = directionTtsSettings;
    const configured = normalizeId(settings?.service ?? settings?.tts_service);
    const service = speechOptions.catalogue.services.find((entry) =>
      [entry.id, entry.name].some((id) => normalizeId(id) === configured)
    );
    const serviceId = service?.id;
    const model = String(
      settings?.model || settings?.xtts_model || service?.default_model || ''
    );
    const voiceRef = settings?.audio_cpp_voice_ref;
    speechCapabilities = undefined;
    if (!settings || !serviceId || !model || speechOptions.loading) return;
    let stale = false;
    void getTtsServiceDetail(serviceId, { models: model })
      .then((detail) => {
        if (stale) return;
        const profile = detail.service.expressive_capabilities?.[model];
        const family = detail.service.model_catalog?.find(
          (entry) => entry.id === model
        )?.family;
        speechCapabilities =
          serviceId === 'audio_cpp' &&
          family === 'fireredtts3' &&
          voiceRef &&
          profile
            ? {
                ...profile,
                instructions: 'none',
                instruction_scope: [],
                emotion: { mode: 'none', tags: [] }
              }
            : profile;
      })
      .catch(() => {
        /* Unknown routes remain visibly unverified. */
      });
    return () => {
      stale = true;
    };
  });
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
      ? speechOptions.voices.flatMap((voice) => {
          const registration =
            voice?.metadata_json?.providers?.[selectedTtsServiceId];
          return registration?.status === 'ready' && registration?.voice_id
            ? [String(registration.voice_id)]
            : [];
        })
      : [];
    const managedRegistrations = new Map(
      speechOptions.voices.flatMap((voice) => {
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
  const projectMultiVoice = $derived(
    speechOptions.settings.casting_enabled === true
  );
  let speakerNames = $state<Record<string, string>>({});
  let speakerNamesError = $state('');
  let speakerNamesRequest = 0;
  async function loadSpeakerNames() {
    const request = ++speakerNamesRequest;
    const scope = sessionId;
    if (!projectMultiVoice) {
      speakerNames = {};
      speakerNamesError = '';
      return;
    }
    try {
      const controls = await generationApi.controls(scope);
      if (request !== speakerNamesRequest || scope !== sessionId) return;
      speakerNames = Object.fromEntries(
        controls.characters.map((entry) => [entry.id, entry.display_name])
      );
      speakerNamesError = '';
    } catch (caught) {
      if (request === speakerNamesRequest && scope === sessionId)
        speakerNamesError = errorMessage(caught);
    }
  }
  $effect(() => {
    // One shared roster read when the project mode or identity changes.
    void loadSpeakerNames();
  });
  const optimizedPlan = $derived(
    !selectedRunId &&
      summary?.plan_revision_id === payload.plan_revision_id &&
      summary?.plan_source_role === 'tts_optimized'
  );
  function openCurrentPlan() {
    if (inspectorPending) return;
    const change = () => {
      void editQueue
        .settledIds([])
        .then(() => {
          closeInspector();
          speechSelection = null;
          selectedRunId = '';
          selectedRunVersionId = '';
          viewMode = 'segments';
          textMode = 'speech';
        })
        .catch((caught) => {
          error = errorMessage(caught);
        });
    };
    if (inspector) inspector.requestClose(change);
    else change();
  }
  function changeTextMode(next: 'display' | 'speech') {
    if (inspectorPending || next === textMode) return;
    const change = () => {
      void editQueue
        .settledIds([])
        .then(() => {
          closeInspector();
          speechSelection = null;
          textMode = next;
        })
        .catch((caught) => {
          error = errorMessage(caught);
        });
    };
    if (inspector) inspector.requestClose(change);
    else change();
  }

  function positionPlanMenu(node: HTMLElement) {
    const position = () => {
      const anchor = node.parentElement
        ?.querySelector('button')
        ?.getBoundingClientRect();
      if (!anchor) return;
      const size = node.getBoundingClientRect();
      node.style.position = 'fixed';
      node.style.right = 'auto';
      node.style.left = `${Math.max(8, Math.min(anchor.right - size.width, document.documentElement.clientWidth - size.width - 8))}px`;
      node.style.top = `${Math.max(8, Math.min(anchor.bottom + 4, innerHeight - size.height - 8))}px`;
    };
    position();
    const resize = new ResizeObserver(position);
    resize.observe(node);
    window.addEventListener('resize', position);
    window.addEventListener('scroll', position, true);
    return {
      destroy() {
        resize.disconnect();
        window.removeEventListener('resize', position);
        window.removeEventListener('scroll', position, true);
      }
    };
  }
  const marked = $derived(
    payload.items.filter((item) => item.marked).map((item) => item.id)
  );
  const selectedSegmentIds = $derived([...new Set(selectedRows)]);
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
    topologyBusy ||
      inspectorDirty ||
      inspectorPending ||
      Boolean(selectedRunId) ||
      !payload.plan_revision_id
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
    (selectedRun?.audio_version
      ? (selectedRun.audio_version_assemblies?.[audioVersionSide] ??
        (selectedRun.assembly?.audio_version_side === audioVersionSide
          ? selectedRun.assembly
          : null))
      : selectedRun?.assembly) ??
      (!selectedRun && !assembly?.generation_run_id ? assembly : null)
  );
  const headerAssembly = $derived(
    mode === 'collapsed' ? summary?.assembly : selectedAssembly
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
      if (
        !paragraph ||
        paragraph.kind !== 'paragraph' ||
        paragraph.closed ||
        paragraph.items.length >= 12
      ) {
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

  function visibleRows() {
    if (!segmentScrollRoot) return [];
    const rect = segmentScrollRoot.getBoundingClientRect();
    const visible = [
      ...segmentScrollRoot.querySelectorAll<HTMLElement>('[data-segment-id]')
    ].filter((node) => {
      const bounds = node.getBoundingClientRect();
      return bounds.bottom > rect.top && bounds.top < rect.bottom;
    });
    const ids = new Set(visible.map((node) => node.dataset.segmentId));
    return payload.items.filter((item) => ids.has(item.id));
  }

  function rememberView() {
    const first = visibleRows()[0];
    if (first) lastVisibleAnchor = { id: first.id, ordinal: first.ordinal };
    workspaceState.preferences = {
      selectedRunId,
      selectedRunVersionId,
      audioVersionSide,
      settingsSourceRunId,
      filter,
      selectedRow,
      selectedRows: [...selectedRows],
      viewMode,
      showAudioColumns,
      showDirectionDetails,
      textMode,
      searchQuery,
      searchOptions: { ...searchOptions },
      searchOpen,
      searchScope,
      scrollTop: segmentScrollRoot?.scrollTop ?? 0,
      startOrdinal: Math.max(0, lastVisibleAnchor.ordinal - 10),
      anchorId: lastVisibleAnchor.id
    };
  }

  function loadOptions() {
    const rows = visibleRows();
    return {
      filter,
      selectedRunId,
      selectedRunVersionId,
      audioVersionSide,
      search: searchParams,
      ...(rows.length
        ? {
            visibleOrdinals: {
              start: rows[0].ordinal,
              end: rows.at(-1)!.ordinal
            }
          }
        : {}),
      ...(generationStore.status === 'idle' && preferences
        ? { startOrdinal: preferences.startOrdinal }
        : {})
    };
  }

  async function load(reset = true, preserveLoaded = reset) {
    try {
      applyLoadResult(
        await generationStore.load({ ...loadOptions(), reset, preserveLoaded })
      );
      if (!restoredPosition && preferences && payload.items.length) {
        restoredPosition = true;
        await tick();
        if (preferences.anchorId)
          await revealSegment(preferences.anchorId, 'start');
      }
    } catch (caught) {
      error = errorMessage(caught);
    }
  }

  async function loadPrevious() {
    const rows = visibleRows();
    const anchor = rows[0]?.id;
    const element = anchor
      ? segmentScrollRoot?.querySelector<HTMLElement>(
          `[data-segment-id="${CSS.escape(anchor)}"]`
        )
      : null;
    const before = element?.getBoundingClientRect().top;
    try {
      await generationStore.loadPrevious(loadOptions());
      await tick();
      if (anchor && before != null && segmentScrollRoot) {
        await revealSegment(anchor);
        const after = segmentScrollRoot
          .querySelector<HTMLElement>(
            `[data-segment-id="${CSS.escape(anchor)}"]`
          )
          ?.getBoundingClientRect().top;
        if (after != null) segmentScrollRoot.scrollTop += after - before;
      }
    } catch (caught) {
      error = errorMessage(caught);
    }
  }

  function onSegmentScroll() {
    if (!segmentScrollRoot) return;
    if (viewportTimer) window.clearTimeout(viewportTimer);
    viewportTimer = window.setTimeout(() => {
      rememberView();
      if (
        generationStore.needsVisibleRefresh(
          visibleRows().map((item) => item.id)
        ) &&
        !generationStore.paging
      )
        void load(true, true);
    }, 200);
  }

  async function openTakeHistory(item: GenerationSegment) {
    try {
      await generationStore.loadTakeHistory(item, loadOptions());
    } catch (caught) {
      error = errorMessage(caught);
    }
  }

  function expandIfCollapsed() {
    if (mode === 'collapsed') mode = 'full';
  }

  function applyLoadResult(result: GenerationLoadResult) {
    if (result.discarded) return;
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
    pinnedRevisionId: string | null = null,
    attempt?: ReturnType<typeof generationApi.createStartAttempt>
  ): Promise<boolean> {
    if (inspectorPending) return false;
    if (inspectorDirty) {
      inspector?.requestClose(() => {
        closeInspector();
        void start(
          operation,
          ids,
          selectedSegmentOverride,
          staleOnly,
          pinnedRevisionId,
          attempt
        );
      });
      return false;
    }
    if (operation === 'rvc' && !rvcModel) {
      showRvc = true;
      expandIfCollapsed();
      error = 'Choose an RVC model before converting audio.';
      return false;
    }
    loading = true;
    error = '';
    regenerationNotice = '';
    let accepted = false;
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
        settingsSourceRunId || null,
        attempt
      );
      // Acceptance survives a later refresh error; retrying would create another take.
      accepted = true;
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
    return accepted;
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
      topologyBusy ||
      inspectorDirty ||
      inspectorPending
    ) {
      throw new Error(
        'The plan is still changing. Inspect the updated plan before marking it reviewed.'
      );
    }
  }

  async function openGenerationStart(mode: GenerationStartMode) {
    if (inspectorPending) return;
    if (inspectorDirty) {
      inspector?.requestClose(() => {
        closeInspector();
        void openGenerationStart(mode);
      });
      return;
    }
    if (
      !payload.plan_revision_id ||
      (summary?.plan_revision_id &&
        summary.plan_revision_id !== payload.plan_revision_id)
    ) {
      await load(true, false);
      if (generationStore.error) return;
    }
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
        : speechOptions.settings;
    const sourceRvc =
      snapshot.rvc && typeof snapshot.rvc === 'object'
        ? (snapshot.rvc as Record<string, unknown>)
        : {};
    return { sourceTts, sourceRvc };
  }

  function openAlternateRegeneration(ids: string[]) {
    if (!ids.length) return;
    const { sourceTts, sourceRvc } = sourceSettingsForAlternate();
    alternateDraft = {
      segmentIds: [...new Set(ids)],
      sourceTts,
      sourceRvc,
      fallbackModel: selectedTtsModel,
      fallbackVoice: inheritedVoice,
      fallbackLanguage: inheritedLanguage,
      sourceLabel: selectedRun
        ? `History · ${selectedHistoryRun?.label ?? selectedRun.label}`
        : settingsSourceRun
          ? `History · ${settingsSourceRun.label}`
          : 'the current project settings'
    };
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

  async function loadSupportingOptions(editing: boolean) {
    const scope = editing ? 'advanced' : 'simple';
    if (supportingOptionsScope === scope) return;
    supportingOptionsScope = scope;
    await Promise.all([
      editing ? loadRvc() : Promise.resolve(),
      speechOptions.load({ editing })
    ]);
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
        await generationApi.createAssembly(
          sessionId,
          selectedRun?.id || null,
          {},
          selectedRun?.audio_version ? audioVersionSide : 'after'
        )
      );
      await load();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      loading = false;
    }
  }

  const takeSelection = $derived<TakeSelection>({
    historical: Boolean(selectedRun),
    audioVersion: Boolean(selectedRun?.audio_version),
    sequence: Number(selectedRun?.sequence_number ?? 0),
    sequences: Object.fromEntries(
      generationStore.runs.map((item) => [
        item.id,
        Number(item.sequence_number || 0)
      ])
    )
  });
  function activeTake(item: GenerationSegment): PlayableTake | undefined {
    return selectedGenerationTake(item, takeSelection);
  }
  const playback = workspaceState.playback;
  $effect(() => {
    const query = generationStore.pageQuery({
      filter,
      selectedRunId,
      selectedRunVersionId,
      audioVersionSide,
      search: searchParams
    });
    if (payload.plan_revision_id)
      query.set('plan_revision_id', payload.plan_revision_id);
    untrack(() => workspaceState.sync(payload, query, takeSelection));
  });
  $effect(() => {
    if (playback.activePlayingId) selectedRow = playback.activePlayingId;
  });
  const playlistActive = $derived(playback.active);
  const playlistPaused = $derived(playback.paused);
  const activePlayingId = $derived(playback.activePlayingId);

  function stopPlayback() {
    playback.stop();
  }

  async function togglePlaylistPlayback() {
    if (generationStore.status === 'idle') {
      await load(true, false);
      if (generationStore.error) return;
    }
    // Starting the playlist takes over audibility: drop any mounted row
    // preview so the previous take stops with its unmounted element.
    if (!playback.active) previewSegmentId = '';
    playback.toggle(selectedRow);
  }

  function playOnly(item: GenerationSegment) {
    // Controller playback (keyboard shortcut, reading view, popovers) also
    // takes over: unmount the row preview so it stops.
    previewSegmentId = '';
    const queued = workspaceState.items.find(
      (candidate) => candidate.id === item.id
    );
    return queued ? playback.playOnly(queued) : Promise.resolve();
  }

  function requestSegmentPreview(item: GenerationSegment) {
    // Row preview takes over: stop playlist playback, then mount only this
    // row's player. Switching rows unmounts the previous player, and its
    // onDestroy pauses the old audio element.
    playback.stop();
    previewSegmentId = item.id;
  }

  function takeLabel(take: AudioTake) {
    const historyLoaded =
      payload.items.some(
        (item) =>
          item.take_history_loaded === false &&
          item.takes.some((candidate) => candidate.id === take.id)
      ) === false;
    const recordingLabel = historyLoaded
      ? `Take ${takeNumbers.get(take.id) ?? 1}`
      : 'Selected recording';
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
    if (take.rendered_voices?.length) {
      const voices = [
        ...new Set(
          take.rendered_voices.map((part) => {
            const speakers = part.speaker_labels?.length
              ? part.speaker_labels
              : part.speaker_ids.length
                ? part.speaker_ids
                : [
                    part.source === 'narrator' || part.source === 'base'
                      ? 'Narrator'
                      : 'Voice'
                  ];
            return `${speakers.join(', ')}${part.voice ? `: ${part.voice}` : ''}`;
          })
        )
      ];
      return `${owner?.label ?? 'Legacy'} · ${voices.join(', ')} · ${take.is_reused ? 'Reused recording' : recordingLabel}`;
    }
    return `${label} · ${recordingLabel} · ${String(take.kind || 'audio').toUpperCase()}`;
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
    const picker = event.currentTarget as HTMLSelectElement;
    const next = picker.value;
    picker.value = selectedRunId;
    if (inspectorPending) return;
    const change = () => {
      closeInspector();
      void selectAudioRun(next);
    };
    if (inspector) inspector.requestClose(change);
    else change();
  }

  async function selectAudioRun(next: string) {
    selectedRunId = next;
    selectedRunVersionId = '';
    audioVersionSide = 'after';
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
    if (inspectorPending) return;
    if (inspectorDirty) {
      inspector?.requestClose(() => {
        closeInspector();
        void selectRepairVersion(versionId);
      });
      return;
    }
    closeInspector();
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
          audioVersionSide,
          reset: true,
          preserveLoaded: false,
          startOrdinal: item.ordinal
        })
      );
    }
    if (viewMode !== 'segments') viewMode = 'segments';
    await revealSegment(item.id, 'center');
    const matchingLayer =
      searchField === 'spoken' ? textMode === 'speech' : textMode === 'display';
    if (matchingLayer && !simpleMode)
      await segmentTable?.editSegmentText(item.id, match);
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
    else if (!field)
      document
        .querySelector<HTMLElement>(
          `[data-segment-id="${CSS.escape(item.id)}"] .speech-editing-text`
        )
        ?.focus({ preventScroll: true });
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
        alternateDraft ||
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
    if (target?.closest('[role="toolbar"], .speech-inspector')) return;
    if (
      target &&
      (target.tagName === 'INPUT' ||
        target.tagName === 'TEXTAREA' ||
        target.tagName === 'SELECT' ||
        target.isContentEditable ||
        Boolean(target.closest('[data-speech-editing]')))
    ) {
      return;
    }
    if (event.key === 'Escape') {
      displayMenuOpen = false;
      settingsMenuOpen = false;
      regenerateMenuOpen = false;
      planMenuOpen = false;
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
    } else if (
      !simpleMode &&
      event.key === ' ' &&
      index >= 0 &&
      !onNativeControl
    ) {
      const item = payload.items[index];
      patchSegment(item, {
        marked: !item.marked
      });
      event.preventDefault();
    } else if (!simpleMode && event.key === 'Delete' && index >= 0) {
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

  onMount(() =>
    projectInterface?.guard(async (nextMode) => {
      if (nextMode !== 'simple') return true;
      if (inspectorDirty || inspectorPending || topologyBusy) return false;
      const focused = document.activeElement;
      if (
        focused instanceof HTMLElement &&
        segmentScrollRoot?.contains(focused)
      )
        focused.blur();
      await editQueue.settledIds([]);
      speechSelection = null;
      passageActions = null;
      closeInspector();
      displayMenuOpen = false;
      settingsMenuOpen = false;
      regenerateMenuOpen = false;
      planMenuOpen = false;
      return true;
    })
  );

  let allowNavigation = false;
  beforeNavigate((navigation) => {
    if (
      allowNavigation ||
      navigation.to?.url.pathname === navigation.from?.url.pathname
    )
      return;
    rememberView();
    const focused = document.activeElement;
    if (focused instanceof HTMLElement && segmentScrollRoot?.contains(focused))
      focused.blur();
    if (inspectorDirty || inspectorPending) {
      if (navigation.willUnload) return; // The inspector supplies the browser's unload guard.
      navigation.cancel();
      const destination = navigation.to?.url;
      if (destination && !inspectorPending)
        inspector?.requestClose(() => {
          closeInspector();
          void editQueue
            .settledIds([])
            .then(async () => {
              allowNavigation = true;
              await goto(destination, {
                replaceState: navigation.type === 'popstate'
              });
            })
            .catch((caught) => {
              error = errorMessage(caught);
            })
            .finally(() => {
              allowNavigation = false;
            });
        });
      return;
    }
    if (
      pendingSegmentUpdates <= 0 &&
      !editQueue.hasPending &&
      !editQueue.hasFailures
    )
      return;
    navigation.cancel();
    if (navigation.willUnload || !navigation.to) return;
    const target = navigation.to.url;
    void editQueue
      .settledIds([])
      .then(async () => {
        allowNavigation = true;
        await goto(target, { replaceState: navigation.type === 'popstate' });
      })
      .catch((caught) => {
        error = errorMessage(caught);
      })
      .finally(() => {
        allowNavigation = false;
      });
  });

  onMount(() => {
    workspaceState.loadFromView = async () => {
      await Promise.all([load(false), speechOptions.refreshSettings()]);
      await tick();
    };
    if (settingsSourceRunId)
      void generationStore.ensureRun(settingsSourceRunId).catch((caught) => {
        error = errorMessage(caught);
      });
    const openPlan = (requestedSessionId: string) => {
      if (requestedSessionId !== sessionId) return;
      const open = () => {
        closeInspector();
        selectedRunId = '';
        filter = 'all';
        textMode = 'speech';
        viewMode = 'segments';
        void speechOptions.refreshSettings();
      };
      if (inspector) inspector.requestClose(open);
      else open();
    };
    const disconnectPlanEditor = subscribeSpeechPlanEditor(openPlan);
    const visible = () => {
      if (document.visibilityState === 'visible') {
        generationStore.reconcileActivity();
        void load(true, true);
        void speechOptions.refreshSettings();
      }
    };
    document.addEventListener('visibilitychange', visible);
    const disconnectSpeechOptions = invalidationBus.subscribe((batch) => {
      if (
        (invalidates(batch, 'sessions', sessionId) ||
          invalidates(batch, 'workflow', sessionId)) &&
        batch.events.some(
          (event) => !event.job_id && !event.type.startsWith('job.')
        )
      ) {
        void speechOptions.refreshSettings();
        void loadSpeakerNames();
      }
    });
    const disconnect = generationStore.connect(
      loadOptions,
      applyLoadResult,
      () => document.visibilityState !== 'hidden'
    );
    return () => {
      rememberView();
      workspaceState.loadFromView = null;
      // The project retains only the compact audio queue while it is playing.
      if (!playback.active) workspaceState.releaseQueue();
      speechOptions.dispose();
      speakerNamesRequest++;
      disconnect();
      disconnectPlanEditor();
      disconnectSpeechOptions();
      document.removeEventListener('visibilitychange', visible);
      if (timer) window.clearTimeout(timer);
      if (viewportTimer) window.clearTimeout(viewportTimer);
      searchController?.abort();
      startedRunReconciliation?.abort();
    };
  });
  $effect(() => {
    if (
      !nextPageSentinel ||
      !segmentScrollRoot ||
      payload.next_cursor == null ||
      generationStore.paging ||
      generationStore.error ||
      generationStore.status === 'loading'
    )
      return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (
          entries.some((entry) => entry.isIntersecting) &&
          !generationStore.paging
        )
          void load(false);
      },
      { root: segmentScrollRoot, rootMargin: '0px 0px 400px 0px' }
    );
    observer.observe(nextPageSentinel);
    return () => observer.disconnect();
  });
  $effect(() => {
    if (
      !previousPageSentinel ||
      !segmentScrollRoot ||
      generationStore.startOrdinal <= 0 ||
      !payload.items.length ||
      generationStore.paging ||
      generationStore.error
    )
      return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (
          entries.some((entry) => entry.isIntersecting) &&
          !generationStore.paging
        )
          void loadPrevious();
      },
      { root: segmentScrollRoot, rootMargin: '200px 0px 0px 0px' }
    );
    observer.observe(previousPageSentinel);
    return () => observer.disconnect();
  });
  $effect(() => {
    const editing = !simpleMode;
    if (mode !== 'collapsed')
      void untrack(() => loadSupportingOptions(editing));
  });
  let lastSpeechScope = '';
  $effect(() => {
    const scope = `${payload.plan_revision_id ?? ''}|${selectedRunId}|${selectedRunVersionId}|${audioVersionSide}`;
    if (scope !== untrack(() => lastSpeechScope)) {
      lastSpeechScope = scope;
      speechPreviews = {};
      untrack(closeInspector);
      speechSelection = null;
    }
  });
  let lastPreviewScope = '';
  $effect(() => {
    // Preview reset lives apart from the annotation clearing above: every
    // payload replacement (including take adoptions and reloads) re-fires
    // effects that read through the payload object, even when the revision
    // ID is unchanged. Compare the previous/current primitive scope so a
    // take switch keeps its mounted player instead of unmounting it.
    const scope = `${payload.plan_revision_id ?? ''}|${selectedRunId}|${selectedRunVersionId}|${audioVersionSide}`;
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
    if (mode === 'collapsed') return;
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
      (headerRun &&
        [
          'queued',
          'running',
          'pausing',
          'pause_requested',
          'cancel_requested'
        ].includes(headerRun.status)) ||
      (headerAssembly && ['queued', 'running'].includes(headerAssembly.status));
    if (active) {
      // Events remain the primary update path, but a stream can look healthy
      // while a terminal event is lost during reconnect. Reconcile active work
      // periodically so filtered segments cannot remain stale indefinitely.
      timer = window.setTimeout(
        () => {
          if (document.visibilityState !== 'hidden') {
            generationStore.reconcileActivity();
            void load(true, true);
          }
        },
        appState.eventsHealthy ? 10_000 : 2_500
      );
    }
  });
</script>

<svelte:window onkeydown={onGlobalDrawerKeydown} />

{#snippet speechEditor(item: GenerationSegment)}
  {#if inspectorTarget && inspectorTarget.item.id === item.id}
    {@const target = { ...inspectorTarget, item }}
    {#key inspectorTargetKey(target)}
      <SpeechInspector
        bind:this={inspector}
        {sessionId}
        {target}
        multiVoice={projectMultiVoice}
        blockedReason={pendingSegmentUpdates > 0
          ? 'Wait for the text edit to finish saving.'
          : topologyBusy
            ? 'Wait for the speech-block edit to finish.'
            : ['queued', 'running', 'pausing', 'cancel_requested'].includes(
                  run?.status ?? ''
                )
              ? 'Wait for generation to stop before changing speech directions.'
              : ''}
        canPlay={Boolean(activeTake(item)) && !item.removed}
        onplay={() => void playOnly(item)}
        onclose={closeInspector}
        onapplied={speechChanged}
        onloaded={(preview) => {
          speechPreviews = { ...speechPreviews, [item.id]: preview };
        }}
        ondraft={(state) => {
          inspectorDirty = state.dirty;
          inspectorPending = state.pending;
        }}
      />
    {/key}
  {/if}
{/snippet}

{#if helpOpen}<SpeechEditingHelp
    {sessionId}
    {isVoiceover}
    multiVoice={projectMultiVoice}
    onclose={() => (helpOpen = false)}
  />{/if}

{#if displayMenuOpen || settingsMenuOpen || regenerateMenuOpen || planMenuOpen}
  <button
    type="button"
    class="fixed inset-0 z-40 cursor-default bg-transparent"
    onclick={() => {
      displayMenuOpen = false;
      settingsMenuOpen = false;
      regenerateMenuOpen = false;
      planMenuOpen = false;
    }}
    tabindex="-1"
    aria-label="Close menu"
  ></button>
{/if}

<section
  data-testid="generation-workspace"
  aria-label="Generation workspace"
  data-generation-layout="full"
  data-workspace-mode="unified"
  class="generation-workspace overflow-hidden rounded-2xl"
>
  <header
    class="generation-header flex shrink-0 flex-wrap items-center gap-3 border-b border-[var(--line)] px-4 py-3 lg:flex-nowrap"
  >
    <h2 class="flex min-h-11 items-center gap-2 font-semibold">Generation</h2>
    <span
      class="drawer-summary muted min-w-0 text-xs lg:flex-1 lg:truncate"
      title={`${headerTotal} segments · ${selectedHistoryRun?.label ?? 'Active mix'}${headerAssembly ? ` · output ${headerAssembly.status}` : ''}`}
      >{#if summaryStore.status === 'loading' && !summary}Loading generation
        summary…{:else if summaryStore.status === 'failed' && !headerPlanId}Could
        not load generation controls{:else}{headerTotal} segments · {selectedHistoryRun?.label ??
          'Active mix'}{#if headerAssembly}
          · output {headerAssembly.status}{/if}{/if}</span
    >
    {#if selectedRunUsage?.commercial}<span class="cost-pill"
        >{selectedRunUsage.estimated ? 'Est.' : ''}
        {selectedRunCost}{selectedRunUsage.has_unpriced_usage
          ? ' + unpriced usage'
          : ''}</span
      >{/if}
    {#if headerRun && ['queued', 'running', 'pausing', 'pause_requested', 'cancel_requested'].includes(headerRun.status)}
      <div
        class="run-progress"
        role="progressbar"
        aria-label="Generation progress"
        aria-valuemin="0"
        aria-valuemax="100"
        aria-valuenow={progressPercent(headerRun.progress)}
      >
        <span style={`width:${progressPercent(headerRun.progress)}%`}></span>
      </div>
      <span class="muted text-[.65rem] tabular-nums"
        >{progressPercent(headerRun.progress)}%</span
      >
      {#if headerRun.progress_detail || headerRun.status === 'queued'}
        <span
          class="muted max-w-64 truncate text-[.65rem]"
          title={headerRun.progress_detail ?? ''}
          >{headerRun.progress_detail ??
            'Waiting for an available worker'}</span
        >
      {/if}
    {/if}
    {#if headerAssembly && ['queued', 'running'].includes(headerAssembly.status)}
      <div
        class="run-progress"
        role="progressbar"
        aria-label="Output assembly progress"
        aria-valuemin="0"
        aria-valuemax="100"
        aria-valuenow={progressPercent(headerAssembly.progress)}
      >
        <span style={`width:${progressPercent(headerAssembly.progress)}%`}
        ></span>
      </div>
      <span class="muted text-[.65rem] tabular-nums"
        >{progressPercent(headerAssembly.progress)}%</span
      >
      {#if headerAssembly.progress_detail || headerAssembly.status === 'queued'}
        <span
          class="muted max-w-64 truncate text-[.65rem]"
          title={headerAssembly.progress_detail ?? ''}
          >{headerAssembly.progress_detail ??
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
        {playlistActive
          ? playlistPaused
            ? 'Resume'
            : 'Pause'
          : mixedAssemblyArtifactId
            ? 'Play speech'
            : 'Play'}
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
    <ProjectSoundControls
      bind:this={soundControls}
      {sessionId}
      {workflowKind}
      editable={!simpleMode && !selectedRunId}
      baseArtifactId={selectedAssembly?.artifact_id}
      onmixchange={(mix) => {
        mixedAssemblyArtifactId =
          mix &&
          ['ready', 'completed'].includes(mix.status) &&
          mix.base_artifact_id === selectedAssembly?.artifact_id
            ? mix.artifact_id
            : null;
      }}
    />
    {#if mode !== 'collapsed'}
      <button
        type="button"
        data-generation-search-toggle
        onclick={() => (searchOpen ? closeSearch() : openSearch())}
        class="action icon-action"
        class:active={searchOpen}
        title={simpleMode ? 'Search (Ctrl+K)' : 'Search and replace (Ctrl+K)'}
        aria-label={simpleMode ? 'Search' : 'Search and replace'}
        aria-keyshortcuts="Control+k Meta+k"
        aria-expanded={searchOpen}
        aria-controls="generation-search-panel"
      >
        <Search size={14} />
      </button>
      {#if !simpleMode}
        <div class="dropdown-wrapper">
          <button
            onclick={() => {
              displayMenuOpen = !displayMenuOpen;
              settingsMenuOpen = false;
              regenerateMenuOpen = false;
              planMenuOpen = false;
            }}
            class="action icon-action"
            class:active={displayMenuOpen}
            title={`Display options · ${activeFilterLabel}`}
            aria-label="Display options"
            aria-expanded={displayMenuOpen}
          >
            <Eye size={14} />
          </button>
          {#if displayMenuOpen}
            <div class="dropdown-menu left">
              <span class="dropdown-section-title">Show segments</span>
              <label class="dropdown-item"
                >Filter<select
                  aria-label="Segments to display"
                  bind:value={filter}
                  onchange={() => (displayMenuOpen = false)}
                  class="mini"
                  >{#each SEGMENT_FILTER_OPTIONS as option}<option
                      value={option.value}>{option.label}</option
                    >{/each}</select
                ></label
              >
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
              <button
                type="button"
                class="dropdown-item"
                aria-pressed={showAudioColumns}
                onclick={() => {
                  showAudioColumns = !showAudioColumns;
                  displayMenuOpen = false;
                }}
              >
                {showAudioColumns ? 'Hide audio columns' : 'Show audio columns'}
              </button>
              <button
                type="button"
                class="dropdown-item"
                aria-pressed={showDirectionDetails}
                onclick={() => {
                  showDirectionDetails = !showDirectionDetails;
                  displayMenuOpen = false;
                }}
              >
                {showDirectionDetails
                  ? 'Hide direction details'
                  : 'Show direction details'}
              </button>
              <div class="dropdown-divider"></div>
              <span class="dropdown-section-title">Text layer</span>
              <button
                type="button"
                class="dropdown-item"
                class:active={textMode === 'display'}
                onclick={() => {
                  changeTextMode('display');
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
                  changeTextMode('speech');
                  displayMenuOpen = false;
                }}
              >
                Speech text · optimized where available
              </button>
              <div class="dropdown-divider"></div>
              <button
                type="button"
                class="dropdown-item"
                aria-pressed={showSpeechAnnotations}
                onclick={() => {
                  showSpeechAnnotations = !showSpeechAnnotations;
                  if (showSpeechAnnotations) showPassageBoundaries = false;
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
        <div class="dropdown-wrapper">
          <button
            type="button"
            class="action"
            aria-expanded={planMenuOpen}
            disabled={!payload.plan_revision_id}
            onclick={() => {
              planMenuOpen = !planMenuOpen;
              displayMenuOpen = false;
              settingsMenuOpen = false;
              regenerateMenuOpen = false;
            }}>Speech plans</button
          >
          {#if planMenuOpen}<div
              class="dropdown-menu plan-menu left"
              use:positionPlanMenu
            >
              {#if payload.plan_revision_id}{#key `${sessionId}:${payload.plan_revision_id}:${selectedRunId}`}<GenerationPlanReviewBar
                    {sessionId}
                    revisionId={payload.plan_revision_id}
                    current={!selectedRunId &&
                      payload.is_active_revision !== false}
                    refreshKey={payload.plan_revision_id}
                    blocked={loading ||
                      generationStore.status === 'loading' ||
                      generationStore.status === 'stale' ||
                      topologyBusy ||
                      inspectorDirty ||
                      inspectorPending ||
                      pendingSegmentUpdates > 0 ||
                      [
                        'queued',
                        'running',
                        'pausing',
                        'cancel_requested'
                      ].includes(run?.status ?? '')}
                    beforeReview={settlePlanReviewEdits}
                    onrefresh={() => load(true, false)}
                  />{/key}{/if}
              <button
                type="button"
                class="dropdown-item"
                onclick={() => {
                  planMenuOpen = false;
                  openCurrentPlan();
                }}><PencilLine size={14} />Edit current plan</button
              >
              <button
                type="button"
                class="dropdown-item"
                disabled={Boolean(selectedRunId)}
                onclick={() => {
                  planMenuOpen = false;
                  planHistory?.openHistory();
                }}>Details &amp; versions…</button
              >
            </div>{/if}
        </div>
        <SpeechPlanHistory
          bind:this={planHistory}
          showTrigger={false}
          {sessionId}
          activeRevisionId={selectedRunId
            ? null
            : payload.plan_revision_id || null}
          disabled={loading ||
            topologyBusy ||
            inspectorDirty ||
            inspectorPending ||
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
            disabled={topologyDisabled}
            class="action icon-action"
            title={topologyUndoLabel}
            aria-label={topologyUndoLabel}
          >
            <Undo2 size={14} />
          </button>
        {/if}
      {/if}
    {/if}
    <div class="drawer-actions ml-auto flex flex-wrap gap-2">
      {#if simpleMode}<a class="action" href={`/sessions/${sessionId}/output`}
          >Output</a
        >{/if}
      {#if mode === 'collapsed' && headerRun}
        <button class="action" onclick={() => (mode = 'full')}
          >Open generation controls</button
        >
      {:else if !run || ['completed', 'partial', 'failed', 'canceled', 'paused'].includes(run.status)}
        <button
          onclick={() => openGenerationStart(selectedRun ? 'all' : 'refresh')}
          disabled={loading ||
            generationStore.loading ||
            topologyBusy ||
            pendingSegmentUpdates > 0}
          class="action primary"
          title={settingsSourceRunId
            ? 'Generate using the selected run’s saved voice and settings'
            : 'Generate using the current voice and settings'}
          ><Play size={14} /> Generate</button
        >
      {/if}
      {#if mode !== 'collapsed' && run?.status === 'paused'}
        <button
          onclick={() => action('resume')}
          class="action"
          title="Continue the paused run with its saved voice and settings"
          ><Play size={14} /> Resume previous run</button
        >
      {:else if mode !== 'collapsed' && run && ['queued', 'running'].includes(run.status)}
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
    </div>
    {#if !simpleMode}<button
        type="button"
        class="action icon-action"
        title="Editing and generation help"
        aria-label="Editing and generation help"
        onclick={() => (helpOpen = true)}><CircleHelp size={16} /></button
      >{/if}
  </header>

  {#if mode !== 'collapsed'}
    <div class="flex min-h-0 min-w-0 flex-1 flex-col overflow-y-auto">
      {#if !simpleMode}<div
          class="flex flex-wrap items-center justify-end gap-2 border-b border-[var(--line)] p-3"
        >
          <span class="mr-auto text-xs text-[var(--muted)]">
            {textMode === 'speech'
              ? 'TTS speech'
              : 'Display / subtitles'}{textMode === 'speech' && optimizedPlan
              ? ' · optimized source'
              : ''} ·
            <a class="underline" href={`/sessions/${sessionId}#voice-setup`}
              >{projectMultiVoice
                ? 'Multiple voices'
                : isVoiceover
                  ? 'One voice'
                  : 'One narrator'}</a
            >
            {#if filter !== 'all'}
              · {activeFilterLabel}{/if}
          </span>
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
              title="Project generation settings & speech services"
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
        {#if selectedRun?.audio_version}
          <div class="border-b border-[var(--line)] p-3">
            <GenerationAudioVersionHistory
              run={selectedRun}
              side={audioVersionSide}
              disabled={loading}
              onchoose={(side) => {
                audioVersionSide = side;
                stopPlayback();
                void load(true, false);
              }}
              onundone={(updated) => {
                generationStore.upsertRun(updated);
                selectedRunId = '';
                selectedRunVersionId = '';
                audioVersionSide = 'after';
                void load(true, false);
              }}
            />
          </div>
        {/if}
      {/if}
      {#if searchOpen}
        <div
          id="generation-search-panel"
          class="border-b border-[var(--line)] px-3 py-2"
        >
          <SearchReplaceBar
            readOnly={simpleMode}
            texts={editableTexts}
            onreplace={applySearchReplacements}
            onnavigate={navigateSearchMatch}
            disabled={searchLoading ||
              loading ||
              inspectorDirty ||
              inspectorPending}
            label={searchScopeLabel}
            onsearch={changeSearch}
            searching={searchLoading}
            showScopeSelector={isVoiceover && !simpleMode}
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
          <strong class="text-xs"
            >{mixedAssemblyArtifactId
              ? 'Speech with sound effects'
              : 'Assembled speech'}</strong
          >
          <div class="min-w-64 flex-1">
            <AudioPlayer
              src={`/api/v1/artifacts/${mixedAssemblyArtifactId ?? selectedAssembly.artifact_id}/content`}
              label={mixedAssemblyArtifactId
                ? 'Speech with sound effects'
                : 'Assembled speech'}
              preload="none"
            />
          </div>
          <a
            class="action"
            download
            href={`/api/v1/artifacts/${mixedAssemblyArtifactId ?? selectedAssembly.artifact_id}/content`}
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
            Completed recordings are still available. Continue unfinished audio
            keeps them and generates only missing, edited, or failed blocks.
          </p>
          <button
            type="button"
            class="action mt-2"
            disabled={loading || topologyBusy || pendingSegmentUpdates > 0}
            onclick={() =>
              openGenerationStart(selectedRun ? 'all' : 'continue')}
            data-testid="generation-recovery-continue"
            >Continue unfinished audio…</button
          >
        </div>
      {/if}

      {#if generationStore.loading}<p
          class="muted px-4 py-3 text-sm"
          role="status"
        >
          Loading segments…
        </p>{/if}

      {#if error}<p class="p-3 text-sm text-red-500" role="alert">
          {error}
        </p>{/if}
      {#if speechOptions.error || speakerNamesError}
        <div
          class="flex flex-wrap items-center gap-3 p-3 text-sm text-red-500"
          role="alert"
        >
          <p>
            Could not refresh speech options: {speechOptions.error ||
              speakerNamesError}
          </p>
          <button
            type="button"
            class="action"
            disabled={speechOptions.loading}
            onclick={() => {
              void speechOptions.load({ editing: !simpleMode });
              void loadSpeakerNames();
            }}>Retry speech options</button
          >
        </div>
      {/if}
      {#if run?.status === 'queued' && run.waiting_for_job}
        <p class="px-4 py-2 text-sm" role="status">
          Generation queued — waiting for {run.waiting_for_job.kind.startsWith(
            'export'
          )
            ? 'export'
            : 'other project work'}.
          {run.waiting_for_job.progress_detail ?? ''}
        </p>
      {/if}
      {#if regenerationNotice}<p
          class="p-3 text-sm text-[var(--muted)]"
          role="status"
        >
          {regenerationNotice}
        </p>{/if}

      <div class="flex min-h-0 flex-1">
        <div
          bind:this={segmentScrollRoot}
          onscroll={onSegmentScroll}
          data-testid="generation-scroll-root"
          class="min-h-[12rem] min-w-0 flex-1 overflow-auto"
        >
          {#if generationStore.startOrdinal > 0}<div
              bind:this={previousPageSentinel}
              class="py-2 text-center"
            >
              <button
                class="btn btn-secondary"
                disabled={generationStore.paging}
                onclick={loadPrevious}>Load earlier segments</button
              >
            </div>{/if}
          {#if !headerPlanId && generationStore.status === 'empty'}<p
              class="muted p-8 text-center"
            >
              Prepare the speech plan from the project overview to start
              generating audio.
            </p>{/if}
          {#if simpleMode || viewMode === 'segments'}
            {#key simpleMode}<GenerationSegmentTable
                previewOnly={simpleMode}
                wordEditingDisabled={topologyBusy || !payload.plan_revision_id}
                showAudioColumns={simpleMode || showAudioColumns}
                showDirectionDetails={!simpleMode && showDirectionDetails}
                {optimizedPlan}
                multiVoice={projectMultiVoice}
                {speakerNames}
                inlineEditorId={inspectorTarget?.presentation === 'inline'
                  ? inspectorTarget.item.id
                  : ''}
                inlineEditor={speechEditor}
                inlineInstructionEditorId={inspectorTarget?.presentation ===
                'block-instruction'
                  ? inspectorTarget.item.id
                  : ''}
                oninstruction={editBlockInstruction}
                oneditstart={() => {
                  speechSelection = null;
                  closeInspector();
                }}
                compactRows={simpleMode || workflowKind === 'audiobook'}
                bind:this={segmentTable}
                scrollRoot={segmentScrollRoot}
                activeSegmentId={activePlayingId}
                pinnedSegmentIds={[
                  selectedRow,
                  speechSelection?.item.id ?? '',
                  inspectorTarget?.item.id ?? '',
                  passageActions?.item.id ?? ''
                ]}
                {showSpeechAnnotations}
                {speechPreviews}
                soundCues={soundControls?.store.cues ?? {}}
                onvisiblesegments={(ids) => soundControls?.store.show(ids)}
                onsound={(item) => soundControls?.addSegment(item)}
                onsoundedit={(cue) => soundControls?.editCue(cue)}
                {speechCapabilities}
                performanceEnabled={Boolean(
                  directionTtsSettings
                    ? directionTtsSettings.performance_enabled
                    : true
                )}
                vocalizationsEnabled={Boolean(
                  directionTtsSettings
                    ? directionTtsSettings.performance_allow_vocalizations
                    : true
                )}
                onspeech={inspectSpeech}
                onselection={selectedRunId ? undefined : editSelectedSpeech}
                showPassageBoundaries={!simpleMode && showPassageBoundaries}
                onpassage={inspectPassage}
                items={payload.items}
                {pendingGeneration}
                {selectedRows}
                loading={loading || inspectorDirty || inspectorPending}
                speechOptionsLoading={speechOptions.loading}
                selectedTtsServiceName={selectedTtsService?.name}
                {selectedTtsModel}
                {inheritedVoice}
                {inheritedLanguage}
                textMode={simpleMode ? 'speech' : textMode}
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
                ontakehistory={openTakeHistory}
                ontakelabel={takeLabel}
                onverificationtitle={verificationTitle}
                onregenerate={(item) => start('regenerate', [item.id])}
                onregeneratewith={(item) =>
                  openAlternateRegeneration([item.id])}
                onmerge={mergeSpeechBlocks}
                onsplit={splitSpeechBlock}
                {topologyDisabled}
                {previewSegmentId}
                onpreviewrequest={requestSegmentPreview}
              />{/key}
          {:else}
            <GenerationReadingView
              bind:this={readingView}
              scrollRoot={segmentScrollRoot}
              multiVoice={projectMultiVoice}
              {speakerNames}
              detailsVisible={showDirectionDetails}
              {showSpeechAnnotations}
              {speechPreviews}
              {speechCapabilities}
              performanceEnabled={Boolean(
                directionTtsSettings
                  ? directionTtsSettings.performance_enabled
                  : true
              )}
              vocalizationsEnabled={Boolean(
                directionTtsSettings
                  ? directionTtsSettings.performance_allow_vocalizations
                  : true
              )}
              onspeech={inspectSpeech}
              onselection={selectedRunId ? undefined : editSelectedSpeech}
              {showPassageBoundaries}
              onpassage={inspectPassage}
              pinnedSegmentIds={[
                selectedRow,
                speechSelection?.item.id ?? '',
                inspectorTarget?.item.id ?? '',
                passageActions?.item.id ?? ''
              ]}
              blocks={readingBlocks}
              selectedRunLabel={selectedHistoryRun?.label ?? 'Active mix'}
              {textMode}
              loaded={payload.items.length}
              total={payload.total}
              {activePlayingId}
              {selectedRows}
              loading={loading || inspectorDirty || inspectorPending}
              ontextmode={changeTextMode}
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
          {#if payload.next_cursor != null}
            <div bind:this={nextPageSentinel} class="p-3 text-center">
              <button
                class="btn btn-secondary"
                disabled={generationStore.paging}
                onclick={() => load(false)}
              >
                {generationStore.paging
                  ? 'Loading segments…'
                  : generationStore.error
                    ? 'Retry loading segments'
                    : 'Load more'}
              </button>
            </div>
          {/if}
        </div>
        {#if inspectorTarget && (!inspectorTarget.presentation || inspectorTarget.presentation === 'selection')}{@render speechEditor(
            inspectorTarget.item
          )}{/if}
      </div>
    </div>
  {/if}
</section>
{#if !simpleMode && speechSelection && payload.plan_revision_id}
  <SpeechSelectionToolbar
    selection={speechSelection}
    multiVoice={projectMultiVoice}
    onopen={openSelectedInspector}
    onsound={(selection) => {
      soundControls?.addSelection(selection);
      speechSelection = null;
    }}
    onclose={() => {
      const anchor = speechSelection?.anchor;
      speechSelection = null;
      window.getSelection()?.removeAllRanges();
      if (anchor?.isConnected) anchor.focus({ preventScroll: true });
    }}
  />
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
{#if alternateDraft}
  <GenerationAlternateDialog
    draft={alternateDraft}
    {speechOptions}
    {rvcModels}
    onvoicelabel={voiceLabel}
    onclose={() => (alternateDraft = null)}
    onstart={async (ids, override, attempt) => {
      const accepted = await start(
        'regenerate',
        ids,
        override,
        false,
        null,
        attempt
      );
      return { accepted, error };
    }}
  />
{/if}
{#if ttsServicesOpen}<TtsServicesModal
    onclose={() => {
      ttsServicesOpen = false;
      speechOptions.load({ editing: !simpleMode });
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
  .generation-workspace {
    display: flex;
    flex-direction: column;
    height: max(36rem, calc(100dvh - 13rem));
    min-height: 32rem;
    border: 1px solid var(--line);
    background: var(--paper-strong);
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
    .header-playback :global(button),
    .drawer-actions :global(button) {
      min-height: 2.75rem;
      min-width: 2.75rem;
    }
    .run-picker .mini {
      max-width: calc(100vw - 8rem);
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
  .plan-menu {
    width: min(25rem, calc(100vw - 6rem));
    max-height: 65dvh;
    overflow: auto;
    right: 0;
    left: auto;
  }
  .dropdown-menu.plan-menu.left {
    right: 0;
    left: auto;
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
    .generation-workspace {
      transition: none;
    }
  }
</style>
