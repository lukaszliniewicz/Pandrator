<script lang="ts">
  import { ImagePlus, RotateCcw, Save, Trash2 } from '@lucide/svelte';
  import { api } from './api';
  import ArtifactPreview from './ArtifactPreview.svelte';
  import { artifactFilename } from './artifact-display';

  let { sessionId }: { sessionId: string } = $props();
  let settings = $state<any>(null);
  let audioSettings = $state<any>(null);
  let session = $state<any>(null);
  let capabilities = $state<any>({});
  let draft = $state<Record<string, unknown>>({});
  let audioDraft = $state<Record<string, unknown>>({});
  let images = $state<any[]>([]);
  let preview = $state<any|null>(null);
  let busy = $state(false);
  let message = $state('');
  let error = $state('');

  const audiobookWorkspace = $derived(session?.workflow_kind==='audiobook');
  const subtitleWorkspace = $derived(session?.workflow_kind==='subtitles');
  const context = $derived(settings?.context??{});
  const sourceProfile = $derived(String(context.source_profile??'none'));
  const hasSourceVideo = $derived(Boolean(context.has_source_video));
  const hasSourceAudio = $derived(Boolean(context.has_source_audio));
  const coverId = $derived(String(Object.prototype.hasOwnProperty.call(draft,'cover_artifact_id') ? draft.cover_artifact_id ?? '' : settings?.effective?.cover_artifact_id ?? ''));
  const selectedCover = $derived(images.find((item)=>item.id===coverId));
  const burnVideoEncoders = $derived(Array.isArray(capabilities?.ffmpeg?.burn_video_encoders) && capabilities.ffmpeg.burn_video_encoders.length ? capabilities.ffmpeg.burn_video_encoders : [{id:'libx264',label:'H.264 software (most compatible)',hardware:false,codec:'h264'}]);
  const value = (key:string, fallback:unknown='') => Object.prototype.hasOwnProperty.call(draft,key) ? draft[key] : settings?.effective?.[key] ?? fallback;
  const audioValue = (key:string, fallback:unknown='') => Object.prototype.hasOwnProperty.call(audioDraft,key) ? audioDraft[key] : audioSettings?.effective?.[key] ?? fallback;
  const set = (key:string, next:unknown) => draft={...draft,[key]:next};
  const setAudio = (key:string, next:unknown) => audioDraft={...audioDraft,[key]:next};
  const exportMode = $derived(String(value('export_mode',subtitleWorkspace?'subtitles':'media')));
  const audioMode = $derived(String(value('audio_mode',hasSourceAudio?'mixed':'dubbing_only')));
  const subtitleMode = $derived(String(value('subtitle_mode','none')));

  const sharedSubtitleKeys = ['export_mode','subtitle_selection','subtitle_format','language'];
  const videoKeys = ['audio_mode','subtitle_mode','burn_video_encoder','burn_video_quality','burn_video_speed','burn_audio_codec','burn_audio_bitrate'];
  const mixKeys = ['mix_source_gain_db','mix_voice_gain_db','mix_voice_lufs','mix_ducking','mix_attack_ms','mix_release_ms','mix_audio_bitrate'];
  const audioSyncKeys = ['synchronization_delay_ms','synchronization_speed','synchronization_sentence_gap_ms'];
  type SavedOutputProfile = {output:Record<string,unknown>;audio:Record<string,unknown>};

  function applicableOutputKeys() {
    if (audiobookWorkspace) return new Set(['format','bitrate','language','title','artist','album','genre','cover_artifact_id']);
    if (subtitleWorkspace) return new Set(sharedSubtitleKeys);
    const keys=new Set(sharedSubtitleKeys);
    if(hasSourceAudio){keys.add('audio_mode');for(const key of mixKeys)keys.add(key)}
    if(hasSourceVideo){for(const key of videoKeys)keys.add(key)}
    else{keys.add('format');keys.add('bitrate')}
    return keys;
  }

  function sanitizeOutput(source:Record<string,unknown>) {
    const allowed=applicableOutputKeys();
    return Object.fromEntries(Object.entries(source).filter(([key])=>allowed.has(key)));
  }

  function sanitizeAudio(source:Record<string,unknown>) {
    return Object.fromEntries(Object.entries(source).filter(([key])=>audioSyncKeys.includes(key)));
  }

  async function load() {
    [session, settings, audioSettings, {items:images}, capabilities] = await Promise.all([
      api<any>(`/sessions/${sessionId}`),
      api<any>(`/sessions/${sessionId}/settings/output`),
      api<any>(`/sessions/${sessionId}/settings/audio`),
      api<any>(`/artifacts?session_id=${sessionId}&limit=500`).then((payload)=>({items:(payload.items??[]).filter((item:any)=>item.state==='current'&&(item.kind==='image'||String(item.mime_type??'').startsWith('image/')))})),
      api<any>('/capabilities').catch(()=>({}))
    ]);
    draft=sanitizeOutput({...settings.override});
    audioDraft=sanitizeAudio({...audioSettings.override});
    if(session.workflow_kind==='subtitles'&&!['subtitles','text'].includes(String(value('export_mode','subtitles'))))draft={...draft,export_mode:'subtitles'};
  }

  async function save(nextDraft=draft, nextAudioDraft=audioDraft, rethrow=false):Promise<SavedOutputProfile|null> {
    busy=true;error='';message='';
    try {
      const cleanedOutput=sanitizeOutput(nextDraft);
      const cleanedAudio=sanitizeAudio(nextAudioDraft);
      settings=await api<any>(`/sessions/${sessionId}/settings/output`,{method:'PUT',headers:{'If-Match':`"${settings.revision}"`},body:JSON.stringify({value:cleanedOutput})});
      if(!audiobookWorkspace&&!subtitleWorkspace){
        audioSettings=await api<any>(`/sessions/${sessionId}/settings/audio`,{method:'PUT',headers:{'If-Match':`"${audioSettings.revision}"`},body:JSON.stringify({value:cleanedAudio})});
      }
      draft=sanitizeOutput({...settings.override});
      audioDraft=sanitizeAudio({...audioSettings.override});
      message='Output settings saved for this session.';
      return {output:{...(settings.effective??{})},audio:{...(audioSettings.effective??{})}};
    } catch(caught){
      error=caught instanceof Error?caught.message:String(caught);
      if(rethrow)throw caught;
      return null;
    } finally{busy=false}
  }

  export async function saveForExport():Promise<SavedOutputProfile> {
    if(!settings||!audioSettings)throw new Error('Output settings are still loading. Please try again.');
    const saved=await save(draft,audioDraft,true);
    if(!saved)throw new Error('Output settings could not be saved.');
    return saved;
  }

  async function saveAsDefaults() {
    busy=true;error='';message='';
    try {
      const promoted=Object.fromEntries(Object.entries(sanitizeOutput(draft)).filter(([key])=>key!=='cover_artifact_id'));
      const defaults=await api<any>('/defaults/output');
      await api('/settings/defaults.output',{method:'PUT',headers:{'If-Match':`"${defaults.revision}"`},body:JSON.stringify({value:{...(defaults.value??{}),...promoted}})});
      if(!audiobookWorkspace&&!subtitleWorkspace){
        const audioDefaults=await api<any>('/defaults/audio');
        await api('/settings/defaults.audio',{method:'PUT',headers:{'If-Match':`"${audioDefaults.revision}"`},body:JSON.stringify({value:{...(audioDefaults.value??{}),...sanitizeAudio(audioDraft)}})});
      }
      const retained=Object.fromEntries(Object.entries(sanitizeOutput(draft)).filter(([key])=>!Object.prototype.hasOwnProperty.call(promoted,key)));
      settings=await api<any>(`/sessions/${sessionId}/settings/output`,{method:'PUT',headers:{'If-Match':`"${settings.revision}"`},body:JSON.stringify({value:retained})});
      if(!audiobookWorkspace&&!subtitleWorkspace){
        audioSettings=await api<any>(`/sessions/${sessionId}/settings/audio`,{method:'PUT',headers:{'If-Match':`"${audioSettings.revision}"`},body:JSON.stringify({value:{}})});
      }
      draft=sanitizeOutput({...settings.override});audioDraft={};
      message='Saved as the application defaults for compatible outputs.';
    } catch(caught){error=caught instanceof Error?caught.message:String(caught)} finally{busy=false}
  }

  async function uploadCover(event:Event) {
    const input=event.currentTarget as HTMLInputElement; const file=input.files?.[0]; if(!file)return;
    busy=true;error='';message='Uploading cover artwork…';
    try {
      const form=new FormData(); form.set('session_id',sessionId); form.set('purpose','cover'); form.set('file',file);
      const result=await api<any>('/uploads',{method:'POST',body:form});
      const artifacts=await api<any>(`/artifacts?session_id=${sessionId}&limit=500`);
      images=(artifacts.items??[]).filter((item:any)=>item.state==='current'&&(item.kind==='image'||String(item.mime_type??'').startsWith('image/')));
      const next={...draft,cover_artifact_id:result.artifact_id}; draft=next; await save(next,audioDraft);
    } catch(caught){error=caught instanceof Error?caught.message:String(caught)} finally{busy=false;input.value=''}
  }

  const sourceDescription = $derived(sourceProfile==='video'?'Video source: final media can preserve, replace, or mix its original soundtrack.':sourceProfile==='audio'?'Audio source: export the source, generated voiceover, or a controlled mix.':sourceProfile==='subtitles'?'Subtitle source: media output is standalone generated voiceover audio.':sourceProfile==='document'?'Document source: media output is standalone generated voiceover audio.':'No current source is attached; generated audio and document exports remain available.');

  load().catch((caught)=>error=caught instanceof Error?caught.message:String(caught));
</script>

<section class="surface rounded-2xl p-5">
  <div class="flex flex-wrap items-start justify-between gap-4"><div><div class="eyebrow">Output profile</div><h2 class="mt-1 text-xl font-semibold">{audiobookWorkspace?'Audiobook file, metadata, and artwork':subtitleWorkspace?'Subtitle files':hasSourceVideo?'Video soundtrack, subtitles, and mix':'Voiceover audio and documents'}</h2><p class="muted mt-2 text-sm">{audiobookWorkspace?'Configure the narration container and book metadata.':subtitleWorkspace?'Choose SRT, WebVTT, or a plain-text transcript.':sourceDescription}</p></div><div class="flex flex-wrap gap-2"><button onclick={()=>{draft={};audioDraft={};save({}, {})}} disabled={busy||(!Object.keys(draft).length&&!Object.keys(audioDraft).length)} class="tool"><RotateCcw size={15}/> Revert to defaults</button><button onclick={saveAsDefaults} disabled={busy||(!Object.keys(draft).length&&!Object.keys(audioDraft).length)} class="tool"><Save size={15}/> Save as defaults</button><button onclick={()=>save()} disabled={busy} class="tool primary"><Save size={15}/> {busy?'Saving…':'Save output profile'}</button></div></div>
  {#if error}<p class="mt-4 rounded-xl bg-red-500/10 p-3 text-sm text-red-500">{error}</p>{/if}{#if message}<p class="mt-4 rounded-xl bg-[var(--accent-soft)] p-3 text-sm">{message}</p>{/if}
  {#if settings}
    {#if subtitleWorkspace}
      <div class="mt-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <label>Export target<select value={exportMode} onchange={(event)=>set('export_mode',event.currentTarget.value)} class="field"><option value="subtitles">Subtitle file</option><option value="text">Concatenated text</option></select></label>
        {#if exportMode==='subtitles'}<label>Subtitle format<select value={String(value('subtitle_format','srt'))} onchange={(event)=>set('subtitle_format',event.currentTarget.value)} class="field"><option value="srt">SubRip (.srt)</option><option value="vtt">WebVTT (.vtt)</option></select></label>{/if}
        <label>Subtitle document<select value={String(value('subtitle_selection','source'))} onchange={(event)=>set('subtitle_selection',event.currentTarget.value)} class="field"><option value="source">Source / corrected</option><option value="translation">Translation</option><option value="dual">Source and translation</option></select></label>
        <label>Language identifier<input value={String(value('language',''))} oninput={(event)=>set('language',event.currentTarget.value)} placeholder="en" class="field"/></label>
      </div>
    {:else if audiobookWorkspace}
      <div class="mt-6 grid gap-5 xl:grid-cols-[1fr_18rem]"><div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <label>Format<select value={String(value('format','wav'))} onchange={(event)=>set('format',event.currentTarget.value)} class="field"><option value="m4b">M4B audiobook</option><option value="mp3">MP3</option><option value="opus">Opus</option><option value="flac">FLAC</option><option value="wav">PCM WAV</option></select></label>
        <label>Bitrate<input value={String(value('bitrate','192k'))} oninput={(event)=>set('bitrate',event.currentTarget.value)} class="field"/></label>
        <label>Language identifier<input value={String(value('language',''))} oninput={(event)=>set('language',event.currentTarget.value)} placeholder="en" class="field"/></label>
        <label>Title<input value={String(value('title',''))} oninput={(event)=>set('title',event.currentTarget.value)} class="field"/></label>
        <label>Author / artist<input value={String(value('artist',''))} oninput={(event)=>set('artist',event.currentTarget.value)} class="field"/></label>
        <label>Album / series<input value={String(value('album',''))} oninput={(event)=>set('album',event.currentTarget.value)} class="field"/></label>
        <label>Genre<input value={String(value('genre','Audiobook'))} oninput={(event)=>set('genre',event.currentTarget.value)} class="field"/></label>
        <div class="rounded-xl bg-[var(--accent-soft)] p-3 text-xs leading-relaxed sm:col-span-2"><strong>Narration audio</strong><p class="muted mt-1">M4B carries book metadata, cover artwork, and chapter markers from the generation plan.</p></div>
      </div><aside class="rounded-2xl border border-[var(--line)] p-4"><div class="text-sm font-semibold">Cover artwork</div>{#if selectedCover}<button onclick={()=>{preview=selectedCover}} class="mt-3 block aspect-square w-full overflow-hidden rounded-xl bg-[var(--paper)]"><img src={`/api/v1/artifacts/${selectedCover.id}/content`} alt={artifactFilename(selectedCover)} class="size-full object-cover"/></button><div class="muted mt-2 truncate text-xs">{artifactFilename(selectedCover)}</div>{:else}<div class="muted mt-3 grid aspect-square place-items-center rounded-xl border border-dashed border-[var(--line)] text-center text-xs">No cover selected</div>{/if}<select value={coverId} onchange={(event)=>set('cover_artifact_id',event.currentTarget.value)} class="field mt-3"><option value="">No cover</option>{#each images as image}<option value={image.id}>{artifactFilename(image)}</option>{/each}</select><div class="mt-3 flex gap-2"><label class="tool flex flex-1 cursor-pointer justify-center"><ImagePlus size={15}/> Upload<input type="file" accept="image/png,image/jpeg,image/webp" onchange={uploadCover} class="sr-only"/></label><button onclick={()=>set('cover_artifact_id','')} disabled={!coverId} class="tool" aria-label="Remove cover"><Trash2 size={15}/></button></div></aside></div>
    {:else}
      <div class="mt-6 space-y-5">
        <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <label>Export target<select value={exportMode} onchange={(event)=>set('export_mode',event.currentTarget.value)} class="field"><option value="media">{hasSourceVideo?'Rendered video':'Voiceover audio and companion files'}</option><option value="subtitles">Subtitles only</option><option value="text">Concatenated text only</option></select></label>
          {#if exportMode==='media'}
            {#if hasSourceAudio}<label>Audio result<select value={audioMode} onchange={(event)=>set('audio_mode',event.currentTarget.value)} class="field"><option value="mixed">Mixed source and voiceover (recommended)</option><option value="preserve">Preserve source audio</option><option value="dubbing_only">Voiceover only</option></select></label>{:else}<div class="rounded-xl bg-[var(--accent-soft)] p-3 text-xs"><strong>Voiceover only</strong><p class="muted mt-1">This source has no soundtrack to preserve or mix.</p></div>{/if}
            {#if hasSourceVideo}<label>Subtitles<select value={subtitleMode} onchange={(event)=>set('subtitle_mode',event.currentTarget.value)} class="field"><option value="none">No subtitles</option><option value="soft">Soft / selectable tracks</option><option value="burned">Burned into video</option></select></label>{:else}<label>Audio format<select value={String(value('format','wav'))} onchange={(event)=>set('format',event.currentTarget.value)} class="field"><option value="wav">PCM WAV</option><option value="mp3">MP3</option><option value="opus">Opus</option><option value="flac">FLAC</option></select></label><label>Bitrate<input value={String(value('bitrate','192k'))} oninput={(event)=>set('bitrate',event.currentTarget.value)} class="field"/></label>{/if}
          {:else if exportMode==='subtitles'}<label>Subtitle format<select value={String(value('subtitle_format','srt'))} onchange={(event)=>set('subtitle_format',event.currentTarget.value)} class="field"><option value="srt">SubRip (.srt)</option><option value="vtt">WebVTT (.vtt)</option></select></label>{/if}
          {#if exportMode!=='media'||(hasSourceVideo&&subtitleMode!=='none')}<label>Subtitle tracks<select value={String(value('subtitle_selection','translation'))} onchange={(event)=>set('subtitle_selection',event.currentTarget.value)} class="field"><option value="source">Source / corrected</option><option value="translation">Translation</option><option value="dual">Source and translation</option></select></label>{/if}
        </div>

        {#if exportMode==='media'&&hasSourceAudio&&audioMode==='mixed'}
          <fieldset class="rounded-2xl border border-[var(--line)] p-5"><legend class="px-2 text-sm font-semibold">Soundtrack mix</legend><p class="muted text-xs">Voiceover is normalized first, the source ducks only while speech is present, and a true-peak limiter prevents clipping.</p><div class="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <label>Source level (dB)<input type="number" min="-60" max="12" step="0.5" value={Number(value('mix_source_gain_db',0))} oninput={(event)=>set('mix_source_gain_db',Number(event.currentTarget.value))} class="field"/></label>
            <label>Voiceover level (dB)<input type="number" min="-30" max="12" step="0.5" value={Number(value('mix_voice_gain_db',0))} oninput={(event)=>set('mix_voice_gain_db',Number(event.currentTarget.value))} class="field"/></label>
            <label>Ducking<select value={String(value('mix_ducking','strong'))} onchange={(event)=>set('mix_ducking',event.currentTarget.value)} class="field"><option value="strong">Strong (recommended)</option><option value="balanced">Balanced</option><option value="gentle">Gentle</option><option value="off">Off</option></select></label>
            <label>Voice loudness target (LUFS)<input type="number" min="-30" max="-8" step="0.5" value={Number(value('mix_voice_lufs',-16))} oninput={(event)=>set('mix_voice_lufs',Number(event.currentTarget.value))} class="field"/></label>
            <label>Ducking attack (ms)<input type="number" min="1" max="2000" value={Number(value('mix_attack_ms',25))} oninput={(event)=>set('mix_attack_ms',Number(event.currentTarget.value))} class="field"/></label>
            <label>Ducking release (ms)<input type="number" min="10" max="5000" value={Number(value('mix_release_ms',350))} oninput={(event)=>set('mix_release_ms',Number(event.currentTarget.value))} class="field"/></label>
          </div></fieldset>
        {/if}

        {#if exportMode==='media'}
          <fieldset class="rounded-2xl border border-[var(--line)] p-5"><legend class="px-2 text-sm font-semibold">Voiceover synchronization</legend><p class="muted text-xs">Timing is recalculated against each remaining subtitle window so long lines cannot silently compound drift.</p><div class="mt-4 grid gap-4 sm:grid-cols-3">
            <label>Maximum start delay (ms)<input type="number" min="0" max="10000" step="50" value={Number(audioValue('synchronization_delay_ms',2000))} oninput={(event)=>setAudio('synchronization_delay_ms',Number(event.currentTarget.value))} class="field"/></label>
            <label>Maximum speed-up<input type="number" min="1" max="4" step="0.01" value={Number(audioValue('synchronization_speed',1.15))} oninput={(event)=>setAudio('synchronization_speed',Number(event.currentTarget.value))} class="field"/></label>
            <label>Sentence gap (ms)<input type="number" min="0" max="5000" step="10" value={Number(audioValue('synchronization_sentence_gap_ms',100))} oninput={(event)=>setAudio('synchronization_sentence_gap_ms',Number(event.currentTarget.value))} class="field"/></label>
          </div></fieldset>
        {/if}

        {#if exportMode==='media'&&hasSourceVideo&&subtitleMode==='burned'}
          <div class="rounded-2xl border border-[var(--line)] p-5"><div class="text-sm font-semibold">Burned-subtitle transcoding</div><div class="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3"><label>Video encoder<select value={String(value('burn_video_encoder','libx264'))} onchange={(event)=>set('burn_video_encoder',event.currentTarget.value)} class="field">{#if !burnVideoEncoders.some((item:any)=>item.id===String(value('burn_video_encoder','libx264')))}<option value={String(value('burn_video_encoder','libx264'))}>{String(value('burn_video_encoder','libx264'))} (currently unavailable)</option>{/if}{#each burnVideoEncoders as encoder}<option value={encoder.id}>{encoder.label}</option>{/each}</select></label><label>Quality<input value={Number(value('burn_video_quality',18))} oninput={(event)=>set('burn_video_quality',Number(event.currentTarget.value))} type="number" min="0" max="51" step="1" class="field"/></label><label>Encoding speed<select value={String(value('burn_video_speed','balanced'))} onchange={(event)=>set('burn_video_speed',event.currentTarget.value)} class="field"><option value="fast">Fast</option><option value="balanced">Balanced</option><option value="quality">Quality</option></select></label><label>Audio<select value={String(value('burn_audio_codec','copy'))} onchange={(event)=>set('burn_audio_codec',event.currentTarget.value)} class="field"><option value="copy">Copy without transcoding</option><option value="aac">Transcode to AAC</option></select></label>{#if value('burn_audio_codec','copy')==='aac'}<label>AAC bitrate<input value={String(value('burn_audio_bitrate','192k'))} oninput={(event)=>set('burn_audio_bitrate',event.currentTarget.value)} class="field"/></label>{/if}</div></div>
        {/if}
      </div>
    {/if}
  {/if}
</section>
{#if preview}<ArtifactPreview artifact={preview} onclose={()=>preview=null}/>{/if}

<style>.field{margin-top:.4rem;width:100%;border:1px solid var(--line);border-radius:.7rem;background:var(--paper);padding:.65rem .75rem;font-weight:400}.tool{display:flex;align-items:center;gap:.4rem;border:1px solid var(--line);border-radius:.65rem;padding:.55rem .75rem;font-size:.75rem;font-weight:700}.tool.primary{background:var(--action-bg);color:white}.tool.primary:hover{background:var(--action-hover)}.tool:disabled{opacity:.4}label{font-size:.75rem;font-weight:650}</style>
