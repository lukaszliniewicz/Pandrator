<script lang="ts">
  import { page } from '$app/state';
  import { Activity, AudioLines, BookOpenText, Captions, ChevronRight, CirclePlus, FolderClock, ServerCog } from '@lucide/svelte';
  import { appState } from '$lib/app-state.svelte';
  import { api } from '$lib/api';
  import NewSessionWizard from '$lib/NewSessionWizard.svelte';
  import SetupChecklist from '$lib/SetupChecklist.svelte';
  import { onMount } from 'svelte';
  let wizard = $state(false);
  let initialKind = $state<'audiobook'|'subtitles'|'voiceover'>('subtitles');
  let skipKindStep = $state(false);
  function start(kind: typeof initialKind) { initialKind=kind;skipKindStep=true;wizard=true; }
  const setupOpen=$derived(page.url.searchParams.get('setup')==='1');
  onMount(async()=>{if(sessionStorage.getItem('pandrator-guided-creation-shown'))return;try{const setting=await api<any>('/settings/web.preferences');if(setting.value?.show_startup_wizard){wizard=true;sessionStorage.setItem('pandrator-guided-creation-shown','1')}}catch{}});
</script>

<div class="mx-auto max-w-7xl">
  <header class="flex flex-wrap items-end justify-between gap-6"><div><div class="eyebrow">Home</div><h1 class="mt-2 text-4xl font-semibold tracking-[-.04em]">What shall we make?</h1><p class="muted mt-3 max-w-2xl">Start with a clear outcome or return to a session. Advanced controls remain close, without crowding the first decision.</p></div><button onclick={()=>{initialKind='subtitles';skipKindStep=false;wizard=true}} class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-3 text-sm font-semibold text-white"><CirclePlus size={18}/> New session</button></header>
  <section class="mt-9 grid gap-4 md:grid-cols-3"><button onclick={()=>start('subtitles')} class="task"><Captions size={26}/><h2>Create subtitles</h2><p>Transcribe media or refine and translate an existing SRT.</p></button><button onclick={()=>start('voiceover')} class="task"><AudioLines size={26}/><h2>Create a voiceover</h2><p>Build dubbed audio directly from subtitles or start with media.</p></button><button onclick={()=>start('audiobook')} class="task"><BookOpenText size={26}/><h2>Generate an audiobook</h2><p>Clean, prepare, narrate, review, and export long-form text.</p></button></section>
  <div class="mt-10 grid gap-7 xl:grid-cols-[1.4fr_.8fr]">
    <section><div class="mb-3 flex items-center justify-between"><div class="eyebrow">Recent sessions</div><a href="/sessions" class="muted flex items-center gap-1 text-xs font-semibold">View all <ChevronRight size={14}/></a></div><div class="surface overflow-hidden rounded-2xl">{#each appState.sessions.slice(0,6) as session}<a href={`/sessions/${session.id}`} class="flex items-center gap-4 border-b border-[var(--line)] p-4 last:border-0 hover:bg-[var(--accent-soft)]"><div class="grid size-10 place-items-center rounded-xl bg-[var(--accent-soft)] text-[var(--accent)]">{#if session.workflow_kind==='audiobook'}<BookOpenText size={18}/>{:else if session.workflow_kind==='voiceover'}<AudioLines size={18}/>{:else}<Captions size={18}/>{/if}</div><div class="min-w-0 flex-1"><div class="truncate font-semibold">{session.name}</div><div class="muted mt-1 text-xs capitalize">{session.workflow_kind} · {session.status}</div></div><ChevronRight size={17}/></a>{:else}<div class="muted p-9 text-center text-sm">No sessions yet. Start with one of the outcome tiles above.</div>{/each}</div></section>
    <aside><div class="eyebrow mb-3">Readiness</div><div class="surface rounded-2xl p-5"><div class="space-y-4"><div class="readiness"><ServerCog size={18}/><div><strong>Speech services</strong><span>{Object.values(appState.capabilities?.services??{}).filter(Boolean).length} installed components detected</span></div></div><div class="readiness"><Activity size={18}/><div><strong>Background work</strong><span>{appState.jobs.filter((job)=>['queued','running'].includes(job.status)).length} active or queued jobs</span></div></div><div class="readiness"><FolderClock size={18}/><div><strong>Sessions</strong><span>{appState.sessions.length} available workspaces</span></div></div></div><a href="/providers" onclick={()=>appState.showSetupReturn('Configure providers and services, then return to finish setup.')} class="mt-5 block rounded-xl border border-[var(--line)] px-4 py-2.5 text-center text-sm font-semibold">Review setup</a></div></aside>
  </div>
</div>
{#if wizard}<NewSessionWizard initialKind={initialKind} startAtSource={skipKindStep} onclose={()=>wizard=false}/>{/if}
{#if setupOpen}<SetupChecklist onclose={()=>location.href='/'}/>{/if}

<style>
  .task{display:block;min-height:12rem;border:1px solid var(--line);border-radius:1.5rem;background:var(--paper-strong);padding:1.5rem;text-align:left;box-shadow:var(--shadow)}.task :global(svg){color:var(--accent)}.task h2{margin-top:1.3rem;font-size:1.15rem;font-weight:700}.task p{margin-top:.5rem;color:var(--muted);font-size:.85rem;line-height:1.55}.task:hover{border-color:var(--accent);transform:translateY(-2px)}.readiness{display:flex;gap:.8rem;align-items:flex-start}.readiness :global(svg){color:var(--accent);flex:none}.readiness strong,.readiness span{display:block}.readiness strong{font-size:.85rem}.readiness span{margin-top:.15rem;color:var(--muted);font-size:.72rem}
</style>
