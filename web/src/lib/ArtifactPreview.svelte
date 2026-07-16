<script lang="ts">
  import { Download, FileQuestion, LoaderCircle, X } from '@lucide/svelte';
  import { onMount } from 'svelte';
  import { artifactFilename, artifactRoleLabel, formatBytes, type PreviewableArtifact } from './artifact-display';
  import AudioPlayer from './AudioPlayer.svelte';
  import TextDiff from './TextDiff.svelte';

  let { artifact, onclose }: { artifact: PreviewableArtifact; onclose: () => void } = $props();
  let text = $state('');
  let error = $state('');
  let loading = $state(false);
  let comparisonText = $state('');
  let comparisonName = $state('');
  let comparisonId = $state('');
  let comparisonMode = $state<'single' | 'side' | 'diff'>('single');
  type SubtitleTrack = { artifact_id: string; language: string; title: string; default: boolean };
  let subtitleTracks = $state<SubtitleTrack[]>([]);
  const url = $derived(`/api/v1/artifacts/${artifact.id}/content`);
  const filename = $derived(artifactFilename(artifact));
  const extension = $derived(filename.split('.').at(-1)?.toLowerCase() ?? '');
  const mime = $derived(String(artifact.mime_type ?? '').toLowerCase());
  const isAudio = $derived(mime.startsWith('audio/') || ['wav','mp3','flac','m4a','ogg','opus','aac'].includes(extension));
  const isVideo = $derived(mime.startsWith('video/') || ['mp4','mkv','webm','mov','avi'].includes(extension));
  const isImage = $derived(mime.startsWith('image/') || ['png','jpg','jpeg','gif','webp','svg'].includes(extension));
  const isPdf = $derived(mime === 'application/pdf' || extension === 'pdf');
  const isText = $derived(mime.startsWith('text/') || ['txt','srt','vtt','ass','ssa','json','xml','csv','md','log','yaml','yml'].includes(extension));

  function parsedSubtitleTracks(value: unknown): SubtitleTrack[] {
    if (!Array.isArray(value)) return [];
    return value.flatMap((item: any) => item?.artifact_id ? [{
      artifact_id: String(item.artifact_id),
      language: String(item.language || 'und'),
      title: String(item.title || 'Subtitles'),
      default: Boolean(item.default)
    }] : []);
  }

  function showDefaultSubtitleTrack(event: Event) {
    const video = event.currentTarget as HTMLVideoElement;
    Array.from(video.textTracks).forEach((track, index) => {
      track.mode = subtitleTracks[index]?.default ? 'showing' : 'disabled';
    });
  }

  function loadSubtitleTrack(event: Event, isDefault: boolean) {
    (event.currentTarget as HTMLTrackElement).track.mode = isDefault ? 'showing' : 'disabled';
  }

  onMount(async () => {
    loading = isText;
    try {
      if (isText) {
        const response = await fetch(url, { credentials: 'same-origin', headers: { Range: 'bytes=0-2097151' } });
        if (!response.ok && response.status !== 206) throw new Error(`Preview failed (${response.status})`);
        text = await response.text();
        if (text.length >= 2_097_000) text += '\n\n— Preview truncated at 2 MiB —';
      }

      const contextResponse = await fetch(`/api/v1/artifacts/${artifact.id}/context`, { credentials: 'same-origin' });
      if (contextResponse.ok) {
        const context = await contextResponse.json();
        if (isVideo) {
          subtitleTracks = parsedSubtitleTracks(context.artifact?.metadata_json?.subtitle_tracks ?? artifact.metadata_json?.subtitle_tracks);
          if (!subtitleTracks.length) {
            subtitleTracks = (context.parents ?? []).flatMap((item: any) => {
              const name = String(item.relative_path ?? '').toLowerCase();
              if (item.kind !== 'vtt' && !name.endsWith('.vtt')) return [];
              return [{ artifact_id: String(item.id), language: String(item.metadata_json?.language || 'und'), title: String(item.metadata_json?.title || 'Subtitles'), default: Boolean(item.metadata_json?.default) }];
            });
          }
        }
        if (isText) {
          const parent = (context.parents ?? []).find((item: any) => {
            const name = String(item.relative_path ?? '').toLowerCase();
            const type = String(item.mime_type ?? '').toLowerCase();
            return item.kind === 'text' || type.startsWith('text/') || ['.txt','.md','.srt','.vtt','.json'].some((suffix) => name.endsWith(suffix));
          });
          if (parent) {
            const parentResponse = await fetch(`/api/v1/artifacts/${parent.id}/content`, { credentials: 'same-origin', headers: { Range: 'bytes=0-2097151' } });
            if (parentResponse.ok || parentResponse.status === 206) {
              comparisonText = await parentResponse.text();
              comparisonId = parent.id;
              comparisonName = artifactFilename(parent);
            }
          }
        }
      }
    } catch (caught) { error = caught instanceof Error ? caught.message : String(caught); }
    finally { loading = false; }
  });
</script>

<div class="fixed inset-0 z-[80] grid place-items-center bg-black/55 p-3 backdrop-blur-sm" role="presentation" onclick={(event)=>event.target===event.currentTarget&&onclose()}>
  <div class="preview-panel flex max-h-[96vh] w-full max-w-6xl flex-col overflow-hidden rounded-3xl" role="dialog" aria-modal="true" aria-labelledby="artifact-preview-title">
    <header class="flex items-start gap-4 border-b border-[var(--line)] px-5 py-4 sm:px-6">
      <div class="min-w-0 flex-1"><div class="eyebrow">{artifactRoleLabel(artifact.role)}</div><h2 id="artifact-preview-title" class="mt-1 truncate text-xl font-semibold">{filename}</h2><div class="muted mt-1 flex flex-wrap gap-2 text-xs"><span>{artifact.mime_type || artifact.kind || 'Unknown file type'}</span>{#if artifact.size_bytes != null}<span>· {formatBytes(artifact.size_bytes)}</span>{/if}{#if artifact.state}<span>· {artifact.state}</span>{/if}</div></div>
      {#if comparisonId}<div class="flex gap-1"><button onclick={() => comparisonMode = comparisonMode === 'side' ? 'single' : 'side'} class:active={comparisonMode === 'side'} class="tool">Side by side</button><button onclick={() => comparisonMode = comparisonMode === 'diff' ? 'single' : 'diff'} class:active={comparisonMode === 'diff'} class="tool">Diff</button></div>{/if}
      <a href={url} download={filename} class="tool flex items-center gap-2"><Download size={16}/> Download</a>
      <button onclick={onclose} class="rounded-xl p-2" aria-label="Close preview"><X size={20}/></button>
    </header>
    <div class="min-h-0 flex-1 overflow-auto bg-[var(--paper)] p-4 sm:p-6">
      {#if isAudio}<div class="grid min-h-72 place-items-center"><div class="w-full max-w-3xl"><AudioPlayer src={url}/></div></div>
      {:else if isVideo}<!-- svelte-ignore a11y_media_has_caption --><video controls preload="metadata" src={url} onloadedmetadata={showDefaultSubtitleTrack} class="mx-auto max-h-[72vh] max-w-full rounded-xl bg-black">{#each subtitleTracks as track}<track kind="subtitles" src={`/api/v1/artifacts/${track.artifact_id}/content`} srclang={track.language} label={track.title} default={track.default} onload={(event)=>loadSubtitleTrack(event,track.default)}/>{/each}</video>
      {:else if isImage}<img src={url} alt={filename} class="mx-auto max-h-[74vh] max-w-full rounded-xl object-contain"/>
      {:else if isPdf}<iframe src={url} title={filename} class="h-[74vh] w-full rounded-xl border border-[var(--line)] bg-white"></iframe>
      {:else if isText}
        {#if loading}<div class="grid min-h-72 place-items-center"><LoaderCircle class="animate-spin" size={24}/></div>
        {:else if error}<p class="text-sm text-red-500">{error}</p>
        {:else if comparisonMode === 'side' && comparisonId}<div class="comparison-grid"><section><h3>Before · {comparisonName}</h3><pre>{comparisonText}</pre></section><section><h3>After · {filename}</h3><pre>{text}</pre></section></div>
        {:else if comparisonMode === 'diff' && comparisonId}<div><div class="muted mb-2 text-xs">Removed text is red and struck through; added text is green.</div><TextDiff before={comparisonText} after={text}/></div>
        {:else}<pre class="whitespace-pre-wrap break-words rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] p-5 font-mono text-sm leading-6">{text}</pre>{/if}
      {:else}<div class="grid min-h-72 place-items-center text-center"><div><FileQuestion class="muted mx-auto" size={38}/><h3 class="mt-4 font-semibold">Preview not available for this file type</h3><p class="muted mt-2 text-sm">Download the managed artifact to open it in a compatible application.</p></div></div>{/if}
    </div>
  </div>
</div>

<style>
  .preview-panel{border:1px solid var(--line);background:var(--paper-strong);box-shadow:0 22px 70px rgba(0,0,0,.25)}
  .tool{border:1px solid var(--line);border-radius:.7rem;padding:.55rem .75rem;font-size:.75rem;font-weight:650}.tool.active{background:var(--accent-soft);color:var(--accent)}
  .comparison-grid{display:grid;gap:1rem}@media(min-width:768px){.comparison-grid{grid-template-columns:1fr 1fr}}
  .comparison-grid section{min-width:0}.comparison-grid h3{position:sticky;top:-1rem;z-index:2;margin-bottom:.5rem;border:1px solid var(--line);border-radius:.7rem;background:var(--paper-strong);padding:.65rem .8rem;font-size:.7rem;font-weight:750;color:var(--muted)}
  .comparison-grid pre{min-height:18rem;white-space:pre-wrap;overflow-wrap:anywhere;border:1px solid var(--line);border-radius:.8rem;background:var(--paper-strong);padding:1rem;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.75rem;line-height:1.55}
</style>
