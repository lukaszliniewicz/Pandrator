import { appState } from './app-state.svelte';
import { SessionStore } from './session-store.svelte';
import { WorkflowStore } from './workflow-store.svelte';
import type { SessionContext } from './session-context';
import type { SessionRecord } from './api-models';
import { invalidates, invalidationBus } from './invalidation';
import { TranslationProjectStore } from './translation-project-store.svelte';
import { SvelteMap } from 'svelte/reactivity';

export const PROJECT_WORKSPACE = Symbol('pandrator-project-workspace');

export class WorkspaceScope {
  readonly record: SessionStore;
  readonly workflow: WorkflowStore;
  readonly context: SessionContext;
  readonly languages: TranslationProjectStore;

  constructor(
    readonly id: string,
    customize: () => void
  ) {
    this.record = new SessionStore(id, (session) =>
      appState.upsertSession(session)
    );
    this.workflow = new WorkflowStore(id);
    this.languages = new TranslationProjectStore(id);
    const record = this.record;
    this.context = {
      get session() {
        return record.session;
      },
      get outcome() {
        return record.outcome;
      },
      get status() {
        return record.status;
      },
      get loading() {
        return record.loading;
      },
      get error() {
        return record.error;
      },
      workflow: this.workflow,
      reload: async () => {
        await record.load(true);
      },
      customize
    };
  }

  connect() {
    const record = this.record.connect();
    const workflow = this.workflow.connect();
    return () => {
      record();
      workflow();
    };
  }
}

/** Cache only scopes the user opens; inactive languages never fetch themselves. */
export class ProjectWorkspace {
  selectedId = $state('');
  private readonly scopes = new SvelteMap<string, WorkspaceScope>();

  constructor(
    id: string,
    private readonly customize: () => void
  ) {
    this.scope(id);
    this.selectedId = id;
  }

  private scope(id: string) {
    let scope = this.scopes.get(id);
    if (!scope) {
      scope = new WorkspaceScope(id, this.customize);
      this.scopes.set(id, scope);
    }
    return scope;
  }

  select(id: string) {
    this.scope(id);
    this.selectedId = id;
  }

  get selected() {
    return this.scopes.get(this.selectedId)!;
  }
  get membership(): SessionRecord['translation_project'] {
    const known =
      this.selected.record.session?.translation_project ??
      appState.sessions.find((session) => session.id === this.selectedId)
        ?.translation_project;
    if (known) return known;
    // A newly added language may precede the global library refresh. Resolve
    // its source immediately from compact metadata to keep source cards mounted.
    for (const scope of this.scopes.values()) {
      const project = scope.languages.value?.project;
      if (!project) continue;
      const branch = project.branches.find(
        (item) => item.session_id === this.selectedId
      );
      if (branch || project.source_session_id === this.selectedId)
        return {
          id: project.id,
          name: project.name,
          source_session_id: project.source_session_id,
          role: branch ? 'branch' : 'source',
          ...(branch ? { target_language: branch.target_language } : {})
        };
    }
    return null;
  }
  get sourceId() {
    return this.membership?.source_session_id ?? this.selectedId;
  }
  get source() {
    return this.scopes.get(this.sourceId) ?? this.selected;
  }

  async load(force = false) {
    const selected = this.selected;
    const source = this.scope(this.sourceId);
    await Promise.all([
      selected.record.load(force),
      ...(source !== selected ? [source.record.load(false)] : [])
    ]);
    // Membership may arrive in the detail response before the library snapshot.
    if (selected.id === this.selectedId && this.sourceId !== source.id)
      await this.scope(this.sourceId).record.load(false);
  }

  /** Track stale cached scopes without loading inactive language histories. */
  connectCache() {
    return invalidationBus.subscribe((batch) => {
      for (const [id, scope] of this.scopes) {
        if (id === this.selectedId || id === this.sourceId) continue;
        if (
          invalidates(batch, 'sessions', id) ||
          invalidates(batch, 'workflow', id)
        )
          scope.record.markStale();
        if (
          invalidates(batch, 'workflow', id) ||
          invalidates(batch, 'sources', id) ||
          invalidates(batch, 'jobs', id) ||
          invalidates(batch, 'output', id)
        )
          scope.workflow.markStale();
        if (
          invalidates(batch, 'sessions', id) ||
          invalidates(batch, 'workflow', id)
        )
          scope.languages.markStale();
      }
    });
  }
}
