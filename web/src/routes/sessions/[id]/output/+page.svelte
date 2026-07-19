<script lang="ts">
  import { page } from '$app/state';
  import { onMount } from 'svelte';
  import { CheckCircle2, CircleAlert, Download, Eye, FileAudio, FileText, FileVideo, LoaderCircle, PackageCheck, Trash2 } from '@lucide/svelte';
  import { api, type JobRecord } from '$lib/api';
  import ArtifactPreview from '$lib/ArtifactPreview.svelte';
  import { artifactFilename, artifactRoleLabel, formatBytes } from '$lib/artifact-display';
  import OutputSettingsPanel from '$lib/OutputSettingsPanel.svelte';
  const sessionId=String(page.params.id);
  let artifacts=$state<any[]>([]); let runs=$state<any[]>([]); let exportJobs=$state<JobRecord[]>([]); let session=$state<any>(null); let outputProfile=$state<any>(null); let selectedRunId=$state(''); let busy=$state(false); let message=$state(''); let error=$state(''); let preview=$state<any|null>(null); let deleting=$state<Record<string,boolean>>({});
  let settingsPanel=$state<{saveForExport:()=>Promise<{output:Record<string,unknown>;audio:Record<string,unknown>}>}|null>(null);
  async function load(){
    const [artifactPayload,runPayload,sessionPayload,settingsPayload,jobPayload]=await Promise.all([api<{items:any[]}>(`/artifacts?session_id=${sessionId}&limit=300`),api<{items:any[]}>(`/sessions/${sessionId}/generation-runs`),api<any>(`/sessions/${sessionId}`),api<any>(`/sessions/${sessionId}/settings/output`),api<{items:JobRecord[]}>(`/jobs?limit=500`)]);
    artifacts=artifactPayload.items.filter((item:any)=>item.role==='export'||item.role.startsWith('export_')||['assembled_audio','audiobook_audio','dubbing_audio','output_assembly','rvc_audio'].includes(item.role)).sort((left:any,right:any)=>String(right.created_at).localeCompare(String(left.created_at)));
    runs=runPayload.items??[];
    exportJobs=(jobPayload.items??[]).filter((item)=>item.session_id===sessionId&&item.kind==='export.create').slice(0,8);
    session=sessionPayload;
    outputProfile=settingsPayload;
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
      if(!settingsPanel)throw new Error('Output settings are still loading. Please try again.');
      const savedProfile=await settingsPanel.saveForExport();
      const selected=runs.find((item:any)=>item.id===selectedRunId);
      const effective=savedProfile.output??{};
      const usesGeneratedAudio=session?.workflow_kind==='audiobook'||(effective.export_mode==='media'&&effective.audio_mode!=='preserve');
      if(usesGeneratedAudio&&!selectedRunId)throw new Error('Select a completed audio version for this media export.');
      const needsAssembly=usesGeneratedAudio&&Boolean(selectedRunId);
      const resolvedAssemblySettings=needsAssembly?await api<any>(`/sessions/${sessionId}/settings/resolve`,{method:'POST',body:JSON.stringify({sections:['audio','output']})}):null;
      const assemblyMatchesSettings=selected?.assembly?.settings_hash===resolvedAssemblySettings?.settings_hash;
      const assemblyIsCurrent=Boolean(
        needsAssembly&&
        selected?.assembly?.status==='completed'&&
        assemblyMatchesSettings
      );
      if(needsAssembly&&!assemblyIsCurrent){
        message=`Assembling ${selected?.label??'the selected version'}…`;
        if(!assemblyMatchesSettings||!['queued','running'].includes(selected?.assembly?.status))await api(`/sessions/${sessionId}/output-assemblies`,{method:'POST',body:JSON.stringify({generation_run_id:selectedRunId})});
        await waitForAssembly(selectedRunId);
      }
      const job=await api<any>(`/sessions/${sessionId}/stages/export/run`,{method:'POST',body:JSON.stringify(needsAssembly?{generation_run_id:selectedRunId}:{})});
      exportJobs=[job,...exportJobs.filter((item)=>item.id!==job.id)].slice(0,8);
      message=`Export ${job.id.slice(0,8)} was submitted${needsAssembly&&selected?.label?` from ${selected.label}`:''}. Live progress is shown below.`;
      await load();
    }catch(caught){error=caught instanceof Error?caught.message:String(caught)}finally{busy=false}
  }
  function canRemove(artifact:any){return artifact.kind==='export'||artifact.role==='export'||String(artifact.role??'').startsWith('export_')}
  async function removeExport(artifact:any){
    if(deleting[artifact.id]||!window.confirm(`Remove ${artifact.relative_path.split('/').at(-1)??'this export'}? This deletes the exported file but leaves its source artifacts intact.`))return;
    deleting={...deleting,[artifact.id]:true}; error='';
    try{await api(`/sessions/${sessionId}/outputs/${artifact.id}`,{method:'DELETE'});artifacts=artifacts.filter((item)=>item.id!==artifact.id);if(preview?.id===artifact.id)preview=null;message='Export removed.'}
    catch(caught){error=caught instanceof Error?caught.message:String(caught)}
    finally{deleting={...deleting,[artifact.id]:false}}
  }
  function jobLabel(job:JobRecord){return job.status==='running'?'Running':job.status==='queued'?'Queued':job.status==='succeeded'?'Completed':job.status==='failed'?'Failed':job.status.replaceAll('_',' ')}
  onMount(()=>{
    let disposed=false;
    let refreshing=false;
    const refresh=async()=>{if(disposed||refreshing)return;refreshing=true;try{await load()}catch(caught){if(!disposed)error=caught instanceof Error?caught.message:String(caught)}finally{refreshing=false}};
    refresh();
    const timer=window.setInterval(()=>{if(exportJobs.some((item)=>['queued','running','cancel_requested'].includes(item.status)))refresh()},900);
    const changed=()=>refresh();
    window.addEventListener('pandrator:generation-changed',changed);
    return()=>{disposed=true;window.clearInterval(timer);window.removeEventListener('pandrator:generation-changed',changed)};
  });
</script>

<div class="space-y-5">
  <div class="flex flex-wrap items-end justify-between gap-4"><div><h2 class="text-2xl font-semibold">{session?.workflow_kind==='subtitles'?'Export subtitles':session?.workflow_kind==='audiobook'?'Audiobook output':outputProfile?.context?.has_source_video?'Video output':'Voiceover output'}</h2><p class="muted mt-2">{session?.workflow_kind==='subtitles'?'Save the selected subtitle document as SRT, WebVTT, or concatenated plain text.':session?.workflow_kind==='audiobook'?'Assemble the selected narration takes with book metadata, chapters, and optional cover artwork.':outputProfile?.context?.has_source_video?'Create a mixed, source-only, or voiceover-only video with optional subtitle tracks.':'Create standalone voiceover audio plus optional subtitle or text documents.'}</p></div><div class="flex flex-wrap items-end gap-2">{#if runs.length&&session?.workflow_kind!=='subtitles'}<label class="text-xs font-semibold">Audio version<select bind:value={selectedRunId} class="mt-1 block max-w-sm rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] px-3 py-2 text-sm font-normal"><option value="">Do not select generated audio</option>{#each runs.filter((item:any)=>item.status==='completed') as item}<option value={item.id}>{item.label}</option>{/each}</select></label>{/if}<button onclick={assemble} disabled={busy} class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-50">{#if busy}<LoaderCircle class="animate-spin" size={16}/>{:else}<PackageCheck size={16}/>{/if} {session?.workflow_kind==='subtitles'?'Create subtitle export':'Create export'}</button></div></div>
  {#if message}<p class="rounded-xl bg-[var(--accent-soft)] p-3 text-sm">{message}</p>{/if}{#if error}<p class="rounded-xl bg-red-500/10 p-3 text-sm text-red-500">{error}</p>{/if}
  {#if exportJobs.length}<section class="surface rounded-2xl p-5"><div class="eyebrow">Export activity</div><div class="mt-4 space-y-3">{#each exportJobs.slice(0,4) as job (job.id)}<div class="flex flex-wrap items-center gap-3 rounded-xl border border-[var(--line)] p-3">{#if ['queued','running','cancel_requested'].includes(job.status)}<LoaderCircle class="animate-spin text-[var(--accent)]" size={18}/>{:else if job.status==='succeeded'}<CheckCircle2 class="text-[var(--success)]" size={18}/>{:else}<CircleAlert class="text-red-500" size={18}/>{/if}<div class="min-w-0 flex-1"><div class="font-semibold">{jobLabel(job)} export <span class="muted font-mono text-xs">{job.id.slice(0,8)}</span></div><div class="muted mt-1 text-xs">{Math.round(Number(job.progress??0)*100)}% · {new Date(job.created_at).toLocaleString()}</div>{#if job.error_message}<div class="mt-1 text-xs text-red-500">{job.error_message}</div>{/if}</div><div class="h-1.5 w-32 overflow-hidden rounded-full bg-[var(--line)]"><div class="h-full bg-[var(--accent)] transition-[width]" style={`width:${Math.max(job.status==='running'?2:0,Number(job.progress??0)*100)}%`}></div></div></div>{/each}</div></section>{/if}
  <OutputSettingsPanel {sessionId} bind:this={settingsPanel}/>
  <section class="surface rounded-2xl p-5"><div class="eyebrow">Completed outputs</div><div class="mt-4 space-y-2">
    {#each artifacts as artifact}<article class="flex w-full flex-wrap items-center gap-3 rounded-xl border border-[var(--line)] px-4 py-3"><div class="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-[var(--accent-soft)] text-[var(--accent)]">{#if String(artifact.mime_type??'').startsWith('video/')||String(artifact.relative_path??'').endsWith('.mp4')}<FileVideo size={19}/>{:else if String(artifact.mime_type??'').startsWith('audio/')}<FileAudio size={19}/>{:else}<FileText size={19}/>{/if}</div><button onclick={()=>{preview=artifact}} class="min-w-0 flex-1 text-left"><div class="flex min-w-0 flex-wrap items-baseline gap-x-2"><strong class="truncate">{artifactFilename(artifact)}</strong><span class="muted text-xs">{artifactRoleLabel(artifact.role)}</span></div><div class="muted mt-1 flex flex-wrap gap-x-2 gap-y-1 text-xs"><time datetime={artifact.created_at}>{new Date(artifact.created_at).toLocaleString()}</time><span>· {artifact.mime_type||artifact.kind||'File'}</span>{#if artifact.size_bytes!=null}<span>· {formatBytes(artifact.size_bytes)}</span>{/if}<span class="max-w-full truncate">· {artifact.relative_path}</span></div></button><div class="ml-auto flex items-center gap-1"><button onclick={()=>{preview=artifact}} class="rounded-lg p-2 hover:bg-[var(--accent-soft)]" title="Preview output" aria-label={`Preview ${artifactFilename(artifact)}`}><Eye size={16}/></button><a href={`/api/v1/artifacts/${artifact.id}/content`} download={artifactFilename(artifact)} class="rounded-lg p-2 hover:bg-[var(--accent-soft)]" title="Download output" aria-label={`Download ${artifactFilename(artifact)}`}><Download size={16}/></a>{#if canRemove(artifact)}<button onclick={()=>removeExport(artifact)} disabled={deleting[artifact.id]} aria-label={`Remove export ${artifactFilename(artifact)}`} title="Remove export" class="rounded-lg p-2 text-red-500 hover:bg-red-500/10 disabled:opacity-50">{#if deleting[artifact.id]}<LoaderCircle class="animate-spin" size={16}/>{:else}<Trash2 size={16}/>{/if}</button>{/if}</div></article>{:else}<p class="muted text-sm">No completed outputs yet.</p>{/each}
  </div></section>
</div>
{#if preview}<ArtifactPreview artifact={preview} onclose={()=>preview=null}/>{/if}
