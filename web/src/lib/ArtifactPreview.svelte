<script lang="ts">
  import { Download, FileQuestion, LoaderCircle, X } from '@lucide/svelte';
  import { onMount } from 'svelte';
  import { artifactFilename, artifactRoleLabel, formatBytes, type PreviewableArtifact } from './artifact-display';

  let { artifact, onclose }: { artifact: PreviewableArtifact; onclose: () => void } = $props();
  let text = $state('');
  let error = $state('');
  let loading = $state(false);
  const url = $derived(`/api/v1/artifacts/${artifact.id}/content`);
  const filename = $derived(artifactFilename(artifact));
  const extension = $derived(filename.split('.').at(-1)?.toLowerCase() ?? '');
  const mime = $derived(String(artifact.mime_type ?? '').toLowerCase());
  const isAudio = $derived(mime.startsWith('audio/') || ['wav','mp3','flac','m4a','ogg','opus','aac'].includes(extension));
  const isVideo = $derived(mime.startsWith('video/') || ['mp4','mkv','webm','mov','avi'].includes(extension));
  const isImage = $derived(mime.startsWith('image/') || ['png','jpg','jpeg','gif','webp','svg'].includes(extension));
  const isPdf = $derived(mime === 'application/pdf' || extension === 'pdf');
  const isText = $derived(mime.startsWith('text/') || ['txt','srt','vtt','ass','ssa','json','xml','csv','md','log','yaml','yml'].includes(extension));

  onMount(async () => {
    if (!isText) return;
    loading = true;
    try {
      const response = await fetch(url, { credentials: 'same-origin', headers: { Range: 'bytes=0-2097151' } });
      if (!response.ok && response.status !== 206) throw new Error(`Preview failed (${response.status})`);
      text = await response.text();
      if (text.length >= 2_097_000) text += '\n\n— Preview truncated at 2 MiB —';
    } catch (caught) { error = caught instanceof Error ? caught.message : String(caught); }
    finally { loading = false; }
  });
</script>

<div class="fixed inset-0 z-[80] grid place-items-center bg-black/55 p-3 backdrop-blur-sm" role="presentation" onclick={(event)=>event.target===event.currentTarget&&onclose()}>
  <div class="surface flex max-h-[96vh] w-full max-w-6xl flex-col overflow-hidden rounded-3xl" role="dialog" aria-modal="true" aria-labelledby="artifact-preview-title">
    <header class="flex items-start gap-4 border-b border-[var(--line)] px-5 py-4 sm:px-6">
      <div class="min-w-0 flex-1"><div class="eyebrow">{artifactRoleLabel(artifact.role)}</div><h2 id="artifact-preview-title" class="mt-1 truncate text-xl font-semibold">{filename}</h2><div class="muted mt-1 flex flex-wrap gap-2 text-xs"><span>{artifact.mime_type || artifact.kind || 'Unknown file type'}</span>{#if artifact.size_bytes != null}<span>· {formatBytes(artifact.size_bytes)}</span>{/if}{#if artifact.state}<span>· {artifact.state}</span>{/if}</div></div>
      <a href={url} download={filename} class="tool flex items-center gap-2"><Download size={16}/> Download</a>
      <button onclick={onclose} class="rounded-xl p-2" aria-label="Close preview"><X size={20}/></button>
    </header>
    <div class="min-h-0 flex-1 overflow-auto bg-[var(--paper)] p-4 sm:p-6">
      {#if isAudio}<div class="grid min-h-72 place-items-center"><audio controls autoplay={false} preload="metadata" src={url} class="w-full max-w-3xl"></audio></div>
      {:else if isVideo}<!-- svelte-ignore a11y_media_has_caption --><video controls preload="metadata" src={url} class="mx-auto max-h-[72vh] max-w-full rounded-xl bg-black"></video>
      {:else if isImage}<img src={url} alt={filename} class="mx-auto max-h-[74vh] max-w-full rounded-xl object-contain"/>
      {:else if isPdf}<iframe src={url} title={filename} class="h-[74vh] w-full rounded-xl border border-[var(--line)] bg-white"></iframe>
      {:else if isText}{#if loading}<div class="grid min-h-72 place-items-center"><LoaderCircle class="animate-spin" size={24}/></div>{:else if error}<p class="text-sm text-red-500">{error}</p>{:else}<pre class="whitespace-pre-wrap break-words rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] p-5 font-mono text-sm leading-6">{text}</pre>{/if}
      {:else}<div class="grid min-h-72 place-items-center text-center"><div><FileQuestion class="muted mx-auto" size={38}/><h3 class="mt-4 font-semibold">Preview not available for this file type</h3><p class="muted mt-2 text-sm">Download the managed artifact to open it in a compatible application.</p></div></div>{/if}
    </div>
  </div>
</div>

<style>.tool{border:1px solid var(--line);border-radius:.7rem;padding:.55rem .75rem;font-size:.75rem;font-weight:650}</style>
