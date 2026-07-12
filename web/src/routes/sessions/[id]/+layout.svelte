<script lang="ts">
  import { page } from '$app/state';
  import { setContext } from 'svelte';
  import type { Snippet } from 'svelte';
  import { Activity, AudioLines, ChevronLeft, FileText, Layers3, Settings2, Sparkles, WandSparkles } from '@lucide/svelte';
  import { api, type SessionRecord } from '$lib/api';
  import { appState } from '$lib/app-state.svelte';
  import { SESSION_CONTEXT, type SessionContext } from '$lib/session-context';
  import WorkflowCustomizer from '$lib/WorkflowCustomizer.svelte';
  import GenerationDrawer from '$lib/GenerationDrawer.svelte';
  let {children}:{children:Snippet}=$props();
  let customizeOpen=$state(false);
  const contextState:SessionContext=$state({session:null,outcome:null,loading:true,error:'',reload,customize:()=>customizeOpen=true});
  async function reload(){contextState.loading=true;try{const [session,outcome]=await Promise.all([api<SessionRecord>(`/sessions/${page.params.id}`),api(`/sessions/${page.params.id}/outcome-plan`)]);contextState.session=session;contextState.outcome=outcome;appState.upsertSession(session)}catch(caught){contextState.error=caught instanceof Error?caught.message:String(caught)}finally{contextState.loading=false}}
  setContext(SESSION_CONTEXT,contextState);reload();
  const tabs=[{href:'',label:'Overview',icon:Sparkles},{href:'/sources',label:'Sources',icon:Layers3},{href:'/text',label:'Text & subtitles',icon:FileText},{href:'/voice',label:'Voice & audio',icon:AudioLines},{href:'/output',label:'Output',icon:Settings2},{href:'/activity',label:'Activity',icon:Activity},{href:'/cleaning',label:'Cleaning',icon:WandSparkles}];
  const active=(suffix:string)=>suffix?page.url.pathname.endsWith(suffix):page.url.pathname===`/sessions/${page.params.id}`;
</script>
{#if contextState.loading}
  <div class="surface grid min-h-64 place-items-center rounded-3xl"><div class="eyebrow animate-pulse">Loading session…</div></div>
{:else if contextState.session}
  <div class="session-shell mx-auto max-w-[100rem]">
    <a href="/sessions" class="muted flex items-center gap-1 text-sm font-semibold"><ChevronLeft size={16}/> Sessions</a>
    <header class="mt-5 flex flex-wrap items-end justify-between gap-5"><div><div class="eyebrow capitalize">{contextState.session.workflow_kind} workspace</div><h1 class="mt-1 text-3xl font-semibold tracking-[-.035em]">{contextState.session.name}</h1><div class="muted mt-2 text-xs capitalize">{contextState.session.status} · {contextState.outcome?.value?.focus??'custom'} plan</div></div><button onclick={()=>customizeOpen=true} class="flex items-center gap-2 rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] px-4 py-2.5 text-sm font-semibold"><Settings2 size={16}/> Customize workflow</button></header>
    <nav class="mt-7 flex gap-1 overflow-x-auto border-b border-[var(--line)]">{#each tabs as tab}{@const Icon=tab.icon}<a href={`/sessions/${page.params.id}${tab.href}`} class:active={active(tab.href)} class="session-tab flex shrink-0 items-center gap-2 px-3 py-3 text-sm font-semibold"><Icon size={16}/>{tab.label}</a>{/each}</nav>
    <div class="py-7">{@render children()}</div>
    <GenerationDrawer sessionId={contextState.session.id}/>
  </div>
  {#if customizeOpen}<WorkflowCustomizer sessionId={contextState.session.id} onclose={()=>customizeOpen=false} onsaved={contextState.reload}/>{/if}
{:else}<p class="text-red-500">{contextState.error||'Session not found.'}</p>{/if}
<style>.session-tab{border-bottom:2px solid transparent;color:var(--muted)}.session-tab.active{border-color:var(--accent);color:var(--ink)}</style>
