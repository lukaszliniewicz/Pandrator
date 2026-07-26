import { generationApi, type GenerationSegmentChanges } from './domain-api';
import type {
  GenerationRun,
  GenerationSegment,
  GenerationSegmentPage,
  LoadState,
  OutputAssembly
} from './api-models';
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
  reset?: boolean;
  preserveLoaded?: boolean;
};

export type GenerationLoadResult = {
  selectedRunId: string;
  shouldExpand: boolean;
};

export class GenerationStore {
  payload = $state<GenerationSegmentPage>({ ...EMPTY_SEGMENTS });
  runs = $state<GenerationRun[]>([]);
  activeRun = $state<GenerationRun | null>(null);
  assembly = $state<OutputAssembly | null>(null);
  status = $state<LoadState>('idle');
  error = $state('');
  private loadedFilter: SegmentFilter | '' = '';
  private initialized = false;
  private controller?: AbortController;
  private unsubscribe?: () => void;

  constructor(readonly sessionId: string) {}

  get loading() {
    return this.status === 'loading';
  }

  async load(options: GenerationLoadOptions): Promise<GenerationLoadResult> {
    const {
      filter,
      reset = true,
      preserveLoaded = reset
    } = options;
    this.controller?.abort();
    const controller = new AbortController();
    this.controller = controller;
    this.status = this.payload.total ? 'stale' : 'loading';
    this.error = '';
    try {
      const query = new URLSearchParams({ limit: '100' });
      if (filter === 'marked') query.set('marked', 'true');
      else if (filter === 'verification_issues') query.set('verification', 'issues');
      else if (filter !== 'all') query.set('status', filter);
      if (!reset && this.payload.next_cursor != null) {
        query.set('cursor', String(this.payload.next_cursor));
      }
      const previousTotal = this.payload.total;
      const previousRunId = this.activeRun?.id ?? '';
      const [runPayload, latestAssembly] = await Promise.all([
        generationApi.runs(this.sessionId, controller.signal),
        generationApi.latestAssembly(this.sessionId, controller.signal)
      ]);
      if (controller.signal.aborted) {
        return {
          selectedRunId: options.selectedRunId,
          shouldExpand: false
        };
      }
      const runs = runPayload.items;
      const activeRun = runs.find((item) =>
        ['queued', 'running', 'pausing', 'pause_requested', 'cancel_requested', 'paused']
          .includes(item.status)
      ) ?? runs[0] ?? null;
      const selectedRunId = options.selectedRunId
        && runs.some((item) => item.id === options.selectedRunId)
        ? options.selectedRunId
        : activeRun?.id ?? '';
      if (selectedRunId) query.set('generation_run_id', selectedRunId);
      const next = await generationApi.segments(
        this.sessionId,
        query,
        controller.signal
      );
      if (controller.signal.aborted) {
        return { selectedRunId, shouldExpand: false };
      }
      let payload = next;
      if (!reset) {
        const known = new Set(this.payload.items.map((item) => item.id));
        payload = {
          ...next,
          items: [
            ...this.payload.items,
            ...next.items.filter((item) => !known.has(item.id))
          ]
        };
      } else if (
        preserveLoaded
        && this.loadedFilter === filter
        && this.payload.plan_revision_id === next.plan_revision_id
        && this.payload.items.length > next.items.length
      ) {
        const incoming = new Map(next.items.map((item) => [item.id, item]));
        const lastOrdinal = next.items.at(-1)?.ordinal ?? -1;
        payload = {
          ...next,
          items: [
            ...next.items,
            ...this.payload.items.filter(
              (item) => item.ordinal > lastOrdinal && !incoming.has(item.id)
            )
          ],
          next_cursor: this.payload.next_cursor
        };
      }
      this.payload = payload;
      this.runs = runs;
      this.activeRun = activeRun;
      this.assembly = latestAssembly.item;
      this.loadedFilter = filter;
      const shouldExpand = this.initialized && (
        (previousTotal === 0 && payload.total > 0)
        || (
          activeRun?.id
          && activeRun.id !== previousRunId
          && ['queued', 'running', 'pausing'].includes(activeRun.status)
        )
      );
      this.initialized = true;
      this.status = payload.total ? 'ready' : 'empty';
      return { selectedRunId, shouldExpand: Boolean(shouldExpand) };
    } catch (caught) {
      if (controller.signal.aborted) {
        return {
          selectedRunId: options.selectedRunId,
          shouldExpand: false
        };
      }
      this.error = caught instanceof Error ? caught.message : String(caught);
      this.status = 'failed';
      throw caught;
    } finally {
      if (this.controller === controller) this.controller = undefined;
    }
  }

  async updateSegment(
    item: GenerationSegment,
    changes: GenerationSegmentChanges
  ) {
    const updated = await generationApi.updateSegment(item, changes);
    this.payload = {
      ...this.payload,
      items: this.payload.items.map((candidate) =>
        candidate.id === updated.id ? updated : candidate
      )
    };
    return updated;
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
    this.runs = [
      run,
      ...this.runs.filter((item) => item.id !== run.id)
    ].sort((left, right) => right.sequence_number - left.sequence_number);
    this.activeRun = run;
  }

  setAssembly(assembly: OutputAssembly | null) {
    this.assembly = assembly;
  }

  removeRun(runId: string) {
    this.runs = this.runs.filter((item) => item.id !== runId);
    if (this.activeRun?.id === runId) {
      this.activeRun = this.runs[0] ?? null;
    }
  }

  connect(
    getOptions: () => GenerationLoadOptions,
    onLoaded: (result: GenerationLoadResult) => void
  ) {
    if (this.unsubscribe) return this.unsubscribe;
    this.unsubscribe = invalidationBus.subscribe((batch) => {
      this.patchLiveProgress(batch);
      if (
        invalidates(batch, 'generation', this.sessionId)
        || invalidates(batch, 'output', this.sessionId)
      ) {
        this.load({
          ...getOptions(),
          reset: true,
          preserveLoaded: true
        }).then(onLoaded).catch(() => undefined);
      }
    });
    return () => {
      this.unsubscribe?.();
      this.unsubscribe = undefined;
      this.controller?.abort();
    };
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
      ...(event.progress !== undefined ? { progress: Number(event.progress) } : {}),
      ...(event.detail !== undefined ? { progress_detail: event.detail } : {}),
      ...(['queued', 'running', 'cancel_requested'].includes(String(event.status ?? ''))
        ? { status: String(event.status) }
        : {})
    });
    for (const event of relevant) {
      const generationRunId = String(event.generation_run_id ?? '');
      const assemblyId = String(event.output_assembly_id ?? '');
      this.runs = this.runs.map((item) => {
        let next = item;
        if (
          (generationRunId && item.id === generationRunId)
          || (event.job_id && item.job_id === event.job_id)
        ) {
          next = patch(next, event);
        }
        if (
          next.assembly
          && (
            (assemblyId && next.assembly.id === assemblyId)
            || (event.job_id && next.assembly.job_id === event.job_id)
          )
        ) {
          next = { ...next, assembly: patch(next.assembly, event) };
        }
        return next;
      });
      if (
        this.activeRun
        && (
          (generationRunId && this.activeRun.id === generationRunId)
          || (event.job_id && this.activeRun.job_id === event.job_id)
        )
      ) {
        this.activeRun = patch(this.activeRun, event);
      }
      if (
        this.assembly
        && (
          (assemblyId && this.assembly.id === assemblyId)
          || (event.job_id && this.assembly.job_id === event.job_id)
        )
      ) {
        this.assembly = patch(this.assembly, event);
      }
    }
  }
}
