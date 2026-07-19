<script lang="ts">
  import { BookOpenText, FileText, Link2, LoaderCircle, Upload, X } from '@lucide/svelte';
  import { api, uploadManagedFile } from './api';

  let {
    sessionId,
    onclose,
    onadded
  }: {
    sessionId: string;
    onclose: () => void;
    onadded: (message: string) => void | Promise<void>;
  } = $props();

  type SourceMode = 'upload' | 'paste' | 'url' | 'reuse';
  let mode = $state<SourceMode>('upload');
  let file = $state<File | null>(null);
  let pastedText = $state('');
  let pastedName = $state('Pasted text');
  let sourceUrl = $state('');
  let sourceAssetId = $state('');
  let sources = $state<any[]>([]);
  let busy = $state(false);
  let progress = $state(0);
  let error = $state('');

  const choices = [
    { id: 'upload', label: 'Upload', description: 'Choose a file from this device.', icon: Upload },
    { id: 'paste', label: 'Paste text', description: 'Create a reusable text source.', icon: FileText },
    { id: 'url', label: 'Public URL', description: 'Download supported audio or video.', icon: Link2 },
    { id: 'reuse', label: 'Source library', description: 'Attach an existing managed source.', icon: BookOpenText }
  ] as const;

  async function loadSources() {
    try {
      sources = (await api<{items:any[]}>('/sources')).items;
      sourceAssetId ||= sources[0]?.id ?? '';
    } catch {
      sources = [];
    }
  }

  function valid() {
    if (mode === 'upload') return Boolean(file);
    if (mode === 'paste') return Boolean(pastedText.trim() && pastedName.trim());
    if (mode === 'url') return Boolean(sourceUrl.trim());
    return Boolean(sourceAssetId);
  }

  async function add() {
    if (!valid()) return;
    busy = true;
    error = '';
    try {
      let message = 'Source added and selected as the current input.';
      if (mode === 'upload' && file) {
        await uploadManagedFile(file, sessionId, (value) => progress = value);
      } else if (mode === 'paste') {
        const safeName = pastedName.trim().replace(/[\\/:*?"<>|]+/g, '-').replace(/\.txt$/i, '') || 'Pasted text';
        const textFile = new File([pastedText.trim()], `${safeName}.txt`, { type: 'text/plain' });
        await uploadManagedFile(textFile, sessionId, (value) => progress = value);
      } else if (mode === 'url') {
        await api(`/sessions/${sessionId}/sources/url`, {
          method: 'POST',
          body: JSON.stringify({ url: sourceUrl.trim() })
        });
        message = 'Source download queued. It will become the current input when the download finishes.';
      } else {
        await api(`/sessions/${sessionId}/sources`, {
          method: 'POST',
          body: JSON.stringify({ source_asset_id: sourceAssetId, role: 'primary' })
        });
        message = 'Source-library item attached and selected as the current input.';
      }
      await onadded(message);
      onclose();
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    } finally {
      busy = false;
    }
  }

  loadSources();
</script>

<div class="fixed inset-0 z-[80] grid place-items-center bg-black/40 p-4 backdrop-blur-sm" role="presentation" onclick={(event)=>event.target===event.currentTarget&&onclose()}>
  <!-- svelte-ignore a11y_no_noninteractive_element_to_interactive_role -->
  <section class="surface max-h-[92vh] w-full max-w-3xl overflow-y-auto rounded-[1.8rem] p-6 sm:p-8" role="dialog" aria-modal="true" aria-labelledby="add-source-title">
    <header class="flex items-start justify-between gap-4"><div><div class="eyebrow">Session input</div><h2 id="add-source-title" class="mt-1 text-2xl font-semibold">Add a source</h2><p class="muted mt-2 text-sm">The new source becomes current; earlier sources and their artifact histories remain available.</p></div><button onclick={onclose} aria-label="Close source picker" class="rounded-xl p-2"><X size={20}/></button></header>
    {#if error}<p role="alert" class="mt-5 rounded-xl bg-red-500/10 p-3 text-sm text-red-500">{error}</p>{/if}
    <div class="mt-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      {#each choices as choice}{@const Icon=choice.icon}<button onclick={()=>mode=choice.id} class:active={mode===choice.id} class="source-mode"><Icon size={18}/><span><strong>{choice.label}</strong><small>{choice.description}</small></span></button>{/each}
    </div>
    <div class="mt-5 rounded-2xl border border-[var(--line)] p-5">
      {#if mode==='upload'}
        <label class="text-sm font-semibold">Source file<input type="file" onchange={(event)=>file=(event.currentTarget as HTMLInputElement).files?.[0]??null} class="mt-2 block w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] p-3 font-normal"/></label>
      {:else if mode==='paste'}
        <label class="text-sm font-semibold">Source name<input bind:value={pastedName} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"/></label>
        <label class="mt-4 block text-sm font-semibold">Text<textarea bind:value={pastedText} rows="8" placeholder="Paste text here…" class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] p-4 font-normal"></textarea></label>
      {:else if mode==='url'}
        <label class="text-sm font-semibold">Public media URL<input bind:value={sourceUrl} type="url" placeholder="https://www.youtube.com/watch?v=…" class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"/></label><p class="muted mt-3 text-xs leading-relaxed">Pandrator uses yt-dlp for supported public video and audio sites. Playlists are not downloaded automatically.</p>
      {:else}
        <label class="text-sm font-semibold">Reusable source<select bind:value={sourceAssetId} class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"><option value="">No source-library items available</option>{#each sources as source}<option value={source.id}>{source.display_name} · {source.kind}</option>{/each}</select></label>
      {/if}
      {#if busy&&mode!=='url'}<div class="mt-5 h-2 overflow-hidden rounded-full bg-[var(--line)]"><div class="h-full bg-[var(--accent)]" style={`width:${Math.max(3,progress*100)}%`}></div></div>{/if}
    </div>
    <footer class="mt-6 flex justify-end gap-2"><button onclick={onclose} disabled={busy} class="rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold">Cancel</button><button onclick={add} disabled={busy||!valid()} class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-40">{#if busy}<LoaderCircle class="animate-spin" size={16}/>{:else}<Upload size={16}/>{/if} {busy?'Adding…':'Add and select'}</button></footer>
  </section>
</div>

<style>
  .source-mode{display:flex;align-items:flex-start;gap:.6rem;border:1px solid var(--line);border-radius:1rem;padding:.9rem;text-align:left}.source-mode.active{border-color:var(--accent);background:var(--accent-soft)}.source-mode :global(svg){margin-top:.1rem;flex:none;color:var(--accent)}.source-mode strong,.source-mode small{display:block}.source-mode strong{font-size:.8rem}.source-mode small{margin-top:.2rem;color:var(--muted);font-size:.67rem;line-height:1.35}
</style>
