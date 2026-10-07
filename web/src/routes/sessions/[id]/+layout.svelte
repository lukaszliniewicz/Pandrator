<script lang="ts">
  import { page } from '$app/state';
  import { goto } from '$app/navigation';
  import { onMount, setContext, tick } from 'svelte';
  import type { Snippet } from 'svelte';
  import {
    Activity,
    AudioLines,
    Check,
    FileText,
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
  import { sessionApi } from '$lib/domain-api';
  import { errorMessage } from '$lib/errors';
  import { invalidates, invalidationBus } from '$lib/invalidation';
  import { loadLazyModule } from '$lib/lazy-module';
  import { SESSION_CONTEXT, type SessionContext } from '$lib/session-context';
  import { SessionStore } from '$lib/session-store.svelte';
  import { WorkflowStore } from '$lib/workflow-store.svelte';
  import type WorkflowCustomizer from '$lib/WorkflowCustomizer.svelte';
  import type GenerationDrawer from '$lib/GenerationDrawer.svelte';
  let { children }: { children: Snippet } = $props();
  let customizeOpen = $state(false);
  let WorkflowCustomizerComponent = $state<typeof WorkflowCustomizer | null>(
    null
  );
  let GenerationDrawerComponent = $state<typeof GenerationDrawer | null>(null);
  let drawerLoadFailed = $state(false);
  let customizerLoadFailed = $state(false);
  let loadingCustomizer = $state(false);
  let mounted = false;
  let sourceProfile = $state('none');
  let editingName = $state(false);
  let nameDraft = $state('');
  let nameRevision = $state(0);
  let savingName = $state(false);
  let nameError = $state('');
  let nameInput = $state<HTMLInputElement>();
  const sessionStore = new SessionStore(page.params.id ?? '', (session) =>
    appState.upsertSession(session)
  );
  const workflowStore = new WorkflowStore(page.params.id ?? '');
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
    workflow: workflowStore,
    reload: async () => {
      await Promise.all([sessionStore.load(true), loadSourceProfile()]);
    },
    customize: () => void openWorkflowCustomizer()
  };
  setContext(SESSION_CONTEXT, contextState);
  // Declared before the initial reload(): loadSourceProfile() bumps this
  // counter synchronously, so a later declaration would throw (TDZ).
  let sourceProfileRequest = 0;
  reload();
  // SvelteKit reuses this layout when navigating between /sessions/[id]
  // pages, but the stores above capture page.params.id once at construction.
  // Retarget them on id change; reset() discards in-flight loads for the old
  // id via the resource epoch, and reload() fetches the new session.
  const routeSessionId = $derived(page.params.id ?? '');
  const membership = $derived(
    contextState.session?.translation_project ??
      appState.sessions.find((item) => item.id === routeSessionId)
        ?.translation_project
  );
  const sourceSessionId = $derived(
    membership?.source_session_id ?? routeSessionId
  );
  const sourceSession = $derived(
    appState.sessions.find((item) => item.id === sourceSessionId)
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
  const sectionHref = (suffix: string) =>
    `/sessions/${sharedSections.has(suffix) ? sourceSessionId : routeSessionId}${suffix}`;
  function switchLanguage(id: string) {
    const suffix = page.url.pathname.slice(
      `/sessions/${routeSessionId}`.length
    );
    const version = languageVersions.find((item) => item.id === id);
    const nextSuffix =
      sharedSections.has(suffix) && id !== sourceSessionId
        ? version?.workflow_kind === 'subtitles'
          ? '/text'
          : '/voice'
        : suffix === '/voice' && version?.workflow_kind === 'subtitles'
          ? '/text'
          : suffix;
    void goto(`/sessions/${id}${nextSuffix}`);
  }
  $effect(() => {
    const id = routeSessionId;
    const sessionRetargeted = sessionStore.retarget(id);
    const workflowRetargeted = workflowStore.retarget(id);
    if (sessionRetargeted || workflowRetargeted) void reload();
  });
  async function loadSourceProfile() {
    const request = ++sourceProfileRequest;
    const id = page.params.id ?? '';
    try {
      const settings = await sessionApi.settings(id, 'output');
      if (request !== sourceProfileRequest) return;
      sourceProfile = String(settings.context?.source_profile ?? 'none');
    } catch {
      if (request !== sourceProfileRequest) return;
      sourceProfile = 'none';
    }
  }
  function reload() {
    return contextState.reload();
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
  async function loadGenerationDrawer() {
    try {
      const { default: component } = await loadLazyModule(
        () => import('$lib/GenerationDrawer.svelte')
      );
      if (mounted) {
        GenerationDrawerComponent = component;
        drawerLoadFailed = false;
      }
    } catch {
      if (mounted) drawerLoadFailed = true;
    }
  }
  function reloadPage() {
    window.location.reload();
  }
  async function editName() {
    if (!contextState.session) return;
    nameDraft = contextState.session.name;
    nameRevision = contextState.session.revision;
    nameError = '';
    editingName = true;
    await tick();
    nameInput?.focus();
    nameInput?.select();
  }
  async function saveName() {
    if (!contextState.session || savingName || !nameDraft.trim()) return;
    if (nameDraft.trim() === contextState.session.name) {
      editingName = false;
      return;
    }
    savingName = true;
    nameError = '';
    try {
      await sessionApi.update(contextState.session.id, nameRevision, {
        name: nameDraft.trim()
      });
      await sessionStore.load(true);
      editingName = false;
    } catch (caught) {
      nameError = errorMessage(caught);
    } finally {
      savingName = false;
    }
  }
  onMount(() => {
    mounted = true;
    const disconnectSession = sessionStore.connect();
    const disconnectWorkflow = workflowStore.connect();
    const disconnectSourceProfile = invalidationBus.subscribe((batch) => {
      if (
        invalidates(batch, 'sources', page.params.id ?? '') ||
        invalidates(batch, 'workflow', page.params.id ?? '')
      )
        void loadSourceProfile();
    });
    void loadGenerationDrawer();
    return () => {
      mounted = false;
      disconnectSession();
      disconnectWorkflow();
      disconnectSourceProfile();
    };
  });
  const tabs = $derived(
    [
      { href: '', label: 'Overview', icon: Sparkles },
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
        (tab.href !== '/voice' ||
          contextState.session?.workflow_kind !== 'subtitles') &&
        (tab.href !== '/edit' ||
          (sourceSession?.workflow_kind ??
            contextState.session?.workflow_kind) === 'media_edit') &&
        (tab.href !== '/cleaning' || sourceProfile === 'document')
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
      title: contextState.session?.name ?? 'Project',
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

{#if contextState.loading}
  <div class="surface grid min-h-64 place-items-center rounded-3xl">
    <div class="section-label animate-pulse">Loading project…</div>
  </div>
{:else if contextState.session}
  <div class="session-shell mx-auto min-w-0 max-w-[100rem] overflow-x-clip">
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
        {#if membership}
          <a
            class="muted text-sm font-semibold underline underline-offset-4"
            href={`/sessions/${sourceSessionId}`}>{membership.name}</a
          >
        {/if}
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
          <h1 class="mt-1 text-3xl font-semibold tracking-[-.035em]">
            <button
              onclick={editName}
              aria-label={`Rename project ${contextState.session.name}`}
              title="Click to rename this project"
              class="group inline-flex max-w-full items-center gap-2 rounded-lg text-left hover:text-[var(--accent)] focus-visible:outline-2 focus-visible:outline-[var(--accent)]"
              ><span class="min-w-0 break-words [overflow-wrap:anywhere]"
                >{contextState.session.name}</span
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
            >{contextState.session.status} · {contextState.outcome?.value
              ?.focus ?? 'custom'} plan</span
          ><span
            class="rounded-full bg-[var(--accent-soft)] px-2 py-1 font-semibold uppercase text-[var(--accent)]"
            >{contextState.session
              .source_language}{#if contextState.session.target_language}
              → {contextState.session.target_language}{/if}</span
          >
        </div>
      </div>
      <div class="flex flex-wrap items-end gap-3">
        {#if membership && languageVersions.length > 1}
          <label class="text-xs font-semibold"
            >Project language
            <select
              class="mt-1 block max-w-72 rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2.5 text-sm"
              value={routeSessionId}
              onchange={(event) => switchLanguage(event.currentTarget.value)}
            >
              {#each languageVersions as version (version.id)}
                <option value={version.id}
                  >{version.id === sourceSessionId
                    ? 'Source · '
                    : ''}{languageLabel(
                    version.target_language ?? version.source_language
                  )}</option
                >
              {/each}
            </select>
          </label>
        {/if}
        <button
          onclick={openWorkflowCustomizer}
          disabled={loadingCustomizer}
          class="flex items-center gap-2 rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] px-4 py-2.5 text-sm font-semibold"
          ><Settings2 size={16} /> Customize workflow</button
        >
      </div>
    </header>
    {#if membership?.role === 'branch'}
      <p class="muted mt-4 text-sm">
        This language version uses a pinned copy of the corrected source. Voice
        settings, generation and outputs apply to this language. <a
          class="font-semibold underline underline-offset-4"
          href={`/sessions/${sourceSessionId}/text`}
          >Open source &amp; corrections</a
        >
      </p>
    {/if}
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
    <div class="session-content min-w-0 max-w-full py-7">
      {@render children()}
    </div>
  </div>
  {#if customizeOpen && WorkflowCustomizerComponent}<WorkflowCustomizerComponent
      sessionId={contextState.session.id}
      onclose={() => (customizeOpen = false)}
      onsaved={contextState.reload}
    />{/if}
{:else}<p class="text-red-500">
    {contextState.error || 'Project not found.'}
  </p>{/if}
{#if Boolean(contextState.session) && GenerationDrawerComponent && contextState.session?.workflow_kind !== 'subtitles'}{#key page.params.id}<GenerationDrawerComponent
      sessionId={page.params.id ?? ''}
      workflowKind={contextState.session?.workflow_kind ?? 'audiobook'}
    />{/key}{/if}
{#if contextState.session && !GenerationDrawerComponent && !drawerLoadFailed && contextState.session.workflow_kind !== 'subtitles'}
  <div
    class="surface fixed inset-x-3 bottom-3 z-40 rounded-2xl border border-[var(--line)] px-4 py-4 text-sm md:left-[calc(var(--sidebar-offset,5rem)+.35rem)] md:right-[.35rem]"
    role="status"
  >
    Loading generation controls…
  </div>
{/if}
{#if contextState.session && drawerLoadFailed && contextState.session.workflow_kind !== 'subtitles'}
  <div
    class="surface fixed inset-x-3 bottom-3 z-40 mx-auto flex max-w-2xl flex-wrap items-center justify-between gap-3 rounded-2xl border border-[var(--line)] p-4 text-sm shadow-lg"
    role="alert"
  >
    <p>
      Audio controls couldn’t load. Save any open edits, then reload this page.
    </p>
    <button class="btn btn-secondary" onclick={reloadPage}>Reload page</button>
  </div>
{/if}

<style>
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
