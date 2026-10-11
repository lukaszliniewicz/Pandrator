<script lang="ts">
  import SoundPlayground from '$lib/SoundPlayground.svelte';
  import SoundLibrary from '$lib/SoundLibrary.svelte';
  import { page } from '$app/state';
  import { LibraryBig, Sparkles } from '@lucide/svelte';
  const returnTarget = $derived.by(() => {
    const target = page.url.searchParams.get('return_to');
    if (!target) return null;
    try {
      const parsed = new URL(target, 'http://local.invalid');
      return parsed.origin === 'http://local.invalid' &&
        /^\/sessions\/[a-f0-9-]{36}\/generation$/.test(parsed.pathname)
        ? parsed
        : null;
    } catch {
      return null;
    }
  });
  const returnUrl = $derived.by(() => {
    if (!returnTarget) return null;
    const query = new URLSearchParams(returnTarget.search);
    if (selectedId) query.set('sound_item', selectedId);
    return `${returnTarget.pathname}?${query}`;
  });
  let tab = $state<'playground' | 'library'>('playground');
  let selectedId = $state<string | null>(null);
  let saveItemId = $state<string | undefined>();
</script>

<svelte:head><title>Sounds & Music — Pandrator</title></svelte:head>
<header class="mb-6 flex flex-wrap items-end justify-between gap-4">
  <div>
    <h1 class="text-3xl font-semibold">Sounds & Music</h1>
    <p class="muted mt-1">
      Create, play and save effects and music for your projects.
    </p>
  </div>
  <div
    class="sounds-navigation flex gap-1 rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] p-1"
    role="group"
    aria-label="Sounds and music view"
  >
    <button
      class="btn"
      class:btn-primary={tab === 'playground'}
      aria-pressed={tab === 'playground'}
      onclick={() => (tab = 'playground')}
      ><Sparkles size={16} /> Playground</button
    ><button
      class="btn"
      class:btn-primary={tab === 'library'}
      aria-pressed={tab === 'library'}
      onclick={() => (tab = 'library')}><LibraryBig size={16} /> Library</button
    >
  </div>
</header>
{#if returnUrl}<div
    class="mb-4 flex flex-wrap items-center gap-3 rounded-lg border border-[var(--line)] p-3"
  >
    <a class="btn btn-primary" href={returnUrl}
      >{selectedId ? 'Use saved sound in project' : 'Return to project'}</a
    >
    <p class="muted text-sm">
      Choose the exact variant and placement when you return.
    </p>
  </div>{/if}
{#if tab === 'playground'}
  {#key saveItemId}<SoundPlayground
      {saveItemId}
      onsaved={(id) => {
        selectedId = id;
        saveItemId = undefined;
        tab = 'library';
      }}
    />{/key}
{:else}
  <SoundLibrary
    bind:selectedId
    onvariant={(id) => {
      saveItemId = id;
      tab = 'playground';
    }}
  />
{/if}
