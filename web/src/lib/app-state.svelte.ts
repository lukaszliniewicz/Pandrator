import { isJobStatus } from './job-status';
import { errorMessage } from './errors';
import { exchangeBootstrapToken, setCsrfToken } from './api';
import { appApi } from './domain-api';
import type {
  AuthStatus,
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
import { browserStorageKey, setBrowserIdentity } from './browser-identity';
import type { WorkspaceRecord } from './workspace-api';
import {
  activeProjectLink,
  clearProjectLink,
  projectLinkUrl
} from './project-link-context';

const EVENT_TYPES = [
  'job.queued',
  'job.started',
  'job.reclaimed',
  'job.waiting_for_resource',
  'job.waiting_for_storage',
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
  instanceId = $state('');
  collaborationEnabled = $state(false);
  collaborationAvailable = $state(false);
  installationRevision = $state(0);
  publicLinksEnabled = $state(false);
  publicLinksConfigured = $state(false);
  linkContext = $state<ReturnType<typeof activeProjectLink>>(null);
  administratorPasswordConfigured = $state(false);
  workspaces = $state<WorkspaceRecord[]>([]);
  activeWorkspaceId = $state('');
  workspacesReady = $state(false);
  workspaceSwitching = $state(false);
  workspaceError = $state('');
  principal = $state<AuthStatus['principal']>(null);
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
  mobileSessionNavigation = $state<{
    sessionId: string;
    title: string;
    items: { href: string; label: string }[];
  } | null>(null);
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
    return this.collaborationEnabled && this.activeWorkspaceId
      ? this.sessionsResource.value.filter(
          (item) => item.workspace_id === this.activeWorkspaceId
        )
      : this.sessionsResource.value;
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

  get isAdministrator() {
    return (
      this.authenticated &&
      this.principal?.kind !== 'project_link' &&
      this.principal?.role === 'administrator' &&
      this.principal.scopes.includes('app.admin')
    );
  }

  get activeWorkspace() {
    return this.workspaces.find((item) => item.id === this.activeWorkspaceId);
  }

  canManageWorkspace(workspaceId?: string) {
    if (this.projectLink) return false;
    if (!this.collaborationEnabled) return this.authenticated;
    return Boolean(
      workspaceId &&
      this.workspaces
        .find((item) => item.id === workspaceId)
        ?.allowed_actions.includes('manage')
    );
  }

  get identityKey() {
    const identity = `${this.instanceId}:${this.principal?.user_id ?? ''}:${this.principal?.auth_revision ?? 0}`;
    return this.projectLink
      ? `${identity}:link:${this.projectLink.id}`
      : identity;
  }

  get projectLink() {
    return this.linkContext;
  }

  get contentKey() {
    return `${this.identityKey}:${this.collaborationEnabled ? this.activeWorkspaceId : 'solo'}`;
  }

  async refreshWorkspaces(deferReady = false) {
    if (this.projectLink) {
      this.workspacesReady = true;
      return;
    }
    if (!this.collaborationEnabled || !this.authenticated) return;
    const revision = this.authenticationRevision;
    this.workspaceError = '';
    try {
      const { workspaceApi } = await import('./workspace-api');
      const result = await workspaceApi.list();
      if (revision !== this.authenticationRevision || !this.authenticated)
        return;
      this.workspaces = result.items;
      const remembered = (() => {
        try {
          return (
            localStorage.getItem(browserStorageKey('active-workspace')) ?? ''
          );
        } catch {
          return '';
        }
      })();
      const requested =
        location.pathname === '/sessions'
          ? new URLSearchParams(location.search).get('workspace')
          : null;
      const selected = requested || this.activeWorkspaceId || remembered;
      this.activeWorkspaceId =
        result.items.find((item) => item.id === selected)?.id ??
        result.items.find((item) => item.kind === 'personal')?.id ??
        '';
      if (!deferReady) this.workspacesReady = true;
      this.adoptSessionWorkspace(location.pathname.split('/')[2] ?? '');
    } catch (caught) {
      if (revision === this.authenticationRevision)
        this.workspaceError = errorMessage(caught);
      throw caught;
    }
  }

  selectWorkspace(id: string) {
    if (
      !this.workspaces.some((item) => item.id === id && item.state === 'active')
    )
      return;
    this.activeWorkspaceId = id;
    try {
      localStorage.setItem(browserStorageKey('active-workspace'), id);
    } catch {
      /* Optional preference. */
    }
    this.mobileSessionNavigation = null;
  }

  workspaceForSession(sessionId: string) {
    return this.sessionsResource.value.find((item) => item.id === sessionId)
      ?.workspace_id;
  }

  adoptSessionWorkspace(sessionId: string) {
    if (!this.collaborationEnabled) return;
    const item = this.sessionsResource.value.find(
      (item) => item.id === sessionId
    );
    if (item?.workspace_id) this.selectWorkspace(item.workspace_id);
  }

  private applyIdentity(status: AuthStatus) {
    const next = status.authenticated ? (status.principal ?? null) : null;
    this.linkContext = activeProjectLink();
    if (
      this.principal?.user_id !== next?.user_id ||
      this.principal?.kind !== next?.kind ||
      this.principal?.auth_revision !== next?.auth_revision
    ) {
      this.authenticationRevision += 1;
      this.disconnectEvents();
      this.sessionsResource.reset([]);
      this.jobsResource.reset([]);
      this.capabilitiesResource.reset({});
      this.workspaces = [];
      this.activeWorkspaceId = '';
      this.workspacesReady = false;
      this.workspaceSwitching = false;
      this.mobileSessionNavigation = null;
    }
    this.instanceId = status.instance_id ?? this.instanceId;
    this.collaborationEnabled = Boolean(
      status.installation?.collaboration_enabled
    );
    this.collaborationAvailable = Boolean(
      status.installation?.collaboration_available
    );
    this.installationRevision = status.installation?.revision ?? 0;
    this.publicLinksEnabled = Boolean(status.public_links?.enabled);
    this.publicLinksConfigured = Boolean(status.public_links?.configured);
    this.administratorPasswordConfigured = Boolean(status.initialized);
    setBrowserIdentity(
      this.instanceId,
      this.projectLink ? `link:${this.projectLink.id}` : (next?.user_id ?? ''),
      next?.auth_revision ?? 0
    );
    this.authenticated = status.authenticated;
    this.principal = status.authenticated ? (status.principal ?? null) : null;
    this.remoteAccess = Boolean(status.remote_access);
    this.securityWarning = String(status.security_warning ?? '');
    setCsrfToken(status.csrf_token);
  }

  async refreshIdentity() {
    const revision = this.authenticationRevision;
    const before = this.identityKey;
    const wasCollaboration = this.collaborationEnabled;
    const status = await appApi.authStatus();
    if (revision !== this.authenticationRevision) return;
    this.applyIdentity(status);
    if (
      this.authenticated &&
      (before !== this.identityKey ||
        (!wasCollaboration && this.collaborationEnabled))
    ) {
      await this.synchronizeAuthenticatedState(this.authenticationRevision);
    }
    if (!this.authenticated) {
      this.disconnectEvents();
      this.authenticationRevision += 1;
      this.snapshotLoading = false;
      this.sessionsResource.reset([]);
      this.jobsResource.reset([]);
      this.capabilitiesResource.reset({});
    }
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
      this.applyIdentity(status);
      this.authenticationRevision += 1;
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

  async login(password: string, loginName?: string) {
    const result = await appApi.login(password, loginName);
    setCsrfToken(result.csrf_token);
    this.applyIdentity(await appApi.authStatus());
    if (!this.authenticated)
      throw new Error(
        'The sign-in session is no longer valid. Please sign in again.'
      );
    this.error = '';
    this.authenticationRevision += 1;
    await this.synchronizeAuthenticatedState(this.authenticationRevision);
  }

  async logout() {
    const leavingLink = Boolean(this.projectLink);
    if (this.projectLink) {
      const { publicLinkApi } = await import('./public-link-api');
      await publicLinkApi.leave(this.projectLink.id);
      clearProjectLink();
    } else await appApi.logout();
    this.suspendIdentity();
    if (leavingLink) await this.initialize();
  }

  suspendIdentity() {
    this.disconnectEvents();
    this.initialized = false;
    this.authenticated = false;
    this.linkContext = null;
    this.principal = null;
    this.workspaces = [];
    this.activeWorkspaceId = '';
    this.workspacesReady = false;
    this.mobileSessionNavigation = null;
    setBrowserIdentity(this.instanceId, '', 0);
    setCsrfToken(null);
    this.authenticationRevision += 1;
    this.snapshotLoading = false;
    this.sessionsResource.reset([]);
    this.jobsResource.reset([]);
    this.capabilitiesResource.reset({});
  }

  async refresh() {
    const revision = this.authenticationRevision;
    const [sessions, jobs, capabilities] = await Promise.all([
      appApi.sessions(),
      appApi.jobs(),
      appApi.capabilities()
    ]);
    if (revision !== this.authenticationRevision || !this.authenticated) return;
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
      {
        ...this.sessionsResource.value.find((item) => item.id === record.id),
        ...record
      },
      ...this.sessionsResource.value.filter((item) => item.id !== record.id)
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

  async retryWorkspaceLoad() {
    await this.synchronizeAuthenticatedState(this.authenticationRevision);
  }

  private async synchronizeAuthenticatedState(authenticationRevision: number) {
    this.snapshotLoading = true;
    try {
      const [loaded] = await Promise.all([
        this.loadEventSnapshot(authenticationRevision),
        this.collaborationEnabled
          ? this.refreshWorkspaces(true)
          : Promise.resolve()
      ]);
      if (loaded) {
        if (this.collaborationEnabled)
          this.adoptSessionWorkspace(location.pathname.split('/')[2] ?? '');
        if (this.collaborationEnabled) this.workspacesReady = true;
        this.connectEvents(this.eventCursor);
      }
    } catch (caught) {
      if (authenticationRevision !== this.authenticationRevision) return;
      this.error = errorMessage(caught);
      if (this.collaborationEnabled) this.workspaceError = this.error;
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
        status: isJobStatus(event.status) ? event.status : 'queued',
        progress: Number(event.progress ?? 0),
        created_at: String(event.created_at ?? new Date().toISOString())
      }),
      ...(event.job_kind ? { kind: String(event.job_kind) } : {}),
      ...(event.session_id !== undefined
        ? { session_id: event.session_id }
        : {}),
      ...(isJobStatus(event.status) ? { status: event.status } : {}),
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
    const refreshJobSnapshot = batch.events.some((event) =>
      [
        'job.succeeded',
        'job.failed',
        'job.retry_scheduled',
        'job.waiting_for_storage',
        'job.canceled'
      ].includes(event.type)
    );
    if (refreshJobSnapshot && batch.resources.includes('jobs')) {
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
      try {
        await this.refreshIdentity();
        if (this.authenticated)
          await Promise.all([
            this.refreshSessions().catch(() => undefined),
            this.refreshJobs().catch(() => undefined)
          ]);
      } catch {
        /* Resume when authentication can be checked again. */
      }
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
      projectLinkUrl(`/api/v1/events?after=${encodeURIComponent(cursor)}`)
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
