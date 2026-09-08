<script lang="ts">
  import { errorMessage } from './errors';
  import {
    BookOpenText,
    FileText,
    Link2,
    LoaderCircle,
    Upload,
    X
  } from '@lucide/svelte';
  import { uploadManagedFile } from './api';
  import { sessionApi, sourceApi } from './domain-api';
  import type { SourceAsset } from './api-models';
  import SearchReplaceBar from './SearchReplaceBar.svelte';
  import type { TextSearchMatch } from './search-replace';
  import { modalFocus } from './modal-focus';

  let {
    sessionId,
    allowTranscriptRole = false,
    onclose,
    onadded
  }: {
    sessionId: string;
    allowTranscriptRole?: boolean;
    onclose: () => void;
    onadded: (message: string) => void | Promise<void>;
  } = $props();

  type SourceMode = 'upload' | 'paste' | 'url' | 'reuse';
  let mode = $state<SourceMode>('upload');
  let role = $state<'primary' | 'transcript'>('primary');
  let file = $state<File | null>(null);
  let pastedText = $state('');
  let pastedName = $state('Pasted text');
  let sourceUrl = $state('');
  let sourceAssetId = $state('');
  let sources = $state<SourceAsset[]>([]);
  let busy = $state(false);
  let progress = $state(0);
  let progressDetail = $state('');
  let error = $state('');
  let pastedTextArea = $state<HTMLTextAreaElement>();
  let previousRole: typeof role = 'primary';

  function isRoleCompatible(source: SourceAsset) {
    const name =
      `${source.display_name ?? ''}.${source.kind ?? ''}`.toLowerCase();
    const mime = String(source.mime_type ?? '').toLowerCase();
    return role === 'transcript'
      ? /\.(srt|vtt|txt)(?:\.|$)/.test(name) ||
          ['text/plain', 'text/vtt', 'application/x-subrip'].includes(mime)
      : mime.startsWith('video/') ||
          /\.(mp4|mkv|mov|avi|webm|m4v|mpeg|mpg)(?:\.|$)/.test(name);
  }

  const compatibleSources = $derived(
    allowTranscriptRole ? sources.filter(isRoleCompatible) : sources
  );

  $effect(() => {
    if (
      allowTranscriptRole &&
      !compatibleSources.some((source) => source.id === sourceAssetId)
    )
      sourceAssetId = compatibleSources[0]?.id ?? '';
  });

  $effect(() => {
    const nextRole = role;
    if (!allowTranscriptRole || nextRole === previousRole) return;
    file = null;
    if (
      (nextRole === 'primary' && mode === 'paste') ||
      (nextRole === 'transcript' && mode === 'url')
    )
      mode = 'upload';
    previousRole = nextRole;
  });

  function navigatePastedText(match: TextSearchMatch) {
    pastedTextArea?.focus();
    pastedTextArea?.setSelectionRange(match.start, match.end);
  }

  const choices = [
    {
      id: 'upload',
      label: 'Upload',
      description: 'Choose a file from this device.',
      icon: Upload
    },
    {
      id: 'paste',
      label: 'Paste text',
      description: 'Create a reusable text source.',
      icon: FileText
    },
    {
      id: 'url',
      label: 'Public URL',
      description: 'Download supported audio or video.',
      icon: Link2
    },
    {
      id: 'reuse',
      label: 'Source library',
      description: 'Attach an existing managed source.',
      icon: BookOpenText
    }
  ] as const;

  async function loadSources() {
    try {
      sources = (await sourceApi.list()).items;
      sourceAssetId ||= sources[0]?.id ?? '';
    } catch {
      sources = [];
    }
  }

  function valid() {
    if (mode === 'upload') return Boolean(file);
    if (mode === 'paste')
      return Boolean(pastedText.trim() && pastedName.trim());
    if (mode === 'url') return Boolean(sourceUrl.trim());
    return Boolean(
      sourceAssetId &&
      (!allowTranscriptRole ||
        compatibleSources.some((source) => source.id === sourceAssetId))
    );
  }

  async function attachSource(sourceId: string) {
    const session = await sessionApi.get(sessionId);
    return sessionApi.attachSource(sessionId, sourceId, session.revision, role);
  }

  async function add() {
    if (!valid()) return;
    busy = true;
    progress = 0;
    progressDetail =
      mode === 'paste' ? 'Creating pasted source' : 'Uploading source';
    error = '';
    try {
      let message =
        role === 'transcript'
          ? 'Captions attached as the current editorial transcript.'
          : 'Source added and selected as the current input.';
      if (mode === 'upload' && file) {
        const uploaded = await uploadManagedFile(
          file,
          role === 'primary' ? sessionId : undefined,
          (value) => (progress = value)
        );
        if (role === 'transcript') {
          const sourceId = String(uploaded.source_asset_id ?? '');
          if (!sourceId)
            throw new Error('The upload did not create a reusable source.');
          await attachSource(sourceId);
        }
      } else if (mode === 'paste') {
        const safeName =
          pastedName
            .trim()
            .replace(/[\\/:*?"<>|]+/g, '-')
            .replace(/\.txt$/i, '') || 'Pasted text';
        const textFile = new File([pastedText.trim()], `${safeName}.txt`, {
          type: 'text/plain'
        });
        const uploaded = await uploadManagedFile(
          textFile,
          role === 'primary' ? sessionId : undefined,
          (value) => (progress = value)
        );
        if (role === 'transcript') {
          const sourceId = String(uploaded.source_asset_id ?? '');
          if (!sourceId)
            throw new Error('The upload did not create a reusable source.');
          await attachSource(sourceId);
        }
      } else if (mode === 'url') {
        await sessionApi.downloadSourceUrl(sessionId, sourceUrl.trim());
        message =
          'Source download queued. It will become the current input when the download finishes.';
      } else {
        await attachSource(sourceAssetId);
        message =
          role === 'transcript'
            ? 'Source-library item attached as the current editorial transcript.'
            : 'Source-library item attached and selected as the current input.';
      }
      await onadded(message);
      onclose();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }

  loadSources();
</script>

<div
  class="fixed inset-0 z-[80] grid place-items-center bg-black/40 p-4 backdrop-blur-sm"
  role="presentation"
  onclick={(event) => event.target === event.currentTarget && onclose()}
>
  <!-- svelte-ignore a11y_no_noninteractive_element_to_interactive_role -->
  <section
    use:modalFocus={{ onclose }}
    class="surface max-h-[92vh] w-full max-w-3xl overflow-y-auto rounded-[1.8rem] p-6 sm:p-8"
    role="dialog"
    aria-modal="true"
    aria-labelledby="add-source-title"
  >
    <header class="flex items-start justify-between gap-4">
      <div>
        <div class="eyebrow">Session input</div>
        <h2 id="add-source-title" class="mt-1 text-2xl font-semibold">
          Add a source
        </h2>
        <p class="muted mt-2 text-sm">
          {allowTranscriptRole
            ? 'Attach the recording to edit or a timed transcript to guide it. Earlier source history remains available.'
            : 'The new source becomes current; earlier sources and their artifact histories remain available.'}
        </p>
      </div>
      <button
        onclick={onclose}
        aria-label="Close source picker"
        class="rounded-xl p-2"><X size={20} /></button
      >
    </header>
    {#if error}<p
        role="alert"
        class="mt-5 rounded-xl bg-red-500/10 p-3 text-sm text-red-500"
      >
        {error}
      </p>{/if}
    {#if allowTranscriptRole}<fieldset class="mt-6">
        <legend class="text-sm font-semibold">Use this source as</legend>
        <div class="mt-2 grid gap-2 sm:grid-cols-2">
          <label class:active={role === 'primary'} class="source-role"
            ><input type="radio" bind:group={role} value="primary" /><span
              ><strong>Recording</strong><small
                >The video that will be cut.</small
              ></span
            ></label
          ><label class:active={role === 'transcript'} class="source-role"
            ><input type="radio" bind:group={role} value="transcript" /><span
              ><strong>Captions</strong><small
                >Zoom VTT, SRT, or another timed transcript.</small
              ></span
            ></label
          >
        </div>
      </fieldset>{/if}
    <div class="mt-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      {#each choices.filter( (choice) => (role === 'transcript' ? choice.id !== 'url' : !allowTranscriptRole || choice.id !== 'paste') ) as choice}{@const Icon =
          choice.icon}<button
          onclick={() => (mode = choice.id)}
          class:active={mode === choice.id}
          class="source-mode"
          ><Icon size={18} /><span
            ><strong>{choice.label}</strong><small>{choice.description}</small
            ></span
          ></button
        >{/each}
    </div>
    <div class="mt-5 rounded-2xl border border-[var(--line)] p-5">
      {#if mode === 'upload'}
        <label class="text-sm font-semibold"
          >{allowTranscriptRole
            ? role === 'transcript'
              ? 'Caption file'
              : 'Video file'
            : 'Source file'}<input
            type="file"
            accept={allowTranscriptRole
              ? role === 'transcript'
                ? '.srt,.vtt,.txt,text/plain,text/vtt,application/x-subrip'
                : 'video/*,.mp4,.mkv,.mov,.avi,.webm,.m4v,.mpeg,.mpg'
              : undefined}
            onchange={(event) =>
              (file =
                (event.currentTarget as HTMLInputElement).files?.[0] ?? null)}
            class="mt-2 block w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] p-3 font-normal"
          /></label
        >
      {:else if mode === 'paste'}
        <label class="text-sm font-semibold"
          >Source name<input
            bind:value={pastedName}
            class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
          /></label
        >
        <label class="mt-4 block text-sm font-semibold"
          >Text<textarea
            bind:this={pastedTextArea}
            bind:value={pastedText}
            rows="8"
            placeholder="Paste text here…"
            class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] p-4 font-normal"
          ></textarea></label
        >
        <div class="mt-2">
          <SearchReplaceBar
            texts={[pastedText]}
            onreplace={(updates) => {
              if (updates[0]) pastedText = updates[0].text;
            }}
            onnavigate={navigatePastedText}
            label="pasted source"
          />
        </div>
      {:else if mode === 'url'}
        <label class="text-sm font-semibold"
          >Public media URL<input
            bind:value={sourceUrl}
            type="url"
            placeholder="https://www.youtube.com/watch?v=…"
            class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
          /></label
        >
        <p class="muted mt-3 text-xs leading-relaxed">
          Pandrator uses yt-dlp for supported public video and audio sites.
          Playlists are not downloaded automatically.
        </p>
      {:else}
        <label class="text-sm font-semibold"
          >Reusable source<select
            bind:value={sourceAssetId}
            class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
            ><option value="">No source-library items available</option
            >{#each compatibleSources as source}<option value={source.id}
                >{source.display_name} · {source.kind}</option
              >{/each}</select
          ></label
        >
      {/if}
      {#if busy && ['upload', 'paste'].includes(mode)}<div class="mt-5">
          <div class="mb-1.5 flex items-center justify-between gap-3 text-xs">
            <span class="muted">{progressDetail}</span><span
              class="muted tabular-nums"
              >{Math.round(Math.max(0, Math.min(1, progress)) * 100)}%</span
            >
          </div>
          <div
            class="h-2 overflow-hidden rounded-full bg-[var(--line)]"
            role="progressbar"
            aria-label="Source upload progress"
            aria-valuemin="0"
            aria-valuemax="100"
            aria-valuenow={Math.round(Math.max(0, Math.min(1, progress)) * 100)}
          >
            <div
              class="h-full bg-[var(--accent)] transition-[width]"
              style={`width:${Math.max(0, Math.min(1, progress)) * 100}%`}
            ></div>
          </div>
        </div>{/if}
    </div>
    <footer class="mt-6 flex justify-end gap-2">
      <button
        onclick={onclose}
        disabled={busy}
        class="rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold"
        >Cancel</button
      ><button
        onclick={add}
        disabled={busy || !valid()}
        class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-40"
        >{#if busy}<LoaderCircle class="animate-spin" size={16} />{:else}<Upload
            size={16}
          />{/if}
        {busy ? 'Adding…' : 'Add and select'}</button
      >
    </footer>
  </section>
</div>

<style>
  .source-mode {
    display: flex;
    align-items: flex-start;
    gap: 0.6rem;
    border: 1px solid var(--line);
    border-radius: 1rem;
    padding: 0.9rem;
    text-align: left;
  }
  .source-mode.active {
    border-color: var(--accent);
    background: var(--accent-soft);
  }
  .source-role {
    display: flex;
    gap: 0.65rem;
    border: 1px solid var(--line);
    border-radius: 0.9rem;
    padding: 0.8rem;
  }
  .source-role.active {
    border-color: var(--accent);
    background: var(--accent-soft);
  }
  .source-role input {
    margin-top: 0.15rem;
    accent-color: var(--accent);
  }
  .source-role strong,
  .source-role small {
    display: block;
  }
  .source-role small {
    margin-top: 0.15rem;
    color: var(--muted);
    font-size: 0.7rem;
  }
  .source-mode :global(svg) {
    margin-top: 0.1rem;
    flex: none;
    color: var(--accent);
  }
  .source-mode strong,
  .source-mode small {
    display: block;
  }
  .source-mode strong {
    font-size: 0.8rem;
  }
  .source-mode small {
    margin-top: 0.2rem;
    color: var(--muted);
    font-size: 0.67rem;
    line-height: 1.35;
  }
</style>
