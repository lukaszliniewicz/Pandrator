<script lang="ts">
  import { Check, LoaderCircle, Save, X } from '@lucide/svelte';
  import { onMount, tick } from 'svelte';
  import { api } from './api';
  import TextDiff from './TextDiff.svelte';
  import SearchReplaceBar from './SearchReplaceBar.svelte';
  import type { TextReplacement, TextSearchMatch } from './search-replace';

  let { artifactId, onclose, onsaved }: { artifactId: string; onclose: () => void; onsaved: () => void } = $props();
  let rows = $state<{ index: number; source: string; optimized: string }[]>([]);
  let changedOnly = $state(false);
  let diffView = $state(false);
  let loading = $state(true);
  let saving = $state(false);
  let error = $state('');
  const visibleRows = $derived(changedOnly ? rows.filter((row) => row.source.trim() !== row.optimized.trim()) : rows);
  const editableTexts = $derived(rows.map((row) => row.optimized));

  function applySearchReplacements(updates: TextReplacement[]) {
    for (const update of updates) {
      if (rows[update.index]) rows[update.index].optimized = update.text;
    }
  }

  async function navigateSearchMatch(match: TextSearchMatch) {
    if (changedOnly) changedOnly = false;
    await tick();
    const field = document.querySelector<HTMLTextAreaElement>(`[data-optimization-search-index="${match.itemIndex}"]`);
    field?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    field?.focus({ preventScroll: true });
    field?.setSelectionRange(match.start, match.end);
  }

  onMount(async () => {
    try {
      const response = await fetch(`/api/v1/artifacts/${artifactId}/content`, { credentials: 'same-origin' });
      if (!response.ok) throw new Error(`Review could not be loaded (${response.status}).`);
      const payload = await response.json();
      if (!Array.isArray(payload)) throw new Error('This optimization artifact is not a segment list.');
      rows = payload.map((item: any, index: number) => ({
        index,
        source: String(item?.source_text ?? item?.original_sentence ?? item?.text ?? ''),
        optimized: String(item?.tts_optimized_sentence ?? item?.processed_sentence ?? item?.text ?? '')
      }));
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    } finally {
      loading = false;
    }
  });

  async function save() {
    saving = true;
    error = '';
    try {
      await api(`/artifacts/${artifactId}/optimization-review`, {
        method: 'POST',
        body: JSON.stringify({ items: rows.map((row) => ({ index: row.index, text: row.optimized })) })
      });
      onsaved();
      onclose();
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    } finally {
      saving = false;
    }
  }
</script>

<div class="fixed inset-0 z-[90] grid place-items-center bg-black/55 p-3 backdrop-blur-sm" role="presentation" onclick={(event) => event.target === event.currentTarget && onclose()}>
  <div class="surface flex max-h-[96vh] w-full max-w-7xl flex-col overflow-hidden rounded-3xl" role="dialog" aria-modal="true" aria-labelledby="optimization-review-title">
    <header class="flex flex-wrap items-center gap-3 border-b border-[var(--line)] px-5 py-4 sm:px-6">
      <div class="min-w-0 flex-1"><div class="eyebrow">Speech optimization review</div><h2 id="optimization-review-title" class="mt-1 text-xl font-semibold">Original and LLM-optimized narration</h2><p class="muted mt-1 text-xs">Edits create a new reviewed artifact; the original optimization remains in history.</p></div>
      <label class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-3 py-2 text-xs font-semibold"><input type="checkbox" bind:checked={changedOnly} class="accent-[var(--accent)]"/> Changed only</label>
      <label class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-3 py-2 text-xs font-semibold"><input type="checkbox" bind:checked={diffView} class="accent-[var(--accent)]"/> Diff view</label>
      <button onclick={onclose} class="rounded-xl p-2" aria-label="Close review"><X size={20}/></button>
    </header>
    {#if rows.length}<div class="border-b border-[var(--line)] px-5 py-2 sm:px-6"><SearchReplaceBar texts={editableTexts} onreplace={applySearchReplacements} onnavigate={navigateSearchMatch} label="optimized narration segments"/></div>{/if}
    <div class="min-h-0 flex-1 overflow-auto p-4 sm:p-6">
      {#if loading}<div class="grid min-h-72 place-items-center"><LoaderCircle class="animate-spin" size={26}/></div>
      {:else if error && !rows.length}<p class="rounded-xl bg-red-500/10 p-4 text-sm text-red-600">{error}</p>
      {:else}
        <div class="mb-3 hidden grid-cols-[4rem_1fr_1fr] gap-3 px-3 text-xs font-bold uppercase tracking-wider text-[var(--muted)] md:grid"><span>Item</span><span>Original</span><span>Optimized · editable</span></div>
        <div class="space-y-3">
          {#each visibleRows as row (row.index)}
            <article class:changed={row.source.trim() !== row.optimized.trim()} class="grid gap-3 rounded-2xl border border-[var(--line)] p-3 md:grid-cols-[4rem_1fr_1fr]">
              <div class="flex items-center gap-2 text-xs font-bold text-[var(--muted)]"><span>#{row.index + 1}</span>{#if row.source.trim() === row.optimized.trim()}<Check size={13}/>{/if}</div>
              {#if diffView}<TextDiff before={row.source} after={row.optimized}/>{:else}<div class="rounded-xl bg-[var(--paper)] p-3 text-sm leading-6">{row.source}</div>{/if}
              <textarea bind:value={row.optimized} data-optimization-search-index={row.index} rows="3" class="min-h-24 resize-y rounded-xl border border-[var(--line)] bg-[var(--paper)] p-3 text-sm leading-6"></textarea>
            </article>
          {/each}
        </div>
      {/if}
    </div>
    <footer class="flex flex-wrap items-center justify-end gap-3 border-t border-[var(--line)] px-5 py-4 sm:px-6">
      {#if error && rows.length}<p class="mr-auto text-sm text-red-500">{error}</p>{/if}
      <button onclick={onclose} class="rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold">Close</button>
      <button onclick={save} disabled={loading || saving || !rows.length || rows.some((row) => !row.optimized.trim())} class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-40">{#if saving}<LoaderCircle class="animate-spin" size={16}/>{:else}<Save size={16}/>{/if} Save reviewed revision</button>
    </footer>
  </div>
</div>

<style>article.changed{border-color:color-mix(in srgb,var(--accent) 38%,var(--line));background:color-mix(in srgb,var(--accent-soft) 35%,transparent)}</style>
