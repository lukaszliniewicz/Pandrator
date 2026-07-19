<script lang="ts">
  import { ArchiveRestore, ChevronDown, CirclePlus, FileAudio, FileText, RefreshCw, Search, Trash2 } from '@lucide/svelte';
  import { api, type SessionRecord } from '$lib/api';
  import { appState } from '$lib/app-state.svelte';
  import ArtifactPreview from '$lib/ArtifactPreview.svelte';
  import NewSessionWizard from '$lib/NewSessionWizard.svelte';
  import { artifactRoleLabel } from '$lib/artifact-display';
  let items=$state<SessionRecord[]>([]); let search=$state(''); let showTrash=$state(false); let expanded=$state(''); let artifacts=$state<Record<string,any[]>>({}); let error=$state(''); let preview=$state<any|null>(null); let wizard=$state(false);
  const visible=$derived(items.filter((item)=>item.name.toLowerCase().includes(search.toLowerCase())));
  async function load(){try{items=(await api<{items:SessionRecord[]}>(`/sessions?include_trashed=${showTrash}`)).items}catch(caught){error=caught instanceof Error?caught.message:String(caught)}}
  async function expand(id:string){expanded=expanded===id?'':id;if(expanded&&!artifacts[id])artifacts[id]=(await api<{items:any[]}>(`/artifacts?session_id=${id}&limit=200`)).items}
  async function trash(item:SessionRecord){try{const updated=await api<SessionRecord>(`/sessions/${item.id}`,{method:'DELETE',headers:{'If-Match':`"${item.revision}"`}});items=items.map((value)=>value.id===item.id?updated:value);await appState.refresh()}catch(caught){error=caught instanceof Error?caught.message:String(caught)}}
  async function restore(item:SessionRecord){try{const updated=await api<SessionRecord>(`/sessions/${item.id}/restore`,{method:'POST',headers:{'If-Match':`"${item.revision}"`}});items=items.map((value)=>value.id===item.id?updated:value);await appState.refresh()}catch(caught){error=caught instanceof Error?caught.message:String(caught)}}
  async function reindex(item:SessionRecord){const result=await api<{reports:any[]}>(`/sessions/${item.id}/reindex`,{method:'POST'});error=result.reports.length?`${result.reports.length} artifact issue(s) found.`:'Reindex complete; no artifact problems found.'}
  $effect(()=>{showTrash;load()});
</script>

<div class="mx-auto max-w-7xl">
  <header class="flex flex-wrap items-end justify-between gap-5">
    <div><div class="eyebrow">Sessions</div><h1 class="mt-2 text-4xl font-semibold">Your workspaces</h1><p class="muted mt-3">Inspect sources, generated artifacts, revisions, and recoverable trash.</p></div>
    <div class="flex flex-wrap items-center gap-3"><label class="flex items-center gap-2 text-sm font-semibold"><input type="checkbox" bind:checked={showTrash} class="accent-[var(--accent)]"/> Show trash</label><button onclick={()=>wizard=true} class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white"><CirclePlus size={17}/> Add session</button></div>
  </header>
  {#if error}<div class="mt-5 rounded-xl border border-[var(--line)] bg-[var(--accent-soft)] p-3 text-sm">{error}</div>{/if}
  <div class="mt-7 flex items-center gap-3 rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] px-4"><Search class="muted" size={17}/><input bind:value={search} placeholder="Search sessions" class="w-full bg-transparent py-3 outline-none"/></div>
  <div class="surface mt-5 overflow-hidden rounded-2xl">
    {#each visible as item}
      <article class="border-b border-[var(--line)] last:border-0">
        <div class="flex flex-wrap items-center gap-3 p-4"><a href={`/sessions/${item.id}`} class="min-w-0 flex-1"><div class="truncate font-semibold">{item.name}</div><div class="muted mt-1 text-xs capitalize">{item.workflow_kind} · {item.status} · updated {new Date(item.updated_at).toLocaleString()}</div></a><button onclick={()=>reindex(item)} title="Reindex artifacts" class="tool"><RefreshCw size={16}/></button>{#if item.status==='trashed'}<button onclick={()=>restore(item)} class="tool"><ArchiveRestore size={16}/> Restore</button>{:else}<button onclick={()=>trash(item)} class="tool text-red-500"><Trash2 size={16}/> Trash</button>{/if}<button onclick={()=>expand(item.id)} class="tool"><ChevronDown class={expanded===item.id?'rotate-180':''} size={17}/></button></div>
        {#if expanded===item.id}
          <div class="border-t border-[var(--line)] bg-[var(--paper)] p-4"><div class="eyebrow mb-3">Artifacts</div><div class="grid gap-2 md:grid-cols-2">
            {#each artifacts[item.id]??[] as artifact}
              <button onclick={()=>{preview=artifact}} class="flex items-center gap-3 rounded-xl border border-[var(--line)] p-3 text-left">{#if artifact.kind==='audio'}<FileAudio size={17}/>{:else}<FileText size={17}/>{/if}<div class="min-w-0"><div class="truncate text-sm font-semibold">{artifactRoleLabel(artifact.role)}</div><div class="muted truncate text-xs">{artifact.relative_path} · {artifact.state}</div></div></button>
            {:else}<p class="muted text-sm">No registered artifacts.</p>{/each}
          </div></div>
        {/if}
      </article>
    {:else}<div class="muted p-10 text-center">No matching sessions.</div>{/each}
  </div>
</div>
{#if preview}<ArtifactPreview artifact={preview} onclose={()=>preview=null}/>{/if}
{#if wizard}<NewSessionWizard onclose={()=>wizard=false}/>{/if}
<style>.tool{display:flex;align-items:center;gap:.4rem;border:1px solid var(--line);border-radius:.7rem;padding:.55rem .7rem;font-size:.75rem;font-weight:650}</style>
