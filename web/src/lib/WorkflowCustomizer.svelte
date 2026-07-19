<script lang="ts">
  import { ArrowRight, X } from '@lucide/svelte';
  import { api, type SessionRecord } from './api';
  import { LANGUAGE_OPTIONS } from './settings-fields';

  let { sessionId, onclose, onsaved }: { sessionId:string; onclose:()=>void; onsaved:()=>void }=$props();
  let payload=$state<any>(null);
  let session=$state<SessionRecord|null>(null);
  let sourceLanguage=$state('auto');
  let targetLanguage=$state('en');
  let originalWorkflowKind=$state('audiobook');
  let error=$state('');
  let saving=$state(false);
  const transformations=$derived(payload?.value?.transformations??{});
  const deliverables=$derived(payload?.value?.deliverables??{});
  const speechOptimizationEnabled=$derived(Boolean(transformations.llm_tts_document_optimization||transformations.llm_tts_optimization));
  const speechOptimizationTiming=$derived(transformations.llm_tts_document_optimization?'document':'generation');

  function setSpeechOptimizationEnabled(enabled:boolean){
    transformations.llm_tts_document_optimization=enabled&&speechOptimizationTiming==='document';
    transformations.llm_tts_optimization=enabled&&speechOptimizationTiming==='generation';
  }

  function setSpeechOptimizationTiming(timing:string){
    transformations.llm_tts_document_optimization=speechOptimizationEnabled&&timing==='document';
    transformations.llm_tts_optimization=speechOptimizationEnabled&&timing==='generation';
  }

  function setTransformation(key:string,enabled:boolean){
    transformations[key]=enabled;
    if(key==='translate'){
      if(enabled&&transformations.generate_audio) payload.value.inputs.generation='translation';
      if(!enabled&&payload.value.inputs.generation==='translation') payload.value.inputs.generation=transformations.correct?'correction':'source';
    }
    if(key==='generate_audio'&&enabled&&transformations.translate) payload.value.inputs.generation='translation';
    if(key==='correct'&&!enabled){
      if(payload.value.inputs.translation==='correction') payload.value.inputs.translation='source';
      if(payload.value.inputs.generation==='correction') payload.value.inputs.generation=transformations.translate?'translation':'source';
    }
  }

  function setWorkflowKind(kind:'subtitles'|'voiceover'){
    if(!payload?.value)return;
    payload.value.workflow_kind=kind;
    deliverables.audiobook=false;
    if(kind==='subtitles'){
      deliverables.subtitles=true;
      deliverables.voiceover=false;
      transformations.generate_audio=false;
      transformations.rvc=false;
      transformations.deterministic_normalization=false;
      transformations.llm_tts_document_optimization=false;
      transformations.llm_tts_optimization=false;
      payload.value.export={...(payload.value.export??{}),mode:'subtitles',audio:'preserve',subtitle_mode:'none'};
    }else{
      deliverables.voiceover=true;
      transformations.generate_audio=true;
      if(transformations.translate) payload.value.inputs.generation='translation';
      transformations.deterministic_normalization=true;
      payload.value.export={...(payload.value.export??{}),mode:'media',audio:'generated'};
    }
  }

  async function load(){
    [payload,session]=await Promise.all([api(`/sessions/${sessionId}/outcome-plan`),api<SessionRecord>(`/sessions/${sessionId}`)]);
    originalWorkflowKind=session.workflow_kind;
    payload.value.workflow_kind=payload.value.workflow_kind??session.workflow_kind;
    sourceLanguage=session.source_language??'auto';
    targetLanguage=session.target_language??'en';
  }

  async function save(){
    saving=true;
    try{
      if(session) await api(`/sessions/${sessionId}`,{method:'PATCH',headers:{'If-Match':`"${session.revision}"`},body:JSON.stringify({source_language:sourceLanguage,target_language:transformations.translate?targetLanguage:null})});
      const selectedWorkflowKind=String(payload.value.workflow_kind??originalWorkflowKind);
      if(selectedWorkflowKind!==originalWorkflowKind&&['subtitles','voiceover'].includes(selectedWorkflowKind)){
        const output=await api<any>(`/sessions/${sessionId}/settings/output`);
        const nextOutput=selectedWorkflowKind==='subtitles'
          ? {...output.override,export_mode:'subtitles',audio_mode:'preserve',subtitle_mode:'none',subtitle_selection:'source'}
          : {...output.override,export_mode:'media',audio_mode:'dubbing_only',subtitle_mode:output.override?.subtitle_mode==='burned'?'burned':'soft'};
        await api(`/sessions/${sessionId}/settings/output`,{method:'PUT',headers:{'If-Match':`"${output.revision}"`},body:JSON.stringify({value:nextOutput})});
      }
      await api(`/sessions/${sessionId}/outcome-plan`,{method:'PUT',headers:{'If-Match':`"${payload.revision}"`},body:JSON.stringify({value:{...payload.value,export:{...(payload.value.export??{}),target_language:targetLanguage}}})});
      onsaved();onclose();
    }catch(caught){error=caught instanceof Error?caught.message:String(caught)}finally{saving=false}
  }

  load().catch((caught)=>error=caught instanceof Error?caught.message:String(caught));
</script>

<div class="fixed inset-0 z-[70] grid place-items-center bg-black/40 p-5">
  <section class="surface max-h-[92vh] w-full max-w-3xl overflow-y-auto rounded-3xl p-7" role="dialog" aria-modal="true">
    <header class="flex justify-between"><div><div class="eyebrow">Customize workflow</div><h2 class="mt-1 text-2xl font-semibold">Choose transformations and deliverables</h2></div><button onclick={onclose}><X size={19}/></button></header>
    {#if error}<p class="mt-4 text-sm text-red-500">{error}</p>{/if}
    {#if payload}
      {#if session?.workflow_kind!=='audiobook'}
        <div class="mt-6 rounded-2xl border border-[var(--line)] p-5"><div class="eyebrow">Workspace type</div><div class="mt-3 grid gap-3 sm:grid-cols-2"><button type="button" onclick={()=>setWorkflowKind('subtitles')} class:active={payload.value.workflow_kind==='subtitles'} class="workspace-kind"><strong>Subtitles</strong><span>SRT, VTT, or concatenated text; no voice or video controls.</span></button><button type="button" onclick={()=>setWorkflowKind('voiceover')} class:active={payload.value.workflow_kind==='voiceover'} class="workspace-kind"><strong>Voiceover</strong><span>Add speech generation, audio review, and rendered media exports.</span></button></div></div>
      {/if}
      <div class="mt-6 grid gap-3 sm:grid-cols-2">
        <label class="text-sm font-semibold">Source language<select bind:value={sourceLanguage} class="field">{#each LANGUAGE_OPTIONS as item}<option value={item.value}>{item.label}</option>{/each}</select></label>
        {#if transformations.translate}<label class="text-sm font-semibold">Target language<select bind:value={targetLanguage} class="field">{#each LANGUAGE_OPTIONS.filter((item)=>item.value!=='auto') as item}<option value={item.value}>{item.label}</option>{/each}</select></label>{/if}
      </div>
      <div class="mt-7 grid gap-4 md:grid-cols-2">
        <div class="rounded-2xl border border-[var(--line)] p-5"><div class="eyebrow">Transformations</div><div class="mt-4 space-y-3">{#each [{key:'transcribe',label:'Transcribe media'},{key:'correct',label:'Correct same-language subtitles'},{key:'translate',label:'Translate'},{key:'deterministic_normalization',label:'Deterministic speech normalization'},{key:'generate_audio',label:'Generate speech'},{key:'rvc',label:'Create RVC variants'}].filter((item)=>payload.value.workflow_kind!=='subtitles'||!['deterministic_normalization','generate_audio','rvc'].includes(item.key)) as item}<label class="flex items-start gap-3 text-sm"><input type="checkbox" checked={Boolean(transformations[item.key])} onchange={(event)=>setTransformation(item.key,event.currentTarget.checked)} class="mt-1 accent-[var(--accent)]"/><span>{item.label}</span></label>{/each}{#if payload.value.workflow_kind!=='subtitles'}<div class="rounded-xl border border-[var(--line)] p-3"><label class="flex items-start gap-3 text-sm"><input type="checkbox" checked={speechOptimizationEnabled} onchange={(event)=>setSpeechOptimizationEnabled(event.currentTarget.checked)} class="mt-1 accent-[var(--accent)]"/><span>Optimize text for speech</span></label>{#if speechOptimizationEnabled}<select value={speechOptimizationTiming} onchange={(event)=>setSpeechOptimizationTiming(event.currentTarget.value)} class="mt-3 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-xs"><option value="document">Before generation · whole document</option><option value="generation">During generation · segment batches</option></select>{/if}</div>{/if}</div></div>
        <div class="rounded-2xl border border-[var(--line)] p-5"><div class="eyebrow">Deliverables</div><div class="mt-4 rounded-xl bg-[var(--accent-soft)] p-3 text-sm"><strong>{payload.value.workflow_kind==='audiobook'?'Audiobook audio':payload.value.workflow_kind==='voiceover'?'Voiceover / dubbed media':'SRT, VTT, or plain text'}</strong><p class="muted mt-1 text-xs">This primary deliverable follows the selected workspace type.</p></div>{#if payload.value.workflow_kind==='voiceover'}<label class="mt-4 flex items-start gap-3 text-sm"><input type="checkbox" bind:checked={deliverables.subtitles} class="mt-1 accent-[var(--accent)]"/><span>Also include subtitle files or tracks</span></label>{/if}{#if transformations.translate}<label class="mt-5 block text-sm font-semibold">Translation input<select bind:value={payload.value.inputs.translation} class="field"><option value="source">Source / transcription directly</option><option value="correction" disabled={!transformations.correct}>Corrected subtitles</option></select></label>{/if}{#if transformations.generate_audio}<label class="mt-4 block text-sm font-semibold">Generation input<select bind:value={payload.value.inputs.generation} class="field"><option value="source">Source text/subtitles</option><option value="correction" disabled={!transformations.correct}>Correction</option><option value="translation" disabled={!transformations.translate}>Translation</option></select></label>{/if}</div>
      </div>
      <div class="mt-5 rounded-2xl bg-[var(--accent-soft)] p-5"><div class="eyebrow">Resolved pipeline</div><div class="mt-3 flex flex-wrap items-center gap-2">{#each payload.pipeline as stage,index}<span class="rounded-lg bg-[var(--paper-strong)] px-3 py-2 text-sm font-semibold">{stage.title}</span>{#if index<payload.pipeline.length-1}<ArrowRight size={15}/>{/if}{/each}</div></div>
      <footer class="mt-7 flex justify-end gap-3"><button onclick={onclose} class="rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold">Cancel</button><button onclick={save} disabled={saving} class="rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white">{saving?'Saving…':'Save workflow'}</button></footer>
    {/if}
  </section>
</div>

<style>.field{margin-top:.5rem;width:100%;border:1px solid var(--line);border-radius:.75rem;background:var(--paper);padding:.65rem .75rem;font-weight:400}.workspace-kind{display:flex;flex-direction:column;gap:.35rem;border:1px solid var(--line);border-radius:1rem;padding:1rem;text-align:left}.workspace-kind span{color:var(--muted);font-size:.75rem;line-height:1.45}.workspace-kind.active{border-color:var(--accent);background:var(--accent-soft)}</style>
