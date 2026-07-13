<script lang="ts">
  import { ArrowLeft, ArrowRight, AudioLines, BookOpenText, Captions, FileText, Gauge, Link2, Upload, X } from '@lucide/svelte';
  import { untrack } from 'svelte';
  import { api, uploadManagedFile, type SessionRecord } from './api';
  import { appState } from './app-state.svelte';
  import { LANGUAGE_OPTIONS } from './settings-fields';

  let { initialKind = 'subtitles', startAtSource = false, onclose }: { initialKind?: 'audiobook'|'subtitles'|'voiceover'; startAtSource?: boolean; onclose: () => void } = $props();
  let step = $state(untrack(() => startAtSource ? 2 : 1));
  let kind = $state<'audiobook'|'subtitles'|'voiceover'>(untrack(() => initialKind));
  let custom = $state(false);
  let sourceMode = $state<'upload'|'paste'|'url'|'reuse'|'later'>('upload');
  let sourceFile = $state<File|null>(null);
  let pastedText = $state('');
  let sourceUrl = $state('');
  let sourceAssetId = $state('');
  let reusableSources = $state<any[]>([]);
  let name = $state('');
  let correct = $state(false);
  let translate = $state(false);
  let keepSourceSubtitles = $state(true);
  let sourceLanguage = $state('auto');
  let targetLanguage = $state('en');
  let subtitleMode = $state<'none'|'soft'|'burned'>('soft');
  let normalizeText = $state(true);
  let optimizeTts = $state(false);
  let audiobookFormat = $state<'m4b'|'mp3'|'opus'|'flac'|'wav'>('m4b');
  let creating = $state(false);
  let progress = $state(0);
  let error = $state('');

  const isSrt = $derived((sourceFile?.name ?? reusableSources.find((item)=>item.id===sourceAssetId)?.display_name ?? '').toLowerCase().endsWith('.srt'));
  const needsTranscription = $derived(kind !== 'audiobook' && !isSrt && sourceMode !== 'later');
  const pipeline = $derived([
    ...(kind==='audiobook'?['Clean source']:needsTranscription?['Transcribe']:['Use subtitles']),
    ...(kind!=='audiobook'&&correct?['Correct']:[]),
    ...(kind!=='audiobook'&&translate?['Translate']:[]),
    ...(kind==='audiobook'?['Segment narration']:[]),
    ...((kind==='voiceover'||(kind==='audiobook'&&normalizeText))?['Deterministic speech normalization']:[]),
    ...(kind==='audiobook'&&optimizeTts?['LLM speech optimization']:[]),
    ...((kind==='voiceover'||kind==='audiobook')?['Generate audio']:[]),
    'Export'
  ]);

  async function loadSources() {
    try { reusableSources = (await api<{items:any[]}>('/sources')).items; sourceAssetId ||= reusableSources[0]?.id ?? ''; }
    catch { reusableSources = []; }
  }
  loadSources();

  function chooseKind(value: typeof kind, full=false) { kind=value; if(value==='audiobook'&&sourceLanguage==='auto')sourceLanguage='en'; custom=full; step=2; }
  function inferName() {
    if (name.trim()) return;
    const raw = sourceFile?.name || reusableSources.find((item)=>item.id===sourceAssetId)?.display_name || (sourceMode==='url'?sourceUrl.split('/').filter(Boolean).at(-1):'') || (kind==='audiobook'?'New audiobook':kind==='voiceover'?'New voiceover':'New subtitles');
    name = String(raw).replace(/\.[^.]+$/, '').replace(/[_-]+/g,' ').trim();
  }
  function nextFromSource() { inferName(); step = custom ? 4 : 3; }

  async function create() {
    if (!name.trim()) return;
    creating=true;error='';
    try {
      const existing=new Set(appState.sessions.map((item)=>item.name.toLocaleLowerCase()));let unique=name.trim();let suffix=2;while(existing.has(unique.toLocaleLowerCase()))unique=`${name.trim()} ${suffix++}`;
      const included = custom ? [] : [
        ...(kind==='audiobook'?['clean_source','prepare_text']:[]),
        ...(needsTranscription?['transcribe']:[]), ...(correct?['correct']:[]), ...(translate?['translate']:[]),
        ...((kind==='voiceover'||kind==='audiobook')?['generate_audio']:[]), 'export'
      ];
      const session=await api<SessionRecord>('/sessions',{method:'POST',body:JSON.stringify({name:unique,workflow_kind:kind,source_language:sourceLanguage,target_language:translate?targetLanguage:null,workflow_preset:'custom',included_stages:included})});
      const current=await api<{value:any;revision:number}>(`/sessions/${session.id}/outcome-plan`);
      const value={
        ...current.value, workflow_kind:kind, focus:custom?'custom':'guided',
        deliverables:{audiobook:kind==='audiobook',subtitles:kind==='subtitles'||subtitleMode!=='none',voiceover:kind==='voiceover'},
        transformations:{transcribe:needsTranscription,correct:kind==='audiobook'?false:correct,translate:kind==='audiobook'?false:translate,deterministic_normalization:kind==='audiobook'?normalizeText:true,llm_tts_document_optimization:false,llm_tts_optimization:kind==='audiobook'&&optimizeTts,generate_audio:kind==='voiceover'||kind==='audiobook',rvc:false},
        inputs:{translation:correct?'correction':'source',generation:translate?'translation':correct?'correction':'source'},
        export:{audio:kind==='voiceover'||kind==='audiobook'?'generated':'preserve',subtitle_mode:kind==='audiobook'?'none':subtitleMode,subtitles:translate?(keepSourceSubtitles?'dual':'translation'):'source',target_language:targetLanguage}
      };
      await api(`/sessions/${session.id}/outcome-plan`,{method:'PUT',headers:{'If-Match':`"${current.revision}"`},body:JSON.stringify({value})});
      const updateSettings=async(section:string,next:Record<string,unknown>)=>{const stored=await api<any>(`/sessions/${session.id}/settings/${section}`);await api(`/sessions/${session.id}/settings/${section}`,{method:'PUT',headers:{'If-Match':`"${stored.revision}"`},body:JSON.stringify({value:{...stored.override,...next}})});};
      const speechLanguage=translate?targetLanguage:(sourceLanguage==='auto'?'en':sourceLanguage);
      await Promise.all([
        updateSettings('stt',{stt_language:sourceLanguage}),
        updateSettings('translation',{source_language:sourceLanguage,target_language:targetLanguage}),
        updateSettings('tts',{language:speechLanguage}),
        updateSettings('output',{language:speechLanguage})
      ]);
      if(kind==='audiobook'){
        const output=await api<{override:any;revision:number}>(`/sessions/${session.id}/settings/output`);
        await api(`/sessions/${session.id}/settings/output`,{method:'PUT',headers:{'If-Match':`"${output.revision}"`},body:JSON.stringify({value:{...output.override,format:audiobookFormat,language:speechLanguage}})});
      }
      let file=sourceMode==='paste'&&pastedText.trim()?new File([pastedText.trim()],`${unique}.txt`,{type:'text/plain'}):sourceMode==='upload'?sourceFile:null;
      if(file)await uploadManagedFile(file,session.id,(value)=>progress=value);
      else if(sourceMode==='url'&&sourceUrl.trim())await api(`/sessions/${session.id}/sources/url`,{method:'POST',body:JSON.stringify({url:sourceUrl.trim()})});
      else if(sourceMode==='reuse'&&sourceAssetId)await api(`/sessions/${session.id}/sources`,{method:'POST',body:JSON.stringify({source_asset_id:sourceAssetId,role:'primary'})});
      appState.upsertSession(session);location.href=`/sessions/${session.id}`;
    }catch(caught){error=caught instanceof Error?caught.message:String(caught)}finally{creating=false}
  }
</script>

<div class="fixed inset-0 z-50 grid place-items-center bg-black/40 p-4 backdrop-blur-sm" role="presentation">
  <!-- svelte-ignore a11y_no_noninteractive_element_to_interactive_role -->
  <section class="surface max-h-[94vh] w-full max-w-4xl overflow-y-auto rounded-[2rem] p-6 sm:p-9" role="dialog" aria-modal="true" aria-labelledby="wizard-title">
    <header class="flex items-start justify-between gap-4"><div><div class="eyebrow">New {kind} session · step {step} of 4</div><h1 id="wizard-title" class="mt-1 text-2xl font-semibold">{step===1?'What would you like to make?':step===2?'Choose the source':step===3?'Choose the result':'Review your workspace'}</h1></div><button onclick={onclose} class="rounded-xl p-2" aria-label="Close"><X size={20}/></button></header>
    {#if error}<div class="mt-5 rounded-xl border border-red-400/40 bg-red-500/10 p-3 text-sm">{error}</div>{/if}
    {#if step===1}
      <div class="mt-7 grid gap-4 md:grid-cols-3"><button onclick={()=>chooseKind('subtitles')} class="choice"><Captions size={25}/><strong>Create subtitles</strong><span>Transcribe, refine, translate, and export.</span></button><button onclick={()=>chooseKind('voiceover')} class="choice"><AudioLines size={25}/><strong>Create a voiceover</strong><span>Start with media or an existing subtitle file.</span></button><button onclick={()=>chooseKind('audiobook')} class="choice"><BookOpenText size={25}/><strong>Generate an audiobook</strong><span>Prepare a document or pasted text for narration.</span></button></div>
      <button onclick={()=>chooseKind(kind,true)} class="mt-5 flex w-full items-center justify-center gap-2 rounded-xl border border-[var(--line)] px-4 py-3 text-sm font-semibold"><Gauge size={17}/> Skip guidance and open a fully customizable workspace</button>
    {:else if step===2}
      <div class="mt-7 grid gap-3 sm:grid-cols-5">{#each [{id:'upload',label:'Upload',icon:Upload},{id:'paste',label:'Paste',icon:FileText},{id:'url',label:'URL',icon:Link2},{id:'reuse',label:'Reuse',icon:BookOpenText},{id:'later',label:'Add later',icon:Gauge}] as mode}{@const Icon=mode.icon}<button onclick={()=>sourceMode=mode.id as typeof sourceMode} class:active={sourceMode===mode.id} class="source-choice"><Icon size={18}/>{mode.label}</button>{/each}</div>
      <div class="mt-5 rounded-2xl border border-[var(--line)] p-5">{#if sourceMode==='upload'}<label class="text-sm font-semibold">Source file<input type="file" onchange={(event)=>{sourceFile=(event.currentTarget as HTMLInputElement).files?.[0]??null;inferName()}} class="mt-2 block w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] p-3"/></label>{:else if sourceMode==='paste'}<textarea bind:value={pastedText} rows="8" placeholder="Paste text here…" class="w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] p-4"></textarea>{:else if sourceMode==='url'}<label class="text-sm font-semibold">Public source URL<input bind:value={sourceUrl} type="url" class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3"/></label>{:else if sourceMode==='reuse'}<label class="text-sm font-semibold">Source library<select bind:value={sourceAssetId} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3">{#each reusableSources as source}<option value={source.id}>{source.display_name} · {source.kind}</option>{/each}</select></label>{:else}<p class="muted text-sm">Create the session now and attach one or more sources from its Sources tab later.</p>{/if}</div>
    {:else if step===3}
      {#if kind==='audiobook'}
        <div class="mt-7 grid gap-4 md:grid-cols-2">
          <label class="option"><input type="checkbox" bind:checked={normalizeText}/><span><strong>Normalize text for speech</strong><small>Deterministically expands and cleans text patterns that speech engines commonly misread. No provider cost.</small></span></label>
          <label class="option"><input type="checkbox" bind:checked={optimizeTts}/><span><strong>Optimize each segment with an LLM</strong><small>Optional pronunciation and numeral cleanup immediately before synthesis. Local models can run without API charges; remote providers may incur cost.</small></span></label>
          <label class="text-sm font-semibold">Narration language<select bind:value={sourceLanguage} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3">{#each LANGUAGE_OPTIONS.filter((item)=>item.value!=='auto') as item}<option value={item.value}>{item.label}</option>{/each}</select></label>
          <label class="text-sm font-semibold">Audiobook format<select bind:value={audiobookFormat} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3"><option value="m4b">M4B · chapters, metadata, and cover</option><option value="mp3">MP3</option><option value="opus">Opus</option><option value="flac">FLAC</option><option value="wav">PCM WAV</option></select></label>
          <div class="rounded-2xl bg-[var(--accent-soft)] p-4 text-sm"><strong>Document cleaning stays reviewable</strong><p class="muted mt-1 text-xs leading-relaxed">Pandrator extracts and cleans the source first. Headings become editable chapter starts in the generation drawer; the original file is preserved.</p></div>
        </div>
      {:else}
        <div class="mt-7 grid gap-4 md:grid-cols-2"><label class="text-sm font-semibold">Source language<select bind:value={sourceLanguage} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3">{#each LANGUAGE_OPTIONS as item}<option value={item.value}>{item.label}</option>{/each}</select></label><label class="option"><input type="checkbox" bind:checked={correct}/><span><strong>Correct same-language subtitles</strong><small>Creates a separate reviewed source-language asset.</small></span></label><label class="option"><input type="checkbox" bind:checked={translate}/><span><strong>Translate</strong><small>A professional translation can clean minor source errors without creating a correction asset.</small></span></label>{#if translate}<label class="text-sm font-semibold">Target language<select bind:value={targetLanguage} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3">{#each LANGUAGE_OPTIONS.filter((item)=>item.value!=='auto') as item}<option value={item.value}>{item.label}</option>{/each}</select></label><label class="option"><input type="checkbox" bind:checked={keepSourceSubtitles}/><span><strong>Keep same-language subtitles</strong><small>Allows source/translation dual-track exports.</small></span></label>{/if}<label class="text-sm font-semibold">Subtitle output<select bind:value={subtitleMode} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3"><option value="none">No subtitle track</option><option value="soft">Soft / separate subtitles</option><option value="burned">Burn into video</option></select></label></div>
      {/if}
    {:else}
      <div class="mt-7 grid gap-6 md:grid-cols-[1fr_1.2fr]"><div><label class="text-sm font-semibold">Session name<input bind:value={name} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3"/></label><p class="muted mt-3 text-sm">This only controls the display name; storage uses a stable UUID.</p></div><div class="rounded-2xl border border-[var(--line)] bg-[var(--paper)] p-5"><div class="eyebrow">Prepared pipeline</div><div class="mt-4 flex flex-wrap items-center gap-2">{#each pipeline as stage,index}<span class="rounded-lg bg-[var(--accent-soft)] px-3 py-2 text-sm font-semibold">{stage}</span>{#if index<pipeline.length-1}<ArrowRight class="muted" size={15}/>{/if}{/each}</div><p class="muted mt-4 text-xs">You can customize this plan later without deleting completed artifacts.</p></div></div>
      {#if creating}<div class="mt-5 h-2 overflow-hidden rounded-full bg-[var(--line)]"><div class="h-full bg-[var(--accent)]" style={`width:${Math.max(3,progress*100)}%`}></div></div>{/if}
    {/if}
    {#if step>1}<footer class="mt-8 flex items-center justify-between"><button onclick={()=>step-=1} disabled={creating} class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold"><ArrowLeft size={16}/> {step===2&&startAtSource?'Change task':'Back'}</button>{#if step===2}<button onclick={nextFromSource} class="primary">Continue <ArrowRight size={16}/></button>{:else if step===3}<button onclick={()=>{inferName();step=4}} class="primary">Review <ArrowRight size={16}/></button>{:else}<button onclick={create} disabled={creating||!name.trim()} class="primary disabled:opacity-40">{creating?'Creating…':'Create workspace'} <ArrowRight size={16}/></button>{/if}</footer>{/if}
  </section>
</div>

<style>
  .choice{display:flex;min-height:11rem;flex-direction:column;align-items:flex-start;gap:.7rem;border:1px solid var(--line);border-radius:1.25rem;padding:1.3rem;text-align:left}.choice:hover,.source-choice.active{border-color:var(--accent);background:var(--accent-soft)}.choice :global(svg){color:var(--accent)}.choice span,.option small{color:var(--muted);font-size:.78rem;line-height:1.5}.source-choice{display:flex;align-items:center;justify-content:center;gap:.45rem;border:1px solid var(--line);border-radius:.8rem;padding:.75rem;font-size:.8rem;font-weight:650}.option{display:flex;align-items:flex-start;gap:.75rem;border:1px solid var(--line);border-radius:1rem;padding:1rem}.option input{margin-top:.2rem;width:1rem;height:1rem;accent-color:var(--accent)}.option strong,.option small{display:block}.primary{display:flex;align-items:center;gap:.5rem;border-radius:.75rem;background:var(--action-bg);padding:.7rem 1rem;color:white;font-size:.85rem;font-weight:700}.primary:hover{background:var(--action-hover)}
</style>
