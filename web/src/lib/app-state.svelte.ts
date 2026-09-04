import { errorMessage } from './errors';
import { exchangeBootstrapToken, setCsrfToken } from './api';
import { appApi } from './domain-api';
import type {
  JobRecord,
  LoadState,
  RuntimeCapabilities,
  SessionRecord
} from './api-models';
import {
  InvalidationCoordinator,
  INVALIDATION_RESOURCES,
  invalidationBus,
  type InvalidationBatch,
  type InvalidationResource,
  type PandratorServerEvent
} from './invalidation';
import { ResourceState } from './resource-state.svelte';

const EVENT_TYPES = [
  'job.queued',
  'job.started',
  'job.reclaimed',
  'job.waiting_for_resource',
  'job.progress',
  'job.succeeded',
  'job.failed',
  'job.retry_scheduled',
  'job.cancel_requested',
  'job.canceled'
];

const KNOWN_INVALIDATIONS = new Set<string>(INVALIDATION_RESOURCES);

class AppState {
  authenticated = $state(false);
  initialized = $state(false);
  loading = $state(true);
  snapshotLoading = $state(false);
  error = $state('');
  readonly sessionsResource = new ResourceState<SessionRecord[]>([]);
  readonly jobsResource = new ResourceState<JobRecord[]>([]);
  readonly capabilitiesResource = new ResourceState<RuntimeCapabilities>({});
  eventsHealthy = $state(false);
  remoteAccess = $state(false);
  securityWarning = $state('');
  sidebarCollapsed = $state(false);
  setupReturnVisible = $state(false);
  setupGuidance = $state('');
  private events?: EventSource;
  private eventCursor = 0;
  private unhealthyTimer?: number;
  private fallbackTimer?: number;
  private resyncing = false;
  private authenticationRevision = 0;
  private invalidations = new InvalidationCoordinator((batch) =>
    this.flushInvalidations(batch)
  );

  get sessions() {
    return this.sessionsResource.value;
  }

  get sessionsState(): LoadState {
    return this.sessionsResource.status;
  }

  get jobs() {
    return this.jobsResource.value;
  }

  get jobsState(): LoadState {
    return this.jobsResource.status;
  }

  get capabilities() {
    return this.capabilitiesResource.value;
  }

  get capabilitiesState(): LoadState {
    return this.capabilitiesResource.status;
  }

  async initialize() {
    if (this.initialized) return;
    this.loading = true;
    try {
      const hash = new URLSearchParams(location.hash.slice(1));
      const bootstrap = hash.get('bootstrap');
      if (bootstrap) {
        await exchangeBootstrapToken(bootstrap);
        history.replaceState({}, '', location.pathname + location.search);
      }
      const status = await appApi.authStatus();
      this.authenticated = status.authenticated;
      this.authenticationRevision += 1;
      this.remoteAccess = Boolean(status.remote_access);
      this.securityWarning = String(status.security_warning ?? '');
      setCsrfToken(status.csrf_token);
      if (this.authenticated) {
        void this.synchronizeAuthenticatedState(this.authenticationRevision);
      }
    } catch (caught) {
      this.error = errorMessage(caught);
    } finally {
      this.loading = false;
      this.initialized = true;
    }
  }

  async login(password: string) {
    const result = await appApi.login(password);
    setCsrfToken(result.csrf_token);
    this.error = '';
    this.authenticated = true;
    this.authenticationRevision += 1;
    await this.synchronizeAuthenticatedState(this.authenticationRevision);
  }

  async logout() {
    await appApi.logout();
    this.disconnectEvents();
    this.authenticated = false;
    this.authenticationRevision += 1;
    this.snapshotLoading = false;
    this.sessionsResource.reset([]);
    this.jobsResource.reset([]);
    this.capabilitiesResource.reset({});
  }

  async refresh() {
    const [sessions, jobs, capabilities] = await Promise.all([
      appApi.sessions(),
      appApi.jobs(),
      appApi.capabilities()
    ]);
    this.sessionsResource.replace(sessions.items, sessions.items.length === 0);
    this.jobsResource.replace(jobs.items, jobs.items.length === 0);
    this.capabilitiesResource.replace(capabilities);
  }

  async refreshSessions() {
    await this.sessionsResource.load(
      async () => (await appApi.sessions()).items,
      { force: true, empty: (items) => items.length === 0 }
    );
  }

  async refreshJobs() {
    await this.jobsResource.load(async () => (await appApi.jobs()).items, {
      force: true,
      empty: (items) => items.length === 0
    });
  }

  async refreshCapabilities() {
    await this.capabilitiesResource.load(() => appApi.capabilities(true), {
      force: true
    });
  }

  upsertSession(record: SessionRecord) {
    const sessions = [
      record,
      ...this.sessions.filter((item) => item.id !== record.id)
    ].sort((left, right) => right.updated_at.localeCompare(left.updated_at));
    this.sessionsResource.replace(sessions);
  }

  showSetupReturn(guidance: string) {
    this.setupGuidance = guidance;
    this.setupReturnVisible = true;
  }

  private async loadEventSnapshot(authenticationRevision?: number) {
    const snapshot = await appApi.eventSnapshot();
    if (
      authenticationRevision !== undefined &&
      (!this.authenticated ||
        authenticationRevision !== this.authenticationRevision)
    )
      return false;
    this.sessionsResource.replace(
      snapshot.sessions.items,
      snapshot.sessions.items.length === 0
    );
    this.jobsResource.replace(
      snapshot.jobs.items,
      snapshot.jobs.items.length === 0
    );
    this.capabilitiesResource.replace(snapshot.capabilities);
    this.eventCursor = Number(snapshot.cursor || 0);
    return true;
  }

  private async synchronizeAuthenticatedState(authenticationRevision: number) {
    this.snapshotLoading = true;
    try {
      if (await this.loadEventSnapshot(authenticationRevision))
        this.connectEvents(this.eventCursor);
    } catch (caught) {
      if (authenticationRevision !== this.authenticationRevision) return;
      this.error = errorMessage(caught);
      this.eventsHealthy = false;
      this.scheduleFallbackRefresh();
    } finally {
      if (authenticationRevision === this.authenticationRevision)
        this.snapshotLoading = false;
    }
  }

  private patchJob(event: PandratorServerEvent) {
    if (!event.job_id) return;
    const jobId = String(event.job_id);
    const index = this.jobs.findIndex((job) => job.id === jobId);
    const existing = index >= 0 ? this.jobs[index] : undefined;
    const next: JobRecord = {
      ...(existing ?? {
        id: jobId,
        kind: String(event.job_kind ?? 'background'),
        status: String(event.status ?? 'queued'),
        progress: Number(event.progress ?? 0),
        created_at: String(event.created_at ?? new Date().toISOString())
      }),
      ...(event.job_kind ? { kind: String(event.job_kind) } : {}),
      ...(event.session_id !== undefined
        ? { session_id: event.session_id }
        : {}),
      ...(event.status ? { status: String(event.status) } : {}),
      ...(event.progress !== undefined
        ? { progress: Number(event.progress) }
        : {}),
      ...(event.detail !== undefined ? { progress_detail: event.detail } : {})
    };
    const jobs =
      index >= 0
        ? this.jobs.map((job, jobIndex) => (jobIndex === index ? next : job))
        : [next, ...this.jobs].slice(0, 40);
    this.jobsResource.replace(jobs, jobs.length === 0);
  }

  private flushInvalidations(batch: InvalidationBatch) {
    const terminal = batch.events.some((event) =>
      [
        'job.succeeded',
        'job.failed',
        'job.retry_scheduled',
        'job.canceled'
      ].includes(event.type)
    );
    if (terminal && batch.resources.includes('jobs')) {
      this.refreshJobs().catch(() => undefined);
    }
    if (batch.resources.includes('sessions')) {
      this.refreshSessions().catch(() => undefined);
    }
    invalidationBus.publish(batch);
  }

  private markEventsHealthy() {
    if (this.unhealthyTimer) window.clearTimeout(this.unhealthyTimer);
    this.unhealthyTimer = undefined;
    if (this.fallbackTimer) window.clearTimeout(this.fallbackTimer);
    this.fallbackTimer = undefined;
    this.eventsHealthy = true;
  }

  private deferEventsUnhealthy(source: EventSource) {
    if (this.unhealthyTimer) window.clearTimeout(this.unhealthyTimer);
    this.unhealthyTimer = window.setTimeout(() => {
      if (this.events !== source) return;
      this.eventsHealthy = false;
      this.scheduleFallbackRefresh();
    }, 3000);
  }

  private scheduleFallbackRefresh() {
    if (this.fallbackTimer || !this.authenticated || this.eventsHealthy) return;
    this.fallbackTimer = window.setTimeout(async () => {
      this.fallbackTimer = undefined;
      if (!this.authenticated || this.eventsHealthy) return;
      await Promise.all([
        this.refreshSessions().catch(() => undefined),
        this.refreshJobs().catch(() => undefined)
      ]);
      this.scheduleFallbackRefresh();
    }, 15000);
  }

  private async resynchronizeEvents(source: EventSource) {
    if (this.events !== source || this.resyncing) return;
    this.resyncing = true;
    source.close();
    try {
      if (await this.loadEventSnapshot(this.authenticationRevision))
        this.connectEvents(this.eventCursor);
    } catch {
      this.eventsHealthy = false;
      this.scheduleFallbackRefresh();
    } finally {
      this.resyncing = false;
    }
  }

  private disconnectEvents() {
    this.events?.close();
    this.events = undefined;
    if (this.unhealthyTimer) window.clearTimeout(this.unhealthyTimer);
    if (this.fallbackTimer) window.clearTimeout(this.fallbackTimer);
    this.unhealthyTimer = undefined;
    this.fallbackTimer = undefined;
    this.eventsHealthy = false;
    this.invalidations.dispose();
  }

  private connectEvents(cursor: number) {
    this.events?.close();
    const source = new EventSource(
      `/api/v1/events?after=${encodeURIComponent(cursor)}`
    );
    this.events = source;
    source.onopen = () => this.markEventsHealthy();
    source.onerror = () => this.deferEventsUnhealthy(source);
    source.addEventListener('stream.reset', () => {
      this.resynchronizeEvents(source).catch(() => undefined);
    });
    for (const type of EVENT_TYPES) {
      source.addEventListener(type, (event) => {
        this.markEventsHealthy();
        const message = event as MessageEvent;
        const parsedCursor = Number(message.lastEventId || 0);
        if (Number.isFinite(parsedCursor) && parsedCursor > this.eventCursor) {
          this.eventCursor = parsedCursor;
        }
        const detail: PandratorServerEvent = { type };
        try {
          const parsed = JSON.parse(message.data || '{}') as unknown;
          if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
            for (const [key, value] of Object.entries(parsed))
              detail[key] = value;
            if (Array.isArray(detail.changed_entities)) {
              detail.changed_entities = detail.changed_entities.filter(
                (value): value is InvalidationResource =>
                  typeof value === 'string' && KNOWN_INVALIDATIONS.has(value)
              );
            } else {
              delete detail.changed_entities;
            }
          }
        } catch {
          /* retain the event type if an older server sent no JSON */
        }
        this.patchJob(detail);
        this.invalidations.enqueue(detail);
      });
    }
  }
}

export const appState = new AppState();
