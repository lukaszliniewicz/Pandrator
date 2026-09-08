<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { page } from '$app/state';
  import { replaceState } from '$app/navigation';
  import {
    Copy,
    Download,
    FileAudio,
    LoaderCircle,
    Trash2
  } from '@lucide/svelte';
  import QuickRecorder from '$lib/QuickRecorder.svelte';
  import {
    quickTranscriptionApi as api,
    type QuickTranscription,
    type TranscriptFormat
  } from '$lib/quick-transcription-api';
  import { speechRecognitionApi, diagnosticsApi } from '$lib/admin-api';
  import type { SttService, RuntimeCapabilities } from '$lib/api-models';
  import { errorMessage } from '$lib/errors';

  import { sessionApi } from '$lib/domain-api';
  import { sttLanguageProblem } from '$lib/stt-language-policy';

  let configuredEngine = $state('');
  let capabilities = $state<RuntimeCapabilities>({});
  let mode = $state<'upload' | 'record'>('upload');
  let file = $state<File | null>(null);
  let previewUrl = $state('');
  let recording = $state(false);
  let format = $state<TranscriptFormat>('txt');
  let language = $state('auto');
  let engine = $state('');
  const languageProblem = $derived(
    sttLanguageProblem(capabilities, engine || configuredEngine, language)
  );
  let services = $state<SttService[]>([]);
  let job = $state<QuickTranscription | null>(null);
  let uploading = $state(false);
  let uploadPercent = $state(0);
  let deleting = $state(false);
  let preview = $state('');
  let truncated = $state(false);
  let loadingResult = $state(false);
  let error = $state('');
  let notice = $state('');
  let retryKey = '';
  let timer: ReturnType<typeof setTimeout> | undefined;
  let destroyed = false;
  let resultGeneration = 0;
  const running = $derived(
    Boolean(
      job &&
      ['queued', 'running', 'cancel_requested', 'deleting'].includes(job.status)
    )
  );
  const busy = $derived(uploading || running || deleting);
  const progressPercent = $derived(Math.round((job?.progress ?? 0) * 100));

  function choose(source: File) {
    error = '';
    notice = '';
    if (!source.size || source.size > 256 * 1024 * 1024) {
      error = 'Choose a nonempty audio or video file up to 256 MiB.';
      return;
    }
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    file = source;
    previewUrl = URL.createObjectURL(source);
    retryKey = '';
  }

  function remember(id: string | null) {
    const url = new URL(window.location.href);
    if (id) url.searchParams.set('id', id);
    else url.searchParams.delete('id');
    replaceState(url, page.state);
  }

  async function loadResult() {
    const generation = ++resultGeneration;
    if (!job?.result_available) {
      preview = '';
      return;
    }
    const id = job.id;
    const selected = format;
    loadingResult = true;
    preview = '';
    try {
      const result = await api.preview(id, selected);
      if (destroyed || generation !== resultGeneration) return;
      preview = result.content;
      truncated = result.next_offset !== null;
    } catch (caught) {
      if (generation === resultGeneration) error = errorMessage(caught);
    } finally {
      if (generation === resultGeneration) loadingResult = false;
    }
  }

  async function refresh(id: string) {
    if (timer) clearTimeout(timer);
    try {
      const result = await api.get(id, format);
      if (destroyed || job?.id !== id) return;
      job = result;
      if (['failed', 'canceled', 'interrupted'].includes(result.status))
        retryKey = '';
      if (result.error) error = result.error.message;
      if (
        ['queued', 'running', 'cancel_requested', 'deleting'].includes(
          result.status
        )
      ) {
        timer = setTimeout(() => void refresh(id), 1000);
      } else if (result.result_available) await loadResult();
    } catch (caught) {
      if (!destroyed) error = errorMessage(caught);
    }
  }

  async function transcribe() {
    if (!file || busy || recording || languageProblem) return;
    error = '';
    notice = '';
    uploading = true;
    uploadPercent = 0;
    preview = '';
    retryKey ||=
      crypto.randomUUID?.() ??
      Array.from(crypto.getRandomValues(new Uint8Array(16)), (value) =>
        value.toString(16).padStart(2, '0')
      ).join('');
    try {
      const result = await api.upload(
        file,
        { format, language, ...(engine ? { engine } : {}) },
        retryKey,
        (percent) => {
          uploadPercent = percent;
        }
      );
      if (destroyed) return;
      job = result;
      remember(result.id);
      await refresh(result.id);
    } catch (caught) {
      error = `${errorMessage(caught)} Retry with the same file and options to resume safely.`;
    } finally {
      uploading = false;
    }
  }

  async function cancel() {
    if (!job) return;
    error = '';
    try {
      job = await api.cancel(job.id);
      await refresh(job.id);
    } catch (caught) {
      error = errorMessage(caught);
    }
  }

  async function discard() {
    if (!job) return;
    deleting = true;
    error = '';
    try {
      await api.delete(job.id);
      if (timer) clearTimeout(timer);
      resultGeneration += 1;
      job = null;
      preview = '';
      retryKey = '';
      remember(null);
      notice = 'Temporary transcription deleted.';
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      deleting = false;
    }
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(preview);
      notice = truncated ? 'Preview copied.' : 'Transcript copied.';
    } catch {
      error =
        'Clipboard access is unavailable. Select the transcript below to copy it.';
    }
  }

  onMount(() => {
    void sessionApi
      .defaults('stt')
      .then((payload) => {
        configuredEngine = String(payload.effective?.stt_engine || '');
      })
      .catch(() => {});
    void diagnosticsApi
      .capabilities()
      .then((value) => {
        capabilities = value;
      })
      .catch(() => {});
    void speechRecognitionApi
      .catalogue()
      .then((catalogue) => {
        services = catalogue.services;
      })
      .catch(() => {});
    const id = page.url.searchParams.get('id');
    if (id) {
      void api
        .get(id, format)
        .then((result) => {
          if (destroyed) return;
          job = result;
          void refresh(id);
        })
        .catch((caught) => {
          error = errorMessage(caught);
        });
    }
  });

  onDestroy(() => {
    destroyed = true;
    resultGeneration += 1;
    if (timer) clearTimeout(timer);
    if (previewUrl) URL.revokeObjectURL(previewUrl);
  });
</script>

<svelte:head><title>Quick Transcribe · Pandrator</title></svelte:head>

<div class="mx-auto max-w-5xl space-y-7">
  <header>
    <div class="eyebrow">Quick tools</div>
    <h1 class="mt-2 text-4xl font-semibold tracking-[-.04em]">
      Quick Transcribe
    </h1>
    <p class="muted mt-3 max-w-2xl">
      Turn a file or a microphone recording into text or subtitles. No session
      to create, no library to tidy up.
    </p>
  </header>
  {#if error}<div
      role="alert"
      class="rounded-xl border border-[var(--danger)] p-4 text-sm"
    >
      {error}
      {#if job}<button
          class="ml-3 underline"
          onclick={() => job && refresh(job.id)}>Check again</button
        >{/if}
    </div>{/if}
  {#if notice}<p role="status" class="text-sm text-[var(--muted)]">
      {notice}
    </p>{/if}
  <section
    class="surface rounded-2xl p-6 sm:p-8"
    aria-label="Transcription source"
  >
    <div class="mb-6 flex gap-2" role="group" aria-label="Audio source">
      <button
        class="source-mode"
        class:selected={mode === 'upload'}
        aria-pressed={mode === 'upload'}
        disabled={busy || recording}
        onclick={() => (mode = 'upload')}>Upload a file</button
      >
      <button
        class="source-mode"
        class:selected={mode === 'record'}
        aria-pressed={mode === 'record'}
        disabled={busy || recording}
        onclick={() => (mode = 'record')}>Record audio</button
      >
    </div>
    {#if mode === 'upload'}
      <label
        class="block rounded-xl border border-dashed border-[var(--line)] bg-[var(--surface-soft)] p-6"
      >
        <span class="mb-3 flex items-center gap-3 font-medium"
          ><FileAudio size={22} /> Audio or video file</span
        >
        <input
          type="file"
          aria-label="Audio or video file"
          accept="audio/*,video/*,.mkv,.opus"
          disabled={busy}
          onchange={(event) => {
            const selected = event.currentTarget.files?.[0];
            if (selected) choose(selected);
          }}
        />
        <span class="muted mt-3 block text-xs"
          >Up to 256 MiB and 2 hours. Video is transcribed from its audio track.</span
        >
      </label>
    {:else}
      <QuickRecorder
        disabled={busy}
        onrecord={choose}
        onbusy={(value) => (recording = value)}
      />
    {/if}
    {#if file}
      <div class="mt-5 space-y-3">
        <p class="break-words text-sm font-medium">
          {file.name}
          <span class="muted font-normal"
            >· {(file.size / 1024 / 1024).toFixed(1)} MiB</span
          >
        </p>
        <audio
          class="w-full"
          controls
          src={previewUrl}
          aria-label="Preview source recording"
        ></audio>
      </div>
    {/if}
    <div class="mt-6 grid gap-4 sm:grid-cols-3">
      <label class="text-sm"
        >Output format
        <select
          class="mt-2 w-full"
          bind:value={format}
          disabled={uploading || loadingResult}
          onchange={(event) => {
            format = event.currentTarget.value as TranscriptFormat;
            if (job?.result_available) void loadResult();
            else retryKey = '';
          }}
        >
          <option value="txt">Plain text (.txt)</option><option value="srt"
            >Subtitles (.srt)</option
          ><option value="json">Structured transcript (.json)</option>
        </select>
      </label>
      <label class="text-sm"
        >Language
        <input
          class="mt-2 w-full"
          bind:value={language}
          disabled={busy}
          placeholder="auto, en, pl…"
          oninput={() => (retryKey = '')}
        />
        <span class="muted mt-1 block text-xs"
          >Use auto to detect the language.</span
        >
      </label>
      <label class="text-sm"
        >Transcription service
        <select
          class="mt-2 w-full"
          bind:value={engine}
          disabled={busy}
          onchange={() => (retryKey = '')}
        >
          <option
            value=""
            disabled={Boolean(
              sttLanguageProblem(capabilities, configuredEngine, language)
            )}
            >Configured default{configuredEngine ||
            capabilities.stt?.default_engine
              ? ` (${configuredEngine || capabilities.stt?.default_engine})`
              : ''}</option
          >
          {#each [['parakeet', 'Parakeet 0.6B v3'], ['whisper', 'Whisper large-v3'], ['moss', 'MOSS Diarize 0.9B']] as [id, label]}
            <option
              value={id}
              disabled={Boolean(sttLanguageProblem(capabilities, id, language))}
              >{label}{sttLanguageProblem(capabilities, id, language)
                ? ' · unsupported language'
                : ''}</option
            >
          {/each}
          {#each services as service}<option
              value={service.id}
              disabled={Boolean(
                sttLanguageProblem(capabilities, service.id, language)
              )}>{service.name || service.id}</option
            >{/each}
        </select>
      </label>
    </div>
    {#if languageProblem}<p class="mt-3 text-sm text-red-600" role="alert">
        {languageProblem}
      </p>{/if}
    <div class="mt-6 flex flex-wrap items-center gap-4">
      <button
        class="action inline-flex items-center gap-2"
        disabled={!file || busy || recording || Boolean(languageProblem)}
        onclick={transcribe}
      >
        {#if uploading}<LoaderCircle size={17} class="animate-spin" /> Uploading…
          {Math.round(uploadPercent)}%{:else}Transcribe{/if}
      </button>
      <span class="muted max-w-lg text-xs"
        >Uses your configured local or cloud STT service. Results expire one
        hour after completion; working audio is removed after processing.</span
      >
    </div>
  </section>
  {#if job}
    <section
      class="surface rounded-2xl p-6 sm:p-8"
      aria-label="Transcription result"
    >
      <div class="flex flex-wrap items-center justify-between gap-4">
        <h2 class="text-xl font-semibold">
          {job.result_available ? 'Your transcript' : 'Transcription'}
        </h2>
        <div class="flex items-center gap-3">
          {#if running && job.status !== 'cancel_requested'}<button
              class="text-sm underline"
              onclick={cancel}>Cancel transcription</button
            >{/if}
          <button
            class="inline-flex items-center gap-2 text-sm"
            disabled={deleting}
            onclick={discard}><Trash2 size={15} /> Delete now</button
          >
        </div>
      </div>
      {#if running}
        <div class="mt-5" role="status">
          <p class="mb-3 text-sm">
            {job.status === 'queued'
              ? 'Waiting for an available transcription worker…'
              : job.status === 'cancel_requested'
                ? 'Canceling transcription…'
                : job.progress_detail || 'Transcribing…'}
          </p>
          <progress
            class="w-full"
            max="100"
            value={progressPercent}
            aria-label="Transcription progress">{progressPercent}%</progress
          >
          <p class="muted mt-2 text-xs">
            You can leave this page and return using this address.
          </p>
        </div>
      {:else if job.result_available}
        <div class="mt-5 flex flex-wrap items-center gap-4">
          <button
            class="inline-flex items-center gap-2 text-sm"
            onclick={copy}
            disabled={loadingResult}
            ><Copy size={16} />
            {truncated ? 'Copy preview' : 'Copy transcript'}</button
          >
          <a
            class="inline-flex items-center gap-2 text-sm"
            href={api.download(job.id, format)}
            download><Download size={16} /> Download .{format}</a
          >
          <span class="muted text-xs"
            >Available until {new Date(
              job.expires_at
            ).toLocaleTimeString()}</span
          >
        </div>
        <label class="mt-5 block text-sm"
          >{truncated
            ? 'Transcript preview (download for the complete result)'
            : 'Transcript'}
          <textarea
            class="mt-2 min-h-72 w-full font-mono text-sm leading-relaxed"
            readonly
            value={loadingResult ? 'Loading transcript…' : preview}></textarea>
        </label>
      {:else}<p class="muted mt-4 text-sm" role="status">
          {job.status === 'canceled'
            ? 'Transcription canceled. You can transcribe the selected file again.'
            : job.error?.message || `Transcription ${job.status}.`}
        </p>{/if}
    </section>
  {/if}
</div>

<style>
  .source-mode {
    padding: 0.65rem 1rem;
    border-radius: 0.7rem;
    font-size: 0.875rem;
    border: 1px solid var(--line);
  }
  .source-mode.selected {
    background: var(--accent-soft);
    border-color: var(--accent);
    color: var(--accent);
  }
  .action {
    background: var(--accent);
    color: white;
    border-radius: 0.75rem;
    padding: 0.8rem 1.5rem;
    font-weight: 600;
  }
  button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  input[type='file'] {
    width: 100%;
    font-size: 0.875rem;
  }
  progress {
    accent-color: var(--accent);
  }
</style>
