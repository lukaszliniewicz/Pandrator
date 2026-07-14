<script lang="ts">
  import { page } from '$app/state';
  import { Download, FileAudio, FileVideo, LoaderCircle, PackageCheck } from '@lucide/svelte';
  import { api } from '$lib/api';
  import ArtifactPreview from '$lib/ArtifactPreview.svelte';
  import { artifactRoleLabel } from '$lib/artifact-display';
  import OutputSettingsPanel from '$lib/OutputSettingsPanel.svelte';
  const sessionId=String(page.params.id);
  let artifacts=$state<any[]>([]); let runs=$state<any[]>([]); let selectedRunId=$state(''); let busy=$state(false); let message=$state(''); let error=$state(''); let preview=$state<any|null>(null);
  async function load(){
    const [artifactPayload,runPayload]=await Promise.all([api<{items:any[]}>(`/artifacts?session_id=${sessionId}&limit=300`),api<{items:any[]}>(`/sessions/${sessionId}/generation-runs`)]);
    artifacts=artifactPayload.items.filter((item:any)=>['export','assembled_audio','audiobook_audio','dubbing_audio','output_assembly','rvc_audio'].includes(item.role));
    runs=runPayload.items??[];
    if(!selectedRunId||!runs.some((item:any)=>item.id===selectedRunId))selectedRunId=runs.find((item:any)=>item.status==='completed')?.id??'';
  }
  async function waitForAssembly(runId:string){
    for(let attempt=0;attempt<300;attempt+=1){
      await new Promise((resolve)=>window.setTimeout(resolve,800));
      const result=await api<{items:any[]}>(`/sessions/${sessionId}/generation-runs`);
      runs=result.items??[];
      const assembly=runs.find((item:any)=>item.id===runId)?.assembly;
      if(assembly?.status==='completed')return assembly;
      if(['failed','canceled'].includes(assembly?.status))throw new Error(assembly.error_message||'The selected version could not be assembled.');
    }
    throw new Error('Assembly is still running. You can return later and export this version.');
  }
  async function assemble(){
    busy=true;error='';
    try{
      const selected=runs.find((item:any)=>item.id===selectedRunId);
      if(selectedRunId&&selected?.assembly?.status!=='completed'){
        message=`Assembling ${selected?.label??'the selected version'}…`;
        if(!['queued','running'].includes(selected?.assembly?.status))await api(`/sessions/${sessionId}/output-assemblies`,{method:'POST',body:JSON.stringify({generation_run_id:selectedRunId})});
        await waitForAssembly(selectedRunId);
      }
      const job=await api<any>(`/sessions/${sessionId}/stages/export/run`,{method:'POST',body:JSON.stringify(selectedRunId?{generation_run_id:selectedRunId}:{})});
      message=`Export queued as ${job.id.slice(0,8)}${selected?.label?` from ${selected.label}`:''}.`;
      await load();
    }catch(caught){error=caught instanceof Error?caught.message:String(caught)}finally{busy=false}
  }
  load();
</script>

<div class="space-y-5">
  <div class="flex flex-wrap items-end justify-between gap-4"><div><h2 class="text-2xl font-semibold">Output</h2><p class="muted mt-2">Choose the named generation version to assemble and export. Unchanged segments are inherited from earlier runs.</p></div><div class="flex flex-wrap items-end gap-2">{#if runs.length}<label class="text-xs font-semibold">Audio version<select bind:value={selectedRunId} class="mt-1 block max-w-sm rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] px-3 py-2 text-sm font-normal">{#each runs.filter((item:any)=>item.status==='completed') as item}<option value={item.id}>{item.label}</option>{/each}</select></label>{/if}<button onclick={assemble} disabled={busy||Boolean(runs.length&&!selectedRunId)} class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-50">{#if busy}<LoaderCircle class="animate-spin" size={16}/>{:else}<PackageCheck size={16}/>{/if} Create export</button></div></div>
  {#if message}<p class="rounded-xl bg-[var(--accent-soft)] p-3 text-sm">{message}</p>{/if}{#if error}<p class="rounded-xl bg-red-500/10 p-3 text-sm text-red-500">{error}</p>{/if}
  <OutputSettingsPanel {sessionId}/>
  <section class="surface rounded-2xl p-5"><div class="eyebrow">Completed outputs</div><div class="mt-4 grid gap-3 md:grid-cols-2">
    {#each artifacts as artifact}<button onclick={()=>{preview=artifact}} class="flex items-center gap-3 rounded-xl border border-[var(--line)] p-4 text-left">{#if artifact.kind==='video'}<FileVideo size={18}/>{:else}<FileAudio size={18}/>{/if}<div class="min-w-0 flex-1"><div class="truncate font-semibold">{artifactRoleLabel(artifact.role)}</div><div class="muted truncate text-xs">{artifact.relative_path}</div></div><Download size={16}/></button>{:else}<p class="muted text-sm">No completed outputs yet.</p>{/each}
  </div></section>
</div>
{#if preview}<ArtifactPreview artifact={preview} onclose={()=>preview=null}/>{/if}
