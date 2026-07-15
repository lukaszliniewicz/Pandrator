<script lang="ts">
  import { ImagePlus, Save, Trash2 } from '@lucide/svelte';
  import { api } from './api';
  import ArtifactPreview from './ArtifactPreview.svelte';
  import { artifactFilename } from './artifact-display';

  let { sessionId }: { sessionId: string } = $props();
  let settings = $state<any>(null);
  let session = $state<any>(null);
  let capabilities = $state<any>({});
  let draft = $state<Record<string, unknown>>({});
  let images = $state<any[]>([]);
  let preview = $state<any|null>(null);
  let busy = $state(false);
  let message = $state('');
  let error = $state('');

  const coverId = $derived(String(Object.prototype.hasOwnProperty.call(draft,'cover_artifact_id') ? draft.cover_artifact_id ?? '' : settings?.effective?.cover_artifact_id ?? ''));
  const selectedCover = $derived(images.find((item)=>item.id===coverId));
  const burnVideoEncoders = $derived(Array.isArray(capabilities?.ffmpeg?.burn_video_encoders) && capabilities.ffmpeg.burn_video_encoders.length ? capabilities.ffmpeg.burn_video_encoders : [{id:'libx264',label:'H.264 software (most compatible)',hardware:false,codec:'h264'}]);
  const value = (key:string, fallback:unknown='') => Object.prototype.hasOwnProperty.call(draft,key) ? draft[key] : settings?.effective?.[key] ?? fallback;
  const set = (key:string, next:unknown) => draft={...draft,[key]:next};

  async function load() {
    [session, settings, {items:images}, capabilities] = await Promise.all([
      api<any>(`/sessions/${sessionId}`),
      api<any>(`/sessions/${sessionId}/settings/output`),
      api<any>(`/artifacts?session_id=${sessionId}&limit=500`).then((payload)=>({items:(payload.items??[]).filter((item:any)=>item.state==='current'&&(item.kind==='image'||String(item.mime_type??'').startsWith('image/')))})),
      api<any>('/capabilities').catch(()=>({}))
    ]);
    draft={...settings.override};
  }

  async function save(nextDraft=draft) {
    busy=true;error='';message='';
    try {
      settings=await api<any>(`/sessions/${sessionId}/settings/output`,{method:'PUT',headers:{'If-Match':`"${settings.revision}"`},body:JSON.stringify({value:nextDraft})});
      draft={...settings.override};
      message='Output settings saved for this session.';
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
      const next={...draft,cover_artifact_id:result.artifact_id}; draft=next; await save(next);
    } catch(caught){error=caught instanceof Error?caught.message:String(caught)} finally{busy=false;input.value=''}
  }

  load().catch((caught)=>error=caught instanceof Error?caught.message:String(caught));
</script>

<section class="surface rounded-2xl p-5">
  <div class="flex flex-wrap items-start justify-between gap-4"><div><div class="eyebrow">Output profile</div><h2 class="mt-1 text-xl font-semibold">Container, metadata, artwork, and tracks</h2><p class="muted mt-2 text-sm">M4B preserves audiobook metadata, cover artwork, and chapter markers carried by the generation plan.</p></div><div class="flex gap-2"><button onclick={()=>{draft={};save({})}} disabled={busy||!Object.keys(draft).length} class="tool">Inherit defaults</button><button onclick={()=>save()} disabled={busy} class="tool primary"><Save size={15}/> {busy?'Saving…':'Save output profile'}</button></div></div>
  {#if error}<p class="mt-4 rounded-xl bg-red-500/10 p-3 text-sm text-red-500">{error}</p>{/if}{#if message}<p class="mt-4 rounded-xl bg-[var(--accent-soft)] p-3 text-sm">{message}</p>{/if}
  {#if settings}
    <div class="mt-6 grid gap-5 xl:grid-cols-[1fr_18rem]">
      <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <label>Format<select value={String(value('format','wav'))} onchange={(event)=>set('format',event.currentTarget.value)} class="field"><option value="m4b">M4B audiobook</option><option value="mp3">MP3</option><option value="opus">Opus</option><option value="flac">FLAC</option><option value="wav">PCM WAV</option></select></label>
        <label>Bitrate<input value={String(value('bitrate','192k'))} oninput={(event)=>set('bitrate',event.currentTarget.value)} class="field"/></label>
        <label>Language identifier<input value={String(value('language',''))} oninput={(event)=>set('language',event.currentTarget.value)} placeholder="en" class="field"/></label>
        <label>Title<input value={String(value('title',''))} oninput={(event)=>set('title',event.currentTarget.value)} class="field"/></label>
        <label>Author / artist<input value={String(value('artist',''))} oninput={(event)=>set('artist',event.currentTarget.value)} class="field"/></label>
        <label>Album / series<input value={String(value('album',''))} oninput={(event)=>set('album',event.currentTarget.value)} class="field"/></label>
        <label>Genre<input value={String(value('genre','Audiobook'))} oninput={(event)=>set('genre',event.currentTarget.value)} class="field"/></label>
        {#if session?.workflow_kind!=='audiobook'}
          <label>Export target<select value={String(value('export_mode','media'))} onchange={(event)=>set('export_mode',event.currentTarget.value)} class="field"><option value="media">Video / media</option><option value="subtitles">Subtitles only</option><option value="text">Concatenated text only</option></select></label>
          {#if value('export_mode','media')==='media'}
            <label>Audio<select value={String(value('audio_mode','preserve'))} onchange={(event)=>set('audio_mode',event.currentTarget.value)} class="field"><option value="preserve">Preserve source audio</option><option value="mixed">Mix source and generated audio</option><option value="dubbing_only">Generated audio only</option></select></label>
            <label>Subtitles<select value={String(value('subtitle_mode','none'))} onchange={(event)=>set('subtitle_mode',event.currentTarget.value)} class="field"><option value="none">No subtitles</option><option value="soft">Soft / selectable tracks</option><option value="burned">Burned into video</option></select></label>
            {#if value('subtitle_mode','none')==='burned'}
              <div class="rounded-xl border border-[var(--line)] p-4 sm:col-span-2 lg:col-span-3">
                <div class="text-sm font-semibold">Burned-subtitle transcoding</div>
                <div class="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                  <label>Video encoder<select value={String(value('burn_video_encoder','libx264'))} onchange={(event)=>set('burn_video_encoder',event.currentTarget.value)} class="field">{#if !burnVideoEncoders.some((item:any)=>item.id===String(value('burn_video_encoder','libx264')))}<option value={String(value('burn_video_encoder','libx264'))}>{String(value('burn_video_encoder','libx264'))} (currently unavailable)</option>{/if}{#each burnVideoEncoders as encoder}<option value={encoder.id}>{encoder.label}</option>{/each}</select></label>
                  <label>Quality<input value={Number(value('burn_video_quality',18))} oninput={(event)=>set('burn_video_quality',Number(event.currentTarget.value))} type="number" min="0" max="51" step="1" class="field"/><small class="muted mt-1 block font-normal">Lower is higher quality; 18 is visually transparent for most material.</small></label>
                  <label>Encoding speed<select value={String(value('burn_video_speed','balanced'))} onchange={(event)=>set('burn_video_speed',event.currentTarget.value)} class="field"><option value="fast">Fast</option><option value="balanced">Balanced</option><option value="quality">Quality</option></select></label>
                  <label>Audio<select value={String(value('burn_audio_codec','copy'))} onchange={(event)=>set('burn_audio_codec',event.currentTarget.value)} class="field"><option value="copy">Copy without transcoding</option><option value="aac">Transcode to AAC</option></select></label>
                  {#if value('burn_audio_codec','copy')==='aac'}<label>AAC bitrate<input value={String(value('burn_audio_bitrate','192k'))} oninput={(event)=>set('burn_audio_bitrate',event.currentTarget.value)} placeholder="192k" class="field"/></label>{/if}
                </div>
                <p class="muted mt-3 text-xs">Hardware encoders appear only when both the GPU and this FFmpeg installation support them. H.264 has the broadest browser and device compatibility.</p>
              </div>
            {/if}
          {:else if value('export_mode','media')==='subtitles'}
            <label>Subtitle format<select value={String(value('subtitle_format','srt'))} onchange={(event)=>set('subtitle_format',event.currentTarget.value)} class="field"><option value="srt">SubRip (.srt)</option><option value="vtt">WebVTT (.vtt)</option></select></label>
          {/if}
          {#if value('export_mode','media')!=='media'||value('subtitle_mode','none')!=='none'}<label>Subtitle tracks<select value={String(value('subtitle_selection','translation'))} onchange={(event)=>set('subtitle_selection',event.currentTarget.value)} class="field"><option value="source">Source / corrected</option><option value="translation">Translation</option><option value="dual">Source and translation</option></select></label>{/if}
        {:else}
          <div class="rounded-xl bg-[var(--accent-soft)] p-3 text-xs leading-relaxed"><strong>Narration audio</strong><p class="muted mt-1">Audiobook assembly uses the selected generated take for each segment. Video mixing and subtitle-track controls are hidden for this workspace.</p></div>
        {/if}
      </div>
      <aside class="rounded-2xl border border-[var(--line)] p-4"><div class="text-sm font-semibold">Cover artwork</div>{#if selectedCover}<button onclick={()=>{preview=selectedCover}} class="mt-3 block aspect-square w-full overflow-hidden rounded-xl bg-[var(--paper)]"><img src={`/api/v1/artifacts/${selectedCover.id}/content`} alt={artifactFilename(selectedCover)} class="size-full object-cover"/></button><div class="muted mt-2 truncate text-xs">{artifactFilename(selectedCover)}</div>{:else}<div class="muted mt-3 grid aspect-square place-items-center rounded-xl border border-dashed border-[var(--line)] text-center text-xs">No cover selected</div>{/if}<select value={coverId} onchange={(event)=>set('cover_artifact_id',event.currentTarget.value)} class="field mt-3"><option value="">No cover</option>{#each images as image}<option value={image.id}>{artifactFilename(image)}</option>{/each}</select><div class="mt-3 flex gap-2"><label class="tool flex flex-1 cursor-pointer justify-center"><ImagePlus size={15}/> Upload<input type="file" accept="image/png,image/jpeg,image/webp" onchange={uploadCover} class="sr-only"/></label><button onclick={()=>set('cover_artifact_id','')} disabled={!coverId} class="tool" aria-label="Remove cover"><Trash2 size={15}/></button></div></aside>
    </div>
  {/if}
</section>
{#if preview}<ArtifactPreview artifact={preview} onclose={()=>preview=null}/>{/if}

<style>.field{margin-top:.4rem;width:100%;border:1px solid var(--line);border-radius:.7rem;background:var(--paper);padding:.65rem .75rem;font-weight:400}.tool{display:flex;align-items:center;gap:.4rem;border:1px solid var(--line);border-radius:.65rem;padding:.55rem .75rem;font-size:.75rem;font-weight:700}.tool.primary{background:var(--action-bg);color:white}.tool.primary:hover{background:var(--action-hover)}.tool:disabled{opacity:.4}label{font-size:.75rem;font-weight:650}</style>
