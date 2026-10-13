<script lang="ts">
  import { page } from '$app/state';
  import { goto } from '$app/navigation';
  import { onDestroy, onMount, setContext, tick, untrack } from 'svelte';
  import type { Snippet } from 'svelte';
  import {
    Activity,
    AudioLines,
    ListMusic,
    Check,
    FileText,
    Folder,
    Layers3,
    Languages,
    Pencil,
    Settings2,
    Scissors,
    Sparkles,
    WandSparkles,
    X
  } from '@lucide/svelte';
  import { appState } from '$lib/app-state.svelte';
  import { languageLabel } from '$lib/language-registry';
  import { sessionApi, translationProjectApi } from '$lib/domain-api';
  import ProjectFolderAssignment from '$lib/ProjectFolderAssignment.svelte';
  import { ProjectFoldersStore } from '$lib/project-folders-store.svelte';
  import { folderPath, projectActivity } from '$lib/project-library';
  import {
    PROJECT_INTERFACE,
    ProjectInterface
  } from '$lib/project-interface.svelte';
  import { errorMessage } from '$lib/errors';
  import { invalidates, invalidationBus } from '$lib/invalidation';
  import { loadLazyModule } from '$lib/lazy-module';
  import {
    SESSION_CONTEXT,
    SOURCE_SESSION_CONTEXT,
    type SessionContext
  } from '$lib/session-context';
  import {
    PROJECT_WORKSPACE,
    ProjectWorkspace
  } from '$lib/project-workspace.svelte';
  import type WorkflowCustomizer from '$lib/WorkflowCustomizer.svelte';
  import GenerationStatusBar from '$lib/GenerationStatusBar.svelte';
  import {
    GENERATION_WORKSPACES,
    type GenerationWorkspaceState
  } from '$lib/generation-workspace-state.svelte';
  import { subscribeSpeechPlanEditor } from '$lib/session-flow';
  let { children }: { children: Snippet } = $props();
  let customizeOpen = $state(false);
  let WorkflowCustomizerComponent = $state<typeof WorkflowCustomizer | null>(
    null
  );
  const generationWorkspaces = new Map<string, GenerationWorkspaceState>();
  setContext(GENERATION_WORKSPACES, generationWorkspaces);
  onDestroy(() => {
    for (const state of generationWorkspaces.values()) state.dispose();
  });
  let customizerLoadFailed = $state(false);
  let loadingCustomizer = $state(false);
  let mounted = $state(false);
  let sourceProfile = $state('none');
  let editingName = $state(false);
  let nameDraft = $state('');
  let nameRevision = $state(0);
  let savingName = $state(false);
  let nameError = $state('');
  let nameInput = $state<HTMLInputElement>();
  let nameMutation: { signature: string; key: string } | null = null;
  let assigningFolders = $state(false);
  const projectFolders = new ProjectFoldersStore();
  const folderContextId = $derived(page.url.searchParams.get('folder') ?? '');
  const folderContext = $derived(
    projectFolders.value.items.find((folder) => folder.id === folderContextId)
  );
  const workspace = new ProjectWorkspace(
    page.params.id ?? '',
    () => void openWorkflowCustomizer()
  );
  const sessionStore = $derived(workspace.selected.record);
  const contextState: SessionContext = {
    get session() {
      return sessionStore.session;
    },
    get outcome() {
      return sessionStore.outcome;
    },
    get status() {
      return sessionStore.status;
    },
    get loading() {
      return sessionStore.loading;
    },
    get error() {
      return sessionStore.error;
    },
    get workflow() {
      return workspace.selected.workflow;
    },
    reload: async () => {
      await workspace.load(true);
      await loadSourceProfile();
    },
    customize: () => void openWorkflowCustomizer()
  };
  setContext(SESSION_CONTEXT, contextState);
  const sourceContext: SessionContext = {
    get session() {
      return workspace.source.record.session;
    },
    get outcome() {
      return workspace.source.record.outcome;
    },
    get status() {
      return workspace.source.record.status;
    },
    get loading() {
      return workspace.source.record.loading;
    },
    get error() {
      return workspace.source.record.error;
    },
    get workflow() {
      return workspace.source.workflow;
    },
    reload: async () => {
      await workspace.source.record.load(true);
    },
    customize: contextState.customize
  };
  setContext(SOURCE_SESSION_CONTEXT, sourceContext);
  const projectInterface = new ProjectInterface(
    () => sourceContext.session,
    () => sourceContext.reload()
  );
  setContext(PROJECT_INTERFACE, projectInterface);
  const simpleMode = $derived(projectInterface.mode === 'simple');
  setContext(PROJECT_WORKSPACE, workspace);
  // Declared before the initial reload(): loadSourceProfile() bumps this
  // counter synchronously, so a later declaration would throw (TDZ).
  let sourceProfileRequest = 0;
  let sourceProfileSessionId = '';
  void workspace.load().catch(() => undefined);
  const routeSessionId = $derived(page.params.id ?? '');
  const membership = $derived(workspace.membership);
  const sourceSessionId = $derived(workspace.sourceId);
  const sourceSession = $derived(sourceContext.session);
  const projectName = $derived(
    workspace.source.languages.value?.project?.name ||
      membership?.name ||
      sourceSession?.name ||
      'Project'
  );
  const languageVersions = $derived(
    appState.sessions
      .filter(
        (item) =>
          item.translation_project?.id === membership?.id && Boolean(membership)
      )
      .sort(
        (a, b) =>
          Number(b.id === sourceSessionId) - Number(a.id === sourceSessionId) ||
          languageLabel(a.target_language ?? a.source_language).localeCompare(
            languageLabel(b.target_language ?? b.source_language)
          )
      )
  );
  const sharedSections = new Set([
    '/sources',
    '/edit',
    '/languages',
    '/cleaning'
  ]);
  const currentSuffix = $derived(
    page.url.pathname.slice(`/sessions/${routeSessionId}`.length)
  );
  const languageContextId = $derived(
    sharedSections.has(currentSuffix) &&
      languageVersions.some(
        (version) => version.id === page.url.searchParams.get('language')
      )
      ? page.url.searchParams.get('language')!
      : routeSessionId
  );
  const languageContext = $derived(
    languageVersions.find((version) => version.id === languageContextId) ??
      sourceSession
  );
  $effect(() => {
    if (folderContextId)
      void untrack(() => projectFolders.load()).catch(() => undefined);
  });
  function navigationSearch(languageId?: string) {
    const params = new URLSearchParams();
    if (folderContextId) params.set('folder', folderContextId);
    if (languageId && languageId !== sourceSessionId)
      params.set('language', languageId);
    return params.size ? `?${params}` : '';
  }
  const sectionHref = (suffix: string) =>
    sharedSections.has(suffix)
      ? `/sessions/${sourceSessionId}${suffix}${navigationSearch(languageContextId)}`
      : `/sessions/${languageContextId}${suffix}${navigationSearch()}`;
  function switchLanguage(id: string) {
    if (!languageVersions.some((version) => version.id === id)) return;
    const suffix = page.url.pathname.slice(
      `/sessions/${routeSessionId}`.length
    );
    const version = languageVersions.find((item) => item.id === id);
    if (sharedSections.has(suffix)) {
      void goto(
        `/sessions/${sourceSessionId}${suffix}${navigationSearch(id)}`,
        { noScroll: true, keepFocus: true }
      );
      return;
    }
    const nextSuffix =
      ['/voice', '/generation'].includes(suffix) &&
      version?.workflow_kind === 'subtitles'
        ? '/text'
        : suffix;
    void goto(`/sessions/${id}${nextSuffix}${navigationSearch()}`, {
      noScroll: true,
      keepFocus: true
    });
  }
  $effect(() => {
    const id = routeSessionId;
    if (id !== workspace.selectedId) {
      for (const [sessionId, generation] of generationWorkspaces) {
        if (sessionId !== id) generation.dispose();
      }
      workspace.select(id);
      void untrack(() => workspace.load()).catch(() => undefined);
    }
  });
  $effect(() => {
    const source = workspace.source;
    const selected = workspace.selected;
    if (!mounted) return;
    const disconnectSource = source.connect();
    const disconnectSelected =
      selected !== source ? selected.connect() : () => {};
    const disconnectLanguages = source.languages.connect();
    if (workspace.membership)
      void untrack(() => source.languages.load()).catch(() => undefined);
    void untrack(loadSourceProfile);
    return () => {
      disconnectSource();
      disconnectSelected();
      disconnectLanguages();
    };
  });
  async function loadSourceProfile(force = false) {
    const id = workspace.sourceId;
    if (!force && sourceProfileSessionId === id) return;
    const request = ++sourceProfileRequest;
    try {
      const settings = await sessionApi.settings(id, 'output');
      if (request !== sourceProfileRequest) return;
      sourceProfile = String(settings.context?.source_profile ?? 'none');
      sourceProfileSessionId = id;
    } catch {
      if (request !== sourceProfileRequest) return;
      sourceProfile = 'none';
    }
  }
  async function openWorkflowCustomizer() {
    if (loadingCustomizer) return;
    loadingCustomizer = true;
    customizerLoadFailed = false;
    try {
      const component =
        WorkflowCustomizerComponent ??
        (await loadLazyModule(() => import('$lib/WorkflowCustomizer.svelte')))
          .default;
      if (!mounted) return;
      WorkflowCustomizerComponent = component;
      customizeOpen = true;
    } catch {
      if (mounted) customizerLoadFailed = true;
    } finally {
      if (mounted) loadingCustomizer = false;
    }
  }
  function reloadPage() {
    window.location.reload();
  }
  async function editName() {
    if (!sourceContext.session) return;
    if (membership)
      await workspace.source.languages.load(true).catch(() => undefined);
    nameDraft = projectName;
    nameRevision = membership
      ? (workspace.source.languages.value?.project?.revision ?? 0)
      : sourceContext.session.revision;
    if (!nameRevision) {
      nameError =
        'Project metadata could not be loaded. Reload before renaming.';
      return;
    }
    nameError = '';
    editingName = true;
    await tick();
    nameInput?.focus();
    nameInput?.select();
  }
  async function saveName() {
    if (!sourceContext.session || savingName || !nameDraft.trim()) return;
    if (nameDraft.trim() === projectName) {
      editingName = false;
      return;
    }
    savingName = true;
    nameError = '';
    try {
      if (membership) {
        const body = {
          expected_revision: nameRevision,
          name: nameDraft.trim()
        };
        const signature = JSON.stringify([membership.id, body]);
        if (nameMutation?.signature !== signature)
          nameMutation = { signature, key: crypto.randomUUID() };
        await translationProjectApi.updateName(
          membership.id,
          body,
          nameMutation.key
        );
        await workspace.source.languages.load(true);
        nameMutation = null;
      } else
        await sessionApi.update(sourceContext.session.id, nameRevision, {
          name: nameDraft.trim()
        });
      await sourceContext.reload();
      await appState.refreshSessions();
      editingName = false;
    } catch (caught) {
      nameError = errorMessage(caught);
    } finally {
      savingName = false;
    }
  }
  onMount(() => {
    mounted = true;
    const disconnectCache = workspace.connectCache();
    const disconnectPlanEditor = subscribeSpeechPlanEditor((sessionId) => {
      const target = `/sessions/${sessionId}/generation`;
      if (page.url.pathname === target) return;
      const state = generationWorkspaces.get(sessionId);
      if (state?.preferences)
        state.preferences = {
          ...state.preferences,
          selectedRunId: '',
          selectedRunVersionId: '',
          filter: 'all',
          startOrdinal: 0,
          anchorId: '',
          viewMode: 'segments',
          textMode: 'speech'
        };
      void goto(`${target}?mode=editing`);
    });
    const disconnectSourceProfile = invalidationBus.subscribe((batch) => {
      if (
        invalidates(batch, 'sources', workspace.sourceId) ||
        invalidates(batch, 'workflow', workspace.sourceId)
      )
        void loadSourceProfile(true);
    });
    return () => {
      mounted = false;
      disconnectCache();
      disconnectPlanEditor();
      disconnectSourceProfile();
    };
  });
  const tabs = $derived(
    [
      { href: '', label: 'Overview', icon: Sparkles },
      { href: '/generation', label: 'Generation', icon: ListMusic },
      { href: '/sources', label: 'Sources', icon: Layers3 },
      { href: '/edit', label: 'Edit', icon: Scissors },
      { href: '/text', label: 'Text & subtitles', icon: FileText },
      { href: '/languages', label: 'Languages', icon: Languages },
      { href: '/voice', label: 'Voice & audio', icon: AudioLines },
      { href: '/output', label: 'Output', icon: Settings2 },
      { href: '/activity', label: 'Activity', icon: Activity },
      { href: '/cleaning', label: 'Cleaning', icon: WandSparkles }
    ].filter(
      (tab) =>
        (tab.href !== '/languages' ||
          contextState.session?.workflow_kind !== 'audiobook') &&
        (!['/voice', '/generation'].includes(tab.href) ||
          contextState.session?.workflow_kind !== 'subtitles') &&
        (tab.href !== '/edit' ||
          (sourceSession?.workflow_kind ??
            contextState.session?.workflow_kind) === 'media_edit') &&
        (tab.href !== '/cleaning' || sourceProfile === 'document') &&
        (!simpleMode ||
          [
            '',
            '/generation',
            '/output',
            '/activity',
            '/languages',
            '/edit'
          ].includes(tab.href) ||
          (tab.href === '/text' &&
            contextState.session?.workflow_kind === 'subtitles'))
    )
  );
  const active = (suffix: string) =>
    suffix
      ? page.url.pathname.endsWith(suffix)
      : page.url.pathname === `/sessions/${page.params.id}`;
  $effect(() => {
    const sessionId = page.params.id ?? '';
    appState.mobileSessionNavigation = {
      sessionId,
      title: projectName,
      items: tabs.map((tab) => ({
        href: sectionHref(tab.href),
        label: tab.label
      }))
    };
    return () => {
      if (appState.mobileSessionNavigation?.sessionId === sessionId)
        appState.mobileSessionNavigation = null;
    };
  });
</script>

{#if sourceContext.loading && !sourceContext.session}
  <div class="surface grid min-h-64 place-items-center rounded-3xl">
    <div class="section-label animate-pulse">Loading project…</div>
  </div>
{:else if sourceContext.session}
  <div class="session-shell mx-auto min-w-0 max-w-[100rem] overflow-x-clip">
    <nav
      aria-label="Project location"
      class="mb-3 flex flex-wrap items-center gap-2 text-xs"
    >
      <a class="muted underline underline-offset-4" href="/sessions">Projects</a
      >
      {#if folderContext}<span class="muted" aria-hidden="true">/</span><a
          class="min-w-0 break-words text-[var(--accent)] underline underline-offset-4"
          href={`/sessions?folder=${encodeURIComponent(folderContext.id)}`}
          >{folderPath(folderContext, projectFolders.value.items)}</a
        >{/if}
    </nav>
    <nav
      aria-label="Project sections"
      class="session-tabs scrollbar-on-demand -mx-1 flex gap-1 overflow-x-auto border-b border-[var(--line)] px-1"
    >
      {#each tabs as tab}{@const Icon = tab.icon}<a
          href={sectionHref(tab.href)}
          class:active={active(tab.href)}
          aria-current={active(tab.href) ? 'page' : undefined}
          class="session-tab flex shrink-0 items-center gap-2 px-3 py-3 text-sm font-semibold"
          ><Icon size={16} />{tab.label}</a
        >{/each}
    </nav>
    <header class="mt-5 flex flex-wrap items-end justify-between gap-5">
      <div class="min-w-0">
        {#if editingName}
          <form
            class="mt-2 flex flex-wrap items-center gap-2"
            onsubmit={(event) => {
              event.preventDefault();
              void saveName();
            }}
          >
            <input
              bind:this={nameInput}
              bind:value={nameDraft}
              aria-label="Project name"
              maxlength="255"
              required
              disabled={savingName}
              onkeydown={(event) => {
                if (event.key === 'Escape' && !savingName) {
                  event.preventDefault();
                  editingName = false;
                  nameError = '';
                }
              }}
              class="min-w-0 flex-1 rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-xl font-semibold sm:min-w-80"
            />
            <button
              type="submit"
              aria-label="Save project name"
              disabled={savingName || !nameDraft.trim()}
              class="btn btn-icon btn-primary"><Check size={18} /></button
            >
            <button
              type="button"
              aria-label="Cancel renaming"
              disabled={savingName}
              onclick={() => {
                editingName = false;
                nameError = '';
              }}
              class="btn btn-icon btn-secondary"><X size={18} /></button
            >
          </form>
        {:else}
          <h1
            aria-label={projectName}
            class="mt-1 text-3xl font-semibold tracking-[-.035em]"
          >
            <button
              onclick={editName}
              aria-label={`Rename project ${projectName}`}
              title="Click to rename this project"
              class="group inline-flex max-w-full items-center gap-2 rounded-lg text-left hover:text-[var(--accent)] focus-visible:outline-2 focus-visible:outline-[var(--accent)]"
              ><span class="min-w-0 break-words [overflow-wrap:anywhere]"
                >{projectName}</span
              ><Pencil
                size={17}
                class="muted shrink-0 opacity-50 group-hover:opacity-100"
              /></button
            >
          </h1>
        {/if}
        {#if nameError}<p class="mt-2 text-sm text-red-500" role="alert">
            {nameError}
          </p>{/if}
        <div
          class="muted mt-2 flex flex-wrap items-center gap-2 text-xs capitalize"
        >
          <span
            >{languageContext
              ? projectActivity(languageContext, appState.jobs)
              : sourceContext.session.status} · {simpleMode
              ? sourceContext.session.workflow_kind.replace('_', ' ')
              : `${sourceContext.outcome?.value?.focus ?? 'custom'} plan`}</span
          ><span
            class="rounded-full bg-[var(--accent-soft)] px-2 py-1 font-semibold uppercase text-[var(--accent)]"
            >{languageLabel(
              workspace.source.languages.value?.project?.source_language ||
                sourceContext.session.source_language
            )} source</span
          >
        </div>
      </div>
      <div class="flex flex-wrap items-end gap-3">
        {#if membership && languageVersions.length > 1}
          <label class="text-xs font-semibold"
            >Project language
            <select
              class="mt-1 block max-w-72 rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2.5 text-sm"
              aria-label="Project language"
              value={languageContextId}
              onchange={(event) => switchLanguage(event.currentTarget.value)}
            >
              {#each languageVersions as version (version.id)}
                <option value={version.id}
                  >{languageLabel(
                    version.target_language ?? version.source_language
                  )}{!version.target_language
                    ? ' · Original language'
                    : ''}</option
                >
              {/each}
            </select>
          </label>
        {/if}
        <button
          class="btn btn-secondary"
          onclick={() => (assigningFolders = true)}
          ><Folder size={16} />Folders</button
        >
        <div
          class="inline-flex rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] p-1"
          role="group"
          aria-label="Project interface"
        >
          {#each ['simple', 'advanced'] as mode}<button
              type="button"
              class="rounded-lg px-3 py-2 text-sm font-semibold capitalize"
              class:interface-active={projectInterface.mode === mode}
              aria-pressed={projectInterface.mode === mode}
              disabled={projectInterface.pending}
              onclick={() =>
                projectInterface.change(mode as 'simple' | 'advanced')}
              >{mode}</button
            >{/each}
        </div>
        {#if !simpleMode}<button
            onclick={openWorkflowCustomizer}
            disabled={loadingCustomizer}
            class="flex items-center gap-2 rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] px-4 py-2.5 text-sm font-semibold"
            ><Settings2 size={16} /> Customize workflow</button
          >{/if}
      </div>
    </header>
    {#if membership?.role === 'branch' && page.url.pathname !== `/sessions/${routeSessionId}`}
      <p class="muted mt-4 text-sm">
        This language version uses a pinned copy of the shared source. Voice
        settings, generation and outputs apply to this language. <a
          class="font-semibold underline underline-offset-4"
          href={`/sessions/${sourceSessionId}/sources${navigationSearch(languageContextId)}`}
          >Open source &amp; corrections</a
        >
      </p>
    {/if}
    {#if projectInterface.error}<p
        class="mt-3 text-sm text-red-500"
        role="alert"
      >
        {projectInterface.error}
      </p>{/if}
    {#if customizerLoadFailed}
      <div class="mt-4 flex flex-wrap items-center gap-3 text-sm" role="alert">
        <p>
          Workflow controls couldn’t load. Save any open edits, then reload this
          page.
        </p>
        <button class="btn btn-secondary" onclick={reloadPage}
          >Reload page</button
        >
      </div>
    {/if}
    <div
      class="session-content min-w-0 max-w-full py-7"
      inert={projectInterface.pending}
      aria-busy={projectInterface.pending}
    >
      {#if contextState.session?.id === routeSessionId}
        {#if page.url.pathname === `/sessions/${routeSessionId}`}
          {@render children()}
        {:else}
          {#key routeSessionId}{@render children()}{/key}
        {/if}
      {:else}
        <p class="muted text-sm" role="status">Loading language controls…</p>
      {/if}
    </div>
  </div>
  {#if assigningFolders}<ProjectFolderAssignment
      store={projectFolders}
      projectId={sourceSessionId}
      {projectName}
      onclose={() => (assigningFolders = false)}
    />{/if}
  {#if customizeOpen && WorkflowCustomizerComponent}<WorkflowCustomizerComponent
      sessionId={contextState.session?.id ?? sourceSessionId}
      onclose={() => (customizeOpen = false)}
      onsaved={contextState.reload}
    />{/if}
{:else}<p class="text-red-500">
    {contextState.error || 'Project not found.'}
  </p>{/if}
{#if contextState.session?.id === routeSessionId && contextState.session.workflow_kind !== 'subtitles'}
  {#key routeSessionId}
    <GenerationStatusBar
      sessionId={routeSessionId}
      workspaceVisible={active('/generation')}
      onusage={(activity) =>
        contextState.workflow.applyGenerationUsage(activity)}
    />
  {/key}
{/if}

<style>
  .interface-active {
    background: var(--accent-soft);
    color: var(--accent);
  }
  .session-tabs {
    display: none;
    position: sticky;
    top: 0;
    z-index: 30;
    background: var(--paper);
  }
  @media (min-width: 768px) {
    .session-tabs {
      display: flex;
    }
  }
  .session-content {
    scroll-margin-top: 4.5rem;
  }
  .session-tab {
    border-bottom: 2px solid transparent;
    color: var(--muted);
  }
  .session-tab.active {
    border-color: var(--accent);
    color: var(--ink);
  }
</style>
