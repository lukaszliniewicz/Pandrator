<script lang="ts">
  import { page } from '$app/state';
  import { onMount, setContext, tick } from 'svelte';
  import type { Snippet } from 'svelte';
  import {
    Activity,
    AudioLines,
    ChevronLeft,
    Check,
    FileText,
    Layers3,
    Pencil,
    Settings2,
    Scissors,
    Sparkles,
    WandSparkles,
    X
  } from '@lucide/svelte';
  import { appState } from '$lib/app-state.svelte';
  import { sessionApi } from '$lib/domain-api';
  import { errorMessage } from '$lib/errors';
  import { invalidates, invalidationBus } from '$lib/invalidation';
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
  reload();
  async function loadSourceProfile() {
    try {
      const settings = await sessionApi.settings(
        page.params.id ?? '',
        'output'
      );
      sourceProfile = String(settings.context?.source_profile ?? 'none');
    } catch {
      sourceProfile = 'none';
    }
  }
  function reload() {
    return contextState.reload();
  }
  async function openWorkflowCustomizer() {
    WorkflowCustomizerComponent ??= (
      await import('$lib/WorkflowCustomizer.svelte')
    ).default;
    customizeOpen = true;
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
    const disconnectSession = sessionStore.connect();
    const disconnectWorkflow = workflowStore.connect();
    const disconnectSourceProfile = invalidationBus.subscribe((batch) => {
      if (
        invalidates(batch, 'sources', page.params.id ?? '') ||
        invalidates(batch, 'workflow', page.params.id ?? '')
      )
        void loadSourceProfile();
    });
    const loadGenerationDrawer = () => {
      void import('$lib/GenerationDrawer.svelte').then(
        ({ default: component }) => (GenerationDrawerComponent = component)
      );
    };
    const idleCallback = window.requestIdleCallback?.(loadGenerationDrawer, {
      timeout: 1000
    });
    const fallbackTimer =
      idleCallback === undefined
        ? window.setTimeout(loadGenerationDrawer, 200)
        : undefined;
    return () => {
      disconnectSession();
      disconnectWorkflow();
      disconnectSourceProfile();
      if (idleCallback !== undefined) window.cancelIdleCallback(idleCallback);
      if (fallbackTimer !== undefined) window.clearTimeout(fallbackTimer);
    };
  });
  const tabs = $derived(
    [
      { href: '', label: 'Overview', icon: Sparkles },
      { href: '/sources', label: 'Sources', icon: Layers3 },
      { href: '/edit', label: 'Edit', icon: Scissors },
      { href: '/text', label: 'Text & subtitles', icon: FileText },
      { href: '/voice', label: 'Voice & audio', icon: AudioLines },
      { href: '/output', label: 'Output', icon: Settings2 },
      { href: '/activity', label: 'Activity', icon: Activity },
      { href: '/cleaning', label: 'Cleaning', icon: WandSparkles }
    ].filter(
      (tab) =>
        (tab.href !== '/voice' ||
          contextState.session?.workflow_kind !== 'subtitles') &&
        (tab.href !== '/edit' ||
          contextState.session?.workflow_kind === 'media_edit') &&
        (tab.href !== '/cleaning' || sourceProfile === 'document')
    )
  );
  const active = (suffix: string) =>
    suffix
      ? page.url.pathname.endsWith(suffix)
      : page.url.pathname === `/sessions/${page.params.id}`;
</script>

{#if contextState.loading}
  <div class="surface grid min-h-64 place-items-center rounded-3xl">
    <div class="eyebrow animate-pulse">Loading session…</div>
  </div>
{:else if contextState.session}
  <div class="session-shell mx-auto min-w-0 max-w-[100rem] overflow-x-hidden">
    <a
      href="/sessions"
      class="muted flex items-center gap-1 text-sm font-semibold"
      ><ChevronLeft size={16} /> Sessions</a
    >
    <header class="mt-5 flex flex-wrap items-end justify-between gap-5">
      <div class="min-w-0">
        <div class="eyebrow capitalize">
          {contextState.session.workflow_kind} workspace
        </div>
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
              aria-label="Session name"
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
              aria-label="Save session name"
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
              aria-label={`Rename session ${contextState.session.name}`}
              title="Click to rename this session"
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
      <button
        onclick={openWorkflowCustomizer}
        class="flex items-center gap-2 rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] px-4 py-2.5 text-sm font-semibold"
        ><Settings2 size={16} /> Customize workflow</button
      >
    </header>
    <nav
      class="scrollbar-on-demand mt-7 flex gap-1 overflow-x-auto border-b border-[var(--line)]"
    >
      {#each tabs as tab}{@const Icon = tab.icon}<a
          href={`/sessions/${page.params.id}${tab.href}`}
          class:active={active(tab.href)}
          class="session-tab flex shrink-0 items-center gap-2 px-3 py-3 text-sm font-semibold"
          ><Icon size={16} />{tab.label}</a
        >{/each}
    </nav>
    <div class="min-w-0 max-w-full py-7">{@render children()}</div>
  </div>
  {#if customizeOpen && WorkflowCustomizerComponent}<WorkflowCustomizerComponent
      sessionId={contextState.session.id}
      onclose={() => (customizeOpen = false)}
      onsaved={contextState.reload}
    />{/if}
{:else}<p class="text-red-500">
    {contextState.error || 'Session not found.'}
  </p>{/if}
{#if Boolean(contextState.session) && GenerationDrawerComponent && contextState.session?.workflow_kind !== 'subtitles'}<GenerationDrawerComponent
    sessionId={page.params.id ?? ''}
  />{/if}

<style>
  .session-tab {
    border-bottom: 2px solid transparent;
    color: var(--muted);
  }
  .session-tab.active {
    border-color: var(--accent);
    color: var(--ink);
  }
</style>
