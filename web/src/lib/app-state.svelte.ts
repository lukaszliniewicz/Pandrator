import { api, exchangeBootstrapToken, setCsrfToken, type JobRecord, type SessionRecord } from './api';

const EVENT_TYPES = [
  'job.queued', 'job.started', 'job.reclaimed', 'job.waiting_for_resource', 'job.progress',
  'job.succeeded', 'job.failed', 'job.retry_scheduled', 'job.cancel_requested', 'job.canceled'
];

class AppState {
  authenticated = $state(false);
  initialized = $state(false);
  loading = $state(true);
  error = $state('');
  sessions = $state<SessionRecord[]>([]);
  jobs = $state<JobRecord[]>([]);
  capabilities = $state<Record<string, any>>({});
  sidebarCollapsed = $state(false);
  setupReturnVisible = $state(false);
  setupGuidance = $state('');
  private events?: EventSource;
  private refreshTimer?: number;

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
        await this.refresh();
        this.connectEvents();
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
    await this.refresh();
    this.connectEvents();
  }

  async logout() {
    await api('/auth/logout', { method: 'POST' });
    this.events?.close();
    this.events = undefined;
    this.authenticated = false;
    this.sessions = [];
    this.jobs = [];
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

  async refreshCapabilities() {
    this.capabilities = await api<Record<string, any>>('/capabilities');
  }

  upsertSession(record: SessionRecord) {
    this.sessions = [record, ...this.sessions.filter((item) => item.id !== record.id)]
      .sort((left, right) => right.updated_at.localeCompare(left.updated_at));
  }

  showSetupReturn(guidance: string) {
    this.setupGuidance = guidance;
    this.setupReturnVisible = true;
  }

  private scheduleRefresh() {
    if (this.refreshTimer) window.clearTimeout(this.refreshTimer);
    this.refreshTimer = window.setTimeout(() => this.refresh().catch(() => undefined), 180);
  }

  private connectEvents() {
    this.events?.close();
    this.events = new EventSource('/api/v1/events');
    for (const type of EVENT_TYPES) this.events.addEventListener(type, () => this.scheduleRefresh());
  }
}

export const appState = new AppState();
