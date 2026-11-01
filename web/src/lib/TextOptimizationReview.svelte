<script lang="ts">
  import { errorMessage } from './errors';
  import {
    Check,
    ChevronLeft,
    ChevronRight,
    LoaderCircle,
    Save
  } from '@lucide/svelte';
  import { onMount, tick } from 'svelte';
  import { apiResponse } from './api';
  import { artifactApi } from './domain-api';
  import TextDiff from './TextDiff.svelte';
  import SearchReplaceBar from './SearchReplaceBar.svelte';
  import type { TextReplacement, TextSearchMatch } from './search-replace';
  import ReviewWorkspace from './ReviewWorkspace.svelte';
  import UnsavedChangesDialog from './UnsavedChangesDialog.svelte';
  import { guardEditorNavigation } from './editor-navigation';

  const PAGE_SIZE = 50;

  let {
    artifactId,
    onclose,
    onsaved
  }: { artifactId: string; onclose: () => void; onsaved: () => void } =
    $props();
  let rows = $state<{ index: number; source: string; optimized: string }[]>([]);
  let changedOnly = $state(false);
  let diffView = $state(false);
  let loading = $state(true);
  let saving = $state(false);
  let error = $state('');
  let toolsOpen = $state(false);
  let baseline = $state<string[]>([]);
  let pendingLeave = $state<(() => void) | null>(null);
  const dirty = $derived(
    rows.some((row, index) => row.optimized !== baseline[index])
  );
  function requestLeave(action = onclose) {
    if (saving) return;
    if (dirty) pendingLeave = action;
    else action();
  }
  function discardAndLeave() {
    const action = pendingLeave;
    pendingLeave = null;
    action?.();
  }
  async function saveAndLeave() {
    const action = pendingLeave;
    if (await save(false)) {
      pendingLeave = null;
      action?.();
    }
  }
  guardEditorNavigation({
    dirty: () => dirty,
    pending: () => saving,
    requestLeave
  });
  let pageIndex = $state(0);
  let rowsViewport = $state<HTMLDivElement>();
  const visibleRows = $derived(
    changedOnly
      ? rows.filter((row) => row.source.trim() !== row.optimized.trim())
      : rows
  );
  const editableTexts = $derived(rows.map((row) => row.optimized));
  const pageCount = $derived(
    Math.max(1, Math.ceil(visibleRows.length / PAGE_SIZE))
  );
  const pageStart = $derived(pageIndex * PAGE_SIZE);
  const pagedRows = $derived(
    visibleRows.slice(pageStart, pageStart + PAGE_SIZE)
  );

  $effect(() => {
    if (pageIndex >= pageCount) pageIndex = pageCount - 1;
  });

  async function changePage(nextPage: number) {
    pageIndex = Math.max(0, Math.min(nextPage, pageCount - 1));
    await tick();
    rowsViewport?.scrollTo({ top: 0 });
  }

  function toggleChangedOnly() {
    changedOnly = !changedOnly;
    pageIndex = 0;
  }

  function applySearchReplacements(updates: TextReplacement[]) {
    for (const update of updates) {
      if (rows[update.index]) rows[update.index].optimized = update.text;
    }
  }

  async function navigateSearchMatch(match: TextSearchMatch) {
    toolsOpen = false;
    if (changedOnly) changedOnly = false;
    pageIndex = Math.floor(match.itemIndex / PAGE_SIZE);
    await tick();
    const field = document.querySelector<HTMLTextAreaElement>(
      `[data-optimization-search-index="${match.itemIndex}"]`
    );
    field?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    field?.focus({ preventScroll: true });
    field?.setSelectionRange(match.start, match.end);
  }

  onMount(async () => {
    try {
      const response = await apiResponse(`/artifacts/${artifactId}/content`);
      const payload: unknown = await response.json();
      if (!Array.isArray(payload))
        throw new Error('This optimization artifact is not a segment list.');
      rows = payload.map((item: unknown, index: number) => {
        const value =
          item && typeof item === 'object'
            ? (item as Record<string, unknown>)
            : {};
        return {
          index,
          source: String(
            value.source_text ?? value.original_sentence ?? value.text ?? ''
          ),
          optimized: String(
            value.tts_optimized_sentence ??
              value.processed_sentence ??
              value.text ??
              ''
          )
        };
      });
      baseline = rows.map((row) => row.optimized);
      pageIndex = 0;
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      loading = false;
    }
  });

  async function save(closeAfter = true) {
    if (saving || loading || !rows.length) return false;
    saving = true;
    error = '';
    try {
      await artifactApi.saveOptimizationReview(
        artifactId,
        rows.map((row) => ({ index: row.index, text: row.optimized }))
      );
      baseline = rows.map((row) => row.optimized);
      onsaved();
      if (closeAfter) onclose();
      return true;
    } catch (caught) {
      error = errorMessage(caught);
      return false;
    } finally {
      saving = false;
    }
  }
</script>

<ReviewWorkspace
  closeLabel="Close review"
  titleId="optimization-review-title"
  onclose={() => requestLeave()}
  busy={saving}
  bind:toolsOpen
  toolsTitle="Find & compare"
  toolsSummary={changedOnly ? 'Changed only' : diffView ? 'Diff view' : ''}
>
  {#snippet header()}
    <div class="min-w-0 flex-1">
      <h2 id="optimization-review-title" class="text-lg font-semibold">
        Review narration optimization
      </h2>
      <p class="muted mt-1 text-xs">
        {dirty ? 'Unsaved changes' : 'Saved text'} · Saves a new artifact; original
        remains in history.
      </p>
    </div>
    <button
      onclick={() => void save()}
      disabled={saving || loading || !rows.length}
      class="btn btn-primary"
      ><Save size={16} /> {saving ? 'Saving…' : 'Save revision'}</button
    >
  {/snippet}
  {#snippet tools()}
    <div class="space-y-4">
      <label
        class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-3 py-2 text-xs font-semibold"
        ><input
          type="checkbox"
          checked={changedOnly}
          onchange={toggleChangedOnly}
          class="accent-[var(--accent)]"
        /> Changed only</label
      >
      <label
        class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-3 py-2 text-xs font-semibold"
        ><input
          type="checkbox"
          bind:checked={diffView}
          class="accent-[var(--accent)]"
        /> Diff view</label
      >
      {#if rows.length}
        <SearchReplaceBar
          texts={editableTexts}
          disabled={saving}
          onreplace={applySearchReplacements}
          onnavigate={navigateSearchMatch}
          label="optimized narration segments"
        />
      {/if}
    </div>
  {/snippet}
  {#snippet notices()}
    {#if error && rows.length}<p
        role="alert"
        class="border-b border-red-500/30 px-4 py-2 text-sm text-red-600"
      >
        {error}
      </p>{/if}
  {/snippet}
  {#snippet footer()}
    {#if !loading && visibleRows.length > PAGE_SIZE}<nav
        class="flex flex-wrap items-center justify-between gap-3 border-t border-[var(--line)] px-5 py-3 text-xs sm:px-6"
        aria-label="Optimization review pages"
      >
        <span class="muted tabular-nums"
          >Showing {pageStart + 1}–{Math.min(
            pageStart + PAGE_SIZE,
            visibleRows.length
          )} of {visibleRows.length} items</span
        >
        <div class="flex items-center gap-2">
          <button
            type="button"
            onclick={() => changePage(pageIndex - 1)}
            disabled={pageIndex === 0}
            class="flex items-center gap-1 rounded-lg border border-[var(--line)] px-2.5 py-1.5 font-semibold disabled:opacity-35"
            ><ChevronLeft size={14} /> Previous</button
          >
          <span class="min-w-20 text-center tabular-nums"
            >Page {pageIndex + 1} of {pageCount}</span
          >
          <button
            type="button"
            onclick={() => changePage(pageIndex + 1)}
            disabled={pageIndex >= pageCount - 1}
            class="flex items-center gap-1 rounded-lg border border-[var(--line)] px-2.5 py-1.5 font-semibold disabled:opacity-35"
            >Next <ChevronRight size={14} /></button
          >
        </div>
      </nav>{/if}
  {/snippet}
  <div
    bind:this={rowsViewport}
    inert={saving}
    class="min-h-0 flex-1 overflow-auto p-4 sm:p-6"
  >
    {#if loading}<div class="grid min-h-72 place-items-center">
        <LoaderCircle class="animate-spin" size={26} />
      </div>
    {:else if error && !rows.length}<p
        class="rounded-xl bg-red-500/10 p-4 text-sm text-red-600"
      >
        {error}
      </p>
    {:else}
      <div
        class="mb-3 hidden grid-cols-[4rem_1fr_1fr] gap-3 px-3 text-xs font-bold uppercase tracking-wider text-[var(--muted)] md:grid"
      >
        <span>Item</span><span>Original</span><span>Optimized · editable</span>
      </div>
      <div class="space-y-3">
        {#each pagedRows as row (row.index)}
          <article
            class:changed={row.source.trim() !== row.optimized.trim()}
            class="grid gap-3 rounded-2xl border border-[var(--line)] p-3 md:grid-cols-[4rem_1fr_1fr]"
          >
            <div
              class="flex items-center gap-2 text-xs font-bold text-[var(--muted)]"
            >
              <span>#{row.index + 1}</span
              >{#if row.source.trim() === row.optimized.trim()}<Check
                  size={13}
                />{/if}
            </div>
            {#if diffView}<TextDiff
                before={row.source}
                after={row.optimized}
              />{:else}<div
                class="rounded-xl bg-[var(--paper)] p-3 text-sm leading-6"
              >
                {row.source}
              </div>{/if}
            <textarea
              bind:value={row.optimized}
              aria-label={`Optimized narration item ${row.index + 1}`}
              disabled={saving}
              data-optimization-search-index={row.index}
              rows="3"
              class="min-h-24 resize-y rounded-xl border border-[var(--line)] bg-[var(--paper)] p-3 text-sm leading-6"
            ></textarea>
          </article>
        {/each}
      </div>
    {/if}
  </div>
</ReviewWorkspace>
{#if pendingLeave}<UnsavedChangesDialog
    onsave={() => void saveAndLeave()}
    ondiscard={discardAndLeave}
    oncancel={() => (pendingLeave = null)}
    busy={saving}
    {error}
  />{/if}

<style>
  article.changed {
    border-color: color-mix(in srgb, var(--accent) 38%, var(--line));
    background: color-mix(in srgb, var(--accent-soft) 35%, transparent);
  }
</style>
