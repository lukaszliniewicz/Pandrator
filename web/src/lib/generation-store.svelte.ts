import { errorMessage } from './errors';
import { generationHistoryVersion } from './generation-history';
import {
  generationApi,
  type GenerationSegmentBatchChange,
  type GenerationSegmentChanges
} from './domain-api';
import type {
  GenerationRun,
  GenerationSegment,
  GenerationSegmentPage,
  LoadState,
  OutputAssembly
} from './api-models';
import {
  adoptUpdatedSegment,
  adoptUpdatedSegments,
  isStaleLoad
} from './generation-segment-adoption';
import {
  invalidates,
  invalidationBus,
  type InvalidationBatch,
  type PandratorServerEvent
} from './invalidation';

export type SegmentFilter =
  | 'all'
  | 'completed'
  | 'queued'
  | 'marked'
  | 'failed'
  | 'stale'
  | 'boundary_flags'
  | 'verification_issues';

export const SEGMENT_FILTER_OPTIONS: {
  value: SegmentFilter;
  label: string;
}[] = [
  { value: 'all', label: 'All segments' },
  { value: 'completed', label: 'Generated' },
  { value: 'queued', label: 'Queued' },
  { value: 'marked', label: 'Marked' },
  { value: 'failed', label: 'Generation failed' },
  { value: 'stale', label: 'Stale' },
  { value: 'boundary_flags', label: 'Block flags' },
  { value: 'verification_issues', label: 'Verification issues' }
];

const EMPTY_SEGMENTS: GenerationSegmentPage = {
  items: [],
  total: 0,
  next_cursor: null,
  plan_revision_id: null
};

export type GenerationLoadOptions = {
  filter: SegmentFilter;
  selectedRunId: string;
  selectedRunVersionId?: string;
  audioVersionSide?: 'before' | 'after';
  reset?: boolean;
  preserveLoaded?: boolean;
  search?: URLSearchParams;
  startOrdinal?: number;
  visibleOrdinals?: { start: number; end: number };
};

export type GenerationLoadResult = {
  selectedRunId: string;
  shouldExpand: boolean;
  /** A canceled or superseded request must not restore its old view selection. */
  discarded?: boolean;
};

const ACTIVE_RUN_STATUS_PRIORITY = [
  'running',
  'pausing',
  'pause_requested',
  'cancel_requested',
  'queued',
  'paused'
] as const;

function preferredActiveRun(runs: GenerationRun[]) {
  for (const status of ACTIVE_RUN_STATUS_PRIORITY) {
    const match = runs.find((item) => item.status === status);
    if (match) return match;
  }
  return null;
}

export class GenerationStore {
  payload = $state<GenerationSegmentPage>({ ...EMPTY_SEGMENTS });
  runs = $state<GenerationRun[]>([]);
  activeRun = $state<GenerationRun | null>(null);
  assembly = $state<OutputAssembly | null>(null);
  status = $state<LoadState>('idle');
  error = $state('');
  private loadedScope = '';
  startOrdinal = $state(0);
  paging = $state(false);
  historyLoading = $state(false);
  historyComplete = $state(false);
  private runsLoaded = false;
  private runsDirty = false;
  private pagePromise?: Promise<GenerationLoadResult>;
  private historyControllers = new Map<string, AbortController>();
  private historyPromise?: Promise<void>;
  private historyController?: AbortController;
  private cacheEpoch = 0;
  private freshRows = new Map<string, number>();
  private controller?: AbortController;
  // Mutation epoch: every adopted save bumps this. A load that started
  // earlier must not apply its payload afterwards (see isStaleLoad); the
  // in-flight request is aborted too, but an already-resolved response can
  // still have a pending continuation.
  private loadEpoch = 0;
  private unsubscribe?: () => void;
  private refreshTimer?: number;
  private refreshInFlight = false;
  private refreshQueued = false;

  constructor(readonly sessionId: string) {}

  get loading() {
    return this.status === 'loading';
  }

  needsVisibleRefresh(ids: string[]) {
    return ids.some((id) => this.freshRows.get(id) !== this.cacheEpoch);
  }

  reconcileActivity() {
    this.runsDirty = true;
    this.cacheEpoch += 1;
  }

  private scope(options: GenerationLoadOptions) {
    return [
      options.filter,
      options.selectedRunId,
      options.selectedRunVersionId ?? '',
      options.audioVersionSide ?? 'after',
      options.search?.toString() ?? ''
    ].join('|');
  }

  pageQuery(options: GenerationLoadOptions, cursor = 0, limit = 100) {
    const query = new URLSearchParams(options.search);
    query.set('limit', String(limit));
    if (cursor) query.set('cursor', String(cursor));
    if (options.filter === 'marked') query.set('marked', 'true');
    else if (options.filter === 'boundary_flags')
      query.set('boundary_flags', 'true');
    else if (options.filter === 'verification_issues')
      query.set('verification', 'issues');
    else if (options.filter !== 'all') query.set('status', options.filter);
    const version = generationHistoryVersion(
      this.runs,
      options.selectedRunId,
      options.selectedRunVersionId
    );
    if (version) {
      query.set('generation_run_id', version.id);
      if (version.audio_version)
        query.set('audio_version_side', options.audioVersionSide ?? 'after');
    }
    // Older ordinary history uses sequence-based take selection. Its full
    // history is intentional; current mixes and pinned recasts need one take.
    query.set(
      'take_history',
      !options.selectedRunId || version?.audio_version ? 'selected' : 'full'
    );
    return query;
  }

  private async loadRuns(signal?: AbortSignal, force = false) {
    if (this.runsLoaded && !this.runsDirty && !force) return;
    const payload = await generationApi.runs(this.sessionId, signal, {
      view: 'compact',
      ...(this.historyComplete ? {} : { limit: 25 })
    });
    if (signal?.aborted) return;
    const known = new Map(this.runs.map((run) => [run.id, run]));
    const ids = new Set(payload.items.map((run) => run.id));
    this.runs = [
      ...payload.items.map((run) => ({ ...known.get(run.id), ...run })),
      ...this.runs.filter((run) => !ids.has(run.id) && run.settings_snapshot)
    ].sort((left, right) => right.sequence_number - left.sequence_number);
    this.activeRun = preferredActiveRun(
      payload.items.filter((run) => !run.early_repair_parent_run_id)
    );
    this.runsLoaded = true;
    this.runsDirty = false;
  }

  async ensureRun(id: string, signal?: AbortSignal) {
    if (!id) return;
    const existing = this.runs.find((run) => run.id === id);
    if (existing?.settings_snapshot || (this.historyComplete && existing))
      return;
    const run = await generationApi.run(id, signal);
    if (signal?.aborted) return;
    this.runs = [run, ...this.runs.filter((item) => item.id !== id)].sort(
      (left, right) => right.sequence_number - left.sequence_number
    );
    this.activeRun = preferredActiveRun(
      this.runs.filter((item) => !item.early_repair_parent_run_id)
    );
  }

  loadHistory(): Promise<void> {
    if (this.historyPromise) return this.historyPromise;
    if (this.historyComplete) return Promise.resolve();
    this.historyLoading = true;
    const controller = new AbortController();
    this.historyController = controller;
    const task = (async () => {
      try {
        const payload = await generationApi.runs(
          this.sessionId,
          controller.signal
        );
        if (controller.signal.aborted) return;
        this.runs = payload.items;
        this.historyComplete = true;
        this.runsLoaded = true;
      } catch (caught) {
        if (!controller.signal.aborted) this.error = errorMessage(caught);
      } finally {
        this.historyLoading = false;
        if (this.historyController === controller)
          this.historyController = undefined;
      }
    })();
    this.historyPromise = task;
    void task.finally(() => {
      if (this.historyPromise === task) this.historyPromise = undefined;
    });
    return task;
  }

  async load(options: GenerationLoadOptions): Promise<GenerationLoadResult> {
    if (options.reset === false) return this.loadPage(options);
    if (
      options.preserveLoaded !== false &&
      this.scope(options) === this.loadedScope &&
      this.pagePromise
    ) {
      await this.pagePromise.catch(() => undefined);
      if (this.scope(options) !== this.loadedScope)
        return {
          selectedRunId: options.selectedRunId,
          shouldExpand: false,
          discarded: true
        };
    }
    this.controller?.abort();
    const controller = new AbortController();
    this.controller = controller;
    const epoch = this.loadEpoch;
    const scope = this.scope(options);
    const keep = options.preserveLoaded !== false && scope === this.loadedScope;
    const previous = this.payload;
    this.status = previous.total ? 'stale' : 'loading';
    this.error = '';
    try {
      await this.loadRuns(controller.signal);
      if (options.selectedRunId) await this.historyPromise;
      await this.ensureRun(options.selectedRunId, controller.signal);
      await this.ensureRun(
        options.selectedRunVersionId ?? '',
        controller.signal
      );
      if (controller.signal.aborted)
        return {
          selectedRunId: options.selectedRunId,
          shouldExpand: false,
          discarded: true
        };
      let cursor = keep ? this.startOrdinal : (options.startOrdinal ?? 0);
      const range =
        keep && previous.items.length ? options.visibleOrdinals : undefined;
      if (range) cursor = Math.max(0, range.start - 5);
      const query = this.pageQuery(options, cursor, range ? 250 : 100);
      if (range) query.set('end_ordinal', String(range.end + 5));
      const [segments, assembly] = await Promise.all([
        generationApi.segments(this.sessionId, query, controller.signal),
        generationApi.latestAssembly(this.sessionId, controller.signal)
      ]);
      let next = segments;
      // An active plan can change while the user is far down its old rows.
      // Start the new revision at the beginning rather than adopting a tail.
      if (
        keep &&
        previous.plan_revision_id !== next.plan_revision_id &&
        cursor
      ) {
        next = await generationApi.segments(
          this.sessionId,
          this.pageQuery(options),
          controller.signal
        );
        cursor = 0;
      }
      if (controller.signal.aborted || isStaleLoad(epoch, this.loadEpoch))
        return {
          selectedRunId: options.selectedRunId,
          shouldExpand: false,
          discarded: true
        };
      const merge = keep && previous.plan_revision_id === next.plan_revision_id;
      if (merge) {
        const replacements = new Map(next.items.map((item) => [item.id, item]));
        const upper = range
          ? range.end + 5
          : (next.items.at(-1)?.ordinal ?? cursor - 1);
        const items = previous.items
          .filter(
            (item) =>
              item.ordinal < cursor ||
              item.ordinal > upper ||
              replacements.has(item.id)
          )
          .map((item) => {
            const updated = replacements.get(item.id);
            if (!updated) return item;
            replacements.delete(item.id);
            // Keep an explicitly opened take history if its cardinality and
            // row revision still match; refresh the chosen take's live metadata.
            if (
              item.take_history_loaded &&
              updated.take_history_loaded === false &&
              item.revision === updated.revision &&
              item.takes.length === updated.take_count
            ) {
              const chosen = new Map(
                updated.takes.map((take) => [take.id, take])
              );
              return {
                ...updated,
                take_history_loaded: true,
                takes: item.takes.map((take) => ({
                  ...take,
                  ...chosen.get(take.id),
                  is_active:
                    chosen.get(take.id)?.is_active ??
                    (updated.takes.some((value) => value.is_active)
                      ? false
                      : take.is_active)
                }))
              };
            }
            return updated;
          });
        items.push(...replacements.values());
        this.payload = {
          ...next,
          items: items.sort((a, b) => a.ordinal - b.ordinal),
          next_cursor: previous.next_cursor
        };
      } else {
        this.payload = next;
        this.startOrdinal = cursor;
      }
      this.loadedScope = scope;
      if (!merge) this.freshRows.clear();
      for (const item of next.items)
        this.freshRows.set(item.id, this.cacheEpoch);
      this.assembly = assembly.item;
      this.status = this.payload.total ? 'ready' : 'empty';
      return { selectedRunId: options.selectedRunId, shouldExpand: false };
    } catch (caught) {
      if (controller.signal.aborted)
        return {
          selectedRunId: options.selectedRunId,
          shouldExpand: false,
          discarded: true
        };
      this.error = errorMessage(caught);
      this.status = 'failed';
      throw caught;
    } finally {
      if (this.controller === controller) this.controller = undefined;
    }
  }

  private loadPage(
    options: GenerationLoadOptions,
    previous = false
  ): Promise<GenerationLoadResult> {
    if (this.pagePromise) return this.pagePromise;
    const task = this.fetchPage(options, previous);
    this.pagePromise = task;
    void task
      .finally(() => {
        if (this.pagePromise === task) this.pagePromise = undefined;
      })
      .catch(() => undefined);
    return task;
  }

  loadPrevious(options: GenerationLoadOptions) {
    return this.loadPage(options, true);
  }

  private async fetchPage(
    options: GenerationLoadOptions,
    previous: boolean
  ): Promise<GenerationLoadResult> {
    const discarded = {
      selectedRunId: options.selectedRunId,
      shouldExpand: false,
      discarded: true
    };
    if (this.scope(options) !== this.loadedScope) return discarded;
    const cursor = previous
      ? Math.max(0, this.startOrdinal - 100)
      : this.payload.next_cursor;
    if (cursor == null || (previous && this.startOrdinal <= 0))
      return discarded;
    this.controller?.abort();
    const controller = new AbortController();
    this.controller = controller;
    const epoch = this.loadEpoch;
    const revision = this.payload.plan_revision_id;
    const query = this.pageQuery(options, cursor);
    if (revision) query.set('plan_revision_id', revision);
    if (previous) query.set('end_ordinal', String(this.startOrdinal - 1));
    this.paging = true;
    this.error = '';
    try {
      const page = await generationApi.segments(
        this.sessionId,
        query,
        controller.signal
      );
      if (
        controller.signal.aborted ||
        isStaleLoad(epoch, this.loadEpoch) ||
        revision !== this.payload.plan_revision_id ||
        this.scope(options) !== this.loadedScope
      )
        return discarded;
      if (page.plan_revision_id !== revision)
        throw new Error('The speech plan changed. Refresh the segment list.');
      if (!previous && page.next_cursor != null && page.next_cursor <= cursor)
        throw new Error(
          'The segment list did not advance. Retry loading the page.'
        );
      const known = new Set(this.payload.items.map((item) => item.id));
      this.payload = {
        ...this.payload,
        ...page,
        items: [
          ...this.payload.items,
          ...page.items.filter((item) => !known.has(item.id))
        ].sort((a, b) => a.ordinal - b.ordinal),
        next_cursor: previous ? this.payload.next_cursor : page.next_cursor
      };
      if (previous) this.startOrdinal = cursor;
      for (const item of page.items)
        this.freshRows.set(item.id, this.cacheEpoch);
      this.status = this.payload.total ? 'ready' : 'empty';
      return { selectedRunId: options.selectedRunId, shouldExpand: false };
    } catch (caught) {
      if (controller.signal.aborted) return discarded;
      this.error = errorMessage(caught);
      throw caught;
    } finally {
      if (this.controller === controller) this.controller = undefined;
      this.paging = false;
    }
  }

  async loadTakeHistory(
    item: GenerationSegment,
    options: GenerationLoadOptions
  ) {
    if (
      item.take_history_loaded !== false ||
      this.historyControllers.has(item.id)
    )
      return;
    const controller = new AbortController();
    this.historyControllers.set(item.id, controller);
    const epoch = this.loadEpoch;
    const revision = this.payload.plan_revision_id;
    const query = this.pageQuery(options);
    for (const key of [
      'status',
      'marked',
      'verification',
      'boundary_flags',
      'q',
      'cursor'
    ])
      query.delete(key);
    query.set('take_history', 'full');
    query.set('around_ordinal', String(item.ordinal));
    query.set('radius', '0');
    if (revision) query.set('plan_revision_id', revision);
    try {
      const page = await generationApi.segments(
        this.sessionId,
        query,
        controller.signal
      );
      if (
        controller.signal.aborted ||
        isStaleLoad(epoch, this.loadEpoch) ||
        revision !== this.payload.plan_revision_id
      )
        return;
      const full = page.items.find(
        (row) => row.id === item.id && row.revision === item.revision
      );
      if (!full)
        throw new Error(
          'This segment changed. Refresh before opening its recordings.'
        );
      this.payload = {
        ...this.payload,
        items: this.payload.items.map((row) =>
          row.id === item.id
            ? {
                ...row,
                takes: full.takes,
                take_count: full.takes.length,
                take_history_loaded: true
              }
            : row
        )
      };
    } finally {
      this.historyControllers.delete(item.id);
    }
  }

  /** Abort older in-flight loads and invalidate their late responses. */
  private invalidateLoadsForAdoption() {
    this.controller?.abort();
    this.loadEpoch += 1;
  }

  async updateSegment(
    item: GenerationSegment,
    changes: GenerationSegmentChanges
  ) {
    const updated = await generationApi.updateSegment(item, changes);
    // Adopt the authoritative mutation response atomically: an edit-copy
    // save returns a new segment ID plus its new plan_revision_id, and the
    // follow-up reload can lose a race with event-driven refreshes.
    // Regenerate correctness must never depend on that reload winning, and
    // a delayed older response must not overwrite this adoption.
    this.invalidateLoadsForAdoption();
    const next = adoptUpdatedSegment(
      {
        items: this.payload.items,
        plan_revision_id: this.payload.plan_revision_id
      },
      item.id,
      updated
    );
    this.payload = {
      ...this.payload,
      items: next.items,
      plan_revision_id: next.plan_revision_id
    };
    return updated;
  }

  async updateSegments(updates: GenerationSegmentBatchChange[]) {
    const result = await generationApi.updateSegments(this.sessionId, updates);
    // Same authoritative adoption as updateSegment; inconsistent mixed-plan
    // responses throw instead of being partially adopted.
    this.invalidateLoadsForAdoption();
    const next = adoptUpdatedSegments(
      {
        items: this.payload.items,
        plan_revision_id: this.payload.plan_revision_id
      },
      result.items
    );
    this.payload = {
      ...this.payload,
      items: next.items,
      plan_revision_id: next.plan_revision_id
    };
    return result.items;
  }

  async selectTake(item: GenerationSegment, takeId: string) {
    const result = await generationApi.selectTake(item, takeId);
    const updated = {
      ...item,
      revision: result.revision,
      takes: item.takes.map((take) => ({
        ...take,
        is_active: take.id === takeId
      }))
    };
    this.payload = {
      ...this.payload,
      items: this.payload.items.map((candidate) =>
        candidate.id === updated.id ? updated : candidate
      )
    };
    return updated;
  }

  async refreshAssembly() {
    this.assembly = (await generationApi.latestAssembly(this.sessionId)).item;
    return this.assembly;
  }

  upsertRun(run: GenerationRun) {
    this.runs = [run, ...this.runs.filter((item) => item.id !== run.id)].sort(
      (left, right) => right.sequence_number - left.sequence_number
    );
    this.activeRun = run;
  }

  setAssembly(assembly: OutputAssembly | null) {
    this.assembly = assembly;
  }

  removeRun(runId: string) {
    this.runs = this.runs.filter(
      (item) => item.id !== runId && item.output_generation_run_id !== runId
    );
    this.activeRun = preferredActiveRun(this.runs);
  }

  connect(
    getOptions: () => GenerationLoadOptions,
    onLoaded: (result: GenerationLoadResult) => void,
    shouldRefresh: () => boolean = () => true
  ) {
    if (this.unsubscribe) return this.unsubscribe;
    this.unsubscribe = invalidationBus.subscribe((batch) => {
      this.patchLiveProgress(batch);
      if (
        batch.events.length &&
        batch.events.every((event) => event.type === 'job.progress')
      )
        return;
      if (
        invalidates(batch, 'generation', this.sessionId) ||
        invalidates(batch, 'output', this.sessionId)
      ) {
        this.runsDirty = true;
        this.cacheEpoch += 1;
      }
      if (
        shouldRefresh() &&
        (invalidates(batch, 'generation', this.sessionId) ||
          invalidates(batch, 'output', this.sessionId))
      ) {
        const terminal = batch.events.some(
          (event) =>
            event.session_id === this.sessionId &&
            ['job.succeeded', 'job.failed', 'job.canceled'].includes(event.type)
        );
        this.scheduleRefresh(getOptions, onLoaded, terminal ? 0 : 600);
      }
    });
    return () => {
      this.unsubscribe?.();
      this.unsubscribe = undefined;
      this.controller?.abort();
      this.historyController?.abort();
      for (const controller of this.historyControllers.values())
        controller.abort();
      this.historyControllers.clear();
      if (this.refreshTimer !== undefined)
        window.clearTimeout(this.refreshTimer);
      this.refreshTimer = undefined;
      this.refreshQueued = false;
    };
  }

  private scheduleRefresh(
    getOptions: () => GenerationLoadOptions,
    onLoaded: (result: GenerationLoadResult) => void,
    delayMs: number
  ) {
    if (this.refreshTimer !== undefined) window.clearTimeout(this.refreshTimer);
    this.refreshTimer = window.setTimeout(() => {
      this.refreshTimer = undefined;
      void this.runRefresh(getOptions, onLoaded);
    }, delayMs);
  }

  private async runRefresh(
    getOptions: () => GenerationLoadOptions,
    onLoaded: (result: GenerationLoadResult) => void
  ) {
    if (this.refreshInFlight) {
      this.refreshQueued = true;
      return;
    }
    this.refreshInFlight = true;
    try {
      await this.pagePromise?.catch(() => undefined);
      const result = await this.load({
        ...getOptions(),
        reset: true,
        preserveLoaded: true
      });
      if (!result.discarded) onLoaded(result);
    } catch {
      // The store exposes load failures; event callbacks remain non-throwing.
    } finally {
      this.refreshInFlight = false;
      if (this.refreshQueued) {
        this.refreshQueued = false;
        this.scheduleRefresh(getOptions, onLoaded, 0);
      }
    }
  }

  private patchLiveProgress(batch: InvalidationBatch) {
    const relevant = batch.events.filter(
      (event) => event.session_id === this.sessionId
    );
    if (!relevant.length) return;
    const patch = <T extends GenerationRun | OutputAssembly>(
      item: T,
      event: PandratorServerEvent
    ): T => ({
      ...item,
      ...(event.progress !== undefined
        ? { progress: Number(event.progress) }
        : {}),
      ...(event.detail !== undefined ? { progress_detail: event.detail } : {}),
      ...(['queued', 'running', 'cancel_requested'].includes(
        String(event.status ?? '')
      )
        ? { status: String(event.status) }
        : {})
    });
    for (const event of relevant) {
      const generationRunId = String(event.generation_run_id ?? '');
      const assemblyId = String(event.output_assembly_id ?? '');
      this.runs = this.runs.map((item) => {
        let next = item;
        if (
          (generationRunId && item.id === generationRunId) ||
          (event.job_id && item.job_id === event.job_id)
        ) {
          next = patch(next, event);
        }
        if (
          next.assembly &&
          ((assemblyId && next.assembly.id === assemblyId) ||
            (event.job_id && next.assembly.job_id === event.job_id))
        ) {
          next = { ...next, assembly: patch(next.assembly, event) };
        }
        return next;
      });
      if (
        this.activeRun &&
        ((generationRunId && this.activeRun.id === generationRunId) ||
          (event.job_id && this.activeRun.job_id === event.job_id))
      ) {
        this.activeRun = patch(this.activeRun, event);
      }
      if (
        this.assembly &&
        ((assemblyId && this.assembly.id === assemblyId) ||
          (event.job_id && this.assembly.job_id === event.job_id))
      ) {
        this.assembly = patch(this.assembly, event);
      }
    }
  }
}
