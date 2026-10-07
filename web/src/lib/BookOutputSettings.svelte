<script lang="ts">
  import { LoaderCircle, Play, Square } from '@lucide/svelte';
  import { onDestroy } from 'svelte';
  import ArtifactPreview from './ArtifactPreview.svelte';
  import { artifactApi, jobApi, sessionApi } from './domain-api';
  import { errorMessage } from './errors';
  import type { ArtifactRecord } from './api-models';

  let {
    sessionId,
    generationRunId,
    settings,
    onChange
  }: {
    sessionId: string;
    generationRunId: string;
    settings: Record<string, unknown>;
    onChange: (key: string, value: unknown) => void;
  } = $props();

  const value = (key: string, fallback: unknown) => settings[key] ?? fallback;
  const mode = $derived(
    value('export_mode', 'media') === 'audio'
      ? 'media'
      : String(value('export_mode', 'media'))
  );
  const video = $derived(mode === 'video_book');
  const timed = $derived(video || mode === 'subtitles');
  const segments = $derived(value('book_cue_mode', 'passages') === 'segments');
  const reading = $derived(value('book_style', 'reading') === 'reading');
  let startSeconds = $state(0);
  let busy = $state(false);
  let jobId = $state('');
  let detail = $state('');
  let error = $state('');
  let warning = $state('');
  let preview = $state<ArtifactRecord | null>(null);
  let previewSignature = $state('');
  const controlsSignature = $derived(
    JSON.stringify([settings, startSeconds, generationRunId])
  );
  let disposed = false;
  let attempt = 0;
  let controller: AbortController | undefined;

  function wait(signal: AbortSignal) {
    return new Promise<void>((resolve, reject) => {
      const abort = () => {
        clearTimeout(timer);
        reject(new DOMException('Aborted', 'AbortError'));
      };
      const timer = setTimeout(() => {
        signal.removeEventListener('abort', abort);
        resolve();
      }, 1500);
      signal.addEventListener('abort', abort, { once: true });
      if (signal.aborted) abort();
    });
  }

  async function renderPreview() {
    if (!generationRunId || busy) return;
    const current = ++attempt;
    const signature = controlsSignature;
    controller?.abort();
    const poll = new AbortController();
    controller = poll;
    busy = true;
    error = '';
    warning = '';
    detail = 'Preparing the selected audio version…';
    try {
      let job = await sessionApi.runStage(sessionId, 'export', {
        generation_run_id: generationRunId,
        output: { ...settings, export_mode: 'video_book' },
        book_preview: true,
        book_preview_start_seconds: Number(startSeconds) || 0,
        book_preview_duration_seconds: 25
      });
      if (disposed || current !== attempt) {
        await jobApi.cancel(job.id).catch(() => null);
        return;
      }
      jobId = job.id;
      while (job.status !== 'succeeded') {
        if (['failed', 'canceled', 'abandoned'].includes(job.status))
          throw new Error(
            job.error_message || 'The video preview could not be rendered.'
          );
        detail = job.progress_detail || 'Preparing the video preview…';
        await wait(poll.signal);
        job = await jobApi.get(job.id, poll.signal);
      }
      const ids = Array.isArray(job.result_json?.artifact_ids)
        ? job.result_json.artifact_ids.map(String)
        : [];
      const contexts = await Promise.all(
        ids.map((id) => artifactApi.context(id))
      );
      const artifact = contexts
        .map((context) => context.artifact)
        .find(
          (item) =>
            item &&
            ids.includes(item.id) &&
            String(item.mime_type ?? '').startsWith('video/')
        );
      if (!artifact)
        throw new Error('The completed preview did not return a video.');
      if (disposed || current !== attempt) return;
      preview = artifact;
      previewSignature = signature;
      const diagnostics = job.result_json?.book_timing_diagnostics;
      if (diagnostics && typeof diagnostics === 'object') {
        const warnings = (diagnostics as Record<string, unknown>).warnings;
        if (Array.isArray(warnings) && warnings.length)
          warning = warnings
            .map((item) =>
              typeof item === 'string'
                ? item
                : 'Some passages use the spoken wording because the original text could not be mapped reliably.'
            )
            .join(' ');
      }
      detail = 'Preview ready. It uses these controls without saving them.';
      jobId = '';
    } catch (caught) {
      if (current === attempt && !poll.signal.aborted)
        error = errorMessage(caught);
    } finally {
      if (current === attempt) busy = false;
      if (controller === poll) controller = undefined;
    }
  }

  async function stopPreview() {
    attempt += 1;
    controller?.abort();
    controller = undefined;
    busy = false;
    const id = jobId;
    jobId = '';
    detail = 'Preview stopped.';
    if (id) {
      try {
        await jobApi.cancel(id);
      } catch (caught) {
        error = errorMessage(caught);
      }
    }
  }

  onDestroy(() => {
    disposed = true;
    attempt += 1;
    controller?.abort();
    if (jobId) void jobApi.cancel(jobId).catch(() => null);
  });
</script>

<div class="book-output mt-5 space-y-4">
  <label class="block max-w-sm text-sm font-medium"
    >Export target
    <select
      class="field mt-1"
      value={mode}
      onchange={(event) => onChange('export_mode', event.currentTarget.value)}
    >
      <option value="media">Narration audio</option>
      <option value="video_book">Video book · audio and reading text</option>
      <option value="subtitles">Timed subtitles · SRT or WebVTT</option>
    </select>
  </label>
  {#if timed}
    <div
      class="rounded-2xl border border-[var(--line)] bg-[var(--paper-strong)] p-4 sm:p-5"
    >
      <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <label
          >Presentation<select
            class="field"
            value={String(value('book_style', 'reading'))}
            onchange={(event) =>
              onChange('book_style', event.currentTarget.value)}
          >
            <option value="reading">Reading passages</option><option
              value="captions">Plain captions</option
            >
          </select></label
        >
        <label
          >Text changes<select
            class="field"
            value={String(value('book_cue_mode', 'passages'))}
            onchange={(event) =>
              onChange('book_cue_mode', event.currentTarget.value)}
          >
            <option value="passages">Fitted passages · word timing</option
            ><option value="segments">Whole segments · faster</option>
          </select></label
        >
        <label
          >Displayed wording<select
            class="field"
            value={String(value('book_text_mode', 'auto'))}
            onchange={(event) =>
              onChange('book_text_mode', event.currentTarget.value)}
          >
            <option value="auto">Original when timing can be mapped</option
            ><option value="original">Require original wording</option><option
              value="spoken">Use spoken wording</option
            >
          </select></label
        >
        {#if !segments && reading}
          <label
            >Passage target (seconds)<input
              class="field"
              type="number"
              min="2"
              max="30"
              value={Number(value('book_target_seconds', 12))}
              oninput={(event) =>
                onChange(
                  'book_target_seconds',
                  Number(event.currentTarget.value)
                )}
            /></label
          >
        {/if}
        <label
          >Subtitle file<select
            class="field"
            value={String(value('subtitle_format', 'srt'))}
            onchange={(event) =>
              onChange('subtitle_format', event.currentTarget.value)}
            ><option value="srt">SRT</option><option value="vtt">WebVTT</option
            ></select
          ></label
        >
        {#if video}
          <label
            >Text size<input
              class="field"
              type="number"
              min="28"
              max="96"
              value={Number(value('book_font_size', 64))}
              oninput={(event) =>
                onChange('book_font_size', Number(event.currentTarget.value))}
            /></label
          >
          <label
            >Background<input
              class="field"
              type="color"
              value={String(value('book_background', '#202427'))}
              oninput={(event) =>
                onChange('book_background', event.currentTarget.value)}
            /></label
          >
          <label
            >Text colour<input
              class="field"
              type="color"
              value={String(value('book_foreground', '#f0eade'))}
              oninput={(event) =>
                onChange('book_foreground', event.currentTarget.value)}
            /></label
          >
          <label
            >Alignment<select
              class="field"
              value={String(value('book_alignment', 'center'))}
              onchange={(event) =>
                onChange('book_alignment', event.currentTarget.value)}
              ><option value="center">Centred</option><option value="left"
                >Left aligned</option
              ></select
            ></label
          >
        {/if}
      </div>
      <p class="muted mt-3 text-sm leading-relaxed">
        {segments
          ? 'Each complete segment uses its saved audio boundaries. Subtitle-only export skips alignment; a video still needs the text to fit.'
          : reading
            ? 'Passages follow sentence and paragraph boundaries and hold through pauses. Changing presentation reuses saved timing.'
            : 'Captions use the project’s language-aware subtitle limits and saved word timing.'}
        {#if video}The MP4 includes burned-in text; the subtitle file is
          retained separately.{/if}
      </p>
      {#if !segments}
        <p class="muted mt-2 text-sm">
          Original names can retain their spelling even when speech uses a
          phonetic version. Other wording changes may need review; any fallback
          is reported with the export.
        </p>
      {/if}
      <details class="mt-4 border-t border-[var(--line)] pt-3">
        <summary class="cursor-pointer text-sm font-semibold"
          >More controls</summary
        >
        <div class="mt-3 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {#if !segments && reading}<label
              >Passage maximum (seconds)<input
                class="field"
                type="number"
                min="3"
                max="60"
                value={Number(value('book_max_seconds', 20))}
                oninput={(event) =>
                  onChange(
                    'book_max_seconds',
                    Number(event.currentTarget.value)
                  )}
              /></label
            >{/if}
          {#if reading && (!segments || video)}<label
              >Maximum lines<input
                class="field"
                type="number"
                min="1"
                max="8"
                value={Number(value('book_max_lines', 4))}
                oninput={(event) =>
                  onChange('book_max_lines', Number(event.currentTarget.value))}
              /></label
            >{/if}
          {#if video}
            <label
              >Resolution<select
                class="field"
                value={String(value('book_resolution', '1080p'))}
                onchange={(event) =>
                  onChange('book_resolution', event.currentTarget.value)}
                ><option value="1080p">1080p</option><option value="720p"
                  >720p</option
                ></select
              ></label
            >
            <label
              >Font file (optional)<input
                class="field"
                value={String(value('book_font_path', ''))}
                placeholder="Use a font for the book language"
                oninput={(event) =>
                  onChange('book_font_path', event.currentTarget.value)}
              /></label
            >
            {#if reading}<label class="flex items-center gap-2 self-end pb-2"
                ><input
                  type="checkbox"
                  checked={Boolean(value('book_show_heading', true))}
                  onchange={(event) =>
                    onChange('book_show_heading', event.currentTarget.checked)}
                /> Show chapter or book heading</label
              >{/if}
          {/if}
          {#if !segments}
            <label
              >Aligner<select
                class="field"
                value={String(value('book_alignment_engine', 'auto'))}
                onchange={(event) =>
                  onChange('book_alignment_engine', event.currentTarget.value)}
                ><option value="auto">Automatic for the language</option><option
                  value="crispasr"
                  >CrispASR · supported European languages</option
                ><option value="qwen"
                  >Qwen · supported languages, including CJK</option
                ></select
              ></label
            >
            <label class="flex items-center gap-2 self-end pb-2"
              ><input
                type="checkbox"
                checked={Boolean(value('book_use_native_timings', true))}
                onchange={(event) =>
                  onChange(
                    'book_use_native_timings',
                    event.currentTarget.checked
                  )}
              /> Use native timing when available</label
            >
          {/if}
        </div>
      </details>
      {#if video}
        <div
          class="mt-4 flex flex-wrap items-end gap-3 border-t border-[var(--line)] pt-4"
        >
          <label class="w-40"
            >Preview from (seconds)<input
              class="field"
              type="number"
              min="0"
              step="0.1"
              bind:value={startSeconds}
            /></label
          >
          <button
            class="tool"
            disabled={!generationRunId || busy || Boolean(jobId)}
            onclick={renderPreview}
            >{#if busy}<LoaderCircle
                size={15}
                class="animate-spin"
              />{:else}<Play size={15} />{/if}Preview 25 seconds</button
          >
          {#if busy || jobId}<button class="tool" onclick={stopPreview}
              ><Square size={14} /> Stop preview</button
            >{/if}
          {#if !generationRunId}<span class="muted text-sm"
              >Select a completed audio version to preview.</span
            >{/if}
        </div>
        <p class="muted mt-2 text-xs">
          The preview uses the full export renderer and these unsaved controls.
        </p>
      {/if}
      {#if video && detail}<p class="muted mt-3 text-sm" role="status">
          {detail}
        </p>{/if}
      {#if video && !busy && previewSignature && previewSignature !== controlsSignature}<p
          class="muted mt-2 text-sm"
        >
          The controls have changed since the last preview. Render again to see
          these changes.
        </p>{/if}
      {#if video && warning}<p
          class="mt-3 text-sm text-[var(--warning)]"
          role="status"
        >
          {warning}
        </p>{/if}
      {#if error}<p class="mt-3 text-sm text-[var(--danger)]" role="alert">
          {error}
        </p>{/if}
    </div>
  {/if}
</div>

{#if preview}<ArtifactPreview
    artifact={preview}
    onclose={() => (preview = null)}
  />{/if}

<style>
  .book-output label {
    font-size: 0.875rem;
    font-weight: 500;
  }
  .book-output .field {
    display: block;
    width: 100%;
    margin-top: 0.3rem;
  }
  .book-output input[type='color'] {
    min-height: 2.6rem;
    padding: 0.25rem;
  }
</style>
