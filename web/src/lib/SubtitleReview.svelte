<script lang="ts">
  import { errorMessage } from './errors';
  import {
    ChevronLeft,
    ChevronDown,
    ChevronRight,
    CircleHelp,
    Columns3,
    Filter,
    Merge,
    Play,
    Plus,
    Save,
    Scissors,
    Trash2,
    X
  } from '@lucide/svelte';
  import { sessionApi } from './domain-api';
  import type {
    SubtitleComparisonRow as Row,
    SubtitleReviewCatalog as Catalog,
    SubtitleReviewCatalogItem as CatalogItem,
    SubtitleReviewColumn as ReviewColumn,
    SubtitleReviewPayload as Payload,
    SubtitleEvidenceCandidate,
    SubtitleEvidenceRecord,
    SubtitleSegment as Segment
  } from './api-models';
  import { onDestroy, onMount, tick } from 'svelte';
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
    sourceAudioArtifactId,
    onclose,
    onsaved
  }: {
    sessionId: string;
    primaryArtifactId: string;
    sourceAudioArtifactId?: string;
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
  let audioPreview = $state<HTMLAudioElement>();
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
  const editStage = $derived(editColumn?.stage ?? '');
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
    Math.max(1, Math.ceil(visibleRows.length / PAGE_SIZE))
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
    segment.review_state = 'uncertain';
    segment.review_note = note;
    if (requestId)
      segment.evidence_ids = Array.from(
        new Set([...(segment.evidence_ids ?? []), requestId])
      );
  }

  function clearSegmentUncertainty(segment: Segment) {
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
      left === right || Boolean(left.id && right.id && left.id === right.id)
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

  function canMergeNext(segment: Segment) {
    const next = nextSegment(segment);
    return (
      Boolean(next) &&
      speakerLabel(segment.speaker).toLocaleLowerCase() ===
        speakerLabel(next?.speaker).toLocaleLowerCase()
    );
  }

  function mergeTitle(segment: Segment) {
    if (!nextSegment(segment)) return 'There is no following cue';
    return canMergeNext(segment)
      ? 'Merge with the next cue'
      : 'Cues from different speakers cannot be merged';
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
    refreshCatalog = false
  ) {
    loading = true;
    error = '';
    try {
      const [nextPayload, nextCatalog] = await Promise.all([
        sessionApi.subtitleReview(sessionId, artifactIds),
        refreshCatalog || !catalog
          ? sessionApi.subtitleCatalog(sessionId)
          : Promise.resolve(catalog)
      ]);
      payload = nextPayload;
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
    const remaining = selectedArtifactIds.filter((item) => item !== artifactId);
    if (editArtifactId === artifactId) editArtifactId = reviewPrimaryArtifactId;
    await load(remaining);
  }

  function split(segment: Segment) {
    const records = editColumn?.segments;
    if (!records) return;
    const index = stageIndex(segment);
    if (index < 0) return;
    const midpoint = Math.max(1, Math.floor(segment.text.length / 2));
    const space = segment.text.lastIndexOf(' ', midpoint);
    const boundary = space > 0 ? space : midpoint;
    const time = Math.floor((segment.start_ms + segment.end_ms) / 2);
    const first = {
      ...segment,
      id: undefined,
      text: segment.text.slice(0, boundary).trim(),
      end_ms: time
    };
    const second = {
      ...segment,
      id: undefined,
      text: segment.text.slice(boundary).trim(),
      start_ms: time
    };
    if (first.text && second.text) {
      records.splice(index, 1, first, second);
      replaceInRows(segment, [first, second]);
    }
  }

  function mergeNext(segment: Segment) {
    const records = editColumn?.segments;
    if (!records) return;
    const index = stageIndex(segment);
    if (index < 0) return;
    const next = records[index + 1];
    if (next && canMergeNext(segment)) {
      const merged = {
        ...segment,
        id: undefined,
        end_ms: next.end_ms,
        text: `${segment.text} ${next.text}`.trim(),
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
    const records = editColumn?.segments;
    const index = stageIndex(segment);
    if (records && index >= 0) {
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
    for (const update of updates) {
      if (records[update.index]) records[update.index].text = update.text;
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
    if (pause) audioPreview?.pause();
  }

  function watchCueBoundary() {
    if (!audioPreview || audioPreview.paused) {
      cuePreviewFrame = null;
      return;
    }
    if (audioPreview.currentTime >= cuePreviewEnd - 0.01) {
      audioPreview.pause();
      audioPreview.currentTime = cuePreviewEnd;
      cuePreviewFrame = null;
      return;
    }
    cuePreviewFrame = window.requestAnimationFrame(watchCueBoundary);
  }

  async function previewSegment(segment: Segment) {
    if (!audioPreview) return;
    stopCuePreview(true);
    audioPreview.currentTime = segment.start_ms / 1000;
    cuePreviewEnd = segment.end_ms / 1000;
    try {
      await audioPreview.play();
      cuePreviewFrame = window.requestAnimationFrame(watchCueBoundary);
    } catch {
      cuePreviewFrame = null;
    }
  }

  onDestroy(() => stopCuePreview(true));

  async function save() {
    const column = editColumn;
    if (!column) return;
    saving = true;
    error = '';
    try {
      const result = await sessionApi.saveSubtitleReview(
        sessionId,
        column.stage,
        {
          source_artifact_id: column.artifact_id,
          expected_revision: column.revision,
          segments: column.segments.map(
            ({
              start_ms,
              end_ms,
              text,
              speaker,
              review_state,
              review_note,
              evidence_ids
            }) => ({
              start_ms,
              end_ms,
              text,
              speaker,
              review_state: review_state ?? 'clear',
              review_note: review_note ?? '',
              evidence_ids: evidence_ids ?? []
            })
          )
        }
      );
      const previousId = column.artifact_id;
      const nextIds = selectedArtifactIds.map((artifactId) =>
        artifactId === previousId ? result.artifact_id : artifactId
      );
      if (reviewPrimaryArtifactId === previousId)
        reviewPrimaryArtifactId = result.artifact_id;
      editArtifactId = result.artifact_id;
      await load(nextIds, true);
      onsaved();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      saving = false;
    }
  }
  onMount(() => {
    reviewPrimaryArtifactId = primaryArtifactId;
    editArtifactId = primaryArtifactId;
    void load([primaryArtifactId], true);
  });

  $effect(() => {
    const artifactId = editArtifactId;
    if (artifactId && artifactId !== evidenceArtifactId)
      void loadEvidence(artifactId);
  });
</script>

<div
  class:maximized
  class="review-overlay fixed inset-0 z-50 bg-black/45 p-3 backdrop-blur-sm sm:p-6"
  role="presentation"
>
  <div
    use:modalFocus={{ onclose }}
    class="surface mx-auto flex h-full max-w-[96rem] flex-col overflow-hidden rounded-[1.5rem]"
    role="dialog"
    aria-modal="true"
    aria-labelledby="review-title"
  >
    <header
      class="flex flex-wrap items-center justify-between gap-4 border-b border-[var(--line)] px-5 py-4 sm:px-7"
    >
      <div>
        <div class="eyebrow">Subtitle review</div>
        <h2
          id="review-title"
          class="mt-1 flex items-center gap-2 text-xl font-semibold"
        >
          <Columns3 size={20} /> Compare and refine
        </h2>
      </div>
      <div class="flex min-w-0 flex-wrap items-center justify-end gap-2">
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
          onclick={onclose}
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
              bind:value={editArtifactId}
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
        {#if editColumn}<SearchReplaceBar
            texts={editableTexts}
            onreplace={applySearchReplacements}
            onnavigate={navigateSearchMatch}
            label={`${editStage.replaceAll('_', ' ')} segments`}
          />{/if}
      </div>
    </details>
    {#if sourceAudioArtifactId}<div
        class="border-b border-[var(--line)] px-5 py-3 sm:px-7"
      >
        <AudioPlayer
          bind:element={audioPreview}
          src={`/api/v1/artifacts/${sourceAudioArtifactId}/content`}
          label="Source audio preview"
        />
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
    <div bind:this={rowsViewport} class="min-h-0 flex-1 overflow-auto">
      {#if loading}<div class="grid h-full place-items-center">
          <div class="eyebrow animate-pulse">Aligning subtitle lineage…</div>
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
                            {#if speakerLabel(item.speaker)}<div class="mb-2">
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
                                    bind:value={item.start_ms}
                                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-transparent px-2 py-1"
                                  /></label
                                ><label class="muted text-[.68rem]"
                                  >End ms<input
                                    type="number"
                                    bind:value={item.end_ms}
                                    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-transparent px-2 py-1"
                                  /></label
                                >
                              </div>
                              <textarea
                                bind:value={item.text}
                                data-subtitle-search-index={searchIndex(item)}
                                rows="3"
                                class="w-full resize-y rounded-lg border border-[var(--line)] bg-transparent p-2 leading-relaxed"
                              ></textarea>
                              <div class="mt-2 flex flex-wrap gap-2">
                                <button
                                  onclick={() => previewSegment(item)}
                                  class="flex items-center gap-1 rounded-lg border border-[var(--line)] px-2 py-1 text-xs"
                                  ><Play size={13} /> Play</button
                                ><button
                                  onclick={() => split(item)}
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
    {#if !loading && visibleRows.length > PAGE_SIZE}<nav
        class="flex flex-wrap items-center justify-between gap-3 border-t border-[var(--line)] px-5 py-3 text-xs sm:px-7"
        aria-label="Subtitle review pages"
      >
        <span class="muted tabular-nums"
          >Showing {pageStart + 1}–{Math.min(
            pageStart + PAGE_SIZE,
            visibleRows.length
          )} of {visibleRows.length} rows</span
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
