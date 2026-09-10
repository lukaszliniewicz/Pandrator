<script lang="ts">
  import { apiJson, uploadManagedFile } from './api';
  import { sessionApi, sourceApi } from './domain-api';
  import { errorMessage } from './errors';
  import type { SourceAsset } from './api-models';

  type Status = {
    supported: boolean;
    session_revision: number;
    source_asset_id: string | null;
    filename: string;
    cue_count: number;
    adoption_required: boolean;
    media_filename: string | null;
    has_video: boolean;
    can_align: boolean;
    word_timing_artifact_id: string | null;
    alignment_note: string | null;
  };
  let {
    sessionId,
    refreshKey,
    onchanged
  }: {
    sessionId: string;
    refreshKey: string;
    onchanged: (message: string) => Promise<unknown>;
  } = $props();
  let status = $state<Status | null>(null);
  let busy = $state(false);
  let error = $state('');
  let message = $state('');
  let progress = $state(0);
  let library = $state<SourceAsset[]>([]);
  let libraryOpen = $state(false);
  let selectedMedia = $state('');
  let method = $state<'ctc' | 'ctc_asr_fallback'>('ctc');
  const base = $derived(
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/sources`
  );
  let requestNumber = 0;

  async function load() {
    const request = ++requestNumber;
    try {
      const result = await apiJson<Status>(`${base}/subtitle-status`);
      if (request === requestNumber) status = result;
    } catch (caught) {
      if (request === requestNumber) error = errorMessage(caught);
    }
  }
  $effect(() => {
    void refreshKey;
    void load();
  });

  async function changed(text: string) {
    message = text;
    await load();
    await onchanged(text);
  }
  async function attach(sourceId: string) {
    const session = await sessionApi.get(sessionId);
    await sessionApi.attachSource(
      sessionId,
      sourceId,
      session.revision,
      'media'
    );
    await changed(
      'Media target attached. Your subtitle source, translations and speech-plan history are unchanged.'
    );
  }
  async function upload(file: File | undefined) {
    if (!file) return;
    busy = true;
    error = '';
    progress = 0;
    try {
      const result = await uploadManagedFile(
        file,
        undefined,
        (value) => (progress = value)
      );
      if (!result.source_asset_id)
        throw new Error('The upload did not create a reusable media source.');
      await attach(String(result.source_asset_id));
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
  async function openLibrary() {
    error = '';
    try {
      library = (await sourceApi.list()).items.filter(
        (item) =>
          /^(audio|video)\//.test(item.mime_type ?? '') ||
          /\.(mp4|mkv|mov|webm|avi|m4v|wav|mp3|flac|m4a|aac|ogg|opus)$/i.test(
            `${item.display_name}.${item.kind}`
          )
      );
      selectedMedia = library[0]?.id ?? '';
      libraryOpen = true;
    } catch (caught) {
      error = errorMessage(caught);
    }
  }
  async function adopt() {
    if (!status?.source_asset_id) return;
    busy = true;
    error = '';
    try {
      await apiJson(`${base}/adopt-subtitles`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'If-Match': `"${status.session_revision}"`
        },
        body: JSON.stringify({ source_asset_id: status.source_asset_id })
      });
      await changed(
        'The existing subtitle source now has a timed revision; no second upload was needed.'
      );
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
  async function align() {
    if (!status?.can_align) return;
    busy = true;
    error = '';
    try {
      await apiJson(`${base}/align-subtitles`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          method,
          expected_revision: status.session_revision
        })
      });
      await changed(
        'Word alignment queued. Imported wording remains authoritative; progress and diagnostics are available in Activity.'
      );
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
</script>

{#if status?.supported}
  <section
    class="mb-5 rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] p-4"
    aria-label="Subtitle source and media target"
  >
    <h2 class="mb-2 font-semibold">Subtitle source &amp; associated media</h2>
    <p class="text-sm">
      Text: <strong>{status.filename}</strong> · {status.cue_count} imported cues
    </p>
    <p class="mt-1 text-sm">
      Media: <strong>{status.media_filename ?? 'Not attached'}</strong
      >{status.media_filename
        ? status.has_video
          ? ' · video export available'
          : ' · audio recording'
        : ''}
    </p>
    {#if status.alignment_note}<p class="muted mt-2 text-sm">
        {status.alignment_note}
      </p>{/if}
    <p class="muted mt-2 text-sm">
      Media is separate from the subtitle text. Attach or replace the original
      recording here, then choose audio-only or video export, original/mixed
      audio, and soft or burned-in subtitles in Export settings.
    </p>
    <div class="mt-3 flex flex-wrap items-center gap-3">
      <label class="text-sm"
        >{status.media_filename ? 'Replace media' : 'Attach audio / video'}
        <input
          type="file"
          accept="audio/*,video/*,.mkv,.m4v,.opus"
          disabled={busy}
          onchange={(event) => void upload(event.currentTarget.files?.[0])}
          class="mt-1 block max-w-full text-sm"
        />
      </label>
      <button
        type="button"
        class="btn"
        disabled={busy}
        onclick={() => void openLibrary()}>Choose existing media</button
      >
      {#if status.adoption_required}<button
          type="button"
          class="btn btn-primary"
          disabled={busy}
          onclick={() => void adopt()}>Register existing subtitles</button
        >{/if}
    </div>
    {#if libraryOpen}
      <div class="mt-3 flex flex-wrap gap-2">
        <select
          aria-label="Existing media source"
          class="rounded-lg border border-[var(--line)] p-2 text-sm"
          bind:value={selectedMedia}
          disabled={busy}
        >
          {#each library as item (item.id)}<option value={item.id}
              >{item.display_name}</option
            >{/each}
        </select>
        <button
          type="button"
          class="btn"
          disabled={busy || !selectedMedia}
          onclick={async () => {
            busy = true;
            try {
              await attach(selectedMedia);
              libraryOpen = false;
            } catch (caught) {
              error = errorMessage(caught);
            } finally {
              busy = false;
            }
          }}>Attach selected media</button
        >
        {#if !library.length}<p class="muted text-sm">
            No reusable recordings found.
          </p>{/if}
      </div>
    {/if}
    <div class="mt-4 flex flex-wrap items-end gap-3">
      <label class="text-sm"
        >Word-timing method
        <select
          class="mt-1 block rounded-lg border border-[var(--line)] p-2"
          bind:value={method}
          disabled={busy}
        >
          <option value="ctc">Local CTC · keep subtitle wording</option>
          <option value="ctc_asr_fallback"
            >CTC, then configured ASR for low coverage</option
          >
        </select>
      </label>
      <button
        type="button"
        class="btn"
        disabled={busy || !status.can_align}
        onclick={() => void align()}>Align existing words</button
      >
      <button
        type="button"
        class="btn"
        disabled={busy}
        onclick={() => void load()}>Refresh status</button
      >
    </div>
    {#if method === 'ctc_asr_fallback'}<p class="muted mt-2 text-sm">
        Fallback uses your configured transcription service, which may be a
        cloud provider. Accepted CTC timing and original wording are retained.
      </p>{/if}
    {#if status.word_timing_artifact_id}<p class="mt-2 text-sm">
        Word-level timing evidence is available.
      </p>{/if}
    {#if busy && progress > 0 && progress < 100}<p
        role="status"
        class="mt-2 text-sm"
      >
        Upload: {Math.round(progress)}%
      </p>{/if}
    {#if message}<p role="status" class="mt-2 text-sm">{message}</p>{/if}
    {#if error}<p role="alert" class="mt-2 text-sm text-red-700">
        {error}
      </p>{/if}
  </section>
{/if}
