<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { Sparkles, Trash2, Upload } from '@lucide/svelte';
  import { ApiMutationAttempt } from './api';
  import { errorMessage } from './errors';
  import ParameterLabel from './ParameterLabel.svelte';
  import SoundCandidateRow from './SoundCandidateRow.svelte';
  import SoundCandidateSettings from './SoundCandidateSettings.svelte';
  import SoundModelControls from './SoundModelControls.svelte';
  import {
    cancelSound,
    discardSound,
    generateSound,
    importSound,
    saveSound,
    soundCandidate,
    soundCandidates,
    soundCatalogue,
    soundDetail,
    soundPending,
    soundTags,
    type SoundBackend,
    type SoundCandidate,
    type SoundCandidateReceipt,
    type SoundCatalogue,
    type SoundItem,
    type SoundKind,
    type SoundPage
  } from './sound-api';
  let {
    onsaved,
    saveItemId
  }: { onsaved: (itemId: string) => void; saveItemId?: string } = $props();
  let saveItem = $state<SoundItem | null>(null);
  let catalogue = $state<SoundCatalogue | null>(null);
  let prompt = $state('');
  let kind = $state<SoundKind>('sound');
  let modelId = $state('stable_audio_3_small_sfx_q8_0');
  let backend = $state<SoundBackend>('cpu');
  let lyrics = $state('');
  let parameters = $state<Record<string, string | number | boolean>>({});
  let duration = $state(4);
  let candidateCount = $state(1);
  let seed = $state<number | undefined>();
  let threads = $state(4);
  let candidate = $state<SoundCandidate | null>(null);
  let expandedId = $state<string | null>(null);
  let recent = $state<SoundCandidate[]>([]);
  let batch = $state<SoundCandidateReceipt[]>([]);
  let pageCursor = $state<string | undefined>();
  let selectedIds = $state(new Set<string>());
  let cursor = $state<string | null>(null);
  let name = $state('');
  let tags = $state('');
  let error = $state('');
  let notice = $state('');
  let busy = $state(false);
  let fileInput: HTMLInputElement;
  let promptInput: HTMLTextAreaElement;
  let nameInput = $state<HTMLInputElement>();
  let activeTake = $state<HTMLDivElement>();
  let alive = true;
  let requestGeneration = 0;
  let recentRequest = 0;
  let polling = false;
  let generationAttempt = new ApiMutationAttempt();
  const saveAttempt = new ApiMutationAttempt();
  const importAttempt = new ApiMutationAttempt();
  const availableModels = $derived(
    catalogue?.models.filter(
      (model) => model.available !== false && model.kinds.includes(kind)
    ) ?? []
  );
  const selectedModel = $derived(
    catalogue?.models.find((model) => model.id === modelId)
  );
  const candidateRows = $derived(
    candidate && !recent.some((item) => item.id === candidate?.id)
      ? [candidate, ...recent]
      : recent.map((item) => (item.id === candidate?.id ? candidate : item))
  );
  const discardableRows = $derived(candidateRows.filter(canDiscard));
  const allSelected = $derived(
    discardableRows.length > 0 &&
      discardableRows.every((item) => selectedIds.has(item.id))
  );
  const generationUnavailable = $derived(
    !selectedModel ||
      selectedModel.available === false ||
      !selectedModel.backends.includes(backend) ||
      !catalogue?.runtime.available
  );
  const pendingBatch = $derived(
    batch.filter((item) =>
      ['queued', 'running', 'pending', 'retrying', 'cancel_requested'].includes(
        item.status
      )
    )
  );
  const readyBatch = $derived(
    batch.filter((item) => item.status === 'ready').length
  );
  const generationActive = $derived(
    Boolean(pendingBatch.length) || soundPending(candidate)
  );

  function updateBatch(items: SoundCandidate[]) {
    batch = batch.map((take) => {
      const item = items.find((entry) => entry.id === take.candidate_id);
      return item
        ? { ...take, status: item.status, item_id: item.item_id }
        : take;
    });
    const selected = items.find((item) => item.id === candidate?.id);
    if (selected && candidate) {
      // Compact list rows update status and audio without dropping the expanded
      // take's recorded lyrics and sampler settings.
      candidate = {
        ...candidate,
        ...selected,
        provenance: { ...candidate.provenance, ...selected.provenance }
      };
    }
  }

  function canDiscard(item: SoundCandidate) {
    return !item.item_id && !soundPending(item) && item.status !== 'discarded';
  }
  function checkCandidate(id: string, checked: boolean) {
    const next = new Set(selectedIds);
    if (checked) next.add(id);
    else next.delete(id);
    selectedIds = next;
  }

  function selectModel(id: string) {
    modelId = id;
    const model = catalogue?.models.find((entry) => entry.id === id);
    if (!model) return;
    duration = model.duration.default;
    backend = model.backends.includes(backend)
      ? backend
      : (model.backends[0] ?? 'cpu');
    parameters = {};
    if (seed !== undefined && seed > model.seed_maximum) {
      seed = undefined;
    }
  }
  function selectKind(value: SoundKind) {
    kind = value;
    const choices =
      catalogue?.models.filter(
        (model) => model.available !== false && model.kinds.includes(value)
      ) ?? [];
    const preferred =
      choices.find((model) => model.cached) ??
      choices.find(
        (model) =>
          model.id ===
          (value === 'music'
            ? 'stable_audio_3_small_music_q8_0'
            : 'stable_audio_3_small_sfx_q8_0')
      ) ??
      choices[0];
    if (preferred) selectModel(preferred.id);
  }
  function restoreRequest(result: SoundCandidate, reuseSeed = false) {
    const provenance = result.provenance;
    const model = catalogue?.models.find(
      (entry) => entry.id === provenance.model_id
    );
    if (model) {
      kind = provenance.kind ?? model.kinds[0];
      modelId = model.id;
      backend = ['cpu', 'vulkan', 'cuda'].includes(provenance.backend ?? '')
        ? (provenance.backend as SoundBackend)
        : (model.backends[0] ?? 'cpu');
      lyrics = provenance.lyrics ?? '';
      parameters = {
        ...(provenance.num_inference_steps === undefined
          ? {}
          : { num_inference_steps: provenance.num_inference_steps }),
        ...(provenance.guidance_scale === undefined
          ? {}
          : { guidance_scale: provenance.guidance_scale }),
        ...provenance.parameters
      };
      threads = provenance.threads ?? catalogue?.defaults.threads ?? threads;
      seed = reuseSeed ? provenance.seed : undefined;
    } else {
      error =
        'This take’s model is no longer in the catalogue. Its audio and recorded settings are still available.';
      return false;
    }
    if (provenance.prompt) prompt = provenance.prompt;
    duration =
      provenance.duration_seconds ?? result.duration_seconds ?? duration;
    return true;
  }

  function applyRecentPage(result: SoundPage<SoundCandidate>, next?: string) {
    recent = result.items.filter((item) => item.status !== 'discarded');
    pageCursor = next;
    updateBatch(result.items);
    cursor = result.next_cursor;
    const selectable = new Set(
      recent.filter(canDiscard).map((item) => item.id)
    );
    if (candidate && canDiscard(candidate)) selectable.add(candidate.id);
    selectedIds = new Set([...selectedIds].filter((id) => selectable.has(id)));
  }
  async function loadRecent(next?: string) {
    const request = ++recentRequest;
    const result = await soundCandidates(next);
    if (!alive || request !== recentRequest) return;
    applyRecentPage(result, next);
  }
  async function choose(id: string) {
    const request = ++requestGeneration;
    try {
      const result = await soundCandidate(id);
      if (!alive || request !== requestGeneration) return null;
      if (candidate?.id !== result.id) {
        name = result.provenance.display_name ?? '';
        tags = '';
      }
      candidate = result;
      expandedId = result.id;
      notice = '';
      error = '';
      return result;
    } catch (caught) {
      if (alive && request === requestGeneration) error = errorMessage(caught);
      return null;
    }
  }
  async function reuseSettings(id: string, reuseSeed = false) {
    if (busy) return;
    busy = true;
    try {
      const result = await choose(id);
      if (result && restoreRequest(result, reuseSeed)) {
        await tick();
        promptInput.focus();
        promptInput.scrollIntoView({ behavior: 'smooth', block: 'center' });
        notice = reuseSeed
          ? `Settings copied with fixed seed ${seed}.`
          : 'Settings copied. The next generation will use a fresh seed.';
      }
    } finally {
      if (alive) busy = false;
    }
  }
  async function regenerate(id: string) {
    if (busy || generationActive) return;
    busy = true;
    let restored = false;
    try {
      const result = await choose(id);
      if (result) restored = restoreRequest(result);
    } finally {
      if (alive) busy = false;
    }
    if (alive && restored) await generate(1);
  }
  async function prepareSave(id: string) {
    if (busy) return;
    busy = true;
    try {
      const result = await choose(id);
      if (result) {
        await tick();
        nameInput?.focus();
        activeTake?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
    } finally {
      if (alive) busy = false;
    }
  }
  function expand(id: string) {
    if (busy) return;
    if (expandedId === id) expandedId = null;
    else {
      busy = true;
      void choose(id).finally(() => {
        if (alive) busy = false;
      });
    }
  }
  async function poll() {
    if (polling || busy || !generationActive) return;
    polling = true;
    const id = candidate?.id;
    const request = requestGeneration;
    const pageRequest = recentRequest;
    const pendingIds = pendingBatch.length
      ? pendingBatch.map((item) => item.candidate_id)
      : id
        ? [id]
        : [];
    try {
      const result = await soundCandidates();
      if (!alive || busy) return;
      updateBatch(result.items);
      if (pageCursor === undefined && pageRequest === recentRequest) {
        applyRecentPage(result);
      }
      // Usually one compact page tracks the entire batch. Only fetch a full
      // record if other generations have pushed a pending take off that page.
      const missing = pendingIds.filter(
        (pendingId) => !result.items.some((item) => item.id === pendingId)
      );
      if (missing.length) {
        const records = await Promise.all(missing.map(soundCandidate));
        if (!alive || busy) return;
        updateBatch(records);
      }
    } catch (caught) {
      if (alive && request === requestGeneration) error = errorMessage(caught);
    } finally {
      polling = false;
    }
  }
  async function generate(count = candidateCount) {
    if (busy || generationActive) return;
    if (!Number.isInteger(count) || count < 1 || count > 8) {
      error = 'Choose between 1 and 8 candidates.';
      return;
    }
    if (generationUnavailable) {
      error = 'Choose an available model and backend before generating.';
      return;
    }
    busy = true;
    error = '';
    notice = '';
    try {
      const receipt = await generateSound(
        {
          prompt,
          duration_seconds: duration,
          model_id: modelId,
          kind,
          backend,
          ...(selectedModel?.supports_lyrics ? { lyrics } : {}),
          parameters,
          ...(count === 1 ? {} : { candidate_count: count }),
          ...(seed === undefined ? {} : { seed }),
          threads
        },
        generationAttempt
      );
      // A confirmed admission ends this attempt. Unknown-outcome retries keep
      // its key and omitted seed so the server replays the resolved seed.
      generationAttempt = new ApiMutationAttempt();
      batch = receipt.candidates ?? [
        {
          candidate_id: receipt.candidate_id,
          job_id: receipt.job_id,
          item_id: receipt.item_id,
          status: receipt.status,
          cache_hit: receipt.cache_hit,
          seed: receipt.resolved_settings.seed
        }
      ];
      await choose(batch.at(-1)?.candidate_id ?? receipt.candidate_id);
      if (!alive) return;
      notice = receipt.cache_hit
        ? 'Reused an existing candidate with these settings.'
        : batch.length > 1
          ? `Accepted ${batch.length} candidates with different seeds.`
          : `Accepted · seed ${receipt.resolved_settings.seed}`;
      await loadRecent();
      await tick();
      activeTake?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
  async function save() {
    if (!candidate || busy || (saveItemId && !saveItem)) return;
    const savedId = candidate.id;
    busy = true;
    error = '';
    try {
      const item = await saveSound(
        saveItem
          ? {
              candidate_id: candidate.id,
              item_id: saveItem.id,
              expected_revision: saveItem.revision
            }
          : { candidate_id: candidate.id, name, tags: soundTags(tags) },
        saveAttempt
      );
      await choose(savedId);
      checkCandidate(savedId, false);
      await loadRecent();
      await tick();
      activeTake?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      if (alive) onsaved(item.id);
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
  async function upload(file?: File) {
    if (!file || busy) return;
    busy = true;
    error = '';
    notice = '';
    try {
      const result = await importSound(file, importAttempt);
      await choose(result.id);
      name = file.name.replace(/\.[^.]+$/, '');
      await loadRecent();
      await tick();
      activeTake?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
      if (fileInput) fileInput.value = '';
    }
  }
  async function discard(ids: string[]) {
    if (!ids.length || busy) return;
    busy = true;
    error = '';
    notice = '';
    const discarded = new Set<string>();
    const failures: string[] = [];
    try {
      for (const id of ids) {
        try {
          await discardSound(id);
          discarded.add(id);
        } catch (caught) {
          failures.push(errorMessage(caught));
        }
      }
      if (!alive) return;
      if (candidate && discarded.has(candidate.id)) {
        requestGeneration++;
        candidate = null;
        expandedId = null;
      }
      recent = recent.filter((item) => !discarded.has(item.id));
      batch = batch.map((item) =>
        discarded.has(item.candidate_id)
          ? { ...item, status: 'discarded' }
          : item
      );
      selectedIds = new Set(
        [...selectedIds].filter((id) => !discarded.has(id))
      );
      notice = discarded.size
        ? `Discarded ${discarded.size} candidate${discarded.size === 1 ? '' : 's'}.`
        : '';
      if (failures.length)
        error = `${failures.length} candidate${failures.length === 1 ? '' : 's'} could not be discarded. ${failures[0]}`;
      await loadRecent();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      if (alive) busy = false;
    }
  }
  async function cancel() {
    if (!candidate?.job_id) return;
    await cancelJobs([candidate.job_id]);
  }
  async function cancelJobs(ids: string[]) {
    if (busy || !ids.length) return;
    busy = true;
    error = '';
    const failures: string[] = [];
    try {
      for (const jobId of new Set(ids)) {
        try {
          await cancelSound(jobId);
        } catch (caught) {
          failures.push(errorMessage(caught));
        }
      }
      if (alive && failures.length) error = failures[0];
    } finally {
      if (alive) busy = false;
    }
    if (alive) await poll();
  }
  onMount(() => {
    alive = true;
    void Promise.all([
      soundCatalogue(),
      loadRecent(),
      saveItemId ? soundDetail(saveItemId) : null
    ])
      .then(([result, , item]) => {
        if (!alive) return;
        catalogue = result;
        saveItem = item;
        threads = result.defaults.threads;
        duration = result.defaults.duration_seconds;
        modelId = result.defaults.model_id;
        if (item?.preferred_variant) restoreRequest(item.preferred_variant);
        if (recent[0]) expand(recent[0].id);
      })
      .catch((caught) => {
        if (alive) error = errorMessage(caught);
      });
    const timer = setInterval(() => void poll(), 2000);
    return () => {
      alive = false;
      requestGeneration++;
      recentRequest++;
      clearInterval(timer);
    };
  });
</script>

<div class="grid min-w-0 grid-cols-1 items-start gap-5">
  <section class="surface rounded-2xl p-5">
    <h2 class="text-lg font-semibold">
      Create {kind === 'music' ? 'music' : 'a sound'}
    </h2>
    {#if saveItem}<p class="mt-2 text-sm">
        New variant for <strong>{saveItem.name}</strong>.
      </p>{/if}
    <p class="muted mb-5 mt-1 text-sm">
      {kind === 'music'
        ? 'Describe the mood, instruments and style. Save a track for an intro, transition or background layer.'
        : 'Describe bells, a boiling kettle, footsteps or another effect. Save it for any project.'}
    </p>
    <form
      class="space-y-4"
      onsubmit={(event) => {
        event.preventDefault();
        void generate();
      }}
    >
      <div class="grid items-start gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <label class="block text-sm font-medium"
          >Create
          <select
            class="sound-field mt-2"
            value={kind}
            onchange={(event) =>
              selectKind(event.currentTarget.value as SoundKind)}
          >
            <option value="sound">Sound effect</option>
            <option value="music">Music</option>
          </select>
        </label>
        <label class="block text-sm font-medium sm:col-span-1 lg:col-span-2"
          >Model
          <select
            class="sound-field mt-2"
            value={modelId}
            onchange={(event) => selectModel(event.currentTarget.value)}
            disabled={!availableModels.length}
          >
            {#each availableModels as model (model.id)}
              <option value={model.id}
                >{model.label}{model.cached ? ' · Installed' : ''}</option
              >
            {/each}
          </select>
        </label>
      </div>
      {#if selectedModel}
        <p class="muted text-sm">
          {selectedModel.requires_lyrics
            ? 'This model needs both a music description and lyrics.'
            : selectedModel.supports_lyrics
              ? 'Use a music description, with optional lyrics for vocals.'
              : 'This model generates audio from your description.'}
        </p>
        {#if selectedModel.notes}
          <p class="muted text-xs">{selectedModel.notes}</p>
        {/if}
        {#if selectedModel.family === 'minimax_h3'}
          <a
            class="inline-block text-xs text-[var(--accent)] underline"
            href="https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/LICENSE"
            target="_blank"
            rel="noreferrer">H3 licence and territorial restrictions</a
          >
        {/if}
        {#if !selectedModel.cached}
          <p class="muted text-xs">
            {(selectedModel.download_bytes / 1e9).toFixed(1)} GB of model files download
            on the first generation. You can cancel while they download.
          </p>
        {/if}
      {/if}
      <label class="block text-sm font-medium"
        >Description
        <textarea
          class="sound-field mt-2"
          bind:this={promptInput}
          bind:value={prompt}
          rows="4"
          maxlength="8000"
          required
          placeholder={kind === 'music'
            ? 'A gentle instrumental piano piece with warm strings, quiet and reflective. No vocals.'
            : 'A kettle boiling, bubbling water and a rising hiss of steam. No speech or music.'}
        ></textarea>
      </label>
      {#if selectedModel?.supports_lyrics}
        <label class="block text-sm font-medium"
          >Lyrics {#if !selectedModel.requires_lyrics}<span
              class="muted font-normal">(optional)</span
            >{/if}
          <textarea
            class="sound-field mt-2"
            bind:value={lyrics}
            rows="5"
            maxlength="16000"
            required={selectedModel.requires_lyrics}
            placeholder="[Verse]&#10;Your lyrics here…"></textarea>
        </label>
      {/if}
      <div class="flex flex-wrap items-end gap-4">
        <label class="block text-sm"
          >{selectedModel?.family === 'yue2'
            ? 'Maximum length (seconds)'
            : selectedModel?.family === 'minimax_h3'
              ? 'Approximate duration (seconds)'
              : 'Duration (seconds)'}<input
            class="sound-field mt-1 max-w-32"
            type="number"
            bind:value={duration}
            min={selectedModel?.duration.minimum ?? 1}
            max={selectedModel?.duration.maximum ?? 30}
            step="0.1"
            required
          /></label
        >
        <label class="block text-sm"
          >Candidates<input
            class="sound-field mt-1 max-w-24"
            type="number"
            bind:value={candidateCount}
            min="1"
            max="8"
            step="1"
            required
          /></label
        >
        <label class="block text-sm"
          >Run on
          <select
            class="sound-field mt-1"
            bind:value={backend}
            disabled={selectedModel?.available === false}
          >
            {#if selectedModel && !selectedModel.backends.includes(backend)}
              <option value={backend} disabled
                >{backend.toUpperCase()} · unavailable</option
              >
            {/if}
            {#each selectedModel?.backends ?? ['cpu'] as choice}
              <option value={choice}
                >{choice === 'cpu'
                  ? 'CPU'
                  : choice === 'cuda'
                    ? 'GPU · CUDA'
                    : 'GPU · Vulkan'}</option
              >
            {/each}
          </select>
        </label>
      </div>
      {#if candidateCount > 1}
        <p class="muted text-xs">
          Same description and settings, with a different seed for each take.
          They run in sequence on the selected backend.
        </p>
      {/if}
      {#if selectedModel}
        <p class="muted text-xs">
          Length: {selectedModel.duration.minimum}–{selectedModel.duration
            .maximum} seconds.
          {#if selectedModel.duration.description}
            {selectedModel.duration.description}
          {:else if kind === 'music'}
            The finished track may be shorter than requested.
          {/if}
        </p>
      {/if}
      <details class="rounded-lg border border-[var(--line)] p-3">
        <summary class="cursor-pointer text-sm">Advanced settings</summary>
        <div class="mt-3 grid gap-3 sm:grid-cols-2">
          <label class="text-sm"
            ><ParameterLabel
              section="sound_generation"
              name="seed"
              label="Seed"
              description="Leave blank for a random seed. Several candidates use one random base and distinct following seeds. Set a seed to compare or repeat the same settings; changing the model, backend or software can change the result."
            /> <span class="muted">(optional)</span><input
              class="sound-field mt-1"
              type="number"
              bind:value={seed}
              min="0"
              max={selectedModel?.seed_maximum ?? 4294967295}
              step="1"
              placeholder="Random for each generation"
            /></label
          >
          <label class="text-sm"
            ><ParameterLabel
              section="sound_generation"
              name="threads"
              label="CPU threads"
              description="CPU workers used by local audio generation, including CPU work on a GPU backend. Start with Pandrator’s default and increase gradually if needed; too many threads can compete with other applications. This controls computation, not the intended sound or style."
            /><input
              class="sound-field mt-1"
              type="number"
              bind:value={threads}
              min="1"
              step="1"
              required
            /></label
          >
        </div>
        {#if selectedModel && Object.keys(selectedModel.parameters).length}
          <div class="mt-4 border-t border-[var(--line)] pt-4">
            <SoundModelControls
              controls={selectedModel.parameters}
              bind:values={parameters}
            />
          </div>
        {/if}
        <p class="muted mt-2 text-xs">
          Leave the seed blank for a new random seed each generation. Set one to
          repeat the same request. Local speech generation may wait while audio
          is being created.
        </p>
        {#if selectedModel?.source_url}<a
            class="mt-2 inline-block text-xs text-[var(--accent)] underline"
            href={selectedModel.source_url}
            target="_blank"
            rel="noreferrer">Model documentation</a
          >{/if}
      </details>
      {#if catalogue?.unavailable_models?.length}
        <details class="rounded-lg border border-[var(--line)] p-3">
          <summary class="cursor-pointer text-sm">Unavailable models</summary>
          <ul class="muted mt-3 space-y-2 text-xs">
            {#each catalogue.unavailable_models as model (model.id)}
              <li><strong>{model.label}</strong> · {model.reason}</li>
            {/each}
          </ul>
        </details>
      {/if}
      {#if catalogue && !catalogue.runtime.available}<p
          role="status"
          class="text-sm"
        >
          {catalogue.runtime.reason ?? 'audio.cpp 0.9.1 or newer is required.'}
        </p>{/if}
      {#if selectedModel?.available === false && selectedModel.reason}
        <p role="status" class="text-sm">{selectedModel.reason}</p>
      {/if}
      <div class="flex flex-wrap gap-2">
        <button
          class="btn btn-primary"
          disabled={busy || generationActive || generationUnavailable}
          ><Sparkles size={16} /> {busy ? 'Working…' : 'Generate'}</button
        >
        <button
          class="btn btn-secondary"
          type="button"
          disabled={busy}
          onclick={() => fileInput.click()}
          ><Upload size={16} /> Import audio</button
        >
        <input
          class="hidden"
          bind:this={fileInput}
          type="file"
          accept="audio/*,.wav,.flac,.mp3,.ogg,.m4a"
          onchange={() => upload(fileInput.files?.[0])}
          aria-label="Import sound audio"
        />
      </div>
      <p class="muted text-xs">
        Imported clips: up to 50 MB and 5 minutes. The original audio is
        preserved.
      </p>
    </form>
    {#if error}<p
        role="alert"
        class="sound-error mt-4 rounded-lg border p-3 text-sm"
      >
        {error}
      </p>{/if}
    {#if notice}<p role="status" class="muted mt-3 text-sm">{notice}</p>{/if}
  </section>
  <section class="surface min-w-0 rounded-2xl p-5" aria-label="Candidates">
    <header class="mb-4 flex flex-wrap items-center justify-between gap-3">
      <div>
        <h2 class="text-lg font-semibold">Candidates</h2>
        <p class="muted mt-1 text-sm">
          Play takes, inspect their settings and save the versions you like.
        </p>
      </div>
      <button
        class="btn btn-secondary"
        disabled={busy}
        onclick={() =>
          loadRecent().catch((caught) => (error = errorMessage(caught)))}
        >Refresh</button
      >
    </header>
    {#if batch.length > 1}
      <div
        class="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-[var(--line)] p-3"
      >
        <p role="status" class="text-sm">
          {readyBatch} of {batch.length} ready{#if pendingBatch.length}
            · {pendingBatch.length} generating or queued{:else if readyBatch < batch.length}
            · {batch.length - readyBatch} cancelled, discarded or failed{/if}
        </p>
        {#if pendingBatch.some((item) => item.job_id)}
          <button
            class="btn btn-secondary"
            disabled={busy}
            onclick={() =>
              cancelJobs(
                pendingBatch.flatMap((item) =>
                  item.job_id ? [item.job_id] : []
                )
              )}>Cancel remaining</button
          >
        {/if}
      </div>
    {/if}
    {#if candidateRows.length}
      <div
        class="mb-4 flex flex-wrap items-center gap-3 border-b border-[var(--line)] pb-3"
      >
        <label class="flex items-center gap-2 text-sm"
          ><input
            type="checkbox"
            checked={allSelected}
            disabled={busy || !discardableRows.length}
            onchange={(event) =>
              (selectedIds = event.currentTarget.checked
                ? new Set(discardableRows.map((item) => item.id))
                : new Set())}
          />Select unsaved</label
        >
        <button
          class="btn btn-secondary"
          disabled={busy || !selectedIds.size}
          onclick={() => discard([...selectedIds])}
          ><Trash2 size={14} /> Discard selected{#if selectedIds.size}
            ({selectedIds.size}){/if}</button
        >
        <span class="muted text-xs">Saved and active takes stay protected.</span
        >
      </div>
      <ul class="space-y-3">
        {#each candidateRows as item (item.id)}
          {@const model = catalogue?.models.find(
            (entry) => entry.id === item.provenance.model_id
          )}
          <li>
            <SoundCandidateRow
              candidate={item}
              {model}
              expanded={expandedId === item.id}
              checked={selectedIds.has(item.id)}
              {busy}
              generationBusy={generationActive}
              canDiscard={canDiscard(item)}
              onexpand={() => expand(item.id)}
              oncheck={(checked) => checkCandidate(item.id, checked)}
              onreuse={() => void reuseSettings(item.id)}
              onregenerate={() => void regenerate(item.id)}
              onsave={() => void prepareSave(item.id)}
              ondiscard={() => void discard([item.id])}
              onopen={() => item.item_id && onsaved(item.item_id)}
            >
              {#if candidate?.id === item.id}
                <div bind:this={activeTake} class="space-y-4">
                  <SoundCandidateSettings {candidate} {model} />
                  {#if candidate.provenance.seed !== undefined && model}
                    <button
                      type="button"
                      class="btn btn-secondary"
                      disabled={busy}
                      onclick={() => reuseSettings(item.id, true)}
                      >Use settings and seed</button
                    >
                  {/if}
                  {#if soundPending(candidate)}
                    <p role="status" class="muted text-sm">
                      Generation continues if you leave this page. This take
                      will appear here when it finishes.
                    </p>
                    <button
                      class="btn btn-secondary"
                      disabled={busy}
                      onclick={cancel}>Cancel generation</button
                    >
                  {:else if candidate.status === 'ready' && !candidate.item_id}
                    <form
                      class="space-y-3 border-t border-[var(--line)] pt-4"
                      onsubmit={(event) => {
                        event.preventDefault();
                        void save();
                      }}
                    >
                      {#if saveItem}
                        <p class="text-sm">
                          Save as the preferred variant of <strong
                            >{saveItem.name}</strong
                          >. Existing project cues keep their chosen variant.
                        </p>
                      {:else}
                        <div class="grid gap-3 sm:grid-cols-2">
                          <label class="block text-sm"
                            >Name<input
                              class="sound-field mt-1"
                              bind:this={nameInput}
                              bind:value={name}
                              required
                              maxlength="255"
                              placeholder={candidate.provenance.kind === 'music'
                                ? 'Quiet piano intro'
                                : 'Kitchen kettle'}
                            /></label
                          >
                          <label class="block text-sm"
                            >Tags <span class="muted">(comma separated)</span
                            ><input
                              class="sound-field mt-1"
                              bind:value={tags}
                              placeholder="kitchen, steam"
                            /></label
                          >
                        </div>
                      {/if}
                      <button
                        class="btn btn-primary"
                        disabled={busy || Boolean(saveItemId && !saveItem)}
                        >Save</button
                      >
                    </form>
                  {:else if candidate.status !== 'ready'}
                    <p role="status" class="muted text-sm">
                      Generation {candidate.status}. Job details are available
                      in Activity.
                    </p>
                  {/if}
                </div>
              {/if}
            </SoundCandidateRow>
          </li>
        {/each}
      </ul>
    {:else}
      <p class="muted text-sm">
        No candidates on this page. Generate or import audio to start.
      </p>
    {/if}
    {#if cursor}
      <div class="mt-4">
        <button
          class="btn btn-secondary"
          disabled={busy}
          onclick={() =>
            loadRecent(cursor ?? undefined).catch(
              (caught) => (error = errorMessage(caught))
            )}>Older candidates</button
        >
      </div>
    {/if}
  </section>
</div>

<style>
  .sound-field {
    display: block;
    width: 100%;
    border: 1px solid var(--line);
    border-radius: 0.55rem;
    padding: 0.6rem 0.7rem;
    background: var(--paper-strong);
  }
  .sound-error {
    color: light-dark(#b42318, #fda4af);
    border-color: color-mix(in srgb, currentColor 35%, var(--line));
    background: color-mix(in srgb, currentColor 5%, var(--paper-strong));
  }
</style>
