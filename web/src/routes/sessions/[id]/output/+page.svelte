<script lang="ts">
  import { page } from '$app/state';
  import { Download, FileAudio, FileText, FileVideo, LoaderCircle, PackageCheck } from '@lucide/svelte';
  import { api } from '$lib/api';
  import ArtifactPreview from '$lib/ArtifactPreview.svelte';
  import { artifactRoleLabel } from '$lib/artifact-display';
  import OutputSettingsPanel from '$lib/OutputSettingsPanel.svelte';
  const sessionId=String(page.params.id);
  let artifacts=$state<any[]>([]); let runs=$state<any[]>([]); let session=$state<any>(null); let selectedRunId=$state(''); let busy=$state(false); let message=$state(''); let error=$state(''); let preview=$state<any|null>(null);
  async function load(){
    const [artifactPayload,runPayload,sessionPayload]=await Promise.all([api<{items:any[]}>(`/artifacts?session_id=${sessionId}&limit=300`),api<{items:any[]}>(`/sessions/${sessionId}/generation-runs`),api<any>(`/sessions/${sessionId}`)]);
    artifacts=artifactPayload.items.filter((item:any)=>item.role==='export'||item.role.startsWith('export_')||['assembled_audio','audiobook_audio','dubbing_audio','output_assembly','rvc_audio'].includes(item.role));
    runs=runPayload.items??[];
    session=sessionPayload;
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
      const outputSettings=await api<any>(`/sessions/${sessionId}/settings/output`);
      const effective=outputSettings.effective??{};
      const usesGeneratedAudio=effective.export_mode==='media'&&effective.audio_mode!=='preserve';
      if(usesGeneratedAudio&&!selectedRunId)throw new Error('Select a completed audio version for this media export.');
      const needsAssembly=usesGeneratedAudio&&Boolean(selectedRunId);
      if(needsAssembly&&selected?.assembly?.status!=='completed'){
        message=`Assembling ${selected?.label??'the selected version'}…`;
        if(!['queued','running'].includes(selected?.assembly?.status))await api(`/sessions/${sessionId}/output-assemblies`,{method:'POST',body:JSON.stringify({generation_run_id:selectedRunId})});
        await waitForAssembly(selectedRunId);
      }
      const job=await api<any>(`/sessions/${sessionId}/stages/export/run`,{method:'POST',body:JSON.stringify(needsAssembly?{generation_run_id:selectedRunId}:{})});
      message=`Export queued as ${job.id.slice(0,8)}${needsAssembly&&selected?.label?` from ${selected.label}`:''}.`;
      await load();
    }catch(caught){error=caught instanceof Error?caught.message:String(caught)}finally{busy=false}
  }
  load();
</script>

<div class="space-y-5">
  <div class="flex flex-wrap items-end justify-between gap-4"><div><h2 class="text-2xl font-semibold">{session?.workflow_kind==='subtitles'?'Export subtitles':'Output'}</h2><p class="muted mt-2">{session?.workflow_kind==='subtitles'?'Save the selected subtitle document as SRT, WebVTT, or concatenated plain text.':'Export video/media, standalone SRT or VTT subtitles, or cue text as one concatenated transcript.'}</p></div><div class="flex flex-wrap items-end gap-2">{#if runs.length&&session?.workflow_kind!=='subtitles'}<label class="text-xs font-semibold">Audio version<select bind:value={selectedRunId} class="mt-1 block max-w-sm rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] px-3 py-2 text-sm font-normal"><option value="">Do not select generated audio</option>{#each runs.filter((item:any)=>item.status==='completed') as item}<option value={item.id}>{item.label}</option>{/each}</select></label>{/if}<button onclick={assemble} disabled={busy} class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-50">{#if busy}<LoaderCircle class="animate-spin" size={16}/>{:else}<PackageCheck size={16}/>{/if} {session?.workflow_kind==='subtitles'?'Create subtitle export':'Create export'}</button></div></div>
  {#if message}<p class="rounded-xl bg-[var(--accent-soft)] p-3 text-sm">{message}</p>{/if}{#if error}<p class="rounded-xl bg-red-500/10 p-3 text-sm text-red-500">{error}</p>{/if}
  <OutputSettingsPanel {sessionId}/>
  <section class="surface rounded-2xl p-5"><div class="eyebrow">Completed outputs</div><div class="mt-4 grid gap-3 md:grid-cols-2">
    {#each artifacts as artifact}<button onclick={()=>{preview=artifact}} class="flex items-center gap-3 rounded-xl border border-[var(--line)] p-4 text-left">{#if String(artifact.mime_type??'').startsWith('video/')||String(artifact.relative_path??'').endsWith('.mp4')}<FileVideo size={18}/>{:else if String(artifact.mime_type??'').startsWith('audio/')}<FileAudio size={18}/>{:else}<FileText size={18}/>{/if}<div class="min-w-0 flex-1"><div class="truncate font-semibold">{artifactRoleLabel(artifact.role)}</div><div class="muted truncate text-xs">{artifact.relative_path}</div></div><Download size={16}/></button>{:else}<p class="muted text-sm">No completed outputs yet.</p>{/each}
  </div></section>
</div>
{#if preview}<ArtifactPreview artifact={preview} onclose={()=>preview=null}/>{/if}
