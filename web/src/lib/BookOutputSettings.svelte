<script lang="ts">
  import {
    AudioLines,
    Captions,
    LoaderCircle,
    Play,
    Square,
    Video
  } from '@lucide/svelte';
  import { onDestroy } from 'svelte';
  import ArtifactPreview from './ArtifactPreview.svelte';
  import ParameterLabel from './ParameterLabel.svelte';
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
  const componentId = $props.id();
  const fieldId = (key: string) => `${componentId}-${key}`;
  const outputChoices = [
    {
      value: 'media',
      label: 'Narration audio',
      detail: 'An audiobook or audio file',
      icon: AudioLines
    },
    {
      value: 'video_book',
      label: 'Video book',
      detail: 'Narration with text on screen',
      icon: Video
    },
    {
      value: 'subtitles',
      label: 'Timed subtitles',
      detail: 'An SRT or WebVTT text file',
      icon: Captions
    }
  ];
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

{#snippet fieldLabel(name: string, label: string, description?: string)}
  <ParameterLabel
    section="output"
    {name}
    {label}
    {description}
    controlId={fieldId(name)}
  />
{/snippet}

<div class="book-output">
  <fieldset class="output-targets">
    <legend>Export target</legend>
    <div class="output-choices">
      {#each outputChoices as choice}
        <label class="output-choice selection-tile">
          <input
            type="radio"
            name={`${componentId}-export-mode`}
            value={choice.value}
            checked={mode === choice.value}
            aria-label={choice.label}
            aria-describedby={fieldId(`${choice.value}-description`)}
            onchange={() => onChange('export_mode', choice.value)}
          />
          <choice.icon size={20} aria-hidden="true" />
          <span class="choice-copy">
            <strong>{choice.label}</strong>
            <span id={fieldId(`${choice.value}-description`)}
              >{choice.detail}</span
            >
          </span>
        </label>
      {/each}
    </div>
  </fieldset>
  {#if timed}
    <div class="presentation-panel">
      <div class="book-fields">
        <div class="book-control">
          {@render fieldLabel('book_style', 'Presentation')}
          <select
            id={fieldId('book_style')}
            class="field"
            value={String(value('book_style', 'reading'))}
            onchange={(event) =>
              onChange('book_style', event.currentTarget.value)}
          >
            <option value="reading">Reading passages</option>
            <option value="captions">Plain captions</option>
          </select>
        </div>
        <div class="book-control">
          {@render fieldLabel('book_cue_mode', 'Text grouping')}
          <select
            id={fieldId('book_cue_mode')}
            class="field"
            value={String(value('book_cue_mode', 'passages'))}
            onchange={(event) =>
              onChange('book_cue_mode', event.currentTarget.value)}
          >
            <option value="passages">Fitted passages · word timing</option>
            <option value="segments">Whole segments · faster</option>
          </select>
        </div>
        <div class="book-control">
          {@render fieldLabel('book_text_mode', 'Displayed wording')}
          <select
            id={fieldId('book_text_mode')}
            class="field"
            value={String(value('book_text_mode', 'auto'))}
            onchange={(event) =>
              onChange('book_text_mode', event.currentTarget.value)}
          >
            <option value="auto">Original when timing can be mapped</option>
            <option value="original">Require original wording</option>
            <option value="spoken">Use spoken wording</option>
          </select>
        </div>
        {#if !segments && reading}
          <div class="book-control">
            {@render fieldLabel(
              'book_target_seconds',
              'Passage target (seconds)'
            )}
            <input
              id={fieldId('book_target_seconds')}
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
            />
          </div>
        {/if}
        <div class="book-control">
          {@render fieldLabel('subtitle_format', 'Subtitle file')}
          <select
            id={fieldId('subtitle_format')}
            class="field"
            value={String(value('subtitle_format', 'srt'))}
            onchange={(event) =>
              onChange('subtitle_format', event.currentTarget.value)}
          >
            <option value="srt">SRT</option><option value="vtt">WebVTT</option>
          </select>
        </div>
        {#if video}
          <div class="book-control">
            {@render fieldLabel('book_font_size', 'Text size')}
            <input
              id={fieldId('book_font_size')}
              class="field"
              type="number"
              min="28"
              max="96"
              value={Number(value('book_font_size', 80))}
              oninput={(event) =>
                onChange('book_font_size', Number(event.currentTarget.value))}
            />
          </div>
          {#if reading && Boolean(value('book_show_heading', true))}
            <div class="book-control">
              {@render fieldLabel('book_heading_font_size', 'Heading size')}
              <input
                id={fieldId('book_heading_font_size')}
                class="field"
                type="number"
                min="18"
                max="72"
                value={Number(value('book_heading_font_size', 44))}
                oninput={(event) =>
                  onChange(
                    'book_heading_font_size',
                    Number(event.currentTarget.value)
                  )}
              />
            </div>
          {/if}
          <div class="book-control">
            {@render fieldLabel('book_background', 'Background')}
            <input
              id={fieldId('book_background')}
              class="field"
              type="color"
              value={String(value('book_background', '#202427'))}
              oninput={(event) =>
                onChange('book_background', event.currentTarget.value)}
            />
          </div>
          <div class="book-control">
            {@render fieldLabel('book_foreground', 'Text colour')}
            <input
              id={fieldId('book_foreground')}
              class="field"
              type="color"
              value={String(value('book_foreground', '#f0eade'))}
              oninput={(event) =>
                onChange('book_foreground', event.currentTarget.value)}
            />
          </div>
          <div class="book-control">
            {@render fieldLabel('book_alignment', 'Alignment')}
            <select
              id={fieldId('book_alignment')}
              class="field"
              value={String(value('book_alignment', 'center'))}
              onchange={(event) =>
                onChange('book_alignment', event.currentTarget.value)}
            >
              <option value="center">Centred</option><option value="left"
                >Left aligned</option
              >
            </select>
          </div>
        {/if}
      </div>
      <p class="layout-note">
        {segments
          ? video
            ? 'Each complete segment stays on screen. Choose fitted passages if a segment is too long to fit.'
            : 'Each complete segment becomes one cue. This export skips word alignment.'
          : reading
            ? 'Passages follow sentence and paragraph boundaries and remain visible through pauses.'
            : 'Captions follow the project’s language-aware subtitle limits.'}
        {#if video}The MP4 includes narration and text; a matching subtitle file
          is also saved.{/if}
      </p>
      <details class="more-controls">
        <summary>More controls</summary>
        <div class="book-fields advanced-fields">
          {#if !segments && reading}
            <div class="book-control">
              {@render fieldLabel(
                'book_max_seconds',
                'Passage maximum (seconds)'
              )}
              <input
                id={fieldId('book_max_seconds')}
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
              />
            </div>
          {/if}
          {#if reading && (!segments || video)}
            <div class="book-control">
              {@render fieldLabel('book_max_lines', 'Maximum lines')}
              <input
                id={fieldId('book_max_lines')}
                class="field"
                type="number"
                min="1"
                max="8"
                value={Number(value('book_max_lines', 4))}
                oninput={(event) =>
                  onChange('book_max_lines', Number(event.currentTarget.value))}
              />
            </div>
          {/if}
          {#if video}
            <div class="book-control">
              {@render fieldLabel('book_resolution', 'Resolution')}
              <select
                id={fieldId('book_resolution')}
                class="field"
                value={String(value('book_resolution', '1080p'))}
                onchange={(event) =>
                  onChange('book_resolution', event.currentTarget.value)}
              >
                <option value="1080p">1080p</option><option value="720p"
                  >720p</option
                >
              </select>
            </div>
            <div class="book-control">
              {@render fieldLabel('book_font_path', 'Custom font (optional)')}
              <input
                id={fieldId('book_font_path')}
                class="field"
                value={String(value('book_font_path', ''))}
                placeholder="Font file path"
                oninput={(event) =>
                  onChange('book_font_path', event.currentTarget.value)}
              />
            </div>
            {#if reading}
              <div class="book-control book-toggle">
                <input
                  id={fieldId('book_show_heading')}
                  type="checkbox"
                  checked={Boolean(value('book_show_heading', true))}
                  onchange={(event) =>
                    onChange('book_show_heading', event.currentTarget.checked)}
                />
                {@render fieldLabel(
                  'book_show_heading',
                  'Show chapter or book heading'
                )}
              </div>
            {/if}
          {/if}
          {#if !segments}
            <div class="book-control">
              {@render fieldLabel('book_alignment_engine', 'Aligner')}
              <select
                id={fieldId('book_alignment_engine')}
                class="field"
                value={String(value('book_alignment_engine', 'auto'))}
                onchange={(event) =>
                  onChange('book_alignment_engine', event.currentTarget.value)}
              >
                <option value="auto">Automatic for the language</option>
                <option value="crispasr"
                  >CrispASR · supported European languages</option
                >
                <option value="qwen"
                  >Qwen · supported languages, including CJK</option
                >
              </select>
            </div>
            <div class="book-control book-toggle">
              <input
                id={fieldId('book_use_native_timings')}
                type="checkbox"
                checked={Boolean(value('book_use_native_timings', true))}
                onchange={(event) =>
                  onChange(
                    'book_use_native_timings',
                    event.currentTarget.checked
                  )}
              />
              {@render fieldLabel(
                'book_use_native_timings',
                'Use native timing when available'
              )}
            </div>
          {/if}
        </div>
      </details>
      {#if video}
        <div class="preview-controls">
          <div class="book-control preview-start">
            {@render fieldLabel(
              'book_preview_start_seconds',
              'Preview from (seconds)',
              'Choose where the 25-second preview begins in the selected audio version. The preview uses your current controls without saving the output profile.'
            )}
            <input
              id={fieldId('book_preview_start_seconds')}
              class="field"
              type="number"
              min="0"
              step="0.1"
              bind:value={startSeconds}
            />
          </div>
          <button
            type="button"
            class="btn btn-secondary"
            title="Render up to 25 seconds using these settings, without saving them."
            disabled={!generationRunId || busy || Boolean(jobId)}
            onclick={renderPreview}
          >
            {#if busy}<LoaderCircle
                size={16}
                class="animate-spin"
              />{:else}<Play size={16} />{/if}
            Preview 25 seconds
          </button>
          {#if busy || jobId}
            <button
              type="button"
              class="btn btn-secondary"
              onclick={stopPreview}><Square size={14} /> Stop preview</button
            >
          {/if}
        </div>
        <p class="preview-note">
          {generationRunId
            ? 'Try these settings before saving or exporting the full book.'
            : 'Select a completed audio version above to preview.'}
        </p>
      {/if}
      {#if video && detail}<p class="layout-note" role="status">
          {detail}
        </p>{/if}
      {#if video && !busy && previewSignature && previewSignature !== controlsSignature}
        <p class="layout-note">
          The controls have changed since the last preview. Render again to see
          these changes.
        </p>
      {/if}
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
  .book-output {
    margin-top: 1.5rem;
  }
  .output-targets {
    min-width: 0;
    border: 0;
    padding: 0;
  }
  .output-targets legend {
    margin-bottom: 0.65rem;
    padding: 0;
    font-size: 0.9375rem;
    font-weight: 650;
  }
  .output-choices {
    display: grid;
    gap: 0.65rem;
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
  .output-choice {
    display: flex;
    min-width: 0;
    align-items: center;
    gap: 0.65rem;
    border-radius: 0.85rem;
    padding: 0.95rem;
  }
  .output-choice :global(svg) {
    flex: none;
    color: var(--accent);
  }
  .choice-copy {
    display: grid;
    min-width: 0;
    gap: 0.25rem;
    line-height: 1.4;
  }
  .choice-copy strong {
    font-size: 0.9375rem;
    font-weight: 650;
  }
  .choice-copy > span {
    color: var(--muted);
    font-size: 0.8125rem;
    font-weight: 400;
  }
  .presentation-panel {
    margin-top: 1.25rem;
    border: 1px solid var(--line);
    border-radius: 1rem;
    background: var(--paper-strong);
    padding: 1.25rem;
  }
  .book-fields {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 1.15rem 1rem;
  }
  .book-control {
    min-width: 0;
  }
  .book-control :global(label) {
    font-size: 0.875rem;
    font-weight: 600;
    line-height: 1.4;
  }
  .book-control :global(.parameter-label-row) {
    min-height: 1.5rem;
  }
  .field {
    display: block;
    width: 100%;
    min-width: 0;
    min-height: 2.75rem;
    margin-top: 0.45rem;
    border: 1px solid var(--line);
    border-radius: 0.65rem;
    background-color: var(--paper);
    padding: 0.65rem 0.75rem;
    color: var(--ink);
    font-size: 0.9375rem;
    font-weight: 400;
    line-height: 1.4;
  }
  .field:hover {
    border-color: color-mix(in srgb, var(--accent) 60%, var(--line));
  }
  select.field {
    appearance: none;
    cursor: pointer;
    padding-right: 2rem;
    background-image:
      linear-gradient(45deg, transparent 50%, var(--muted) 50%),
      linear-gradient(135deg, var(--muted) 50%, transparent 50%);
    background-position:
      calc(100% - 1.05rem) 50%,
      calc(100% - 0.75rem) 50%;
    background-size: 0.3rem 0.3rem;
    background-repeat: no-repeat;
  }
  input[type='color'] {
    padding: 0.35rem;
    cursor: pointer;
  }
  .layout-note {
    margin-top: 1rem;
    color: var(--muted);
    font-size: 0.875rem;
    line-height: 1.65;
  }
  .more-controls {
    margin-top: 1rem;
    border-top: 1px solid var(--line);
    padding-top: 0.65rem;
  }
  .more-controls summary {
    width: fit-content;
    border-radius: 0.5rem;
    padding: 0.35rem 0.5rem;
    font-size: 0.875rem;
    font-weight: 600;
    cursor: pointer;
  }
  .more-controls summary:hover {
    background: var(--accent-soft);
  }
  .more-controls summary:focus-visible {
    outline: 3px solid color-mix(in srgb, var(--accent) 38%, transparent);
    outline-offset: 2px;
  }
  .advanced-fields {
    margin-top: 1rem;
  }
  .book-toggle {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .book-toggle > input {
    width: 1rem;
    height: 1rem;
    flex: none;
    accent-color: var(--accent);
  }
  .preview-controls {
    display: flex;
    flex-wrap: wrap;
    align-items: end;
    gap: 0.75rem;
    margin-top: 1.25rem;
    border-top: 1px solid var(--line);
    padding-top: 1rem;
  }
  .preview-start {
    width: 12rem;
  }
  .preview-controls .btn {
    min-height: 2.75rem;
  }
  .preview-note {
    margin-top: 0.65rem;
    color: var(--muted);
    font-size: 0.8125rem;
    line-height: 1.5;
  }
  @media (width < 70rem) {
    .book-fields {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
  }
  @media (width < 48rem) {
    .output-choices,
    .book-fields {
      grid-template-columns: minmax(0, 1fr);
    }
    .presentation-panel {
      padding: 1rem;
    }
  }
</style>
