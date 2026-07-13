<script lang="ts">
  import { ArrowRight, X } from '@lucide/svelte';
  import { api, type SessionRecord } from './api';
  import { LANGUAGE_OPTIONS } from './settings-fields';

  let { sessionId, onclose, onsaved }: { sessionId:string; onclose:()=>void; onsaved:()=>void }=$props();
  let payload=$state<any>(null);
  let session=$state<SessionRecord|null>(null);
  let sourceLanguage=$state('auto');
  let targetLanguage=$state('en');
  let error=$state('');
  let saving=$state(false);
  const transformations=$derived(payload?.value?.transformations??{});
  const deliverables=$derived(payload?.value?.deliverables??{});

  async function load(){
    [payload,session]=await Promise.all([api(`/sessions/${sessionId}/outcome-plan`),api<SessionRecord>(`/sessions/${sessionId}`)]);
    sourceLanguage=session.source_language??'auto';
    targetLanguage=session.target_language??'en';
  }

  async function save(){
    saving=true;
    try{
      if(session) await api(`/sessions/${sessionId}`,{method:'PATCH',headers:{'If-Match':`"${session.revision}"`},body:JSON.stringify({source_language:sourceLanguage,target_language:transformations.translate?targetLanguage:null})});
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
      <div class="mt-6 grid gap-3 sm:grid-cols-2">
        <label class="text-sm font-semibold">Source language<select bind:value={sourceLanguage} class="field">{#each LANGUAGE_OPTIONS as item}<option value={item.value}>{item.label}</option>{/each}</select></label>
        {#if transformations.translate}<label class="text-sm font-semibold">Target language<select bind:value={targetLanguage} class="field">{#each LANGUAGE_OPTIONS.filter((item)=>item.value!=='auto') as item}<option value={item.value}>{item.label}</option>{/each}</select></label>{/if}
      </div>
      <div class="mt-7 grid gap-4 md:grid-cols-2">
        <div class="rounded-2xl border border-[var(--line)] p-5"><div class="eyebrow">Transformations</div><div class="mt-4 space-y-3">{#each [{key:'transcribe',label:'Transcribe media'},{key:'correct',label:'Correct same-language subtitles'},{key:'translate',label:'Translate'},{key:'deterministic_normalization',label:'Deterministic speech normalization'},{key:'llm_tts_document_optimization',label:'Optimize and review the whole document first'},{key:'llm_tts_optimization',label:'Optimize batches while generating'},{key:'generate_audio',label:'Generate speech'},{key:'rvc',label:'Create RVC variants'}] as item}<label class="flex items-start gap-3 text-sm"><input type="checkbox" bind:checked={transformations[item.key]} class="mt-1 accent-[var(--accent)]"/><span>{item.label}</span></label>{/each}</div></div>
        <div class="rounded-2xl border border-[var(--line)] p-5"><div class="eyebrow">Deliverables</div><div class="mt-4 space-y-3">{#each [{key:'subtitles',label:'Subtitle files or tracks'},{key:'voiceover',label:'Voiceover / dubbed media'},{key:'audiobook',label:'Audiobook audio'}] as item}<label class="flex items-start gap-3 text-sm"><input type="checkbox" bind:checked={deliverables[item.key]} class="mt-1 accent-[var(--accent)]"/><span>{item.label}</span></label>{/each}</div>{#if transformations.translate}<label class="mt-5 block text-sm font-semibold">Translation input<select bind:value={payload.value.inputs.translation} class="field"><option value="source">Source / transcription directly</option><option value="correction" disabled={!transformations.correct}>Corrected subtitles</option></select></label>{/if}{#if transformations.generate_audio}<label class="mt-4 block text-sm font-semibold">Generation input<select bind:value={payload.value.inputs.generation} class="field"><option value="source">Source text/subtitles</option><option value="correction" disabled={!transformations.correct}>Correction</option><option value="translation" disabled={!transformations.translate}>Translation</option></select></label>{/if}</div>
      </div>
      <div class="mt-5 rounded-2xl bg-[var(--accent-soft)] p-5"><div class="eyebrow">Resolved pipeline</div><div class="mt-3 flex flex-wrap items-center gap-2">{#each payload.pipeline as stage,index}<span class="rounded-lg bg-[var(--paper-strong)] px-3 py-2 text-sm font-semibold">{stage.title}</span>{#if index<payload.pipeline.length-1}<ArrowRight size={15}/>{/if}{/each}</div></div>
      <footer class="mt-7 flex justify-end gap-3"><button onclick={onclose} class="rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold">Cancel</button><button onclick={save} disabled={saving} class="rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white">{saving?'Saving…':'Save workflow'}</button></footer>
    {/if}
  </section>
</div>

<style>.field{margin-top:.5rem;width:100%;border:1px solid var(--line);border-radius:.75rem;background:var(--paper);padding:.65rem .75rem;font-weight:400}</style>
