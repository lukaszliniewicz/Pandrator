<script lang="ts">
  import { errorMessage } from './errors';
  import {
    ChevronLeft,
    ChevronDown,
    ChevronRight,
    CircleHelp,
    Columns3,
    Filter,
    LoaderCircle,
    Merge,
    Play,
    Plus,
    RefreshCw,
    Save,
    Scissors,
    Trash2,
    Undo2,
    X
  } from '@lucide/svelte';
  import { artifactApi, jobApi, sessionApi } from './domain-api';
  import type {
    SubtitleComparisonRow as Row,
    SubtitleReviewCatalog as Catalog,
    SubtitleReviewCatalogItem as CatalogItem,
    SubtitleReviewColumn as ReviewColumn,
    SubtitleReviewPayload as Payload,
    SubtitleEvidenceCandidate,
    SubtitleEvidenceRecord,
    SubtitleSegment as Segment,
    SubtitleSplitInspection,
    SpokenPassage
  } from './api-models';
  import { onDestroy, onMount, tick, untrack } from 'svelte';
  import { beforeNavigate, goto } from '$app/navigation';
  import {
    cloneReview,
    forgetReviewDraft,
    readReviewDraft,
    rememberReviewDraft,
    reviewContent,
    reviewDraftKey,
    sameReviewSource
  } from './subtitle-review-drafts';
  import GuidedTour from './GuidedTour.svelte';
  import AudioPlayer from './AudioPlayer.svelte';
  import SubtitleEvidencePanel from './SubtitleEvidencePanel.svelte';
  import TextDiff from './TextDiff.svelte';
  import SearchReplaceBar from './SearchReplaceBar.svelte';
  import type { TextReplacement, TextSearchMatch } from './search-replace';
  import { modalFocus } from './modal-focus';
  import WorkspaceMaximizeButton from './WorkspaceMaximizeButton.svelte';

  const PAGE_SIZE = 50;

  let {
    sessionId,
    primaryArtifactId,
    onclose,
    onsaved
  }: {
    sessionId: string;
    primaryArtifactId: string;
    onclose: () => void;
    onsaved: () => void;
  } = $props();
  let payload = $state<Payload | null>(null);
  let catalog = $state<Catalog | null>(null);
  let error = $state('');
  let loading = $state(true);
  let changedOnly = $state(false);
  let needsReviewOnly = $state(false);
  let diffView = $state(false);
  let reviewPrimaryArtifactId = $state('');
  let editArtifactId = $state('');
  let comparisonChoice = $state('');
  let comparisonLoading = $state(false);
  let saving = $state(false);
  let baseline = $state('');
  let pristinePayload = $state<Payload | null>(null);
  let undoHistory = $state<Payload[]>([]);
  let pendingLeave = $state<(() => void) | null>(null);
  let staleDraft = $state<Payload | null>(null);
  let permitNavigation = false;
  let pendingSave: { signature: string; key: string } | undefined;
  const dirty = $derived(
    Boolean(payload && baseline && reviewContent(payload) !== baseline)
  );
  const draftKey = $derived(
    reviewDraftKey(sessionId, reviewPrimaryArtifactId || primaryArtifactId)
  );

  function checkpoint() {
    if (!payload || loading || saving) return;
    const snapshot = cloneReview(payload);
    if (reviewContent(undoHistory.at(-1) ?? null) === reviewContent(snapshot))
      return;
    undoHistory = [...undoHistory.slice(-19), snapshot];
  }

  function undo() {
    error = '';
    const previous = undoHistory.at(-1);
    if (!previous || saving) return;
    payload = cloneReview(previous);
    undoHistory = undoHistory.slice(0, -1);
    splitSegment = null;
    splitInspection = null;
  }

  function requestLeave(action = onclose) {
    if (saving) return;
    if (dirty) pendingLeave = action;
    else action();
  }

  function discardAndLeave() {
    const action = pendingLeave;
    forgetReviewDraft(draftKey);
    if (pristinePayload) payload = cloneReview(pristinePayload);
    baseline = reviewContent(payload);
    undoHistory = [];
    pendingLeave = null;
    action?.();
  }

  async function saveAndLeave() {
    const action = pendingLeave;
    if (await save()) {
      pendingLeave = null;
      action?.();
    }
  }

  beforeNavigate((navigation) => {
    if (!dirty || permitNavigation) return;
    navigation.cancel();
    if (!navigation.willUnload && navigation.to?.url) {
      const target = navigation.to.url;
      requestLeave(() => {
        permitNavigation = true;
        void goto(target);
      });
    }
  });

  $effect(() => {
    if (!payload || loading || staleDraft) return;
    if (dirty) rememberReviewDraft(draftKey, payload, baseline, editArtifactId);
    else forgetReviewDraft(draftKey);
  });
  let audioPreview = $state<HTMLAudioElement>();
  let videoPreview = $state<HTMLVideoElement>();
  let captionsUrl = $state('');
  let splitSegment = $state<Segment | null>(null);
  let splitInspection = $state<SubtitleSplitInspection | null>(null);
  let splitLoading = $state(false);
  let selectedSplitBoundary = $state('');
  let splitLeft = $state('');
  let splitRight = $state('');
  let splitStartsTurn = $state(false);
  let splitError = $state('');
  let reviewTime = $state(0);
  function playbackElement() {
    return sourceIsVideo && previewMode !== 'audio'
      ? videoPreview
      : audioPreview;
  }

  let sourceAudioUrl = $state('');
  let previewMode = $state<'original' | 'video' | 'audio'>('original');
  let videoPlaybackError = $state('');
  let sourceAudioPreparing = $state(false);
  let sourceAudioError = $state('');
  let sourcePreviewJobId = $state('');
  let cuePlaybackError = $state('');
  let sourceAudioController: AbortController | undefined;
  let cuePreviewFrame: number | null = null;
  let cuePreviewEnd = 0;
  let tourOpen = $state(false);
  let maximized = $state(false);
  let pageIndex = $state(0);
  let rowsViewport = $state<HTMLDivElement>();
  let evidenceRecords = $state<SubtitleEvidenceRecord[]>([]);
  let evidenceArtifactId = $state('');
  let evidencePanelSegmentId = $state('');
  const tourSteps = [
    {
      section: 'Review',
      title: 'Lineage keeps changes together',
      body: 'Rows group transcription, correction, and translation through split/merge lineage, with temporal overlap for legacy artifacts.'
    },
    {
      section: 'Review',
      title: 'Edit the selected revision',
      body: 'Change text and boundaries, split a segment, or merge it with the next while comparison columns remain visible.'
    },
    {
      section: 'Review',
      title: 'Saving creates history',
      body: 'A save creates a reviewed immutable revision and invalidates only affected descendants.'
    }
  ];
  const columns = $derived(payload?.columns ?? []);
  const editColumn = $derived(
    columns.find((column) => column.artifact_id === editArtifactId)
  );
  const editMode = $derived(payload?.edit_mode ?? 'display');
  const passages = $derived(editColumn?.logical_passages ?? []);
  const pagedPassages = $derived(
    passages.slice(pageIndex * PAGE_SIZE, (pageIndex + 1) * PAGE_SIZE)
  );
  function changeEditMode(mode: 'display' | 'passages') {
    if (!payload || editMode === mode) return;
    requestLeave(() => {
      if (payload) payload.edit_mode = mode;
      pageIndex = 0;
      diffView = false;
    });
  }
  function setPassageDeleted(passage: SpokenPassage, deleted: boolean) {
    checkpoint();
    passage.deleted = deleted;
  }
  const sourceIsVideo = $derived(
    editColumn?.source_media_mime_type?.startsWith('video/') ||
      ['mp4', 'webm', 'mkv', 'mov'].includes(
        editColumn?.source_media_kind ?? ''
      )
  );
  const editStage = $derived(editColumn?.stage ?? '');
  const sourceMediaArtifactId = $derived(
    editColumn?.source_media_artifact_id ?? ''
  );
  const sourceMediaError = $derived(editColumn?.source_media_error ?? '');
  const selectedArtifactIds = $derived(
    columns.map((column) => column.artifact_id)
  );
  const comparisonOptions = $derived(
    (catalog?.items ?? []).filter(
      (item) => !selectedArtifactIds.includes(item.artifact_id)
    )
  );
  const comparisonStages = [
    'transcription',
    'correction',
    'translation',
    'tts_optimization'
  ] as const;
  const visibleRows = $derived(
    (payload?.rows ?? []).filter(
      (row) =>
        (!changedOnly || row.changed) &&
        (!needsReviewOnly || rowNeedsReview(row))
    )
  );
  const needsReviewCount = $derived(
    (payload?.rows ?? []).filter((row) => rowNeedsReview(row)).length
  );
  const editableTexts = $derived(
    editColumn?.segments.map((segment) => segment.text) ?? []
  );
  const pageCount = $derived(
    Math.max(
      1,
      Math.ceil(
        (editMode === 'passages' ? passages.length : visibleRows.length) /
          PAGE_SIZE
      )
    )
  );
  const pageStart = $derived(pageIndex * PAGE_SIZE);
  const pagedRows = $derived(
    visibleRows.slice(pageStart, pageStart + PAGE_SIZE)
  );

  $effect(() => {
    if (pageIndex >= pageCount) pageIndex = pageCount - 1;
  });

  async function changePage(nextPage: number) {
    pageIndex = Math.max(0, Math.min(nextPage, pageCount - 1));
    await tick();
    rowsViewport?.scrollTo({ top: 0 });
  }

  function toggleChangedOnly() {
    changedOnly = !changedOnly;
    pageIndex = 0;
  }

  function toggleNeedsReviewOnly() {
    needsReviewOnly = !needsReviewOnly;
    pageIndex = 0;
  }

  function evidenceId(record: SubtitleEvidenceRecord) {
    return record.evidence_id || record.id;
  }

  function evidenceFor(segment: Segment) {
    return evidenceRecords.filter(
      (record) =>
        record.source_artifact_id === editArtifactId &&
        record.cue_id === segment.ordinal + 1
    );
  }

  function segmentNeedsReview(segment: Segment) {
    if (segment.review_state === 'uncertain') return true;
    const latest = evidenceFor(segment)
      .slice()
      .sort((left, right) =>
        right.created_at.localeCompare(left.created_at)
      )[0];
    return Boolean(
      latest &&
      ['queued', 'running', 'completed', 'failed', 'uncertain'].includes(
        latest.status
      )
    );
  }

  function rowNeedsReview(row: Row) {
    return (row.cells[editArtifactId] ?? []).some((segment) =>
      segmentNeedsReview(canonicalSegment(segment))
    );
  }

  async function loadEvidence(artifactId: string) {
    evidenceArtifactId = artifactId;
    try {
      const result = await sessionApi.subtitleEvidence(sessionId, artifactId);
      if (evidenceArtifactId === artifactId) evidenceRecords = result.items;
    } catch (caught) {
      if (evidenceArtifactId === artifactId) error = errorMessage(caught);
    }
  }

  function updateEvidence(record: SubtitleEvidenceRecord) {
    const id = evidenceId(record);
    const index = evidenceRecords.findIndex(
      (candidate) => evidenceId(candidate) === id
    );
    if (index >= 0) evidenceRecords[index] = record;
    else evidenceRecords = [record, ...evidenceRecords];
  }

  function useEvidenceCandidate(
    segment: Segment,
    candidate: SubtitleEvidenceCandidate,
    requestId: string
  ) {
    if (!canEdit(segment, 'text')) {
      error =
        segment.edit_capabilities?.reason ||
        'Edit the spoken passage before applying this evidence.';
      return;
    }
    checkpoint();
    if (candidate.text?.trim()) segment.text = candidate.text.trim();
    segment.review_state = 'clear';
    segment.review_note = '';
    segment.evidence_ids = Array.from(
      new Set([...(segment.evidence_ids ?? []), requestId])
    );
    evidencePanelSegmentId = '';
  }

  function markSegmentUncertain(
    segment: Segment,
    note: string,
    requestId?: string
  ) {
    checkpoint();
    segment.review_state = 'uncertain';
    segment.review_note = note;
    if (requestId)
      segment.evidence_ids = Array.from(
        new Set([...(segment.evidence_ids ?? []), requestId])
      );
  }

  function clearSegmentUncertainty(segment: Segment) {
    checkpoint();
    segment.review_state = 'clear';
    segment.review_note = '';
  }

  function catalogRecord(artifactId: string) {
    return catalog?.items.find((item) => item.artifact_id === artifactId);
  }

  function columnLabel(value: ReviewColumn | CatalogItem) {
    const record =
      'artifact_id' in value ? catalogRecord(value.artifact_id) : undefined;
    const stage = value.stage.replaceAll('_', ' ');
    const version = record?.version
      ? `v${record.version}`
      : `r${value.revision}`;
    const language = value.language ? ` · ${value.language}` : '';
    return `${stage} ${version}${language}`;
  }

  function comparisonGroupLabel(stage: (typeof comparisonStages)[number]) {
    if (stage === 'tts_optimization') return 'TTS optimizations';
    return `${stage[0].toUpperCase()}${stage.slice(1)}s`;
  }

  function stageText(row: Row, artifactId: string) {
    return (row.cells[artifactId] ?? [])
      .map(
        (segment) =>
          (artifactId === editArtifactId ? canonicalSegment(segment) : segment)
            .text
      )
      .join('\n');
  }

  function speakerLabel(value?: string | null) {
    const raw = String(value ?? '')
      .trim()
      .replace(/^[[(]/, '')
      .replace(/[\]):]+$/, '');
    if (!raw) return '';
    const prefixed = raw.match(/^speaker[\s_-]*(.+)$/i);
    if (prefixed) return `Speaker ${prefixed[1].replaceAll('_', ' ')}`;
    const moss = raw.match(/^s(\d+)$/i);
    if (moss) return `Speaker ${moss[1]}`;
    if (/^\d+$/.test(raw)) return `Speaker ${raw}`;
    return raw.replaceAll('_', ' ');
  }

  function sameSegment(left: Segment, right: Segment) {
    return (
      left === right ||
      Boolean(left.id && right.id && left.id === right.id) ||
      Boolean(
        left.draft_id && right.draft_id && left.draft_id === right.draft_id
      )
    );
  }

  function canonicalSegment(segment: Segment) {
    const records = editColumn?.segments;
    return (
      records?.find((candidate) => sameSegment(candidate, segment)) ?? segment
    );
  }

  function stageIndex(segment: Segment) {
    const records = editColumn?.segments;
    return (
      records?.findIndex((candidate) => sameSegment(candidate, segment)) ?? -1
    );
  }

  function replaceInRows(segment: Segment, replacements: Segment[]) {
    for (const row of payload?.rows ?? []) {
      const records = row.cells[editArtifactId];
      const index =
        records?.findIndex((candidate) => sameSegment(candidate, segment)) ??
        -1;
      if (records && index >= 0) records.splice(index, 1, ...replacements);
    }
  }

  function nextSegment(segment: Segment) {
    const records = editColumn?.segments;
    const index = stageIndex(segment);
    return records && index >= 0 ? records[index + 1] : undefined;
  }

  function canEdit(
    segment: Segment,
    action: keyof NonNullable<Segment['edit_capabilities']>
  ) {
    return segment.edit_capabilities?.[action] === true;
  }

  function canMergeNext(segment: Segment) {
    const next = nextSegment(segment);
    return (
      Boolean(next) &&
      canEdit(segment, 'merge') &&
      Boolean(next && canEdit(next, 'merge')) &&
      !segment.origin_segment_id &&
      !next?.origin_segment_id &&
      !next?.starts_new_turn &&
      (segment.turn_id ?? null) === (next?.turn_id ?? null) &&
      speakerLabel(segment.speaker).toLocaleLowerCase() ===
        speakerLabel(next?.speaker).toLocaleLowerCase()
    );
  }

  function mergeTitle(segment: Segment) {
    if (!nextSegment(segment)) return 'There is no following cue';
    if (segment.origin_segment_id || nextSegment(segment)?.origin_segment_id)
      return 'Save the split before merging again';
    return canMergeNext(segment)
      ? 'Merge with the next cue'
      : 'Separate utterances or speakers cannot be merged';
  }

  function previousColumnText(row: Row) {
    const column = previousColumn(row);
    return column ? stageText(row, column.artifact_id) : '';
  }

  function previousColumn(row: Row): ReviewColumn | null {
    const position = columns.findIndex(
      (column) => column.artifact_id === editArtifactId
    );
    for (let index = position - 1; index >= 0; index -= 1) {
      const column = columns[index];
      const text = stageText(row, column.artifact_id);
      if (text) return column;
    }
    return null;
  }

  async function load(
    artifactIds = [reviewPrimaryArtifactId],
    refreshCatalog = false,
    mode?: 'display' | 'passages'
  ) {
    const remembered = payload ? undefined : readReviewDraft(draftKey);
    loading = true;
    splitSegment = null;
    splitInspection = null;
    error = '';
    try {
      const [nextPayload, nextCatalog] = await Promise.all([
        sessionApi.subtitleReview(sessionId, artifactIds),
        refreshCatalog || !catalog
          ? sessionApi.subtitleCatalog(sessionId)
          : Promise.resolve(catalog)
      ]);
      if (mode) nextPayload.edit_mode = mode;
      pristinePayload = cloneReview(nextPayload);
      baseline = reviewContent(nextPayload);
      if (remembered && sameReviewSource(remembered.payload, nextPayload)) {
        baseline = remembered.baseline;
        payload = cloneReview(remembered.payload);
        editArtifactId = remembered.editableArtifactId;
      } else {
        if (remembered) staleDraft = cloneReview(remembered.payload);
        payload = cloneReview(nextPayload);
      }
      catalog = nextCatalog;
      pageIndex = 0;
      if (
        !nextPayload.columns.some(
          (column) => column.artifact_id === editArtifactId
        )
      )
        editArtifactId = nextPayload.primary_artifact_id;
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      loading = false;
    }
  }

  async function addComparison() {
    if (!comparisonChoice || selectedArtifactIds.length >= 4) return;
    if (dirty) {
      requestLeave(() => void addComparison());
      return;
    }
    comparisonLoading = true;
    try {
      await load([...selectedArtifactIds, comparisonChoice]);
      comparisonChoice = '';
    } finally {
      comparisonLoading = false;
    }
  }

  async function removeComparison(artifactId: string) {
    if (artifactId === reviewPrimaryArtifactId) return;
    if (dirty) {
      requestLeave(() => void removeComparison(artifactId));
      return;
    }
    const remaining = selectedArtifactIds.filter((item) => item !== artifactId);
    if (editArtifactId === artifactId) editArtifactId = reviewPrimaryArtifactId;
    await load(remaining);
  }

  function changeEditableArtifact(artifactId: string) {
    if (artifactId === editArtifactId) return;
    requestLeave(() => {
      editArtifactId = artifactId;
    });
  }

  async function inspectSplit(segment: Segment, offset = 0) {
    const column = editColumn;
    if (!column || !segment.id) return;
    splitSegment = segment;
    splitInspection = null;
    splitLoading = true;
    splitError = '';
    const selectedArtifact = column.artifact_id;
    splitStartsTurn = false;
    try {
      const inspection = await sessionApi.subtitleSplitBoundaries(sessionId, {
        source_artifact_id: column.artifact_id,
        segment_id: segment.id,
        expected_revision: column.revision,
        offset
      });
      if (editArtifactId !== selectedArtifact || splitSegment !== segment)
        return;
      splitInspection = inspection;
      selectedSplitBoundary = '';
      if (splitInspection.status === 'unavailable')
        splitError =
          splitInspection.reason ?? 'No verified split boundary is available.';
    } catch (caught) {
      splitError = errorMessage(caught);
    } finally {
      splitLoading = false;
    }
  }

  function chooseSplitBoundary() {
    const anchor = splitInspection?.boundaries.find(
      (value) => value.id === selectedSplitBoundary
    );
    if (!anchor || !splitSegment) return;
    if (Number.isInteger(anchor.text_offset_utf16)) {
      const offset = anchor.text_offset_utf16 as number;
      splitLeft = splitSegment.text.slice(0, offset).trim();
      splitRight = splitSegment.text.slice(offset).trim();
      return;
    }
    const words = splitSegment.text.trim().split(/\s+/);
    splitLeft = words.slice(0, anchor.after_word).join(' ');
    splitRight = words.slice(anchor.after_word).join(' ');
  }

  function applySplit() {
    const segment = splitSegment;
    const anchor = splitInspection?.boundaries.find(
      (value) => value.id === selectedSplitBoundary
    );
    const records = editColumn?.segments;
    if (
      !segment?.id ||
      !anchor ||
      !records ||
      !splitLeft.trim() ||
      !splitRight.trim()
    )
      return;
    const index = stageIndex(segment);
    if (index < 0) return;
    checkpoint();
    const shared = {
      ...segment,
      id: undefined,
      origin_segment_id: segment.id,
      split_boundary_id: anchor.id
    };
    const first = {
      ...shared,
      draft_id: crypto.randomUUID(),
      text: splitLeft.trim(),
      end_ms: anchor.left_end_ms
    };
    const second = {
      ...shared,
      draft_id: crypto.randomUUID(),
      text: splitRight.trim(),
      start_ms: anchor.right_start_ms,
      starts_new_turn: splitStartsTurn
    };
    records.splice(index, 1, first, second);
    replaceInRows(segment, [first, second]);
    splitSegment = null;
    splitInspection = null;
  }

  function mergeNext(segment: Segment) {
    const records = editColumn?.segments;
    if (!records) return;
    const index = stageIndex(segment);
    if (index < 0) return;
    const next = records[index + 1];
    if (next && canMergeNext(segment)) {
      checkpoint();
      const merged = {
        ...segment,
        id: undefined,
        draft_id: crypto.randomUUID(),
        end_ms: next.end_ms,
        text: `${segment.text} ${next.text}`.trim(),
        source_passage_ids: Array.from(
          new Set([
            ...(segment.source_passage_ids ?? []),
            ...(next.source_passage_ids ?? [])
          ])
        ),
        origin_segment_id: undefined,
        split_boundary_id: undefined,
        uncertain_source_cue_ids: Array.from(
          new Set([
            ...(segment.uncertain_source_cue_ids ?? []),
            ...(next.uncertain_source_cue_ids ?? [])
          ])
        ),
        review_state:
          segment.review_state === 'uncertain' ||
          next.review_state === 'uncertain'
            ? ('uncertain' as const)
            : ('clear' as const),
        review_note: [segment.review_note, next.review_note]
          .filter(Boolean)
          .join(' '),
        evidence_ids: Array.from(
          new Set([
            ...(segment.evidence_ids ?? []),
            ...(next.evidence_ids ?? [])
          ])
        )
      };
      records.splice(index, 2, merged);
      replaceInRows(segment, [merged]);
      replaceInRows(next, []);
    }
  }

  function removeSegment(segment: Segment) {
    if (!canEdit(segment, 'delete')) return;
    const records = editColumn?.segments;
    const index = stageIndex(segment);
    if (records && index >= 0) {
      checkpoint();
      records.splice(index, 1);
      replaceInRows(segment, []);
    }
  }
  function searchIndex(segment: Segment) {
    return stageIndex(segment);
  }

  function applySearchReplacements(updates: TextReplacement[]) {
    const records = editColumn?.segments;
    if (!records) return;
    checkpoint();
    for (const update of updates) {
      if (records[update.index] && canEdit(records[update.index], 'text'))
        records[update.index].text = update.text;
      else
        error =
          'Some matching cues combine spoken passages. Edit those passages separately; their text was kept.';
    }
  }

  async function navigateSearchMatch(match: TextSearchMatch) {
    if (diffView) diffView = false;
    if (changedOnly) changedOnly = false;
    const target = editColumn?.segments[match.itemIndex];
    const targetRow = target
      ? (payload?.rows ?? []).findIndex((row) =>
          (row.cells[editArtifactId] ?? []).some((segment) =>
            sameSegment(segment, target)
          )
        )
      : -1;
    if (targetRow >= 0) pageIndex = Math.floor(targetRow / PAGE_SIZE);
    await tick();
    const field = document.querySelector<HTMLTextAreaElement>(
      `[data-subtitle-search-index="${match.itemIndex}"]`
    );
    field?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    field?.focus({ preventScroll: true });
    field?.setSelectionRange(match.start, match.end);
  }
  function stopCuePreview(pause = false) {
    if (cuePreviewFrame !== null) window.cancelAnimationFrame(cuePreviewFrame);
    cuePreviewFrame = null;
    if (pause) playbackElement()?.pause();
  }

  function watchCueBoundary() {
    const player = playbackElement();
    if (!player || player.paused) {
      cuePreviewFrame = null;
      return;
    }
    if (player.currentTime >= cuePreviewEnd - 0.01) {
      player.pause();
      player.currentTime = cuePreviewEnd;
      cuePreviewFrame = null;
      return;
    }
    cuePreviewFrame = window.requestAnimationFrame(watchCueBoundary);
  }

  async function previewSegment(segment: Segment) {
    const player = playbackElement();
    if (!player) {
      cuePlaybackError = 'Source audio is not ready for cue playback yet.';
      return;
    }
    cuePlaybackError = '';
    stopCuePreview(true);
    try {
      player.currentTime = segment.start_ms / 1000;
      cuePreviewEnd = segment.end_ms / 1000;
      await player.play();
      cuePreviewFrame = window.requestAnimationFrame(watchCueBoundary);
    } catch (caught) {
      cuePreviewFrame = null;
      cuePlaybackError =
        errorMessage(caught) || 'This cue could not be played.';
    }
  }

  function waitForPreviewPoll(signal: AbortSignal) {
    return new Promise<void>((resolve, reject) => {
      const aborted = () => {
        window.clearTimeout(timer);
        reject(new DOMException('Aborted', 'AbortError'));
      };
      const timer = window.setTimeout(() => {
        signal.removeEventListener('abort', aborted);
        resolve();
      }, 750);
      signal.addEventListener('abort', aborted, { once: true });
    });
  }

  async function cancelSourcePreview() {
    if (!sourcePreviewJobId) return;
    try {
      await jobApi.cancel(sourcePreviewJobId);
    } catch (caught) {
      sourceAudioError = errorMessage(caught);
    }
  }

  async function prepareSourceAudio(
    artifactId: string,
    mode: 'original' | 'video' | 'audio' = 'original'
  ) {
    if (!artifactId) return;
    sourceAudioController?.abort();
    stopCuePreview(true);
    sourceAudioUrl = '';
    sourcePreviewJobId = '';
    sourceAudioPreparing = false;
    previewMode = mode;
    videoPlaybackError = '';
    sourceAudioError = '';
    if (sourceIsVideo && mode === 'original') {
      sourceAudioUrl = `/api/v1/artifacts/${encodeURIComponent(artifactId)}/content`;
      return;
    }
    previewMode = mode === 'video' ? 'video' : 'audio';
    const requestPreview =
      mode === 'video' ? artifactApi.videoPreview : artifactApi.audioPreview;
    const controller = new AbortController();
    sourceAudioController = controller;
    sourceAudioPreparing = true;
    sourceAudioError = '';
    cuePlaybackError = '';
    try {
      let preparation = await requestPreview(artifactId, controller.signal);
      if (controller.signal.aborted) return;
      if (preparation.status === 'ready' && preparation.content_url) {
        sourceAudioUrl = preparation.content_url;
        return;
      }
      const jobId = preparation.job_id;
      if (!jobId) throw new Error('The media preview was not queued.');
      sourcePreviewJobId = jobId;
      for (let attempt = 0; attempt < 2440; attempt += 1) {
        await waitForPreviewPoll(controller.signal);
        const job = await jobApi.get(jobId, controller.signal);
        if (controller.signal.aborted) return;
        if (job.status === 'succeeded') {
          preparation = await requestPreview(artifactId, controller.signal);
          if (controller.signal.aborted) return;
          if (preparation.status === 'ready' && preparation.content_url) {
            sourceAudioUrl = preparation.content_url;
            return;
          }
          throw new Error('The prepared media preview could not be found.');
        }
        if (['failed', 'interrupted', 'canceled'].includes(job.status)) {
          throw new Error(
            job.status === 'canceled'
              ? 'Preview preparation canceled. You can retry.'
              : job.error_message || 'The media preview could not be prepared.'
          );
        }
      }
      throw new Error(
        'The media preview is still being prepared. Retry to check its progress.'
      );
    } catch (caught) {
      if (!controller.signal.aborted) sourceAudioError = errorMessage(caught);
    } finally {
      if (sourceAudioController === controller) {
        sourceAudioController = undefined;
        sourceAudioPreparing = false;
      }
    }
  }

  onDestroy(() => {
    sourceAudioController?.abort();
    stopCuePreview(true);
  });

  async function save() {
    const column = editColumn;
    if (!column) return false;
    const records =
      editMode === 'passages'
        ? (column.logical_passages ?? []).filter((item) => !item.deleted)
        : column.segments;
    if (!records.length) {
      error =
        'Keep at least one spoken passage or cue. Restore a deleted item or use Undo.';
      return false;
    }
    for (const item of records) {
      const label =
        editMode === 'passages'
          ? `Spoken passage ${item.id}`
          : `Cue ${item.id || ('ordinal' in item ? item.ordinal + 1 : '')}`;
      if (
        !Number.isInteger(item.start_ms) ||
        !Number.isInteger(item.end_ms) ||
        item.start_ms < 0 ||
        item.end_ms <= item.start_ms
      ) {
        error = `${label}: enter a nonnegative start and an end after the start. Your draft is retained.`;
        return false;
      }
      if (!item.text.trim()) {
        error = `${label}: enter text, or delete this item explicitly. Your draft is retained.`;
        return false;
      }
    }
    const signature = JSON.stringify([
      sessionId,
      column.stage,
      editMode,
      column.artifact_id,
      column.revision,
      column.source_content_hash,
      column.composition_hash,
      reviewContent(payload)
    ]);
    if (pendingSave?.signature !== signature)
      pendingSave = { signature, key: crypto.randomUUID() };
    const saveKey = pendingSave.key;
    saving = true;
    error = '';
    try {
      const mode = editMode;
      const result =
        mode === 'passages'
          ? await sessionApi.saveSubtitlePassageReview(
              sessionId,
              column.stage,
              {
                source_artifact_id: column.artifact_id,
                expected_source_hash: column.source_content_hash || '',
                expected_revision: column.revision,
                expected_composition_hash: column.composition_hash || '',
                passages: (column.logical_passages ?? []).map((item) => ({
                  id: item.id,
                  text: item.text,
                  speaker: item.speaker || '',
                  start_ms: item.start_ms,
                  end_ms: item.end_ms,
                  starts_new_turn: item.starts_new_turn ?? false,
                  deleted: item.deleted ?? false,
                  review_state:
                    item.review_state === 'uncertain' ? 'uncertain' : 'clear',
                  review_note: item.review_note || ''
                }))
              },
              saveKey
            )
          : await sessionApi.saveSubtitleReview(
              sessionId,
              column.stage,
              {
                source_artifact_id: column.artifact_id,
                expected_revision: column.revision,
                expected_source_hash: column.source_content_hash,
                segments: column.segments.map((item) => ({
                  id: item.id,
                  turn_id: item.turn_id,
                  source_passage_ids: item.source_passage_ids ?? [],
                  starts_new_turn: item.starts_new_turn ?? false,
                  origin_segment_id: item.origin_segment_id,
                  split_boundary_id: item.split_boundary_id,
                  start_ms: item.start_ms,
                  end_ms: item.end_ms,
                  text: item.text,
                  speaker: item.speaker,
                  review_state: item.review_state ?? 'clear',
                  review_note: item.review_note ?? '',
                  evidence_ids: item.evidence_ids ?? [],
                  uncertain_source_cue_ids: item.uncertain_source_cue_ids ?? []
                }))
              },
              saveKey
            );
      const previousId = column.artifact_id;
      const nextIds = selectedArtifactIds.map((artifactId) =>
        artifactId === previousId ? result.artifact_id : artifactId
      );
      if (reviewPrimaryArtifactId === previousId)
        reviewPrimaryArtifactId = result.artifact_id;
      editArtifactId = result.artifact_id;
      forgetReviewDraft(reviewDraftKey(sessionId, previousId));
      payload = null;
      baseline = '';
      undoHistory = [];
      staleDraft = null;
      await load(nextIds, true, mode);
      pendingSave = undefined;
      onsaved();
      return true;
    } catch (caught) {
      error = errorMessage(caught);
      return false;
    } finally {
      saving = false;
    }
  }
  onMount(() => {
    reviewPrimaryArtifactId = primaryArtifactId;
    editArtifactId = primaryArtifactId;
    const remembered = readReviewDraft(
      reviewDraftKey(sessionId, primaryArtifactId)
    );
    void load(
      remembered?.payload.columns.map((column) => column.artifact_id) ?? [
        primaryArtifactId
      ],
      true
    );
  });

  $effect(() => {
    const artifactId = sourceMediaArtifactId;
    void sourceIsVideo;
    untrack(() => {
      sourceAudioController?.abort();
      stopCuePreview(true);
      sourceAudioUrl = '';
      sourceAudioError = '';
      cuePlaybackError = '';
      sourceAudioPreparing = false;
      if (artifactId) void prepareSourceAudio(artifactId);
    });
  });

  $effect(() => {
    const artifactId = editArtifactId;
    if (artifactId && artifactId !== evidenceArtifactId)
      void loadEvidence(artifactId);
  });
  function vttTime(ms: number) {
    const value = Math.max(0, Math.round(ms));
    return `${String(Math.floor(value / 3600000)).padStart(2, '0')}:${String(Math.floor(value / 60000) % 60).padStart(2, '0')}:${String(Math.floor(value / 1000) % 60).padStart(2, '0')}.${String(value % 1000).padStart(3, '0')}`;
  }
  $effect(() => {
    const cues =
      editMode === 'passages'
        ? passages.filter((item) => !item.deleted)
        : (editColumn?.segments ?? []);
    const vtt =
      'WEBVTT\n\n' +
      cues
        .map(
          (cue, index) =>
            `${index + 1}\n${vttTime(cue.start_ms)} --> ${vttTime(cue.end_ms)}\n${cue.text.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')}\n`
        )
        .join('\n');
    const url = URL.createObjectURL(new Blob([vtt], { type: 'text/vtt' }));
    captionsUrl = url;
    return () => URL.revokeObjectURL(url);
  });
</script>

<div
  class:maximized
  class="review-overlay fixed inset-0 z-50 bg-black/45 p-3 backdrop-blur-sm sm:p-6"
  role="presentation"
>
  <div
    use:modalFocus={{ onclose: () => requestLeave() }}
    class="surface mx-auto flex h-full max-w-[96rem] flex-col overflow-hidden rounded-[1.5rem]"
    role="dialog"
    aria-modal="true"
    aria-labelledby="review-title"
  >
    <header
      class="flex flex-wrap items-center justify-between gap-4 border-b border-[var(--line)] px-5 py-4 sm:px-7"
    >
      <div>
        <h2
          id="review-title"
          class="mt-1 flex items-center gap-2 text-xl font-semibold"
        >
          <Columns3 size={20} /> Compare and refine
        </h2>
      </div>
      <div class="flex min-w-0 flex-wrap items-center justify-end gap-2">
        <span class="muted text-xs" role="status"
          >{dirty ? 'Unsaved changes' : 'Saved revision'}</span
        >
        <button
          onclick={undo}
          disabled={!undoHistory.length || saving}
          class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-3 py-2 text-sm disabled:opacity-40"
          ><Undo2 size={16} /> Undo</button
        >
        <button
          onclick={() => (tourOpen = true)}
          class="rounded-xl border border-[var(--line)] px-3 py-2 text-sm font-semibold"
          >Review tour</button
        >
        <button
          onclick={save}
          disabled={saving || !editColumn}
          class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2 text-sm font-semibold text-white disabled:opacity-40"
          ><Save size={16} />
          {saving ? 'Saving…' : 'Save revision'}</button
        >
        <WorkspaceMaximizeButton
          {maximized}
          ontoggle={() => (maximized = !maximized)}
        />
        <button
          onclick={() => requestLeave()}
          aria-label="Close subtitle review"
          class="rounded-xl border border-[var(--line)] p-2"
          ><X size={18} /></button
        >
      </div>
    </header>
    <details class="review-tools border-b border-[var(--line)]">
      <summary
        class="flex cursor-pointer list-none items-center gap-3 px-5 py-3 text-sm sm:px-7"
      >
        <span
          class="grid size-8 shrink-0 place-items-center rounded-xl bg-[var(--accent-soft)] text-[var(--accent)]"
          ><Filter size={16} /></span
        >
        <span class="min-w-0 flex-1">
          <strong class="block">Find, filter & compare</strong>
          <span class="muted mt-0.5 block text-xs font-normal"
            >Search the editable revision, focus on changes, or compare any
            subtitle revisions.</span
          >
        </span>
        {#if changedOnly || needsReviewOnly || diffView}<span
            class="rounded-full bg-[var(--accent-soft)] px-2 py-1 text-[.68rem] font-semibold text-[var(--accent)]"
            >Active</span
          >{/if}
        <span class="muted shrink-0 text-xs tabular-nums"
          >{selectedArtifactIds.length}/4</span
        >
        <span class="review-tools-chevron muted shrink-0"
          ><ChevronDown size={17} /></span
        >
      </summary>
      <div class="space-y-3 border-t border-[var(--line)] px-5 py-4 sm:px-7">
        <div
          class="flex flex-wrap items-end gap-3"
          aria-label="Subtitle comparisons and filters"
        >
          <label class="min-w-56 text-xs font-semibold">
            Editable revision
            <select
              value={editArtifactId}
              onchange={(event) =>
                changeEditableArtifact(event.currentTarget.value)}
              class="mt-1 w-full min-w-0 rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-sm font-normal"
              aria-label="Subtitle artifact to edit"
              >{#each columns as column}<option value={column.artifact_id}
                  >{columnLabel(column)}</option
                >{/each}</select
            >
          </label>
          <button
            onclick={toggleChangedOnly}
            aria-pressed={changedOnly}
            class:active={changedOnly}
            class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-3 py-2 text-sm font-semibold"
            ><Filter size={16} /> Changed only</button
          >
          <button
            onclick={toggleNeedsReviewOnly}
            aria-pressed={needsReviewOnly}
            class:active={needsReviewOnly}
            class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-3 py-2 text-sm font-semibold"
            ><CircleHelp size={16} /> Needs review
            {#if needsReviewCount}<span
                class="rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[.65rem] tabular-nums text-amber-700"
                >{needsReviewCount}</span
              >{/if}</button
          >
          <button
            onclick={() => (diffView = !diffView)}
            aria-pressed={diffView}
            class:active={diffView}
            class="rounded-xl border border-[var(--line)] px-3 py-2 text-sm font-semibold"
            >Diff view</button
          >
          <label class="min-w-56 flex-1 text-xs font-semibold sm:max-w-lg">
            Add a comparison
            <select
              bind:value={comparisonChoice}
              disabled={selectedArtifactIds.length >= 4 || comparisonLoading}
              class="mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-sm font-normal disabled:opacity-50"
            >
              <option value="">Choose any subtitle revision…</option>
              {#each comparisonStages as stage}
                {@const stageOptions = comparisonOptions.filter(
                  (item) => item.stage === stage
                )}
                {#if stageOptions.length}
                  <optgroup label={comparisonGroupLabel(stage)}>
                    {#each stageOptions as item (item.artifact_id)}
                      <option value={item.artifact_id}
                        >{columnLabel(item)} · {item.segment_count} cues</option
                      >
                    {/each}
                  </optgroup>
                {/if}
              {/each}
            </select>
          </label>
          <button
            onclick={addComparison}
            disabled={!comparisonChoice ||
              selectedArtifactIds.length >= 4 ||
              comparisonLoading}
            class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-3 py-2 text-sm font-semibold disabled:opacity-40"
          >
            <Plus size={15} />
            {comparisonLoading ? 'Loading…' : 'Add'}
          </button>
        </div>
        <div class="flex flex-wrap items-center gap-2">
          {#each columns as column (column.artifact_id)}
            <span
              class="inline-flex max-w-full items-center gap-1 rounded-full bg-[var(--accent-soft)] px-2.5 py-1 text-xs"
            >
              <span class="truncate">{columnLabel(column)}</span>
              {#if column.artifact_id !== reviewPrimaryArtifactId}
                <button
                  onclick={() => removeComparison(column.artifact_id)}
                  aria-label={`Remove ${columnLabel(column)} from comparison`}
                  class="rounded-full p-0.5 hover:bg-[var(--paper)]"
                >
                  <X size={12} />
                </button>
              {:else}
                <span class="font-semibold text-[var(--accent)]">Primary</span>
              {/if}
            </span>
          {/each}
        </div>
        {#if editColumn && editMode === 'display'}<SearchReplaceBar
            texts={editableTexts}
            onreplace={applySearchReplacements}
            onnavigate={navigateSearchMatch}
            label={`${editStage.replaceAll('_', ' ')} segments`}
          />{/if}
      </div>
    </details>
    {#if pendingLeave}<div
        use:modalFocus={{ onclose: () => (pendingLeave = null) }}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="draft-leave-title"
        class="mx-5 my-3 rounded-xl border border-[var(--line)] bg-[var(--paper)] p-4 sm:mx-7"
      >
        <h3 id="draft-leave-title" class="font-semibold">Save your changes?</h3>
        <p class="muted mt-1 text-sm">
          Saving creates a new revision. A failed save keeps this draft open.
        </p>
        <div class="mt-3 flex flex-wrap gap-2">
          <button
            onclick={saveAndLeave}
            disabled={saving}
            class="rounded-lg bg-[var(--accent)] px-3 py-2 text-sm font-semibold text-white"
            >{saving ? 'Saving…' : 'Save'}</button
          >
          <button
            onclick={discardAndLeave}
            disabled={saving}
            class="rounded-lg border border-[var(--line)] px-3 py-2 text-sm"
            >Discard</button
          >
          <button
            onclick={() => (pendingLeave = null)}
            disabled={saving}
            class="rounded-lg border border-[var(--line)] px-3 py-2 text-sm"
            >Cancel</button
          >
        </div>
      </div>{/if}
    {#if staleDraft}<details
        class="mx-5 my-3 rounded-xl border border-amber-500/40 p-4 sm:mx-7"
      >
        <summary class="font-semibold"
          >The source changed. Your previous draft is retained.</summary
        >
        <p class="muted mt-2 text-sm">
          Review the retained text below against the current revision before
          copying changes. It has not been applied to the new source.
        </p>
        <pre
          class="mt-2 max-h-40 overflow-auto whitespace-pre-wrap text-xs">{staleDraft.columns
            .flatMap((column) => column.segments.map((segment) => segment.text))
            .join('\n\n')}</pre>
        <button
          onclick={() => {
            staleDraft = null;
            forgetReviewDraft(draftKey);
          }}
          class="mt-2 rounded-lg border border-[var(--line)] px-3 py-2 text-sm"
          >Discard retained draft</button
        >
      </details>{/if}
    {#if sourceMediaArtifactId || sourceMediaError}<div
        class="border-b border-[var(--line)] px-5 py-3 sm:px-7"
      >
        {#if sourceMediaError}<p class="text-xs text-red-500" role="alert">
            {sourceMediaError}
          </p>{:else if sourceAudioUrl && sourceIsVideo && previewMode !== 'audio'}<div
            class="mx-auto max-w-3xl"
          >
            <video
              bind:this={videoPreview}
              onerror={() =>
                (videoPlaybackError =
                  'Your browser could not play this video.')}
              onloadedmetadata={() => {
                if (
                  videoPreview &&
                  (!videoPreview.videoWidth || !videoPreview.videoHeight)
                )
                  videoPlaybackError =
                    'This file loaded audio but no playable video.';
              }}
              src={sourceAudioUrl}
              controls
              preload="metadata"
              class="max-h-80 w-full rounded-xl bg-black"
              aria-label="Edited video with selected subtitle revision"
              ontimeupdate={() =>
                (reviewTime = (videoPreview?.currentTime ?? 0) * 1000)}
            >
              <track
                kind="captions"
                src={captionsUrl}
                srclang={editColumn?.language || 'en'}
                label="Selected subtitle revision"
                default
              />
            </video>
            <p class="muted mt-1 text-xs">
              {previewMode === 'video'
                ? 'Compatible preview'
                : 'Original media'} · captions include your current draft changes
            </p>
          </div>{:else if sourceAudioUrl}<AudioPlayer
            bind:element={audioPreview}
            src={sourceAudioUrl}
            label="Source audio preview"
          />{:else if sourceAudioPreparing}<div
            class="muted flex items-center gap-2 text-xs"
            role="status"
          >
            <LoaderCircle class="animate-spin" size={15} /> Preparing a compatible
            {previewMode === 'video' ? 'video' : 'audio'} preview…
            {#if sourcePreviewJobId}<button
                type="button"
                class="font-semibold text-[var(--accent)]"
                onclick={cancelSourcePreview}>Cancel preparation</button
              >{/if}
          </div>{:else}<div class="flex flex-wrap items-center gap-2 text-xs">
            <span class="text-red-500" role="alert"
              >{sourceAudioError ||
                'Source audio is not ready for playback.'}</span
            ><button
              type="button"
              onclick={() =>
                prepareSourceAudio(sourceMediaArtifactId, previewMode)}
              class="flex items-center gap-1 rounded-lg border border-[var(--line)] px-2 py-1 font-semibold"
              ><RefreshCw size={13} /> Retry</button
            >
          </div>{/if}
        {#if sourceIsVideo}
          {#if videoPlaybackError}<p
              class="mt-2 text-sm text-red-500"
              role="alert"
            >
              {videoPlaybackError}
            </p>{/if}
          <div class="mt-2 flex flex-wrap gap-3 text-xs">
            <button
              type="button"
              class="font-semibold text-[var(--accent)]"
              disabled={sourceAudioPreparing}
              onclick={() => prepareSourceAudio(sourceMediaArtifactId, 'video')}
              >Prepare compatible video preview</button
            >
            <button
              type="button"
              class="font-semibold text-[var(--accent)]"
              disabled={sourceAudioPreparing}
              onclick={() => prepareSourceAudio(sourceMediaArtifactId, 'audio')}
              >Use audio-only preview</button
            >
          </div>
          <p class="muted mt-1 text-xs">
            Preview files preserve the original. Compatible video can take time
            to prepare.
          </p>
        {/if}
        {#if cuePlaybackError}<p class="mt-2 text-xs text-red-500" role="alert">
            {cuePlaybackError}
          </p>{/if}
      </div>{/if}
    {#if splitSegment}<div
        class="mx-5 mt-4 rounded-xl border border-[var(--line)] bg-[var(--paper)] p-4 sm:mx-7"
      >
        <div class="flex items-center justify-between">
          <strong class="text-sm">Split at a source-word boundary</strong
          ><button
            onclick={() => (splitSegment = null)}
            aria-label="Close split preview"><X size={16} /></button
          >
        </div>
        {#if splitLoading}<p class="muted mt-2 text-xs" role="status">
            Checking word timing…
          </p>{/if}
        {#if splitError}<p class="mt-2 text-xs text-red-500" role="alert">
            {splitError}
          </p>{/if}
        {#if splitInspection?.boundaries.length}<label
            class="mt-3 block text-xs"
            >Boundary<select
              bind:value={selectedSplitBoundary}
              onchange={chooseSplitBoundary}
              class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] p-2"
              ><option value="">Choose a verified boundary</option
              >{#each splitInspection.boundaries as anchor}<option
                  value={anchor.id}
                  >{anchor.left_text} | {anchor.right_text} · {(
                    anchor.left_end_ms / 1000
                  ).toFixed(2)}s</option
                >{/each}</select
            ></label
          >{/if}
        {#if splitInspection?.next_offset != null}<button
            class="mt-2 text-xs underline"
            onclick={() =>
              splitSegment &&
              inspectSplit(splitSegment, splitInspection?.next_offset ?? 0)}
            >More boundaries</button
          >{/if}
        {#if selectedSplitBoundary}<div class="mt-3 grid gap-3 sm:grid-cols-2">
            <label class="text-xs"
              >First part<textarea
                bind:value={splitLeft}
                class="mt-1 w-full rounded-lg border border-[var(--line)] bg-transparent p-2"
                rows="3"></textarea></label
            ><label class="text-xs"
              >Second part<textarea
                bind:value={splitRight}
                class="mt-1 w-full rounded-lg border border-[var(--line)] bg-transparent p-2"
                rows="3"></textarea></label
            >
          </div>
          <label class="muted mt-2 flex items-center gap-2 text-xs"
            ><input type="checkbox" bind:checked={splitStartsTurn} /> Second part
            starts a new utterance</label
          ><button
            onclick={applySplit}
            disabled={!splitLeft.trim() || !splitRight.trim()}
            class="mt-3 rounded-lg bg-[var(--accent)] px-3 py-2 text-xs font-semibold text-white disabled:opacity-40"
            >Apply split</button
          >{/if}
      </div>{/if}
    {#if editColumn}<div
        class="mx-5 mt-4 flex flex-wrap items-center gap-3 sm:mx-7"
      >
        <div
          class="flex gap-1 rounded-lg border border-[var(--line)] p-1"
          aria-label="Subtitle edit view"
        >
          <button
            type="button"
            class="rounded-md px-3 py-1.5 text-sm"
            class:bg-[var(--accent-soft)]={editMode === 'display'}
            aria-pressed={editMode === 'display'}
            disabled={saving}
            onclick={() => changeEditMode('display')}>Display cues</button
          >
          <button
            type="button"
            class="rounded-md px-3 py-1.5 text-sm"
            class:bg-[var(--accent-soft)]={editMode === 'passages'}
            aria-pressed={editMode === 'passages'}
            disabled={saving ||
              !editColumn.composition_hash ||
              !passages.length}
            onclick={() => changeEditMode('passages')}>Spoken passages</button
          >
        </div>
        <p class="muted text-xs">
          {editMode === 'passages'
            ? 'Edit spoken text, timing and speaker here. Saving rebuilds display cues inside each passage’s timing window using the current subtitle limits.'
            : 'Display cues control subtitle presentation. Edit a combined cue’s spoken text and speaker in Spoken passages.'}
        </p>
      </div>{/if}
    {#if error}<div
        class="mx-5 mt-4 rounded-xl border border-red-400/40 bg-red-500/10 px-4 py-3 text-sm sm:mx-7"
      >
        {error}
      </div>{/if}
    {#if diffView}<div
        class="border-b border-[var(--line)] bg-[var(--accent-soft)] px-5 py-2 text-xs sm:px-7"
      >
        Side-by-side diff is read-only. Turn off <strong>Diff view</strong> to edit
        cue text or timing.
      </div>{/if}
    <div
      bind:this={rowsViewport}
      inert={saving || Boolean(pendingLeave)}
      class="min-h-0 flex-1 overflow-auto"
    >
      {#if loading}<div class="grid h-full place-items-center">
          <div class="section-label animate-pulse">
            Aligning subtitle lineage…
          </div>
        </div>
      {:else if editMode === 'passages'}
        <div class="grid gap-3 p-5 sm:p-7">
          {#each pagedPassages as passage (passage.id)}
            <section
              class="rounded-xl border border-[var(--line)] p-4"
              aria-label={`Spoken passage ${passage.id}`}
            >
              <div class="mb-3 flex items-center justify-between gap-3">
                <strong class="text-sm">Spoken passage {passage.id}</strong>
                <button
                  type="button"
                  class="text-xs font-semibold"
                  onclick={() => setPassageDeleted(passage, !passage.deleted)}
                  >{passage.deleted
                    ? 'Restore passage'
                    : 'Delete passage'}</button
                >
              </div>
              {#if passage.deleted}<p class="muted text-sm">
                  Deleted in this draft. Restore it here or use Undo.
                </p>{:else}
                <div class="mb-3 grid gap-3 sm:grid-cols-3">
                  <label class="text-xs"
                    >Spoken start (ms)<input
                      type="number"
                      min="0"
                      bind:value={passage.start_ms}
                      onfocus={checkpoint}
                      onbeforeinput={checkpoint}
                      class="mt-1 w-full rounded-lg border border-[var(--line)] bg-transparent p-2"
                    /></label
                  >
                  <label class="text-xs"
                    >Spoken end (ms)<input
                      type="number"
                      min="1"
                      bind:value={passage.end_ms}
                      onfocus={checkpoint}
                      onbeforeinput={checkpoint}
                      class="mt-1 w-full rounded-lg border border-[var(--line)] bg-transparent p-2"
                    /></label
                  >
                  <label class="text-xs"
                    >Speaker<input
                      bind:value={passage.speaker}
                      onfocus={checkpoint}
                      onbeforeinput={checkpoint}
                      class="mt-1 w-full rounded-lg border border-[var(--line)] bg-transparent p-2"
                    /></label
                  >
                </div>
                <label class="text-xs"
                  >Spoken text<textarea
                    rows="3"
                    bind:value={passage.text}
                    onfocus={checkpoint}
                    onbeforeinput={checkpoint}
                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-transparent p-2"
                  ></textarea></label
                >
                <label class="mt-2 flex items-center gap-2 text-xs"
                  ><input
                    type="checkbox"
                    bind:checked={passage.starts_new_turn}
                    onchange={checkpoint}
                  /> Start a new utterance here</label
                >
                <label class="mt-3 block text-xs"
                  >Review note<textarea
                    rows="2"
                    bind:value={passage.review_note}
                    onfocus={checkpoint}
                    onbeforeinput={checkpoint}
                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-transparent p-2"
                  ></textarea></label
                >
                <label class="mt-2 flex items-center gap-2 text-xs"
                  ><input
                    type="checkbox"
                    checked={passage.review_state === 'uncertain'}
                    onchange={(event) => {
                      checkpoint();
                      passage.review_state = event.currentTarget.checked
                        ? 'uncertain'
                        : 'clear';
                    }}
                  /> Needs another check</label
                >
              {/if}
            </section>
          {/each}
        </div>
      {:else if !visibleRows.length}<div class="grid h-full place-items-center">
          <p class="muted">No comparable subtitle rows are available.</p>
        </div>
      {:else}
        <table class="w-full min-w-[66rem] border-collapse text-sm">
          <thead class="sticky top-0 z-10 bg-[var(--paper-strong)]"
            ><tr
              ><th
                class="w-32 border-b border-r border-[var(--line)] p-3 text-left"
                >Timing</th
              >{#each columns as column (column.artifact_id)}<th
                  class="border-b border-r border-[var(--line)] p-3 text-left capitalize last:border-r-0"
                  ><span class="block">{column.stage.replaceAll('_', ' ')}</span
                  ><span
                    class="muted mt-0.5 block text-[.68rem] font-normal normal-case"
                    >{columnLabel(column)}</span
                  ></th
                >{/each}</tr
            ></thead
          >
          <tbody
            >{#each pagedRows as row}<tr
                class:changed={row.changed}
                class="align-top"
                ><td
                  class="muted border-b border-r border-[var(--line)] p-3 font-mono text-xs"
                  >{(row.start_ms / 1000).toFixed(2)}<br />→ {(
                    row.end_ms / 1000
                  ).toFixed(2)}</td
                >
                {#each columns as column (column.artifact_id)}<td
                    class="border-b border-r border-[var(--line)] p-3 last:border-r-0"
                    >{#if diffView && column.artifact_id === previousColumn(row)?.artifact_id}<TextDiff
                        before={previousColumnText(row)}
                        after={stageText(row, editArtifactId)}
                        view="before"
                      />{:else if diffView && column.artifact_id === editArtifactId}<TextDiff
                        before={previousColumnText(row)}
                        after={stageText(row, editArtifactId)}
                        view="after"
                      />{:else}<div class="space-y-3">
                        {#each row.cells[column.artifact_id] ?? [] as segment}{@const item =
                            column.artifact_id === editArtifactId
                              ? canonicalSegment(segment)
                              : segment}
                          <div
                            class:needs-review={column.artifact_id ===
                              editArtifactId && segmentNeedsReview(item)}
                            class="rounded-xl border border-[var(--line)] bg-[var(--paper)] p-2.5"
                            class:playing-cue={reviewTime >= item.start_ms &&
                              reviewTime < item.end_ms}
                          >
                            {#if column.artifact_id === editArtifactId && segmentNeedsReview(item)}<div
                                class="mb-2 flex items-start gap-2 rounded-lg bg-amber-500/10 px-2 py-1.5 text-[.65rem] text-amber-800"
                              >
                                <CircleHelp size={13} class="mt-0.5 shrink-0" />
                                <span class="min-w-0 flex-1">
                                  <strong>Needs review</strong>
                                  {#if item.review_note}<span class="ml-1"
                                      >{item.review_note}</span
                                    >{/if}
                                </span>
                              </div>{/if}
                            {#if item.turn_id && stageIndex(item) > 0 && editColumn?.segments[stageIndex(item) - 1]?.turn_id !== item.turn_id}<p
                                class="muted mb-2 text-xs font-semibold"
                              >
                                New utterance
                              </p>{/if}
                            {#if column.artifact_id === editArtifactId}<label
                                class="muted mb-2 flex items-center gap-2 text-xs"
                                ><input
                                  type="checkbox"
                                  onchange={checkpoint}
                                  bind:checked={item.starts_new_turn}
                                  disabled={!canEdit(
                                    item,
                                    'start_new_utterance'
                                  )}
                                /> Start a new utterance here</label
                              >{/if}
                            {#if column.artifact_id === editArtifactId && item.edit_capabilities?.reason}
                              <p class="muted mb-2 text-xs" role="note">
                                {item.edit_capabilities.reason}
                              </p>
                              <details class="muted mb-2 text-xs">
                                <summary class="cursor-pointer"
                                  >Spoken passage context</summary
                                >
                                {#each item.owned_passages ?? [] as passage (passage.id)}
                                  <p class="mt-2 whitespace-pre-wrap">
                                    <strong
                                      >{speakerLabel(passage.speaker) ||
                                        'Unassigned speaker'}</strong
                                    >
                                    · {passage.start_ms}–{passage.end_ms} ms<br
                                    />{passage.text}
                                  </p>
                                {/each}
                              </details>
                            {/if}
                            {#if column.artifact_id === editArtifactId && canEdit(item, 'speaker')}
                              <label class="muted mb-2 block text-xs"
                                >Speaker
                                <input
                                  type="text"
                                  onfocus={checkpoint}
                                  onbeforeinput={checkpoint}
                                  bind:value={item.speaker}
                                  placeholder="Unassigned"
                                  class="mt-1 w-full rounded-lg border border-[var(--line)] bg-transparent px-2 py-1"
                                />
                              </label>
                            {:else if speakerLabel(item.speaker)}<div
                                class="mb-2"
                              >
                                <span
                                  class="inline-flex rounded-full bg-[var(--accent-soft)] px-2 py-0.5 text-[.65rem] font-semibold text-[var(--muted)]"
                                  >{speakerLabel(item.speaker)}</span
                                >
                              </div>{/if}
                            {#if column.artifact_id === editArtifactId}<div
                                class="mb-2 grid grid-cols-2 gap-2"
                              >
                                <label class="muted text-[.68rem]"
                                  >Start ms<input
                                    type="number"
                                    onfocus={checkpoint}
                                    onbeforeinput={checkpoint}
                                    disabled={!canEdit(item, 'display_timing')}
                                    bind:value={item.start_ms}
                                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-transparent px-2 py-1"
                                  /></label
                                ><label class="muted text-[.68rem]"
                                  >End ms<input
                                    type="number"
                                    onfocus={checkpoint}
                                    onbeforeinput={checkpoint}
                                    disabled={!canEdit(item, 'display_timing')}
                                    bind:value={item.end_ms}
                                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-transparent px-2 py-1"
                                  /></label
                                >
                              </div>
                              <textarea
                                onfocus={checkpoint}
                                onbeforeinput={checkpoint}
                                readonly={!canEdit(item, 'text')}
                                aria-label="Subtitle text"
                                bind:value={item.text}
                                data-subtitle-search-index={searchIndex(item)}
                                rows="3"
                                class="w-full resize-y rounded-lg border border-[var(--line)] bg-transparent p-2 leading-relaxed"
                              ></textarea>
                              <div class="mt-2 flex flex-wrap gap-2">
                                <button
                                  type="button"
                                  onclick={() => previewSegment(item)}
                                  disabled={!sourceAudioUrl ||
                                    !playbackElement()}
                                  title={sourceAudioUrl && playbackElement()
                                    ? 'Play this cue from the source recording'
                                    : 'Source audio is still being prepared'}
                                  class="flex items-center gap-1 rounded-lg border border-[var(--line)] px-2 py-1 text-xs disabled:cursor-not-allowed disabled:opacity-40"
                                  ><Play size={13} /> Play</button
                                ><button
                                  onclick={() => inspectSplit(item)}
                                  disabled={!item.id ||
                                    splitLoading ||
                                    !canEdit(item, 'split')}
                                  title={canEdit(item, 'split')
                                    ? 'Choose a verified word boundary'
                                    : item.edit_capabilities?.reason}
                                  class="flex items-center gap-1 rounded-lg border border-[var(--line)] px-2 py-1 text-xs"
                                  ><Scissors size={13} /> Split</button
                                ><button
                                  onclick={() => mergeNext(item)}
                                  disabled={!canMergeNext(item)}
                                  title={mergeTitle(item)}
                                  class="flex items-center gap-1 rounded-lg border border-[var(--line)] px-2 py-1 text-xs disabled:cursor-not-allowed disabled:opacity-40"
                                  ><Merge size={13} /> Merge next</button
                                ><button
                                  onclick={() => removeSegment(item)}
                                  disabled={!canEdit(item, 'delete')}
                                  title={item.edit_capabilities?.reason}
                                  class="flex items-center gap-1 rounded-lg border border-red-400/40 px-2 py-1 text-xs text-red-500"
                                  ><Trash2 size={13} /> Delete</button
                                >
                                <button
                                  onclick={() =>
                                    (evidencePanelSegmentId =
                                      evidencePanelSegmentId === item.id
                                        ? ''
                                        : (item.id ?? ''))}
                                  disabled={!item.id}
                                  aria-expanded={evidencePanelSegmentId ===
                                    item.id}
                                  class:active={evidencePanelSegmentId ===
                                    item.id}
                                  class="flex items-center gap-1 rounded-lg border border-[var(--line)] px-2 py-1 text-xs disabled:cursor-not-allowed disabled:opacity-40"
                                  title={item.id
                                    ? 'Re-transcribe a bounded clip around this cue'
                                    : 'Save this new cue before requesting audio evidence'}
                                  ><CircleHelp size={13} /> Recheck audio</button
                                >
                              </div>{:else}<p
                                class="whitespace-pre-wrap leading-relaxed"
                              >
                                {item.text}
                              </p>{/if}
                            {#if column.artifact_id === editArtifactId && evidencePanelSegmentId === item.id}<SubtitleEvidencePanel
                                {sessionId}
                                sourceArtifactId={editArtifactId}
                                segment={item}
                                records={evidenceFor(item)}
                                onupdated={updateEvidence}
                                onaccept={(candidate, requestId) =>
                                  useEvidenceCandidate(
                                    item,
                                    candidate,
                                    requestId
                                  )}
                                ondelete={() => removeSegment(item)}
                                onuncertain={(note, requestId) =>
                                  markSegmentUncertain(item, note, requestId)}
                                onclear={() => clearSegmentUncertainty(item)}
                              />{/if}
                          </div>{/each}
                      </div>{/if}</td
                  >{/each}
              </tr>{/each}</tbody
          >
        </table>
      {/if}
    </div>
    {#if !loading && (editMode === 'passages' ? passages.length : visibleRows.length) > PAGE_SIZE}<nav
        class="flex flex-wrap items-center justify-between gap-3 border-t border-[var(--line)] px-5 py-3 text-xs sm:px-7"
        aria-label="Subtitle review pages"
      >
        <span class="muted tabular-nums"
          >Showing {pageStart + 1}–{Math.min(
            pageStart + PAGE_SIZE,
            editMode === 'passages' ? passages.length : visibleRows.length
          )} of {editMode === 'passages' ? passages.length : visibleRows.length}
          {editMode === 'passages' ? 'passages' : 'rows'}</span
        >
        <div class="flex items-center gap-2">
          <button
            type="button"
            onclick={() => changePage(pageIndex - 1)}
            disabled={pageIndex === 0}
            class="flex items-center gap-1 rounded-lg border border-[var(--line)] px-2.5 py-1.5 font-semibold disabled:opacity-35"
            ><ChevronLeft size={14} /> Previous</button
          >
          <span class="min-w-20 text-center tabular-nums"
            >Page {pageIndex + 1} of {pageCount}</span
          >
          <button
            type="button"
            onclick={() => changePage(pageIndex + 1)}
            disabled={pageIndex >= pageCount - 1}
            class="flex items-center gap-1 rounded-lg border border-[var(--line)] px-2.5 py-1.5 font-semibold disabled:opacity-35"
            >Next <ChevronRight size={14} /></button
          >
        </div>
      </nav>{/if}
  </div>
</div>
<GuidedTour tourId="subtitle-review" steps={tourSteps} bind:open={tourOpen} />

<style>
  .playing-cue {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  .review-overlay.maximized {
    padding: 0;
  }
  .review-overlay.maximized > :global([role='dialog']) {
    max-width: none;
    border-radius: 0;
  }
  tr.changed > td {
    background: color-mix(in srgb, var(--accent-soft) 22%, transparent);
  }
  .needs-review {
    border-color: color-mix(in srgb, #f59e0b 48%, var(--line));
    box-shadow: 0 0 0 1px color-mix(in srgb, #f59e0b 10%, transparent);
  }
  button.active {
    color: var(--accent);
    background: var(--accent-soft);
  }
  .review-tools summary::-webkit-details-marker {
    display: none;
  }
  .review-tools-chevron {
    transition: transform 160ms ease;
  }
  .review-tools[open] .review-tools-chevron {
    transform: rotate(180deg);
  }
  @media (prefers-reduced-motion: reduce) {
    .review-tools-chevron {
      transition: none;
    }
  }
</style>
