<script lang="ts">
  import { X, Search } from '@lucide/svelte';
  import { onMount, onDestroy, untrack } from 'svelte';
  import { modalFocus } from './modal-focus';
  import { ApiMutationAttempt } from './api';
  import { errorMessage } from './errors';
  import {
    soundDetail,
    soundList,
    type SoundItem,
    type SoundCandidate,
    type SoundDetail
  } from './sound-api';
  import {
    changeProjectSound,
    projectSounds,
    publicSoundAnchor,
    soundPlacementLabel,
    type SoundAnchor,
    type SoundPlayback,
    type ProjectSoundCue
  } from './project-sounds-api';
  import { soundReturnUrl } from './sound-selection';
  import SoundPreview from './SoundPreview.svelte';
  import SoundPlaybackFields from './SoundPlaybackFields.svelte';
  let {
    sessionId,
    workflowKind,
    anchor,
    cue,
    itemId,
    onclose,
    onchanged
  }: {
    sessionId: string;
    workflowKind: string;
    anchor: SoundAnchor;
    cue?: ProjectSoundCue;
    itemId?: string;
    onclose: () => void;
    onchanged: () => void;
  } = $props();
  const timed = $derived(workflowKind !== 'audiobook');
  let query = $state(''),
    error = $state(''),
    busy = $state(false),
    loading = $state(true);
  let items = $state<SoundItem[]>([]),
    detail = $state<SoundDetail | null>(null);
  let selected = $state<SoundCandidate | null>(
    untrack(() =>
      cue
        ? {
            id: cue.variant_id,
            status: cue.sound.status,
            artifact_id: cue.sound.artifact_id,
            item_id: null,
            duration_seconds: cue.sound.duration_seconds,
            job_id: null,
            sample_rate: null,
            channels: null,
            created_at: '',
            provenance: {}
          }
        : null
    )
  );
  let revision = $state(0),
    planLoading = $state(true),
    included = $state(untrack(() => cue?.included ?? true));
  let position = $state<SoundAnchor>(untrack(() => ({ ...anchor })));
  let playback = $state<SoundPlayback>(
    untrack(() =>
      cue
        ? { ...cue.playback }
        : {
            mode: workflowKind === 'audiobook' ? 'insert' : 'overlay',
            offset_ms: 0,
            gain_db: 0,
            trim_start_seconds: 0,
            fade_in_ms: 5,
            fade_out_ms: 5
          }
    )
  );
  let alive = true,
    serial = 0;
  const visibleVariants = $derived(
    [
      ...(selected &&
      !detail?.variants.items.some((variant) => variant.id === selected?.id)
        ? [selected]
        : []),
      ...(detail?.variants.items ?? [])
    ].filter((variant) => variant.status === 'ready')
  );
  const attempt = new ApiMutationAttempt();
  function selectVariant(variant: SoundCandidate | null) {
    selected = variant;
    if (!cue && variant?.provenance.kind === 'music') playback.mode = 'overlay';
  }
  async function search() {
    const request = ++serial;
    loading = true;
    error = '';
    try {
      const result = await soundList(query);
      if (alive && request === serial) {
        items = result.items;
        detail = null;
      }
    } catch (failure) {
      if (alive && request === serial) error = errorMessage(failure);
    } finally {
      if (alive && request === serial) loading = false;
    }
  }
  async function choose(id: string, cursor?: string) {
    const request = ++serial;
    loading = true;
    error = '';
    try {
      const result = await soundDetail(id, cursor);
      if (!alive || request !== serial) return;
      detail = result;
      if (!cursor) selectVariant(result.preferred_variant);
    } catch (failure) {
      if (alive && request === serial) error = errorMessage(failure);
    } finally {
      if (alive && request === serial) loading = false;
    }
  }
  async function save() {
    if (!selected || busy) return;
    busy = true;
    error = '';
    const cleanAnchor = publicSoundAnchor(position);
    try {
      await changeProjectSound(
        sessionId,
        revision,
        { variant_id: selected.id, anchor: cleanAnchor, playback, included },
        cue?.logical_id,
        attempt
      );
      if (alive) {
        onchanged();
        onclose();
      }
    } catch (failure) {
      if (alive) error = errorMessage(failure);
    } finally {
      if (alive) busy = false;
    }
  }
  onMount(() => {
    void projectSounds(sessionId)
      .then((plan) => {
        if (alive) {
          revision = plan.revision;
          planLoading = false;
        }
      })
      .catch((failure) => {
        if (alive) error = errorMessage(failure);
      });
    if (itemId) void choose(itemId);
    else void search();
  });
  onDestroy(() => {
    alive = false;
    ++serial;
  });
</script>

<div
  class="fixed inset-0 z-[100] grid place-items-center bg-black/45 p-3"
  role="presentation"
  onclick={(event) => {
    if (event.target === event.currentTarget && !busy) onclose();
  }}
>
  <div
    use:modalFocus={{
      onclose: () => {
        if (!busy) onclose();
      }
    }}
    role="dialog"
    aria-modal="true"
    aria-labelledby="sound-cue-title"
    tabindex="-1"
    class="max-h-[90vh] w-full max-w-xl overflow-auto rounded-2xl border border-[var(--line)] bg-[var(--paper)] p-5 shadow-xl"
  >
    <header class="mb-4 flex items-center justify-between gap-3">
      <h3 id="sound-cue-title" class="text-lg font-semibold">
        {cue ? 'Edit sound or music' : 'Add sound or music'}
      </h3>
      <button
        class="btn btn-secondary"
        aria-label="Close sound chooser"
        disabled={busy}
        onclick={onclose}><X size={16} /></button
      >
    </header>
    <p class="muted mb-3 text-xs">
      Independent of the speech model · saved audio is reused.
    </p>
    <form
      class="flex gap-2"
      onsubmit={(event) => {
        event.preventDefault();
        void search();
      }}
    >
      <input
        class="input min-w-0 flex-1"
        aria-label="Search saved sounds"
        maxlength="200"
        placeholder="Search saved sounds…"
        bind:value={query}
      />
      <button
        class="btn btn-secondary"
        disabled={busy || loading}
        aria-label="Search sounds"><Search size={16} /></button
      >
    </form>
    {#if loading}<p class="muted my-3 text-sm" role="status">
        Loading sounds…
      </p>{/if}
    {#if detail}<div class="my-3 space-y-2">
        <strong class="text-sm">{detail.name}</strong>
        <select
          class="input w-full"
          aria-label="Sound variant"
          value={selected?.id ?? ''}
          onchange={(event) => {
            selectVariant(
              visibleVariants.find(
                (variant) => variant.id === event.currentTarget.value
              ) ?? null
            );
          }}
        >
          <option value="">Choose a variant</option>
          {#each visibleVariants as variant}<option value={variant.id}
              >{variant.duration_seconds?.toFixed(1)} s · {new Date(
                variant.created_at
              ).toLocaleString()}{variant.id === detail.preferred_variant?.id
                ? ' · preferred'
                : ''}</option
            >{/each}
        </select>
        {#if detail.variants.next_cursor}<button
            class="btn btn-secondary"
            disabled={loading}
            onclick={() =>
              void choose(detail!.id, detail!.variants.next_cursor!)}
            >Next variants</button
          >{/if}
      </div>{:else}<div
        class="my-3 max-h-44 overflow-auto rounded-lg border border-[var(--line)]"
      >
        {#each items as item}<button
            class="block w-full border-b border-[var(--line)] px-3 py-2 text-left text-sm hover:bg-[var(--accent-soft)]"
            onclick={() => void choose(item.id)}
            >{item.name}<span class="muted ml-2 text-xs"
              >{item.preferred_variant?.duration_seconds?.toFixed(1)} s</span
            ></button
          >{/each}
        {#if !loading && !items.length}<p class="muted p-3 text-sm">
            No saved sounds yet.
          </p>{/if}
      </div>{/if}
    {#if selected}<SoundPreview candidate={selected} />{/if}
    <a
      class="my-3 inline-block text-sm text-[var(--accent)] underline"
      href={`/sounds?${new URLSearchParams({ return_to: soundReturnUrl(sessionId, position) })}`}
      >Create or import sound or music</a
    >
    <div class="space-y-3 border-t border-[var(--line)] pt-4">
      <label class="block text-sm"
        >Placement
        <select
          class="input mt-1 w-full"
          bind:value={position.kind}
          disabled={busy}
        >
          {#if position.segment_id}<option value="before_segment"
              >Before segment</option
            ><option value="after_segment">After segment</option>{/if}
          {#if position.quote}<option value="after_word"
              >After “{position.quote}”</option
            ><option value="next_pause">Next pause after these words</option
            >{/if}
          {#if timed}<option value="source_time">At a time in the source</option
            >{/if}
        </select>
      </label>
      {#if position.kind === 'source_time'}<label class="block text-sm"
          >Source time (seconds)<input
            class="input mt-1 w-full"
            type="number"
            min="0"
            step="0.01"
            value={position.time_seconds ?? 0}
            oninput={(event) =>
              (position.time_seconds = Number(event.currentTarget.value))}
          /></label
        >{/if}
      {#if position.kind === 'after_word' || position.kind === 'next_pause'}<p
          class="muted text-xs"
        >
          Insertion needs alignment and a quiet gap in the selected take.
          Uncertain positions stay pending. Overlay uses the aligned word
          boundary.
        </p>{/if}
      <SoundPlaybackFields bind:value={playback} {timed} disabled={busy} />
      {#if selected?.provenance.kind === 'music'}
        <p class="muted text-xs">
          For background music, use Overlay and lower the gain. Trim the track
          to fit the project audio.
        </p>
      {/if}
      <label class="flex items-center gap-2 text-sm"
        ><input
          type="checkbox"
          bind:checked={included}
          disabled={busy}
        />Include in the mix</label
      >
    </div>
    {#if error}<p class="mt-3 text-sm text-red-700" role="alert">
        {error}
      </p>{/if}
    <footer class="mt-4 flex items-center justify-between gap-3">
      <span class="muted text-xs">{soundPlacementLabel(position)}</span><button
        class="btn btn-primary"
        disabled={busy || loading || planLoading || !selected}
        onclick={() => void save()}>{busy ? 'Saving…' : 'Save cue'}</button
      >
    </footer>
  </div>
</div>
