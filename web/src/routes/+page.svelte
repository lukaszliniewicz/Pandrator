<script lang="ts">
  import '../app.css';
  import {
    AudioLines,
    BookOpenText,
    Captions,
    ChevronRight,
    CircleHelp,
    FileAudio,
    FolderClock,
    Library,
    Moon,
    Plus,
    Radio,
    Settings2,
    Sparkles,
    Sun,
    WandSparkles,
    Workflow,
    X
  } from '@lucide/svelte';
  import { onMount } from 'svelte';
  import { api, exchangeBootstrapToken, setCsrfToken, type JobRecord, type SessionRecord } from '$lib/api';
  import SessionWorkspace from '$lib/SessionWorkspace.svelte';
  import ProviderManager from '$lib/ProviderManager.svelte';
  import VoiceManager from '$lib/VoiceManager.svelte';

  let theme = $state<'light' | 'dark'>('light');
  let authenticated = $state(false);
  let loading = $state(true);
  let error = $state('');
  let password = $state('');
  let sessions = $state<SessionRecord[]>([]);
  let jobs = $state<JobRecord[]>([]);
  let setupOpen = $state(true);
  let taskOpen = $state(false);
  let newSessionName = $state('');
  let newSessionKind = $state<'audiobook' | 'subtitles' | 'voiceover'>('audiobook');
  let newSourceMode = $state<'upload' | 'paste' | 'later'>('upload');
  let newSourceFile = $state<File | null>(null);
  let newSourceText = $state('');
  let capabilities = $state<Record<string, any>>({});
  let selectedSession = $state<SessionRecord | null>(null);
  let currentView = $state<'home' | 'providers' | 'voices'>('home');
  let wizardOpen = $state(false);
  let wizardRevision = $state(0);
  let disableWizard = $state(false);

  const tasks = [
    { kind: 'subtitles', title: 'Create subtitles', detail: 'Transcribe, correct, translate, review, and export.', icon: Captions },
    { kind: 'voiceover', title: 'Create a voiceover', detail: 'Guide a video, audio, or subtitle file through dubbing.', icon: AudioLines },
    { kind: 'audiobook', title: 'Generate an audiobook', detail: 'Turn a document or pasted text into polished narration.', icon: BookOpenText }
  ] as const;

  const navigation = [
    { label: 'Home', icon: Sparkles },
    { label: 'Sessions', icon: FolderClock },
    { label: 'Voices', icon: Library },
    { label: 'Workflows', icon: Workflow },
    { label: 'Providers', icon: Settings2 }
  ];

  const setupItems = $derived([
    { label: 'LLM providers', ready: true },
    { label: 'Voice references', ready: false },
    { label: 'Local speech tools', ready: Boolean(capabilities?.ffmpeg?.available) }
  ]);

  async function loadWorkspace() {
    const [sessionPayload, jobPayload, capabilityPayload] = await Promise.all([
      api<{ items: SessionRecord[] }>('/sessions'),
      api<{ items: JobRecord[] }>('/jobs?limit=8'),
      api<Record<string, any>>('/capabilities')
    ]);
    sessions = sessionPayload.items;
    jobs = jobPayload.items;
    capabilities = capabilityPayload;
    try {
      const wizard = await api<{ value: { visible?: boolean; version?: number; setup_completed?: boolean }; revision: number }>('/settings/wizard');
      wizardRevision = wizard.revision;
      wizardOpen = wizard.value.visible !== false;
      setupOpen = !wizard.value.setup_completed;
    } catch {
      wizardRevision = 0;
      wizardOpen = true;
      setupOpen = true;
    }
  }

  async function closeWizard() {
    wizardOpen = false;
    if (!disableWizard) return;
    try {
      const saved = await api<{revision:number}>('/settings/wizard', { method:'PUT', headers:{'If-Match': `"${wizardRevision}"`}, body:JSON.stringify({value:{visible:false,version:1,setup_completed:false}}) });
      wizardRevision = saved.revision;
    } catch (caught) { error = caught instanceof Error ? caught.message : String(caught); }
  }

  async function initialize() {
    try {
      const hash = new URLSearchParams(location.hash.slice(1));
      const bootstrap = hash.get('bootstrap');
      if (bootstrap) {
        await exchangeBootstrapToken(bootstrap);
        history.replaceState({}, '', location.pathname + location.search);
      }
      const status = await api<{ authenticated: boolean; csrf_token?: string }>('/auth/status');
      authenticated = status.authenticated;
      setCsrfToken(status.csrf_token);
      if (authenticated) await loadWorkspace();
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    } finally {
      loading = false;
    }
  }

  async function login() {
    error = '';
    try {
      const result = await api<{ authenticated: boolean; csrf_token: string }>('/auth/login', {
        method: 'POST',
        body: JSON.stringify({ password })
      });
      setCsrfToken(result.csrf_token);
      authenticated = true;
      password = '';
      await loadWorkspace();
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    }
  }

  function beginTask(kind: 'audiobook' | 'subtitles' | 'voiceover') {
    newSessionKind = kind;
    newSessionName = '';
    newSourceMode = kind === 'audiobook' ? 'upload' : 'upload';
    newSourceFile = null;
    newSourceText = '';
    taskOpen = true;
  }

  async function createSession() {
    if (!newSessionName.trim()) return;
    try {
      const included = newSessionKind === 'audiobook' ? ['clean_source','prepare_text','generate_audio','export'] : newSessionKind === 'voiceover' ? ['transcribe','correct','translate','generate_audio','export'] : ['transcribe','correct','translate','export'];
      const created = await api<SessionRecord>('/sessions', {
        method: 'POST',
        body: JSON.stringify({ name: newSessionName.trim(), workflow_kind: newSessionKind, workflow_preset: newSessionKind === 'voiceover' ? 'voiceover' : 'custom', included_stages: included })
      });
      const source = newSourceMode === 'paste' && newSourceText.trim() ? new File([newSourceText.trim()], `${newSessionName.trim()}.txt`, {type:'text/plain'}) : newSourceMode === 'upload' ? newSourceFile : null;
      if (source) {
        const form = new FormData(); form.set('session_id', created.id); form.set('file', source);
        await api('/uploads', {method:'POST', body:form});
      }
      sessions = [created, ...sessions];
      taskOpen = false;
      selectedSession = created;
      setupOpen = false;
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    }
  }

  function toggleTheme() {
    theme = theme === 'light' ? 'dark' : 'light';
    document.documentElement.dataset.theme = theme;
    localStorage.setItem('pandrator-theme', theme);
  }

  onMount(() => {
    theme = (localStorage.getItem('pandrator-theme') as 'light' | 'dark') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    document.documentElement.dataset.theme = theme;
    initialize();
  });
</script>

<svelte:head><title>Pandrator — Editorial audio workspace</title></svelte:head>

{#if loading}
  <main class="grid min-h-screen place-items-center"><div class="eyebrow animate-pulse">Preparing your workspace…</div></main>
{:else if !authenticated}
  <main class="grid min-h-screen place-items-center px-6 py-12">
    <section class="surface w-full max-w-md rounded-[2rem] p-8 md:p-10">
      <div class="mb-8 flex items-center gap-4">
        <div class="grid size-14 place-items-center rounded-2xl bg-[var(--accent)] text-2xl font-bold text-white shadow-lg">P</div>
        <div><div class="eyebrow">Pandrator</div><h1 class="text-2xl font-semibold">Welcome back</h1></div>
      </div>
      <p class="muted mb-7 leading-relaxed">Sign in to your private production workspace.</p>
      <label class="mb-2 block text-sm font-semibold" for="password">Owner password</label>
      <input id="password" bind:value={password} onkeydown={(event) => event.key === 'Enter' && login()} type="password" class="w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3" />
      {#if error}<p class="mt-3 text-sm text-red-600">{error}</p>{/if}
      <button onclick={login} class="mt-5 w-full rounded-xl bg-[var(--accent)] px-4 py-3 font-semibold text-white">Open workspace</button>
    </section>
  </main>
{:else}
  <div class="min-h-screen md:grid md:grid-cols-[15.5rem_1fr]">
    <aside class="border-b border-[var(--line)] bg-[color-mix(in_srgb,var(--paper-strong)_84%,transparent)] px-5 py-4 backdrop-blur-xl md:sticky md:top-0 md:h-screen md:border-r md:border-b-0 md:px-5 md:py-7">
      <div class="mb-7 flex items-center justify-between md:mb-10">
        <div class="flex items-center gap-3"><div class="grid size-10 place-items-center rounded-xl bg-[var(--accent)] font-bold text-white">P</div><div><div class="text-lg font-semibold leading-none">Pandrator</div><div class="muted mt-1 text-[.68rem] uppercase tracking-[.14em]">Editorial audio</div></div></div>
        <button onclick={toggleTheme} aria-label="Toggle theme" class="rounded-lg border border-[var(--line)] p-2 md:hidden">{#if theme === 'dark'}<Sun size={17}/>{:else}<Moon size={17}/>{/if}</button>
      </div>
      <nav class="hidden space-y-1 md:block" aria-label="Workspace">
        {#each navigation as item, index}
          {@const Icon = item.icon}
          <button onclick={() => { selectedSession=null; currentView=item.label === 'Providers' ? 'providers' : item.label === 'Voices' ? 'voices' : 'home'; setupOpen=currentView === 'home' ? setupOpen : false; }} class:active={(index === 0 && currentView === 'home') || (item.label === 'Providers' && currentView === 'providers') || (item.label === 'Voices' && currentView === 'voices')} class="nav-item flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left text-sm font-medium"><Icon size={18}/>{item.label}</button>
        {/each}
      </nav>
      <div class="absolute bottom-6 hidden left-5 right-5 md:block">
        <button onclick={toggleTheme} class="muted flex w-full items-center gap-3 rounded-xl border border-[var(--line)] px-3 py-2.5 text-sm">{#if theme === 'dark'}<Sun size={17}/> Light mode{:else}<Moon size={17}/> Dark mode{/if}</button>
      </div>
    </aside>

    <main class="px-5 py-7 sm:px-8 lg:px-12 lg:py-10 xl:px-16">
      {#if selectedSession}
        <SessionWorkspace session={selectedSession} onback={() => selectedSession = null} onupdated={(updated) => { selectedSession = updated; sessions = sessions.map((item) => item.id === updated.id ? updated : item); }}/>
      {:else if currentView === 'providers'}
        <ProviderManager onback={() => currentView='home'}/>
      {:else if currentView === 'voices'}
        <VoiceManager onback={() => currentView='home'}/>
      {:else}
      <header class="mx-auto mb-10 flex max-w-7xl items-end justify-between gap-8">
        <div><div class="eyebrow mb-3">Workspace overview</div><h1 class="max-w-3xl text-3xl font-semibold tracking-[-.035em] sm:text-4xl lg:text-5xl">What would you like to make?</h1><p class="muted mt-4 max-w-2xl text-base leading-relaxed">Start with an outcome. Pandrator will prepare the right stages and keep every artifact reviewable.</p></div>
        <button onclick={() => wizardOpen = true} class="hidden rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] px-4 py-2.5 text-sm font-semibold sm:flex sm:items-center sm:gap-2"><CircleHelp size={17}/> Open wizard</button>
      </header>

      {#if error}<div class="mx-auto mb-6 max-w-7xl rounded-xl border border-red-400/40 bg-red-500/10 px-4 py-3 text-sm">{error}</div>{/if}

      <section class="mx-auto grid max-w-7xl gap-4 lg:grid-cols-3" aria-label="New task">
        {#each tasks as task}
          {@const TaskIcon = task.icon}
          <button onclick={() => beginTask(task.kind)} class="surface lift group min-h-56 rounded-[1.6rem] p-6 text-left sm:p-7">
            <div class="mb-9 flex items-start justify-between"><div class="grid size-12 place-items-center rounded-2xl bg-[var(--accent-soft)] text-[var(--accent)]"><TaskIcon size={24}/></div><ChevronRight class="muted transition-transform group-hover:translate-x-1" size={20}/></div>
            <h2 class="text-xl font-semibold tracking-[-.02em]">{task.title}</h2><p class="muted mt-2 max-w-xs text-sm leading-relaxed">{task.detail}</p>
          </button>
        {/each}
      </section>

      <section class="mx-auto mt-10 grid max-w-7xl gap-7 xl:grid-cols-[1.45fr_.75fr]">
        <div>
          <div class="mb-4 flex items-center justify-between"><div><div class="eyebrow">Continue working</div><h2 class="mt-1 text-2xl font-semibold">Recent sessions</h2></div><button class="muted text-sm font-semibold">View all</button></div>
          <div class="surface overflow-hidden rounded-[1.5rem]">
            {#if sessions.length}
              {#each sessions.slice(0,5) as item, index}
                <button onclick={() => { selectedSession = item; setupOpen = false; }} class="flex w-full items-center gap-4 border-b border-[var(--line)] px-5 py-4 text-left last:border-0 hover:bg-[var(--accent-soft)]/35">
                  <div class="grid size-10 shrink-0 place-items-center rounded-xl bg-[var(--accent-soft)] text-[var(--accent)]">{#if item.workflow_kind === 'audiobook'}<BookOpenText size={19}/>{:else if item.workflow_kind === 'subtitles'}<Captions size={19}/>{:else}<FileAudio size={19}/>{/if}</div>
                  <div class="min-w-0 flex-1"><div class="truncate font-semibold">{item.name}</div><div class="muted mt-1 text-xs capitalize">{item.workflow_kind} · {item.status}</div></div>
                  <ChevronRight class="muted" size={18}/>
                </button>
              {/each}
            {:else}
              <div class="px-6 py-12 text-center"><WandSparkles class="mx-auto mb-3 text-[var(--accent)]" size={27}/><div class="font-semibold">Your first project starts above</div><p class="muted mt-1 text-sm">Choose an outcome and Pandrator will prepare the workflow.</p></div>
            {/if}
          </div>
        </div>
        <div>
          <div class="mb-4"><div class="eyebrow">Activity</div><h2 class="mt-1 text-2xl font-semibold">Jobs</h2></div>
          <div class="surface rounded-[1.5rem] p-5">
            {#if jobs.length}
              <div class="space-y-4">{#each jobs.slice(0,5) as job}<div><div class="mb-1.5 flex justify-between gap-3 text-sm"><span class="truncate font-semibold">{job.kind}</span><span class="muted capitalize">{job.status}</span></div><div class="h-1.5 overflow-hidden rounded-full bg-[var(--accent-soft)]"><div class="h-full rounded-full bg-[var(--accent)]" style={`width:${Math.max(3, job.progress*100)}%`}></div></div></div>{/each}</div>
            {:else}<div class="muted flex items-center gap-3 text-sm"><Radio size={18}/> No active work. Your queue is clear.</div>{/if}
          </div>
        </div>
      </section>
      {/if}
    </main>
  </div>

  {#if setupOpen}
    <aside class="surface fixed right-5 bottom-5 z-30 w-[min(23rem,calc(100vw-2.5rem))] rounded-2xl p-5 shadow-2xl">
      <div class="flex items-start justify-between gap-4"><div><div class="eyebrow">Setup checklist</div><h2 class="mt-1 text-lg font-semibold">Three details to check</h2></div><button onclick={() => setupOpen = false} aria-label="Close setup checklist" class="rounded-lg p-1.5 hover:bg-[var(--accent-soft)]"><X size={18}/></button></div>
      <div class="mt-4 space-y-2">{#each setupItems as item}<button onclick={() => { if (item.label === 'LLM providers') currentView='providers'; else if (item.label === 'Voice references') currentView='voices'; if (item.label !== 'Local speech tools') { selectedSession=null; setupOpen=false; } }} class="flex w-full items-center gap-3 rounded-xl border border-[var(--line)] px-3 py-2.5 text-left text-sm"><span class:item-ready={item.ready} class="size-2 rounded-full bg-[var(--warning)]"></span><span class="flex-1 font-medium">{item.label}</span><ChevronRight class="muted" size={16}/></button>{/each}</div>
      {#if capabilities?.gpu?.guidance}<p class="muted mt-3 rounded-xl bg-[var(--accent-soft)]/30 p-3 text-xs leading-relaxed">{capabilities.gpu.guidance}</p>{/if}
      <button onclick={() => wizardOpen=true} class="mt-4 w-full rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white">Return to guided setup</button>
    </aside>
  {:else}
    <button onclick={() => setupOpen = true} class="surface fixed right-5 bottom-5 z-30 flex items-center gap-2 rounded-full px-4 py-2.5 text-sm font-semibold"><Sparkles size={16} class="text-[var(--accent)]"/> Return to setup</button>
  {/if}

  {#if taskOpen}
    <div class="fixed inset-0 z-50 grid place-items-center bg-black/35 p-5 backdrop-blur-sm" role="presentation" onclick={(event) => event.target === event.currentTarget && (taskOpen=false)}>
      <div class="surface w-full max-w-lg rounded-[1.8rem] p-7" role="dialog" aria-modal="true" aria-labelledby="new-task-title">
        <div class="flex justify-between gap-5"><div><div class="eyebrow">New {newSessionKind}</div><h2 id="new-task-title" class="mt-1 text-2xl font-semibold">Name this session</h2></div><button onclick={() => taskOpen=false} class="rounded-lg p-2"><X size={19}/></button></div>
        <p class="muted mt-3 text-sm leading-relaxed">Choose a source now or leave it for the workspace. Pandrator will prepare the matching stages.</p>
        <label for="session-name" class="mt-6 mb-2 block text-sm font-semibold">Session name</label><input id="session-name" bind:value={newSessionName} onkeydown={(event) => event.key === 'Enter' && createSession()} placeholder="A descriptive title" class="w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3" />
        <div class="mt-5 grid grid-cols-3 gap-2"><button onclick={() => newSourceMode='upload'} class:active={newSourceMode==='upload'} class="rounded-xl border border-[var(--line)] px-3 py-2 text-xs font-semibold">Upload</button><button onclick={() => newSourceMode='paste'} class:active={newSourceMode==='paste'} class="rounded-xl border border-[var(--line)] px-3 py-2 text-xs font-semibold">Paste text</button><button onclick={() => newSourceMode='later'} class:active={newSourceMode==='later'} class="rounded-xl border border-[var(--line)] px-3 py-2 text-xs font-semibold">Choose later</button></div>
        {#if newSourceMode==='upload'}<label class="mt-4 block rounded-xl border border-dashed border-[var(--line)] p-4 text-sm"><span class="font-semibold">Source file</span><input type="file" class="mt-2 block w-full text-xs" onchange={(event) => { const file=(event.currentTarget as HTMLInputElement).files?.[0]??null; newSourceFile=file; if(file&&!newSessionName) newSessionName=file.name.replace(/\.[^.]+$/,''); }}/></label>{:else if newSourceMode==='paste'}<textarea bind:value={newSourceText} rows="5" placeholder="Paste the source text here…" class="mt-4 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] p-3 text-sm"></textarea>{/if}
        <div class="mt-6 flex justify-end gap-3"><button onclick={() => taskOpen=false} class="rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold">Cancel</button><button onclick={createSession} disabled={!newSessionName.trim()} class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-40"><Plus size={17}/> Create session</button></div>
      </div>
    </div>
  {/if}

  {#if wizardOpen}
    <div class="fixed inset-0 z-[70] grid place-items-center overflow-auto bg-black/45 p-4 backdrop-blur-md sm:p-8" role="presentation">
      <div class="surface my-auto min-w-0 w-full max-w-[min(72rem,calc(100vw-2rem))] overflow-x-hidden rounded-[2rem] p-6 shadow-2xl sm:p-9" role="dialog" aria-modal="true" aria-labelledby="wizard-title">
        <header class="flex flex-wrap items-start justify-between gap-5"><div><div class="eyebrow">Pandrator guide</div><h2 id="wizard-title" class="mt-2 text-3xl font-semibold tracking-[-.035em]">Where would you like to begin?</h2><p class="muted mt-3 max-w-2xl leading-relaxed">Resume a production or choose an outcome. The guide prepares the workspace while keeping every advanced control available later.</p></div><button onclick={closeWizard} aria-label="Close wizard" class="rounded-xl border border-[var(--line)] p-2"><X size={19}/></button></header>
        <div class="mt-8 grid gap-7 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.35fr)]"><section class="min-w-0"><div class="eyebrow mb-3">Recent sessions</div><div class="overflow-hidden rounded-2xl border border-[var(--line)]">{#each sessions.slice(0,4) as item}<button onclick={() => { selectedSession=item; wizardOpen=false; setupOpen=false; }} class="flex w-full items-center gap-3 border-b border-[var(--line)] p-4 text-left last:border-0 hover:bg-[var(--accent-soft)]"><div class="grid size-9 place-items-center rounded-xl bg-[var(--accent-soft)] text-[var(--accent)]">{#if item.workflow_kind==='audiobook'}<BookOpenText size={17}/>{:else}<Captions size={17}/>{/if}</div><div class="min-w-0 flex-1"><div class="truncate font-semibold">{item.name}</div><div class="muted text-xs capitalize">{item.workflow_kind} · {item.status}</div></div><ChevronRight size={16}/></button>{:else}<div class="muted p-7 text-center text-sm">No previous sessions yet.</div>{/each}</div></section>
          <section class="min-w-0"><div class="eyebrow mb-3">Create something new</div><div class="grid gap-3 md:grid-cols-2 xl:grid-cols-[repeat(3,minmax(0,1fr))]">{#each tasks as task}{@const WizardIcon=task.icon}<button onclick={() => { beginTask(task.kind); wizardOpen=false; }} class="lift min-w-0 overflow-hidden rounded-2xl border border-[var(--line)] bg-[var(--paper-strong)] p-4 text-left"><div class="mb-5 grid size-10 place-items-center rounded-xl bg-[var(--accent-soft)] text-[var(--accent)]"><WizardIcon size={19}/></div><h3 class="font-semibold">{task.title}</h3><p class="muted mt-1 text-xs leading-relaxed">{task.detail}</p></button>{/each}</div><button onclick={() => { wizardOpen=false; setupOpen=true; }} class="mt-4 flex w-full items-center gap-4 rounded-2xl border border-[var(--line)] bg-[var(--accent-soft)]/35 p-4 text-left"><Settings2 class="text-[var(--accent)]" size={21}/><div class="min-w-0 flex-1"><div class="font-semibold">Review setup and capabilities</div><p class="muted text-xs">Configure LLMs and voices; check local speech tools, FFmpeg, GPU readiness, privacy, and costs.</p></div><ChevronRight size={17}/></button></section></div>
        <div class="mt-7 grid gap-3 rounded-2xl border border-[var(--line)] p-4 text-sm md:grid-cols-3"><div><div class="font-semibold">Language intelligence</div><p class="muted mt-1 text-xs leading-relaxed">LLMs correct and translate subtitles, optimize TTS segments, and help clean extracted documents.</p></div><div><div class="font-semibold">Local-first control</div><p class="muted mt-1 text-xs leading-relaxed">Capability checks explain what is installed; this wizard never installs components itself.</p></div><div><div class="font-semibold">Review before output</div><p class="muted mt-1 text-xs leading-relaxed">Artifacts, edits, API costs, and exports remain visible and independently rerunnable.</p></div></div>
        <p class="muted mt-5 text-xs leading-relaxed">Privacy: local services keep media on this machine; cloud providers receive the content sent to them and may charge per token or audio unit. Outputs remain in Pandrator’s managed data directory and are never removed by cache retention.</p>
        <footer class="mt-4 flex flex-wrap items-center justify-between gap-4"><label class="muted flex items-center gap-2 text-sm"><input type="checkbox" bind:checked={disableWizard} class="size-4 accent-[var(--accent)]"/> Don’t open this wizard again</label><button onclick={closeWizard} class="rounded-xl bg-[var(--accent)] px-5 py-2.5 text-sm font-semibold text-white">Continue to workspace</button></footer>
      </div>
    </div>
  {/if}
{/if}

<style>
  .nav-item { color: var(--muted); }
  .nav-item:hover, .nav-item.active { color: var(--ink); background: var(--accent-soft); }
  .item-ready { background: var(--success); }
  button.active { color: var(--accent); background: var(--accent-soft); }
</style>
