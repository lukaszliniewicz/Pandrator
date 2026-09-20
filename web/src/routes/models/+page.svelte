<script lang="ts">
  import { onMount } from 'svelte';
  import {
    ArrowLeft,
    ArrowRight,
    Search,
    SlidersHorizontal
  } from '@lucide/svelte';
  import { apiJson } from '$lib/api';
  import { errorMessage } from '$lib/errors';
  import AudioCppModelDetails from '$lib/AudioCppModelDetails.svelte';
  import { readable, type AudioCppCatalogue } from '$lib/audio-cpp-catalogue';

  let catalogue = $state<AudioCppCatalogue | null>(null);
  let query = $state('');
  let category = $state('');
  let capability = $state('');
  let language = $state('');
  let commercialUse = $state('');
  let recommended = $state(true);
  let busy = $state(false);
  let error = $state('');
  let requestId = 0;

  async function load(offset = 0) {
    const current = ++requestId;
    busy = true;
    error = '';
    const params = new URLSearchParams({
      query,
      category,
      capability,
      language,
      commercial_use: commercialUse,
      recommended_only: String(recommended),
      limit: '20',
      offset: String(offset)
    });
    try {
      const result = await apiJson<AudioCppCatalogue>(
        `/api/v1/services/audio-cpp/catalogue?${params}`
      );
      if (current === requestId) catalogue = result;
    } catch (caught) {
      if (current === requestId) error = errorMessage(caught);
    } finally {
      if (current === requestId) busy = false;
    }
  }
  onMount(() => {
    void load();
  });
  const size = (bytes?: number) =>
    bytes ? `${(bytes / 1024 ** 3).toFixed(2)} GiB download` : '';
</script>

<svelte:head><title>Audio model catalogue · Pandrator</title></svelte:head>

<div class="mx-auto max-w-6xl space-y-6 pb-8">
  <header class="flex flex-wrap items-start justify-between gap-4">
    <div class="max-w-3xl">
      <div class="eyebrow">
        audio.cpp · {catalogue?.runtime_version ?? 'Model catalogue'}
      </div>
      <h1 class="mt-2 text-3xl font-semibold tracking-tight">
        Find a model for the job
      </h1>
      <p class="muted mt-3 leading-relaxed">
        Start with a few useful choices, or browse every family and variant.
        Compare languages, voice controls and model licences before installing.
      </p>
    </div>
    <a href="/providers" class="btn btn-secondary"
      ><SlidersHorizontal size={15} /> Manage installed models</a
    >
  </header>
  <form
    class="surface rounded-2xl border border-[var(--line)] p-4"
    onsubmit={(event) => {
      event.preventDefault();
      void load();
    }}
  >
    <div class="grid items-end gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <label class="text-xs font-semibold sm:col-span-2 lg:col-span-1"
        >Search models
        <input
          class="field mt-1 w-full"
          type="search"
          bind:value={query}
          placeholder="Qwen, compact, cloning…"
          maxlength="160"
        />
      </label>
      <label class="text-xs font-semibold"
        >Task family
        <select class="field mt-1 w-full" bind:value={category}>
          <option value="">All tasks</option><option value="tts"
            >Speech generation</option
          ><option value="asr">Transcription</option><option
            value="voice_conversion">Voice conversion</option
          ><option value="audio_generation">Music and sound generation</option
          ><option value="audio_tools">Audio tools</option><option
            value="speech_analysis">Speech analysis</option
          ><option value="community">Other community models</option>
        </select>
      </label>
      <label class="text-xs font-semibold"
        >Capability
        <select class="field mt-1 w-full" bind:value={capability}>
          <option value="">Any capability</option><option value="voice_cloning"
            >Voice cloning</option
          ><option value="voice_design">Voice design</option><option
            value="instructions">Directions</option
          ><option value="emotion_control">Emotion controls</option><option
            value="vocal_events">Vocal sounds</option
          ><option value="streaming">Upstream streaming</option><option
            value="sound_generation">Sound effects</option
          ><option value="speech_editing">Upstream speech editing</option>
        </select>
      </label>
      <label class="text-xs font-semibold"
        >Language code
        <input
          class="field mt-1 w-full"
          bind:value={language}
          placeholder="e.g. en, pl, zh"
          maxlength="40"
        />
      </label>
    </div>
    <label class="mt-3 block max-w-sm text-xs font-semibold"
      >Commercial use
      <select class="field mt-1 w-full" bind:value={commercialUse}>
        <option value="">All licences</option>
        <option value="permitted">Permitted, including attribution terms</option
        >
        <option value="conditional">Subject to licence restrictions</option>
        <option value="noncommercial">Non-commercial only</option>
        <option value="unknown">Not yet verified</option>
      </select>
    </label>
    <div class="mt-4 flex flex-wrap items-center justify-between gap-3">
      <label class="flex items-center gap-2 text-sm"
        ><input type="checkbox" bind:checked={recommended} /> Recommended starting
        points</label
      >
      <button type="submit" class="btn btn-primary" disabled={busy}
        ><Search size={15} /> {busy ? 'Searching…' : 'Apply filters'}</button
      >
    </div>
  </form>
  <p class="muted text-sm">
    The catalogue includes models that are not installed. Non-speech tasks and
    speech editing are listed for discovery; this release extends speech
    synthesis. Recommendations describe useful roles, not a quality ranking.
  </p>
  {#if error}<div
      role="alert"
      class="surface rounded-xl border border-red-500/30 p-4"
    >
      {error}<button class="btn btn-sm ml-3" onclick={() => load()}
        >Retry</button
      >
    </div>{/if}
  <div aria-live="polite" class="muted text-sm">
    {#if catalogue}{catalogue.total} matching package{catalogue.total === 1
        ? ''
        : 's'} · {catalogue.families.length} upstream families{:else if busy}Loading
      the catalogue…{/if}
  </div>
  <div class="grid gap-4 lg:grid-cols-2" aria-busy={busy}>
    {#each catalogue?.items ?? [] as model (model.id)}
      <article
        class="surface min-w-0 rounded-2xl border border-[var(--line)] p-5"
      >
        {#if model.recommended_for}<p
            class="text-xs font-semibold text-[var(--accent)]"
          >
            {model.recommended_for}
          </p>{/if}
        <h2 class="mt-1 text-lg font-semibold">{model.label}</h2>
        <p class="muted mt-1 text-xs">
          {model.family_label ?? model.family} · {readable(
            model.upstream_status
          )}{size(model.estimated_download_bytes)
            ? ` · ${size(model.estimated_download_bytes)}`
            : ''}
        </p>
        <p class="mt-3 text-sm leading-relaxed">{model.description}</p>
        {#if typeof model.package_availability === 'object'}
          <p class="muted mt-2 text-xs">
            <strong>{readable(model.package_availability.status)}:</strong>
            {model.package_availability.reason}
          </p>
        {/if}
        <div class="mt-4 border-t border-[var(--line)] pt-3">
          <AudioCppModelDetails {model} />
        </div>
      </article>
    {:else}{#if catalogue && !busy}<p class="muted py-8">
          No models match. Try another language or turn off recommended starting
          points.
        </p>{/if}{/each}
  </div>
  {#if catalogue && (catalogue.offset > 0 || catalogue.next_offset !== null)}
    <nav aria-label="Catalogue pages" class="flex items-center justify-between">
      <button
        class="btn btn-secondary"
        disabled={busy || catalogue.offset === 0}
        onclick={() => load(Math.max(0, (catalogue?.offset ?? 0) - 20))}
        ><ArrowLeft size={15} /> Previous</button
      >
      <span class="muted text-xs"
        >{catalogue.offset + 1}–{catalogue.offset + catalogue.items.length} of {catalogue.total}</span
      >
      <button
        class="btn btn-secondary"
        disabled={busy || catalogue.next_offset === null}
        onclick={() => load(catalogue?.next_offset ?? 0)}
        >Next <ArrowRight size={15} /></button
      >
    </nav>
  {/if}
</div>

<style>
  .field {
    border: 1px solid var(--line);
    border-radius: 0.65rem;
    background: var(--paper);
    color: var(--ink);
    padding: 0.65rem 0.7rem;
    font-weight: 400;
  }
</style>
