import { api, exchangeBootstrapToken, setCsrfToken, type JobRecord, type SessionRecord } from './api';
import {
  InvalidationCoordinator,
  type InvalidationBatch,
  type PandratorServerEvent
} from './invalidation';

const EVENT_TYPES = [
  'job.queued', 'job.started', 'job.reclaimed', 'job.waiting_for_resource', 'job.progress',
  'job.succeeded', 'job.failed', 'job.retry_scheduled', 'job.cancel_requested', 'job.canceled'
];

type EventSnapshot = {
  cursor: number;
  retained_after: number;
  sessions: { items: SessionRecord[] };
  jobs: { items: JobRecord[] };
  capabilities: Record<string, any>;
};

class AppState {
  authenticated = $state(false);
  initialized = $state(false);
  loading = $state(true);
  error = $state('');
  sessions = $state<SessionRecord[]>([]);
  jobs = $state<JobRecord[]>([]);
  capabilities = $state<Record<string, any>>({});
  eventsHealthy = $state(false);
  sidebarCollapsed = $state(false);
  setupReturnVisible = $state(false);
  setupGuidance = $state('');
  private events?: EventSource;
  private eventCursor = 0;
  private unhealthyTimer?: number;
  private fallbackTimer?: number;
  private resyncing = false;
  private invalidations = new InvalidationCoordinator(
    (batch) => this.flushInvalidations(batch)
  );

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
      const status = await api<{ authenticated: boolean; initialized: boolean; csrf_token?: string }>('/auth/status');
      this.authenticated = status.authenticated;
      setCsrfToken(status.csrf_token);
      if (this.authenticated) {
        await this.loadEventSnapshot();
        this.connectEvents(this.eventCursor);
      }
    } catch (caught) {
      this.error = caught instanceof Error ? caught.message : String(caught);
    } finally {
      this.loading = false;
      this.initialized = true;
    }
  }

  async login(password: string) {
    const result = await api<{ authenticated: boolean; csrf_token: string }>('/auth/login', {
      method: 'POST', body: JSON.stringify({ password })
    });
    setCsrfToken(result.csrf_token);
    this.authenticated = true;
    await this.loadEventSnapshot();
    this.connectEvents(this.eventCursor);
  }

  async logout() {
    await api('/auth/logout', { method: 'POST' });
    this.disconnectEvents();
    this.authenticated = false;
    this.sessions = [];
    this.jobs = [];
    this.capabilities = {};
  }

  async refresh() {
    const [sessions, jobs, capabilities] = await Promise.all([
      api<{ items: SessionRecord[] }>('/sessions'),
      api<{ items: JobRecord[] }>('/jobs?limit=40'),
      api<Record<string, any>>('/capabilities')
    ]);
    this.sessions = sessions.items;
    this.jobs = jobs.items;
    this.capabilities = capabilities;
  }

  async refreshSessions() {
    const response = await api<{ items: SessionRecord[] }>('/sessions');
    this.sessions = response.items;
  }

  async refreshJobs() {
    const response = await api<{ items: JobRecord[] }>('/jobs?limit=40');
    this.jobs = response.items;
  }

  async refreshCapabilities() {
    this.capabilities = await api<Record<string, any>>('/capabilities?refresh=true');
  }

  upsertSession(record: SessionRecord) {
    this.sessions = [record, ...this.sessions.filter((item) => item.id !== record.id)]
      .sort((left, right) => right.updated_at.localeCompare(left.updated_at));
  }

  showSetupReturn(guidance: string) {
    this.setupGuidance = guidance;
    this.setupReturnVisible = true;
  }

  private async loadEventSnapshot() {
    const snapshot = await api<EventSnapshot>('/events/snapshot');
    this.sessions = snapshot.sessions.items;
    this.jobs = snapshot.jobs.items;
    this.capabilities = snapshot.capabilities;
    this.eventCursor = Number(snapshot.cursor || 0);
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
      ...(event.session_id !== undefined ? { session_id: event.session_id } : {}),
      ...(event.status ? { status: String(event.status) } : {}),
      ...(event.progress !== undefined ? { progress: Number(event.progress) } : {}),
      ...(event.detail !== undefined ? { progress_detail: event.detail } : {})
    };
    this.jobs = index >= 0
      ? this.jobs.map((job, jobIndex) => jobIndex === index ? next : job)
      : [next, ...this.jobs].slice(0, 40);
  }

  private flushInvalidations(batch: InvalidationBatch) {
    const terminal = batch.events.some((event) =>
      ['job.succeeded', 'job.failed', 'job.retry_scheduled', 'job.canceled'].includes(event.type)
    );
    if (terminal && batch.resources.includes('jobs')) {
      this.refreshJobs().catch(() => undefined);
    }
    if (batch.resources.includes('sessions')) {
      this.refreshSessions().catch(() => undefined);
    }
    window.dispatchEvent(
      new CustomEvent<InvalidationBatch>('pandrator:invalidate', { detail: batch })
    );
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
      await this.loadEventSnapshot();
      if (this.authenticated) this.connectEvents(this.eventCursor);
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
    const source = new EventSource(`/api/v1/events?after=${encodeURIComponent(cursor)}`);
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
        let detail: PandratorServerEvent = { type };
        try {
          detail = { type, ...JSON.parse(message.data || '{}') };
        } catch { /* retain the event type if an older server sent no JSON */ }
        this.patchJob(detail);
        this.invalidations.enqueue(detail);
      });
    }
  }
}

export const appState = new AppState();
