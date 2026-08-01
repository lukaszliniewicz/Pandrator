<script lang="ts">
  import { errorMessage } from './errors';
  import {
    ChevronDown,
    ChevronUp,
    BookOpenText,
    Download,
    ListMusic,
    Maximize2,
    Minimize2,
    Pause,
    Play,
    RefreshCw,
    Sparkles,
    Square,
    Trash2,
    WandSparkles
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
  import SearchReplaceBar from './SearchReplaceBar.svelte';
  import type { TextReplacement, TextSearchMatch } from './search-replace';
  import { LANGUAGE_OPTIONS } from './settings-fields';
  import {
    describeVoice,
    languagesForService,
    type VoiceDescriptor
  } from './voice-catalog';
  import type {
    ComparisonDecisionRow,
    PlayableTake,
    ReadingBlock
  } from './generation-view-models';
  import { GenerationPlaybackController } from './generation-playback.svelte';
  const hasArtifact = (take: AudioTake): take is PlayableTake =>
    Boolean(take.artifact_id);

  let { sessionId }: { sessionId: string } = $props();
  const generationStore = new GenerationStore(untrack(() => sessionId));
  let mode = $state<'collapsed' | 'half' | 'full'>('collapsed');
  const payload = $derived(generationStore.payload);
  const run = $derived(generationStore.activeRun);
  const runs = $derived(generationStore.runs);
  let selectedRunId = $state('');
  const assembly = $derived(generationStore.assembly);
  let filter = $state<SegmentFilter>('all');
  let error = $state('');
  let loading = $state(false);
  let timer: number | undefined;
  let startedRunReconciliation: AbortController | undefined;
  let selectedRow = $state('');
  let selectedRows = $state<string[]>([]);
  let selectionAnchor = $state('');
  let viewMode = $state<'segments' | 'reading'>('segments');
  let readingTextMode = $state<'display' | 'speech'>('display');
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
  let speechOptionsLoading = $state(false);
  let ttsSettings = $state<Record<string, unknown>>({});
  let ttsCatalogue = $state<TtsCatalogue>({ services: [] });
  let libraryVoices = $state<VoiceRecord[]>([]);

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
  const selectedTtsService = $derived.by(() => {
    const configured = String(
      ttsSettings.service ??
        ttsSettings.tts_service ??
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
      ttsSettings.model ||
        ttsSettings.xtts_model ||
        selectedTtsService?.default_model ||
        ''
    )
  );
  const inheritedLanguage = $derived(
    String(ttsSettings.language || ttsSettings.target_language || 'en')
  );
  const selectedTtsServiceId = $derived(
    normalizeId(
      selectedTtsService?.id ?? ttsSettings.service ?? ttsSettings.tts_service
    )
  );
  const modelVoiceDescriptors = $derived.by(() => {
    if (!selectedTtsService) return [] as VoiceDescriptor[];
    const service = selectedTtsService;
    const catalogue = Array.from(
      service.voice_catalogues?.[selectedTtsModel] ?? []
    ).map(String);
    const qwenCloning =
      selectedTtsServiceId === 'kobold_qwen' &&
      selectedTtsModel.toLowerCase() === 'voice cloning';
    const providerVoicesAllowed =
      !service.supports_prebuilt_voices || qwenCloning;
    const published = providerVoicesAllowed
      ? libraryVoices.flatMap((voice) => {
          const registration =
            voice?.metadata_json?.providers?.[selectedTtsServiceId];
          return registration?.status === 'ready' && registration?.voice_id
            ? [String(registration.voice_id)]
            : [];
        })
      : [];
    const live = providerVoicesAllowed
      ? Array.from(service.live_voices ?? []).map(String)
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
    ).map((voice) =>
      describeVoice(
        String(service.id ?? ttsSettings.service ?? ''),
        voice,
        service.voice_metadata?.[`${selectedTtsModel}:${voice}`]
      )
    );
  });
  const supportedSpeechLanguages = $derived.by(() => {
    const discovered = languagesForService(
      String(selectedTtsService?.id ?? ttsSettings.service ?? ''),
      modelVoiceDescriptors
    );
    return discovered.length
      ? discovered
      : LANGUAGE_OPTIONS.filter((item) => item.value !== 'auto');
  });
  const inheritedVoice = $derived(
    String(
      ttsSettings.voice ||
        ttsSettings.speaker ||
        selectedTtsService?.default_voices_by_language?.[selectedTtsModel]?.[
          inheritedLanguage
        ] ||
        selectedTtsService?.default_voices?.[selectedTtsModel] ||
        selectedTtsService?.default_voice ||
        ''
    )
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
    payload.items.map((item) => String(item.text ?? ''))
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
    runs.find((item) => item.id === selectedRunId) ?? null
  );
  const comparisonPlan = $derived<SpeechPlan>(
    comparisonItem?.speech_plan ?? {}
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
    selectedRun?.assembly ?? (!selectedRun ? assembly : null)
  );
  const selectedRunCost = $derived.by(() => {
    const value = selectedRun?.usage?.total_cost_usd;
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
        sessionApi.ttsCatalogue(true),
        sessionApi.voices()
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
          reset,
          preserveLoaded
        })
      );
    } catch (caught) {
      error = errorMessage(caught);
    }
  }

  function applyLoadResult(result: GenerationLoadResult) {
    selectedRunId = result.selectedRunId;
    if (result.shouldExpand) mode = 'half';
  }

  async function patchSegment(
    item: GenerationSegment,
    changes: GenerationSegmentChanges
  ) {
    try {
      const updated = await generationStore.updateSegment(item, changes);
      if (
        'node_kind' in changes ||
        'silence_after_ms' in changes ||
        'removed' in changes
      )
        await refreshAssembly();
      return updated;
    } catch (caught) {
      error = errorMessage(caught);
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
    if (regenerateAfterReview) await start('regenerate', [item.id]);
  }

  async function refreshAssembly() {
    try {
      await generationStore.refreshAssembly();
    } catch {
      generationStore.setAssembly(null);
    }
  }

  async function selectTake(item: GenerationSegment, takeId: string) {
    const updated = await generationStore.selectTake(item, takeId);
    const selectedTake = updated.takes.find((take) => take.id === takeId);
    if (selectedTake?.generation_run_id)
      selectedRunId = selectedTake.generation_run_id;
    await refreshAssembly();
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

  async function reconcileStartedRun(runId: string) {
    startedRunReconciliation?.abort();
    const controller = new AbortController();
    startedRunReconciliation = controller;
    try {
      if (!(await waitForStartedRunReconciliation(controller.signal))) return;
      if (selectedRunId !== runId || controller.signal.aborted) return;
      // A terminal event can arrive immediately after the first running
      // snapshot. Do one prompt authoritative refresh so status-filtered rows
      // return without waiting for the long-lived SSE safety interval.
      await load(true, true);
    } finally {
      if (startedRunReconciliation === controller)
        startedRunReconciliation = undefined;
    }
  }

  async function start(
    operation: 'generate' | 'regenerate' | 'rvc' = 'generate',
    ids: string[] = []
  ) {
    if (operation === 'rvc' && !rvcModel) {
      showRvc = true;
      mode = 'half';
      error = 'Choose an RVC model before converting audio.';
      return;
    }
    loading = true;
    error = '';
    try {
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
                source_run_id: selectedRunId || null
              }
            }
          : {};
      const started = await generationApi.start(
        sessionId,
        operation,
        ids,
        ids.length && operation !== 'rvc' ? selectedRunId || null : null,
        run_override
      );
      generationStore.upsertRun(started);
      selectedRunId = started.id;
      mode = 'half';
      await load();
      void reconcileStartedRun(started.id);
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      loading = false;
    }
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
        await generationApi.createAssembly(sessionId, selectedRunId || null)
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
        runs.map((item) => [item.id, Number(item.sequence_number || 0)])
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
    playback.toggle(selectedRow);
  }

  function playOnly(item: GenerationSegment) {
    return playback.playOnly(item);
  }

  function takeLabel(take: AudioTake) {
    const owner = runs.find((item) => item.id === take.generation_run_id);
    return owner
      ? `${owner.label} · ${String(take.kind || 'audio').toUpperCase()}`
      : `Legacy take · ${String(take.kind || 'audio').toUpperCase()}`;
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
    if (
      !selectedRun ||
      ['queued', 'running', 'pausing', 'cancel_requested'].includes(
        selectedRun.status
      )
    )
      return;
    if (
      !window.confirm(
        `Delete ${selectedRun.label} and all audio takes created by it?`
      )
    )
      return;
    loading = true;
    error = '';
    try {
      await generationApi.deleteRun(selectedRun.id);
      generationStore.removeRun(selectedRun.id);
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
    return String(
      readingTextMode === 'speech' && speech ? speech : item.text || ''
    )
      .replace(/\s+/g, ' ')
      .trim();
  }

  async function loadAllSegments() {
    if (searchLoading || payload.next_cursor == null) return;
    searchLoading = true;
    try {
      while (payload.next_cursor != null) {
        const previousLength = payload.items.length;
        await load(false);
        if (payload.items.length <= previousLength) break;
      }
    } finally {
      searchLoading = false;
    }
  }

  async function changeSelectedRun(event: Event) {
    selectedRunId = (event.currentTarget as HTMLSelectElement).value;
    selectedRow = '';
    selectedRows = [];
    selectionAnchor = '';
    stopPlayback();
    await load(true, false);
  }

  async function applySearchReplacements(updates: TextReplacement[]) {
    let completed = 0;
    error = '';
    try {
      for (const update of updates) {
        const item = payload.items[update.index];
        if (!item || update.text === item.text) continue;
        if (!update.text.trim())
          throw new Error(
            'Replacement would leave a generation segment blank. Remove that segment instead.'
          );
        const changedText = update.text.trim();
        await generationStore.updateSegment(item, { text: changedText });
        completed += 1;
      }
      if (completed) await refreshAssembly();
    } catch (caught) {
      const message = errorMessage(caught);
      error = completed
        ? `${completed} segment(s) were updated before search and replace stopped. ${message}`
        : message;
      await load(true, true);
      throw new Error(error, { cause: caught });
    }
  }

  async function navigateSearchMatch(match: TextSearchMatch) {
    if (viewMode !== 'segments') viewMode = 'segments';
    await tick();
    const field = document.querySelector<HTMLTextAreaElement>(
      `[data-generation-search-index="${match.itemIndex}"]`
    );
    field?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    field?.focus({ preventScroll: true });
    field?.setSelectionRange(match.start, match.end);
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

  function keyboard(event: KeyboardEvent) {
    const index = payload.items.findIndex((item) => item.id === selectedRow);
    if (event.key === 'ArrowDown') {
      const item =
        payload.items[
          Math.min(payload.items.length - 1, Math.max(0, index + 1))
        ];
      if (item) selectSegment(item, event);
      event.preventDefault();
    } else if (event.key === 'ArrowUp') {
      const item = payload.items[Math.max(0, index < 0 ? 0 : index - 1)];
      if (item) selectSegment(item, event);
      event.preventDefault();
    } else if (event.key === ' ' && index >= 0) {
      patchSegment(payload.items[index], {
        marked: !payload.items[index].marked
      });
      event.preventDefault();
    } else if (event.key === 'Delete' && index >= 0) {
      patchSegment(payload.items[index], {
        removed: !payload.items[index].removed
      });
      event.preventDefault();
    }
  }

  onMount(() => {
    loadRvc();
    loadSpeechOptions();
    const disconnect = generationStore.connect(
      () => ({ filter, selectedRunId }),
      applyLoadResult
    );
    return () => {
      disconnect();
      if (timer) window.clearTimeout(timer);
      startedRunReconciliation?.abort();
      stopPlayback();
    };
  });
  $effect(() => {
    void filter;
    untrack(() => load(true, false));
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

{#if payload.total > 0 || run}
  <aside
    class:full={mode === 'full'}
    class:half={mode === 'half'}
    class="generation-drawer fixed inset-x-3 bottom-3 z-50 overflow-hidden rounded-2xl md:left-[calc(var(--sidebar-offset,5rem)+.35rem)] md:right-[.35rem]"
  >
    <header
      class="generation-header flex flex-wrap items-center gap-3 border-b border-[var(--line)] px-4 py-3 lg:flex-nowrap"
    >
      <button
        onclick={() => (mode = mode === 'collapsed' ? 'half' : 'collapsed')}
        class="flex items-center gap-2 font-semibold"
      >
        {#if mode === 'collapsed'}<ChevronUp size={17} />{:else}<ChevronDown
            size={17}
          />{/if}
        Generation
      </button>
      <span
        class="muted min-w-0 text-xs lg:flex-1 lg:truncate"
        title={`${payload.total} segments · ${selectedRun?.label ?? 'No run selected'}${selectedAssembly ? ` · output ${selectedAssembly.status}` : ''}`}
        >{payload.total} segments · {selectedRun?.label ??
          'No run selected'}{#if selectedAssembly}
          · output {selectedAssembly.status}{/if}</span
      >
      {#if selectedRun?.usage?.commercial}<span class="cost-pill"
          >{selectedRun.usage.estimated ? 'Est.' : ''}
          {selectedRunCost}{selectedRun.usage.has_unpriced_usage
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
            : 'Play from the selected segment'}
          aria-label={playlistActive
            ? playlistPaused
              ? 'Resume playlist'
              : 'Pause playlist'
            : 'Play as playlist'}
        >
          {#if playlistActive && !playlistPaused}<Pause size={14} />{:else}<Play
              size={14}
            />{/if}
          {playlistActive
            ? playlistPaused
              ? 'Resume'
              : 'Pause'
            : 'Play as playlist'}
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
        <div class="view-switch" aria-label="Generation review view">
          <button
            onclick={() => (viewMode = 'segments')}
            class:active={viewMode === 'segments'}
            title="Segment review"
            ><ListMusic size={13} /><span class="view-label">Segments</span
            ></button
          >
          <button
            onclick={() => (viewMode = 'reading')}
            class:active={viewMode === 'reading'}
            title="Reading view"
            ><BookOpenText size={13} /><span class="view-label">Reading</span
            ></button
          >
        </div>
      {/if}
      <div class="ml-auto flex flex-wrap gap-2">
        {#if !run || ['completed', 'partial', 'failed', 'canceled'].includes(run.status)}
          <button onclick={() => start()} class="action primary"
            ><Play size={14} /> Start</button
          >
        {:else if run.status === 'paused'}
          <button onclick={() => action('resume')} class="action primary"
            ><Play size={14} /> Resume</button
          >
        {:else if ['queued', 'running'].includes(run.status)}
          <button
            onclick={() => action('pause')}
            class="action"
            title="Stop after the current segment"
            aria-label="Stop safely after the current segment"
            ><Pause size={14} /> Stop</button
          >
          <button onclick={() => action('cancel')} class="action text-red-500"
            ><Square size={14} /> Cancel</button
          >
        {/if}
        {#if mode !== 'collapsed'}
          <button
            onclick={() => (mode = mode === 'full' ? 'half' : 'full')}
            class="action icon-action"
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
      <div class="flex h-[calc(100%-3.8rem)] min-h-0 flex-col overflow-y-auto">
        <div
          class="flex flex-wrap items-center gap-2 border-b border-[var(--line)] p-3"
        >
          {#if runs.length}
            <label
              class="run-picker flex items-center gap-2 text-xs font-semibold"
              >Version
              <select
                value={selectedRunId}
                onchange={changeSelectedRun}
                class="mini max-w-[22rem]"
              >
                {#each runs as item}<option value={item.id}
                    >{item.label} · {item.status}</option
                  >{/each}
              </select>
            </label>
            <button
              onclick={deleteSelectedRun}
              disabled={loading ||
                !selectedRun ||
                ['queued', 'running', 'pausing', 'cancel_requested'].includes(
                  selectedRun.status
                )}
              class="action icon-action text-red-500"
              title="Delete the selected run and its generated takes"
              aria-label="Delete selected generation run"
              ><Trash2 size={14} /></button
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
          <button
            onkeydown={keyboard}
            class="action"
            title="Focus, then use arrows, Shift+arrows to select a range, Space to mark, and Delete to remove"
            >Keyboard navigation</button
          >
          <button
            onclick={() => start('regenerate', marked)}
            disabled={!marked.length}
            class="action"><RefreshCw size={14} /> Regenerate marked</button
          >
          <button
            onclick={assemble}
            disabled={loading ||
              selectedRun?.status !== 'completed' ||
              selectedAssembly?.status === 'queued' ||
              selectedAssembly?.status === 'running'}
            class="action primary"
            title={selectedRun?.status !== 'completed'
              ? 'Generate every remaining segment before assembly'
              : 'Assemble this version'}
          >
            <Sparkles size={14} />
            {selectedAssembly?.status === 'stale'
              ? 'Reassemble output'
              : 'Assemble output'}
          </button>
          <a href={`/sessions/${sessionId}/output`} class="action"
            >Output settings</a
          >
          <button onclick={() => (ttsServicesOpen = true)} class="action"
            >Speech services</button
          >
        </div>
        <div class="border-b border-[var(--line)] px-3 py-2">
          <SearchReplaceBar
            texts={editableTexts}
            onreplace={applySearchReplacements}
            onnavigate={navigateSearchMatch}
            onactivate={loadAllSegments}
            disabled={searchLoading || loading}
            label={searchScopeLabel}
          />{#if searchLoading}<p class="muted mt-1 px-1 text-[.65rem]">
              Loading all {searchScopeLabel} for search…
            </p>{/if}
        </div>

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

        {#if run?.status === 'failed'}
          <div
            class="border-b border-red-400/30 bg-red-500/10 px-4 py-2 text-xs text-red-600"
          >
            Generation failed: {run.error_message ||
              'Open Activity & logs for details, then retry.'}
          </div>
        {/if}

        <div class="border-b border-[var(--line)] px-3 py-2">
          <button onclick={() => (showRvc = !showRvc)} class="action"
            ><WandSparkles size={14} /> RVC speech-to-speech {showRvc
              ? 'settings ▲'
              : 'settings ▼'}</button
          >
          {#if showRvc}
            <div
              class="mt-2 flex flex-wrap items-end gap-2 rounded-xl bg-[var(--accent-soft)] p-3"
            >
              <label class="text-xs font-semibold"
                >Model
                <select bind:value={rvcModel} class="mini ml-2"
                  ><option value="">Choose a model</option
                  >{#each rvcModels as item}<option value={item}>{item}</option
                    >{/each}</select
                >
              </label>
              <label class="text-xs font-semibold"
                >Pitch <input
                  type="number"
                  min="-24"
                  max="24"
                  bind:value={rvcPitch}
                  class="mini ml-2 w-16"
                /></label
              >
              <label class="text-xs font-semibold"
                >Detector
                <select bind:value={rvcF0} class="mini ml-2"
                  ><option value="rmvpe">RMVPE</option><option value="harvest"
                    >Harvest</option
                  ><option value="crepe">CREPE</option><option value="pm"
                    >PM</option
                  ></select
                >
              </label>
              <label class="text-xs font-semibold"
                >Index rate <input
                  type="number"
                  min="0"
                  max="1"
                  step="0.05"
                  bind:value={rvcIndexRate}
                  class="mini ml-2 w-20"
                /></label
              >
              <button
                onclick={() => start('rvc', selectedSegmentIds)}
                disabled={!selectedSegmentIds.length || !rvcModel}
                class="action"
                >RVC selected ({selectedSegmentIds.length})</button
              >
              <button
                onclick={() => start('rvc', marked)}
                disabled={!marked.length || !rvcModel}
                class="action">RVC marked</button
              >
              <button
                onclick={() => start('rvc', [])}
                disabled={!rvcModel}
                class="action">RVC all</button
              >
              <a href="/rvc" class="action">Manage models</a>
            </div>
          {/if}
        </div>

        {#if error}<p class="p-3 text-sm text-red-500">{error}</p>{/if}

        <div class="min-h-[12rem] shrink-0 flex-1 overflow-auto">
          {#if viewMode === 'segments'}
            <GenerationSegmentTable
              items={payload.items}
              {selectedRows}
              {loading}
              {speechOptionsLoading}
              selectedTtsServiceName={selectedTtsService?.name}
              {selectedTtsModel}
              {inheritedVoice}
              {inheritedLanguage}
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
            />
          {:else}
            <GenerationReadingView
              blocks={readingBlocks}
              selectedRunLabel={selectedRun?.label}
              textMode={readingTextMode}
              loaded={payload.items.length}
              total={payload.total}
              {activePlayingId}
              {selectedRows}
              {loading}
              ontextmode={(value) => (readingTextMode = value)}
              onactivate={activateReadingSegment}
              onactivatekeyboard={activateReadingSegmentFromKeyboard}
              ontext={readingSegmentText}
              onhasaudio={(item) => Boolean(activeTake(item))}
              onplay={playOnly}
              onregenerate={(item) => start('regenerate', [item.id])}
              onpatch={patchSegment}
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

<style>
  .generation-drawer {
    height: 3.9rem;
    border: 1px solid var(--line);
    background: var(--paper-strong);
    box-shadow: 0 18px 55px color-mix(in srgb, var(--ink) 18%, transparent);
    transition: height 0.18s ease;
  }
  .generation-drawer.half {
    height: min(52vh, 38rem);
  }
  .generation-drawer.full {
    height: calc(100vh - 1.5rem);
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
  .view-switch {
    display: flex;
    border: 1px solid var(--line);
    border-radius: 0.6rem;
    background: var(--paper);
    padding: 0.15rem;
  }
  .view-switch button {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    border-radius: 0.45rem;
    padding: 0.3rem 0.5rem;
    font-size: 0.68rem;
    font-weight: 700;
    color: var(--muted);
  }
  .view-switch button.active {
    background: var(--accent-soft);
    color: var(--ink);
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
  @media (max-width: 1200px) {
    .view-label {
      display: none;
    }
  }
</style>
