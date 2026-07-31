<script lang="ts">
  import { page } from '$app/state';
  import { onMount, setContext } from 'svelte';
  import type { Snippet } from 'svelte';
  import {
    Activity,
    AudioLines,
    ChevronLeft,
    FileText,
    Layers3,
    Settings2,
    Sparkles,
    WandSparkles
  } from '@lucide/svelte';
  import { appState } from '$lib/app-state.svelte';
  import { SESSION_CONTEXT, type SessionContext } from '$lib/session-context';
  import { SessionStore } from '$lib/session-store.svelte';
  import { WorkflowStore } from '$lib/workflow-store.svelte';
  import WorkflowCustomizer from '$lib/WorkflowCustomizer.svelte';
  import GenerationDrawer from '$lib/GenerationDrawer.svelte';
  let { children }: { children: Snippet } = $props();
  let customizeOpen = $state(false);
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
    reload: () => sessionStore.load(true),
    customize: () => (customizeOpen = true)
  };
  setContext(SESSION_CONTEXT, contextState);
  reload();
  function reload() {
    return contextState.reload();
  }
  onMount(() => {
    const disconnectSession = sessionStore.connect();
    const disconnectWorkflow = workflowStore.connect();
    return () => {
      disconnectSession();
      disconnectWorkflow();
    };
  });
  const tabs = $derived(
    [
      { href: '', label: 'Overview', icon: Sparkles },
      { href: '/sources', label: 'Sources', icon: Layers3 },
      { href: '/text', label: 'Text & subtitles', icon: FileText },
      { href: '/voice', label: 'Voice & audio', icon: AudioLines },
      { href: '/output', label: 'Output', icon: Settings2 },
      { href: '/activity', label: 'Activity', icon: Activity },
      { href: '/cleaning', label: 'Cleaning', icon: WandSparkles }
    ].filter(
      (tab) =>
        tab.href !== '/voice' ||
        contextState.session?.workflow_kind !== 'subtitles'
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
  <div class="session-shell mx-auto max-w-[100rem]">
    <a
      href="/sessions"
      class="muted flex items-center gap-1 text-sm font-semibold"
      ><ChevronLeft size={16} /> Sessions</a
    >
    <header class="mt-5 flex flex-wrap items-end justify-between gap-5">
      <div>
        <div class="eyebrow capitalize">
          {contextState.session.workflow_kind} workspace
        </div>
        <h1 class="mt-1 text-3xl font-semibold tracking-[-.035em]">
          {contextState.session.name}
        </h1>
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
        onclick={() => (customizeOpen = true)}
        class="flex items-center gap-2 rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] px-4 py-2.5 text-sm font-semibold"
        ><Settings2 size={16} /> Customize workflow</button
      >
    </header>
    <nav class="mt-7 flex gap-1 overflow-x-auto border-b border-[var(--line)]">
      {#each tabs as tab}{@const Icon = tab.icon}<a
          href={`/sessions/${page.params.id}${tab.href}`}
          class:active={active(tab.href)}
          class="session-tab flex shrink-0 items-center gap-2 px-3 py-3 text-sm font-semibold"
          ><Icon size={16} />{tab.label}</a
        >{/each}
    </nav>
    <div class="py-7">{@render children()}</div>
  </div>
  {#if customizeOpen}<WorkflowCustomizer
      sessionId={contextState.session.id}
      onclose={() => (customizeOpen = false)}
      onsaved={contextState.reload}
    />{/if}
{:else}<p class="text-red-500">
    {contextState.error || 'Session not found.'}
  </p>{/if}
{#if Boolean(contextState.session) && contextState.session?.workflow_kind !== 'subtitles'}<GenerationDrawer
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
