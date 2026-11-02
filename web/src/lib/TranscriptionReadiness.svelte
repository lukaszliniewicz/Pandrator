<script lang="ts">
  import { errorMessage } from './errors';
  import {
    checkTranscriptionFiles,
    type TranscriptionReadiness
  } from './transcription-readiness-api';

  let {
    sessionId,
    settings,
    unsavedLocalFile = false
  }: {
    sessionId: string;
    settings: Record<string, unknown>;
    unsavedLocalFile?: boolean;
  } = $props();

  let report = $state<TranscriptionReadiness | null>(null);
  let checking = $state(false);
  let error = $state('');
  let refresh = $state(0);
  const selection = $derived(JSON.stringify(settings));

  $effect(() => {
    const selected = JSON.parse(selection) as Record<string, unknown>;
    const id = sessionId;
    const savedFirst = unsavedLocalFile;
    void refresh;
    const controller = new AbortController();
    report = null;
    error = '';
    checking = !savedFirst;
    const timeout = window.setTimeout(async () => {
      if (savedFirst) return;
      try {
        const result = await checkTranscriptionFiles(
          id,
          selected,
          controller.signal
        );
        if (!controller.signal.aborted) report = result;
      } catch (caught) {
        if (!controller.signal.aborted) error = errorMessage(caught);
      } finally {
        if (!controller.signal.aborted) checking = false;
      }
    }, 250);
    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  });

  const assetLabel = (kind: string) =>
    ({
      model: 'Recognition model',
      asr: 'Recognition model',
      vad: 'Speech detector',
      aligner: 'Word aligner',
      language_detector: 'Language detector',
      lid: 'Language detector'
    })[kind] || kind.replaceAll('_', ' ');
</script>

<section
  class="rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] p-4"
  aria-label="Transcription model files"
  aria-busy={checking}
>
  <div class="flex flex-wrap items-center justify-between gap-2">
    <h3 class="text-sm font-semibold">Local model files</h3>
    <button
      type="button"
      class="text-xs font-semibold underline underline-offset-2 disabled:opacity-50"
      disabled={checking || unsavedLocalFile}
      onclick={() => refresh++}>Check again</button
    >
  </div>
  <div role="status" aria-live="polite" class="mt-2 text-xs leading-relaxed">
    {#if unsavedLocalFile}
      Save this existing-file selection, then reopen these settings to check it.
    {:else if checking}
      Checking the selected model and its companion files…
    {:else if error}
      Could not check model files. {error}
    {:else if report?.status === 'not_required'}
      This service does not use local recognition models.
    {:else if report}
      {#if report.status === 'unresolved'}
        <p class="mb-2">
          {report.engine === 'auto'
            ? 'Automatic selection depends on the source language and available runtimes. Choose an explicit model to check its files now.'
            : 'Some companion files cannot yet be determined. Review the source language, model and alignment selections.'}
        </p>
      {/if}
      <ul class="space-y-1">
        {#each report.assets as asset}
          <li class="flex flex-wrap justify-between gap-x-3 gap-y-1">
            <span>{assetLabel(asset.kind)}</span>
            <strong class={asset.status === 'missing' ? 'text-amber-700' : ''}
              >{asset.status === 'cached'
                ? 'File present'
                : asset.status === 'not_required'
                  ? 'Not required'
                  : asset.status === 'missing'
                    ? 'Missing or empty'
                    : 'Check required'}</strong
            >
          </li>
        {/each}
      </ul>
      {#if report.status === 'missing'}
        <p class="mt-2">
          An Administrator can acquire missing models or select an existing
          speech-detector file below. An installed engine alone does not include
          these files.
        </p>
      {:else}
        <p class="muted mt-2">
          File presence is checked here. The runtime checks compatibility and
          model integrity when transcription starts.
        </p>
      {/if}
    {/if}
  </div>
</section>
