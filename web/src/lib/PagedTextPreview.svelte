<script lang="ts">
  import { tick } from 'svelte';
  import { textPreviewPage, TEXT_PREVIEW_PAGE_SIZE } from './text-preview';

  let {
    text,
    label = 'Text preview',
    page = $bindable(0)
  }: { text: string; label?: string; page?: number } = $props();
  let root = $state<HTMLElement>();
  const pages = $derived(
    Math.max(1, Math.ceil(text.length / TEXT_PREVIEW_PAGE_SIZE))
  );
  const displayed = $derived(textPreviewPage(text, page));
  $effect(() => {
    if (page >= pages) page = pages - 1;
  });
  async function move(next: number) {
    page = next;
    await tick();
    root?.closest('[data-preview-content]')?.scrollTo({ top: 0 });
  }
</script>

<section bind:this={root} aria-label={label}>
  {#if pages > 1}<nav aria-label={`${label} portions`} class="preview-pages">
      <span>Preview portion {page + 1} of {pages}</span>
      <button
        class="btn btn-sm btn-secondary"
        disabled={page === 0}
        onclick={() => void move(page - 1)}>Previous</button
      >
      <button
        class="btn btn-sm btn-secondary"
        disabled={page === pages - 1}
        onclick={() => void move(page + 1)}>Next</button
      >
    </nav>{/if}
  <pre>{displayed}</pre>
</section>

<style>
  section {
    min-width: 0;
  }
  .preview-pages {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.75rem;
    font-size: 0.75rem;
  }
  .preview-pages span {
    margin-right: auto;
    color: var(--muted);
  }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    border: 1px solid var(--line);
    border-radius: 0.85rem;
    padding: 1rem;
    background: var(--paper-strong);
    font-size: 0.8rem;
    line-height: 1.65;
  }
</style>
