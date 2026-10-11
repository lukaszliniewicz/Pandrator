<script lang="ts">
  import { onMount } from 'svelte';
  import { errorMessage } from './errors';
  import SoundPreview from './SoundPreview.svelte';
  import {
    soundDetail,
    soundTags,
    updateSound,
    type SoundDetail,
    type SoundItem
  } from './sound-api';
  let {
    itemId,
    onclose,
    onchange,
    onvariant
  }: {
    itemId: string;
    onclose: () => void;
    onchange: () => void;
    onvariant?: (itemId: string) => void;
  } = $props();
  let detail = $state<SoundDetail | null>(null);
  let name = $state('');
  let tags = $state('');
  let error = $state('');
  let busy = $state(false);
  let alive = true;
  let requestId = 0;
  async function load(cursor?: string) {
    const request = ++requestId;
    try {
      const result = await soundDetail(itemId, cursor);
      if (!alive || request !== requestId) return;
      detail = result;
      name = result.name;
      tags = result.tags.join(', ');
    } catch (caught) {
      if (alive && request === requestId) error = errorMessage(caught);
    }
  }
  async function change(changes: Parameters<typeof updateSound>[1]) {
    if (!detail || busy) return;
    busy = true;
    error = '';
    try {
      await updateSound(detail as SoundItem, changes);
      await load();
      if (alive) onchange();
    } catch (caught) {
      if (alive) error = errorMessage(caught);
    } finally {
      if (alive) busy = false;
    }
  }
  onMount(() => {
    alive = true;
    void load();
    return () => {
      alive = false;
      requestId++;
    };
  });
</script>

<section class="surface rounded-2xl p-5" aria-label="Sound or music details">
  <header class="mb-4 flex items-center justify-between gap-3">
    <h2 class="text-lg font-semibold">{detail?.name ?? 'Loading clip…'}</h2>
    <button
      class="btn btn-secondary"
      onclick={onclose}
      aria-label="Close sound details">Close</button
    >
  </header>
  {#if error}<p role="alert" class="mb-3 text-[var(--danger)]">{error}</p>{/if}
  {#if detail}
    <form
      class="space-y-3"
      onsubmit={(event) => {
        event.preventDefault();
        void change({ name, tags: soundTags(tags) });
      }}
    >
      <label class="block text-sm"
        >Name<input
          class="sound-field mt-1"
          bind:value={name}
          required
          maxlength="255"
        /></label
      >
      <label class="block text-sm"
        >Tags <span class="muted">(comma separated)</span><input
          class="sound-field mt-1"
          bind:value={tags}
        /></label
      >
      <div class="flex flex-wrap gap-2">
        <button class="btn btn-primary" disabled={busy}>Save</button>
        <button
          type="button"
          class="btn btn-secondary"
          disabled={busy}
          onclick={() => change({ archived: !detail?.archived })}
          >{detail.archived ? 'Restore to library' : 'Archive'}</button
        >
        {#if onvariant}<button
            type="button"
            class="btn btn-secondary"
            disabled={busy}
            onclick={() => onvariant?.(itemId)}>Add a variant</button
          >{/if}
      </div>
    </form>
    <p class="muted my-4 text-sm">
      Variants keep their original audio. Archiving keeps clips already used in
      projects available.
    </p>
    <h3 class="mb-2 font-semibold">Variants</h3>
    <ul class="space-y-3">
      {#each detail.variants.items as variant (variant.id)}
        <li class="rounded-lg border border-[var(--line)] p-3">
          <div class="mb-2 flex flex-wrap items-center justify-between gap-2">
            <span class="text-sm"
              >{variant.provenance.kind === 'music'
                ? 'Music · '
                : ''}{variant.duration_seconds?.toFixed(2)}s · {variant
                .provenance.operation === 'import'
                ? 'Imported'
                : variant.provenance.model_id}
              {#if variant.id === detail.preferred_variant?.id}<span
                  class="ml-2 text-[var(--accent)]">Preferred</span
                >{/if}
            </span>
            {#if variant.id !== detail.preferred_variant?.id}<button
                class="btn btn-secondary"
                disabled={busy}
                onclick={() => change({ preferred_variant_id: variant.id })}
                >Use by default</button
              >{/if}
          </div>
          <SoundPreview candidate={variant} label={`${detail.name} variant`} />
          {#if variant.provenance.prompt}<p class="muted mt-2 text-sm">
              {variant.provenance.prompt}
            </p>{/if}
          {#if variant.provenance.lyrics || variant.provenance.parameters}
            <details class="mt-3 text-sm">
              <summary class="cursor-pointer text-[var(--accent)]"
                >Generation settings</summary
              >
              {#if variant.provenance.lyrics}<p
                  class="muted mt-2 whitespace-pre-wrap"
                >
                  {variant.provenance.lyrics}
                </p>{/if}
              {#if variant.provenance.parameters && Object.keys(variant.provenance.parameters).length}
                <dl class="muted mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
                  {#each Object.entries(variant.provenance.parameters) as [key, value] (key)}
                    <dt>{key.replaceAll('_', ' ')}</dt>
                    <dd>{String(value)}</dd>
                  {/each}
                </dl>
              {/if}
            </details>
          {/if}
        </li>
      {/each}
    </ul>
    <div class="mt-3 flex gap-2">
      <button class="btn btn-secondary" onclick={() => load()} disabled={busy}
        >Latest variants</button
      >
      {#if detail.variants.next_cursor}<button
          class="btn btn-secondary"
          onclick={() => load(detail?.variants.next_cursor ?? undefined)}
          disabled={busy}>Next variants</button
        >{/if}
    </div>
  {/if}
</section>

<style>
  .sound-field {
    width: 100%;
    border: 1px solid var(--line);
    border-radius: 0.55rem;
    padding: 0.55rem 0.7rem;
    background: var(--paper-strong);
  }
</style>
