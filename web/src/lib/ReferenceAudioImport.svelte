<script lang="ts">
  import { X } from '@lucide/svelte';
  import { onDestroy, untrack } from 'svelte';
  import { ApiMutationAttempt } from './api';
  import { jobApi } from './domain-api';
  import { errorMessage } from './errors';
  import { modalFocus } from './modal-focus';
  import AudioPlayer from './AudioPlayer.svelte';
  import {
    importReferenceAudio,
    prepareReference,
    referenceCrop,
    type ReferenceSample
  } from './voice-reference-api';
  let {
    voiceId,
    revision,
    language,
    samples,
    onclose,
    onchanged
  }: {
    voiceId: string;
    revision: number;
    language: string;
    samples: ReferenceSample[];
    onclose: () => void;
    onchanged: () => Promise<void>;
  } = $props();
  let source = $state(
    untrack(
      () =>
        samples.find((sample) => sample.available !== false)?.artifact_id ?? ''
    )
  );
  let file = $state<File | null>(null),
    importedFile: File | null = null;
  let importedId = '';
  let start = $state(0),
    end = $state(''),
    transcript = $state(''),
    reviewed = $state(false);
  let busy = $state(false),
    error = $state(''),
    jobId = $state(''),
    status = $state('');
  let preview = $state(false),
    alive = true;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const uploadAttempt = new ApiMutationAttempt(),
    prepareAttempt = new ApiMutationAttempt();
  async function poll(id: string) {
    try {
      const job = await jobApi.get(id);
      if (!alive || jobId !== id) return;
      status = job.progress_detail || job.status;
      if (
        ['queued', 'running', 'retrying', 'cancel_requested'].includes(
          job.status
        )
      ) {
        timer = setTimeout(() => void poll(id), 2000);
        return;
      }
      busy = false;
      if (job.status === 'succeeded') {
        await onchanged();
        if (alive) onclose();
      } else
        error = job.error_message || `Reference preparation ${job.status}.`;
    } catch (failure) {
      if (alive) {
        busy = false;
        error = errorMessage(failure);
      }
    }
  }
  async function save() {
    if (busy) return;
    busy = true;
    error = '';
    try {
      const crop = referenceCrop(start, end);
      if (reviewed && !transcript.trim())
        throw new Error('Enter the reviewed words first.');
      let artifactId = source;
      if (!source) {
        if (!file)
          throw new Error('Choose an audio file or an existing sample.');
        if (importedFile !== file) {
          const receipt = await importReferenceAudio(file, uploadAttempt);
          importedFile = file;
          importedId = receipt.artifact_id;
        }
        artifactId = importedId;
      }
      const job = await prepareReference(
        voiceId,
        {
          artifact_id: artifactId,
          expected_voice_revision: revision,
          language,
          ...(transcript.trim() ? { transcript: transcript.trim() } : {}),
          transcript_reviewed: reviewed,
          ...crop
        },
        prepareAttempt
      );
      if (!alive) return;
      jobId = job.id;
      status = job.status;
      void poll(jobId);
    } catch (failure) {
      if (alive) {
        error = errorMessage(failure);
        busy = false;
      }
    }
  }
  async function cancel() {
    if (!jobId) return;
    try {
      await jobApi.cancel(jobId);
    } catch (failure) {
      if (alive) error = errorMessage(failure);
    }
  }
  onDestroy(() => {
    alive = false;
    clearTimeout(timer);
  });
</script>

<div
  class="fixed inset-0 z-[95] grid place-items-center bg-black/45 p-3"
  role="presentation"
>
  <div
    use:modalFocus={{ onclose }}
    role="dialog"
    aria-modal="true"
    aria-labelledby="reference-import-title"
    tabindex="-1"
    class="max-h-[90vh] w-full max-w-lg overflow-auto rounded-2xl border border-[var(--line)] bg-[var(--paper)] p-5 shadow-xl"
  >
    <header class="flex items-center justify-between gap-3">
      <h3 id="reference-import-title" class="text-lg font-semibold">
        Trim and import a voice sample
      </h3>
      <button
        class="action"
        aria-label="Close reference import"
        onclick={onclose}><X size={16} /></button
      >
    </header>
    <p class="muted my-3 text-sm">
      Keep a clean excerpt of speech. The original remains available; the new
      sample gets its own audio and transcript.
    </p>
    <fieldset disabled={busy} class="space-y-3">
      <label class="block text-sm"
        >Source
        <select
          class="input mt-1 w-full"
          bind:value={source}
          onchange={() => (preview = false)}
        >
          <option value="">Import an audio file</option>
          {#each samples.filter((sample) => sample.available !== false) as sample, i}
            <option value={sample.artifact_id}>Existing sample {i + 1}</option>
          {/each}
        </select>
      </label>
      {#if !source}
        <label class="block text-sm"
          >Audio file<input
            class="mt-1 block w-full text-sm"
            type="file"
            accept="audio/*"
            onchange={(event) => {
              file = event.currentTarget.files?.[0] ?? null;
            }}
          /></label
        >
        <p class="muted text-xs">
          Up to 50 MiB and 300 seconds. Audio is inspected before import.
        </p>
      {:else if preview}
        <AudioPlayer
          src={`/api/v1/artifacts/${encodeURIComponent(source)}/content`}
          label="Original voice sample"
          preload="none"
          compact
        />
      {:else}
        <button class="action" onclick={() => (preview = true)}
          >Preview original sample</button
        >
      {/if}
      <div class="grid grid-cols-2 gap-3">
        <label class="block text-sm"
          >Start (seconds)<input
            class="input mt-1 w-full"
            type="number"
            min="0"
            step="0.01"
            bind:value={start}
          /></label
        >
        <label class="block text-sm"
          >End (seconds)<input
            class="input mt-1 w-full"
            inputmode="decimal"
            bind:value={end}
            placeholder="End of recording"
          /></label
        >
      </div>
      <p class="muted text-xs">
        Some models, including VoxCPM, can reject a long reference. Choose a
        shorter excerpt if it exceeds the model's capacity.
      </p>
      <label class="block text-sm"
        >Words in the excerpt (optional)<textarea
          class="input mt-1 w-full"
          rows="3"
          maxlength="4000"
          bind:value={transcript}
          placeholder="Only the speech retained in this sample"
        ></textarea></label
      >
      <label class="flex items-center gap-2 text-sm"
        ><input type="checkbox" bind:checked={reviewed} /> I have checked these words
        against the excerpt</label
      >
      <p class="muted text-xs">
        You can transcribe the new sample afterwards. The full recording's
        transcript is never assumed to match a crop.
      </p>
    </fieldset>
    {#if status}<p class="muted mt-3 text-sm" role="status">
        {status}{busy ? ' · You may close this window; the job continues.' : ''}
      </p>{/if}
    {#if error}<p class="mt-3 text-sm text-red-700" role="alert">
        {error}
      </p>{/if}
    <div class="mt-4 flex flex-wrap gap-2">
      <button class="action primary" disabled={busy} onclick={() => void save()}
        >{busy ? 'Preparing…' : 'Create sample'}</button
      >
      {#if busy && jobId}<button class="action" onclick={() => void cancel()}
          >Cancel preparation</button
        >{/if}
      {#if jobId && !busy}<button
          class="action"
          onclick={() => {
            busy = true;
            void poll(jobId);
          }}>Check job</button
        >{/if}
    </div>
  </div>
</div>
