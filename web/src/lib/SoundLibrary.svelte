<script lang="ts">
  import { onMount } from 'svelte';
  import { errorMessage } from './errors';
  import SoundDetails from './SoundDetails.svelte';
  import { soundList, type SoundItem } from './sound-api';
  let {
    selectedId = $bindable<string | null>(null),
    onvariant
  }: { selectedId?: string | null; onvariant?: (itemId: string) => void } =
    $props();
  let query = $state('');
  let archived = $state(false);
  let items = $state<SoundItem[]>([]);
  let cursor = $state<string | null>(null);
  let busy = $state(false);
  let error = $state('');
  let alive = true;
  let requestId = 0;
  async function load(next?: string) {
    const request = ++requestId;
    busy = true;
    error = '';
    try {
      const result = await soundList(query, next, archived);
      if (alive && request === requestId) {
        items = result.items;
        cursor = result.next_cursor;
      }
    } catch (caught) {
      if (alive && request === requestId) error = errorMessage(caught);
    } finally {
      if (alive && request === requestId) busy = false;
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

<div
  class="grid min-w-0 grid-cols-1 items-start gap-5"
  class:has-details={selectedId !== null}
>
  <section class="surface rounded-2xl p-5">
    <h2 class="text-lg font-semibold">Saved sounds & music</h2>
    <p class="muted mb-5 mt-1 text-sm">
      Play a clip, edit its name and tags, or create another variant.
    </p>
    <form
      class="mb-5 flex flex-wrap items-end gap-3"
      onsubmit={(event) => {
        event.preventDefault();
        void load();
      }}
    >
      <label class="min-w-40 flex-1 text-sm"
        >Find a sound<input
          class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2"
          bind:value={query}
          maxlength="200"
          type="search"
          placeholder="Bells, kettle, piano…"
        /></label
      >
      <button class="btn btn-secondary" disabled={busy}>Search</button>
      <label class="flex items-center gap-2 py-2 text-sm"
        ><input
          type="checkbox"
          bind:checked={archived}
          onchange={() => load()}
        /> Archived</label
      >
    </form>
    {#if error}<p role="alert" class="mb-3 text-[var(--danger)]">
        {error}
      </p>{/if}
    {#if busy}<p role="status" class="muted mb-3 text-sm">
        Loading sounds…
      </p>{/if}
    <ul class="space-y-2">
      {#each items as item (item.id)}
        <li>
          <button
            class="sound-row"
            aria-pressed={selectedId === item.id}
            onclick={() => (selectedId = item.id)}
            ><span class="min-w-0"
              ><span class="block font-medium">{item.name}</span><span
                class="muted text-sm"
                >{item.preferred_variant?.provenance.kind === 'music'
                  ? 'Music · '
                  : ''}{item.tags.join(' · ')}</span
              ></span
            ><span class="muted shrink-0 text-sm"
              >{item.preferred_variant?.duration_seconds?.toFixed(1) ??
                '—'}s</span
            ></button
          >
        </li>
      {/each}
    </ul>
    {#if !busy && !items.length}<p class="muted my-5 text-sm">
        No sounds here yet. Generate or import one in the playground, then save
        it.
      </p>{/if}
    <div class="mt-4 flex gap-2">
      <button class="btn btn-secondary" disabled={busy} onclick={() => load()}
        >First page</button
      >{#if cursor}<button
          class="btn btn-secondary"
          disabled={busy}
          onclick={() => load(cursor ?? undefined)}>Next sounds</button
        >{/if}
    </div>
  </section>
  {#if selectedId}{#key selectedId}<SoundDetails
        itemId={selectedId}
        onclose={() => (selectedId = null)}
        onchange={() => load()}
        {onvariant}
      />{/key}{/if}
</div>

<style>
  .sound-row {
    display: flex;
    gap: 1rem;
    justify-content: space-between;
    align-items: center;
    text-align: left;
    width: 100%;
    padding: 0.8rem;
    border: 1px solid var(--line);
    border-radius: 0.7rem;
    background: var(--paper-strong);
  }
  .sound-row:hover {
    background: var(--accent-soft);
    border-color: var(--accent);
  }
  .sound-row[aria-pressed='true'] {
    background: var(--accent-soft);
    border-color: var(--accent);
  }
  @media (min-width: 1024px) {
    .has-details {
      grid-template-columns: minmax(0, 1fr) minmax(20rem, 1fr);
    }
  }
</style>
