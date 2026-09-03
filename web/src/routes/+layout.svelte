<script lang="ts">
  import { errorMessage } from '$lib/errors';
  import '../app.css';
  import { page } from '$app/state';
  import { onMount } from 'svelte';
  import type { Snippet } from 'svelte';
  import {
    Activity,
    AudioLines,
    AudioWaveform,
    ChevronLeft,
    ChevronRight,
    ExternalLink,
    FolderClock,
    Github,
    Home,
    Languages,
    Library,
    LogOut,
    Menu,
    Mic2,
    Moon,
    ServerCog,
    Settings2,
    ShieldAlert,
    Sun,
    X
  } from '@lucide/svelte';
  import { appState } from '$lib/app-state.svelte';
  import ManagerOperationBanner from '$lib/ManagerOperationBanner.svelte';

  let { children }: { children: Snippet } = $props();
  let password = $state('');
  let loginError = $state('');
  let theme = $state<'light' | 'dark'>('light');
  let mobileOpen = $state(false);
  let tabletRail = $state(false);

  const compactSidebar = $derived(appState.sidebarCollapsed || tabletRail);
  const renderSidebarLabels = $derived(
    !appState.sidebarCollapsed || tabletRail
  );
  const applicationVersion = $derived(
    String(
      (appState.capabilities.application as { version?: unknown } | undefined)
        ?.version ?? ''
    ).trim()
  );

  const navigation = [
    { href: '/', label: 'Home', icon: Home },
    { href: '/sessions', label: 'Sessions', icon: FolderClock },
    { href: '/sources', label: 'Source library', icon: Library },
    { href: '/voices', label: 'Voices', icon: Mic2 },
    { href: '/pronunciations', label: 'Pronunciations', icon: Languages },
    { href: '/providers', label: 'Providers & services', icon: ServerCog },
    { href: '/rvc', label: 'RVC conversion', icon: AudioWaveform },
    { href: '/training', label: 'XTTS training', icon: AudioLines },
    { href: '/activity', label: 'Activity & logs', icon: Activity },
    { href: '/settings', label: 'Application settings', icon: Settings2 }
  ];

  function active(href: string) {
    return href === '/'
      ? page.url.pathname === '/'
      : page.url.pathname === href || page.url.pathname.startsWith(`${href}/`);
  }

  async function login() {
    loginError = '';
    try {
      await appState.login(password);
      password = '';
    } catch (caught) {
      loginError = errorMessage(caught);
    }
  }

  function toggleTheme() {
    theme = theme === 'light' ? 'dark' : 'light';
    document.documentElement.dataset.theme = theme;
    localStorage.setItem('pandrator-theme', theme);
  }

  onMount(() => {
    theme =
      (localStorage.getItem('pandrator-theme') as 'light' | 'dark') ||
      (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    document.documentElement.dataset.theme = theme;
    appState.sidebarCollapsed =
      localStorage.getItem('pandrator-sidebar') === 'collapsed';
    appState.initialize();
    const tabletQuery = matchMedia(
      '(min-width: 768px) and (max-width: 1023px)'
    );
    const updateTabletRail = () => (tabletRail = tabletQuery.matches);
    updateTabletRail();
    tabletQuery.addEventListener('change', updateTabletRail);
    return () => tabletQuery.removeEventListener('change', updateTabletRail);
  });

  $effect(() => {
    if (typeof localStorage !== 'undefined')
      localStorage.setItem(
        'pandrator-sidebar',
        appState.sidebarCollapsed ? 'collapsed' : 'expanded'
      );
  });
</script>

<svelte:head
  ><title>Pandrator — voice, subtitle, and audiobook workspace</title
  ></svelte:head
>

{#if appState.loading}
  <main class="grid min-h-screen place-items-center">
    <div class="eyebrow animate-pulse">Preparing Pandrator…</div>
  </main>
{:else if !appState.authenticated}
  <main class="grid min-h-screen place-items-center p-6">
    <form
      onsubmit={(event) => {
        event.preventDefault();
        login();
      }}
      class="surface w-full max-w-md rounded-[2rem] p-9"
    >
      <div class="mb-7 flex items-center gap-4">
        <img
          src="/pandrator-logo.webp"
          alt="Pandrator"
          width="128"
          height="128"
          class="size-12 rounded-2xl border border-[var(--line)] object-cover"
        />
        <div>
          <div class="eyebrow">Pandrator</div>
          <h1 class="mt-1 text-2xl font-semibold">Open your workspace</h1>
        </div>
      </div>
      {#if appState.securityWarning}<div
          role="alert"
          class="mb-5 flex items-start gap-2 rounded-xl border border-amber-500/40 bg-amber-500/10 p-3 text-xs leading-relaxed"
        >
          <ShieldAlert class="mt-0.5 shrink-0" size={16} /><span
            >{appState.securityWarning}</span
          >
        </div>{/if}
      <label class="text-sm font-semibold"
        >Owner password<input
          bind:value={password}
          type="password"
          autocomplete="current-password"
          class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
        /></label
      >
      {#if loginError || appState.error}<p class="mt-3 text-sm text-red-500">
          {loginError || appState.error}
        </p>{/if}
      <button
        class="mt-6 w-full rounded-xl bg-[var(--accent)] px-4 py-3 font-semibold text-white"
        >Sign in</button
      >
    </form>
  </main>
{:else}
  <div
    class="app-shell min-h-screen md:grid"
    style={`grid-template-columns:${compactSidebar ? '5rem' : '17rem'} minmax(0,1fr);--sidebar-offset:${compactSidebar ? '5rem' : '17rem'}`}
  >
    <button
      onclick={() => (mobileOpen = true)}
      class="fixed left-4 top-4 z-40 rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] p-2.5 shadow md:hidden"
      aria-label="Open navigation"><Menu size={20} /></button
    >
    {#if mobileOpen}<button
        class="fixed inset-0 z-40 bg-black/35 md:hidden"
        onclick={() => (mobileOpen = false)}
        aria-label="Close navigation"
      ></button>{/if}
    <aside
      class:collapsed={appState.sidebarCollapsed}
      class:tablet-rail={tabletRail}
      class:mobile-open={mobileOpen}
      class="app-sidebar fixed inset-y-0 left-0 z-50 flex w-[17rem] flex-col border-r border-[var(--line)] bg-[var(--paper-strong)] px-3 py-4 md:z-20 md:h-[100svh] md:w-auto"
    >
      <div class="sidebar-brand mb-5 flex items-center gap-3 px-2">
        <img
          src="/pandrator-logo.webp"
          alt="Pandrator"
          width="128"
          height="128"
          class="size-11 shrink-0 rounded-2xl border border-[var(--line)] object-cover"
        />{#if renderSidebarLabels}<div class="sidebar-label min-w-0 flex-1">
            <div class="font-semibold">Pandrator</div>
            <a
              href="https://github.com/lukaszliniewicz/Pandrator"
              target="_blank"
              rel="noreferrer"
              class="muted mt-0.5 inline-flex items-center gap-1 text-xs hover:text-[var(--accent)]"
              >View on GitHub <ExternalLink size={11} /></a
            >
          </div>
          <button onclick={() => (mobileOpen = false)} class="md:hidden"
            ><X size={19} /></button
          >{/if}
      </div>
      <nav class="sidebar-nav min-h-0 flex-1 space-y-1 overflow-y-auto">
        {#each navigation as item}{@const Icon = item.icon}<a
            href={item.href}
            onclick={() => (mobileOpen = false)}
            class:active={active(item.href)}
            title={compactSidebar ? item.label : undefined}
            class="nav-item flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-semibold"
            ><Icon class="shrink-0" size={19} />{#if renderSidebarLabels}<span
                class="sidebar-label">{item.label}</span
              >{/if}</a
          >{/each}
      </nav>
      <div class="space-y-1 border-t border-[var(--line)] pt-3">
        <button
          onclick={toggleTheme}
          class="nav-item flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-semibold"
          >{#if theme === 'light'}<Moon size={19} />{:else}<Sun
              size={19}
            />{/if}{#if renderSidebarLabels}<span class="sidebar-label"
              >{theme === 'light' ? 'Dark mode' : 'Light mode'}</span
            >{/if}</button
        >
        <button
          onclick={() => appState.logout()}
          class="nav-item flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-semibold"
          ><LogOut class="shrink-0" size={19} />{#if renderSidebarLabels}<span
              class="sidebar-label">Sign out</span
            >{/if}</button
        >
        <button
          onclick={() =>
            (appState.sidebarCollapsed = !appState.sidebarCollapsed)}
          class="sidebar-preference nav-item hidden w-full items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-semibold md:flex"
          >{#if appState.sidebarCollapsed}<ChevronRight
              size={19}
            />{:else}<ChevronLeft size={19} /><span>Collapse sidebar</span
            >{/if}</button
        >
      </div>
    </aside>
    <div
      class="content-column flex min-h-screen min-w-0 flex-col md:col-start-2"
    >
      <main
        class="min-w-0 flex-1 px-5 pb-12 pt-20 sm:px-8 md:px-6 md:pt-9 lg:px-10 xl:px-14"
      >
        {#if appState.securityWarning}<div
            role="alert"
            class="mb-5 flex items-start gap-2 rounded-xl border border-amber-500/40 bg-amber-500/10 p-3 text-xs leading-relaxed"
          >
            <ShieldAlert class="mt-0.5 shrink-0" size={16} /><span
              >{appState.securityWarning}</span
            >
          </div>{/if}<ManagerOperationBanner />{@render children()}
      </main>
      <footer
        class="app-footer mx-5 flex flex-wrap items-center justify-between gap-3 border-t border-[var(--line)] py-5 text-xs sm:mx-8 md:mx-10 xl:mx-14"
      >
        <span class="muted"
          >Pandrator{applicationVersion ? ` v${applicationVersion}` : ''} · created
          by Łukasz Liniewicz</span
        ><a
          href="https://github.com/lukaszliniewicz/Pandrator"
          target="_blank"
          rel="noreferrer"
          class="inline-flex items-center gap-1.5 font-semibold text-[var(--accent)]"
          ><Github size={14} /> Source on GitHub</a
        >
      </footer>
    </div>
  </div>
  {#if appState.setupReturnVisible}
    <aside
      class="surface fixed bottom-5 right-5 z-40 w-[min(30rem,calc(100vw-2rem))] rounded-2xl p-5"
    >
      <div class="flex items-start gap-3">
        <Settings2 class="mt-0.5 shrink-0 text-[var(--accent)]" size={18} />
        <div class="min-w-0 flex-1">
          <div class="font-semibold">Return to setup</div>
          <p class="muted mt-1 text-sm leading-relaxed">
            {appState.setupGuidance}
          </p>
          <div class="mt-4 flex flex-wrap gap-2">
            <a href="/?setup=1" class="btn btn-primary">Continue setup</a
            ><button
              onclick={() => (appState.setupReturnVisible = false)}
              class="btn btn-secondary">Dismiss</button
            >
          </div>
        </div>
      </div>
    </aside>
  {/if}
{/if}

<style>
  .app-sidebar {
    transform: translateX(-105%);
    transition:
      transform 0.18s ease,
      width 0.18s ease;
  }
  .app-sidebar.mobile-open {
    transform: translateX(0);
  }
  .nav-item {
    color: var(--muted);
  }
  .nav-item:hover,
  .nav-item.active {
    color: var(--ink);
    background: var(--accent-soft);
  }
  .sidebar-nav {
    scrollbar-color: transparent transparent;
  }
  .sidebar-nav::-webkit-scrollbar-track,
  .sidebar-nav::-webkit-scrollbar-thumb {
    background: transparent;
  }
  .app-sidebar:hover .sidebar-nav,
  .app-sidebar:focus-within .sidebar-nav {
    scrollbar-color: color-mix(in srgb, var(--accent) 45%, var(--line))
      transparent;
  }
  .app-sidebar:hover .sidebar-nav::-webkit-scrollbar-thumb,
  .app-sidebar:focus-within .sidebar-nav::-webkit-scrollbar-thumb {
    background: color-mix(in srgb, var(--accent) 45%, var(--line));
  }
  @media (min-width: 768px) {
    .app-sidebar {
      transform: none;
    }
    .app-sidebar.collapsed {
      width: 5rem;
    }
    .app-sidebar.collapsed .sidebar-brand,
    .app-sidebar.collapsed .nav-item {
      justify-content: center;
      gap: 0;
      padding-left: 0;
      padding-right: 0;
    }
    .content-column {
      min-height: 100svh;
    }
    .app-footer {
      margin-left: 2.5rem;
      margin-right: 2.5rem;
    }
  }
  @media (min-width: 768px) and (max-width: 1023px) {
    .app-sidebar.tablet-rail {
      width: 5rem;
      overflow-x: hidden;
      box-shadow: none;
    }
    .app-sidebar.tablet-rail:hover,
    .app-sidebar.tablet-rail:focus-within {
      width: 17rem;
      box-shadow: var(--shadow);
    }
    .app-sidebar.tablet-rail:not(:hover):not(:focus-within) .sidebar-brand,
    .app-sidebar.tablet-rail:not(:hover):not(:focus-within) .nav-item {
      justify-content: center;
      gap: 0;
      padding-left: 0;
      padding-right: 0;
    }
    .app-sidebar.tablet-rail:not(:hover):not(:focus-within) .sidebar-label {
      width: 0;
      overflow: hidden;
      opacity: 0;
      pointer-events: none;
      white-space: nowrap;
    }
    .app-sidebar.tablet-rail .sidebar-label {
      transition: opacity 0.12s ease;
    }
    .app-sidebar.tablet-rail .sidebar-preference {
      display: none;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .app-sidebar {
      transition: none;
    }
  }
</style>
