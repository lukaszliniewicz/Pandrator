<script lang="ts">
  import '../app.css';
  import { page } from '$app/state';
  import { onMount } from 'svelte';
  import type { Snippet } from 'svelte';
  import {
    Activity, AudioLines, AudioWaveform, BookOpenText, ChevronLeft, ChevronRight, FolderClock,
    Home, Library, LogOut, Menu, Mic2, Moon, ServerCog, Settings2, Sun, X
  } from '@lucide/svelte';
  import { appState } from '$lib/app-state.svelte';

  let { children }: { children: Snippet } = $props();
  let password = $state('');
  let loginError = $state('');
  let theme = $state<'light' | 'dark'>('light');
  let mobileOpen = $state(false);

  const navigation = [
    { href: '/', label: 'Home', icon: Home },
    { href: '/sessions', label: 'Sessions', icon: FolderClock },
    { href: '/sources', label: 'Source library', icon: Library },
    { href: '/voices', label: 'Voices', icon: Mic2 },
    { href: '/providers', label: 'Providers & services', icon: ServerCog },
    { href: '/rvc', label: 'RVC conversion', icon: AudioWaveform },
    { href: '/training', label: 'XTTS training', icon: AudioLines },
    { href: '/activity', label: 'Activity & logs', icon: Activity },
    { href: '/settings', label: 'Application settings', icon: Settings2 }
  ];

  function active(href: string) {
    return href === '/' ? page.url.pathname === '/' : page.url.pathname === href || page.url.pathname.startsWith(`${href}/`);
  }

  async function login() {
    loginError = '';
    try { await appState.login(password); password = ''; }
    catch (caught) { loginError = caught instanceof Error ? caught.message : String(caught); }
  }

  function toggleTheme() {
    theme = theme === 'light' ? 'dark' : 'light';
    document.documentElement.dataset.theme = theme;
    localStorage.setItem('pandrator-theme', theme);
  }

  onMount(() => {
    theme = (localStorage.getItem('pandrator-theme') as 'light' | 'dark') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    document.documentElement.dataset.theme = theme;
    appState.sidebarCollapsed = localStorage.getItem('pandrator-sidebar') === 'collapsed';
    appState.initialize();
  });

  $effect(() => {
    if (typeof localStorage !== 'undefined') localStorage.setItem('pandrator-sidebar', appState.sidebarCollapsed ? 'collapsed' : 'expanded');
  });
</script>

<svelte:head><title>Pandrator — Editorial audio workspace</title></svelte:head>

{#if appState.loading}
  <main class="grid min-h-screen place-items-center"><div class="eyebrow animate-pulse">Preparing Pandrator…</div></main>
{:else if !appState.authenticated}
  <main class="grid min-h-screen place-items-center p-6">
    <form onsubmit={(event) => { event.preventDefault(); login(); }} class="surface w-full max-w-md rounded-[2rem] p-9">
      <div class="mb-7 flex items-center gap-4"><div class="grid size-12 place-items-center rounded-2xl bg-[var(--accent)] text-white"><BookOpenText size={24}/></div><div><div class="eyebrow">Pandrator</div><h1 class="mt-1 text-2xl font-semibold">Open your workspace</h1></div></div>
      <label class="text-sm font-semibold">Owner password<input bind:value={password} type="password" autocomplete="current-password" class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"/></label>
      {#if loginError || appState.error}<p class="mt-3 text-sm text-red-500">{loginError || appState.error}</p>{/if}
      <button class="mt-6 w-full rounded-xl bg-[var(--accent)] px-4 py-3 font-semibold text-white">Sign in</button>
    </form>
  </main>
{:else}
  <div class="min-h-screen md:grid" style={`grid-template-columns:${appState.sidebarCollapsed ? '5rem' : '17rem'} minmax(0,1fr)`}>
    <button onclick={() => mobileOpen=true} class="fixed left-4 top-4 z-40 rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] p-2.5 shadow md:hidden" aria-label="Open navigation"><Menu size={20}/></button>
    {#if mobileOpen}<button class="fixed inset-0 z-40 bg-black/35 md:hidden" onclick={() => mobileOpen=false} aria-label="Close navigation"></button>{/if}
    <aside class:collapsed={appState.sidebarCollapsed} class:mobile-open={mobileOpen} class="app-sidebar fixed inset-y-0 left-0 z-50 flex w-[17rem] flex-col border-r border-[var(--line)] bg-[var(--paper-strong)] px-3 py-4 md:sticky md:top-0 md:z-20 md:h-screen md:w-auto">
      <div class="mb-5 flex items-center gap-3 px-2"><div class="grid size-11 shrink-0 place-items-center rounded-2xl bg-[var(--accent)] text-white"><BookOpenText size={21}/></div>{#if !appState.sidebarCollapsed}<div class="min-w-0 flex-1"><div class="font-semibold">Pandrator</div><div class="muted text-xs">Editorial audio studio</div></div><button onclick={() => mobileOpen=false} class="md:hidden"><X size={19}/></button>{/if}</div>
      <nav class="min-h-0 flex-1 space-y-1 overflow-y-auto">
        {#each navigation as item}{@const Icon = item.icon}<a href={item.href} onclick={() => mobileOpen=false} class:active={active(item.href)} title={appState.sidebarCollapsed ? item.label : undefined} class="nav-item flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-semibold"><Icon size={19}/>{#if !appState.sidebarCollapsed}<span>{item.label}</span>{/if}</a>{/each}
      </nav>
      <div class="space-y-1 border-t border-[var(--line)] pt-3">
        <button onclick={toggleTheme} class="nav-item flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-semibold">{#if theme==='light'}<Moon size={19}/>{:else}<Sun size={19}/>{/if}{#if !appState.sidebarCollapsed}<span>{theme==='light'?'Dark mode':'Light mode'}</span>{/if}</button>
        <button onclick={() => appState.logout()} class="nav-item flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-semibold"><LogOut size={19}/>{#if !appState.sidebarCollapsed}<span>Sign out</span>{/if}</button>
        <button onclick={() => appState.sidebarCollapsed=!appState.sidebarCollapsed} class="nav-item hidden w-full items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-semibold md:flex">{#if appState.sidebarCollapsed}<ChevronRight size={19}/>{:else}<ChevronLeft size={19}/><span>Collapse sidebar</span>{/if}</button>
      </div>
    </aside>
    <main class="min-w-0 px-5 pb-28 pt-20 sm:px-8 md:px-10 md:pt-9 xl:px-14">{@render children()}</main>
  </div>
  {#if appState.setupReturnVisible}
    <aside class="surface fixed bottom-5 right-5 z-40 max-w-sm rounded-2xl p-4"><div class="flex items-start gap-3"><Settings2 class="mt-0.5 shrink-0 text-[var(--accent)]" size={18}/><div class="min-w-0"><div class="text-sm font-semibold">Return to setup</div><p class="muted mt-1 text-xs leading-relaxed">{appState.setupGuidance}</p><div class="mt-3 flex gap-2"><a href="/?setup=1" class="rounded-lg bg-[var(--accent)] px-3 py-1.5 text-xs font-semibold text-white">Continue setup</a><button onclick={() => appState.setupReturnVisible=false} class="rounded-lg border border-[var(--line)] px-3 py-1.5 text-xs font-semibold">Dismiss</button></div></div></div></aside>
  {/if}
{/if}

<style>
  .app-sidebar { transform:translateX(-105%);transition:transform .18s ease,width .18s ease; }
  .app-sidebar.mobile-open { transform:translateX(0); }
  .nav-item { color:var(--muted); }
  .nav-item:hover,.nav-item.active { color:var(--ink);background:var(--accent-soft); }
  @media(min-width:768px){.app-sidebar{transform:none}.app-sidebar.collapsed{width:5rem}}
  @media(prefers-reduced-motion:reduce){.app-sidebar{transition:none}}
</style>
