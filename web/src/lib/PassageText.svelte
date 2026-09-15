<script lang="ts">
  import type { GenerationSegment } from './api-models';
  import {
    boundaryLabel,
    passagePieces,
    type PassageBoundary,
    type PassageTextLayer
  } from './passage-structure';

  let {
    item,
    layer = 'display',
    oninspect
  }: {
    item: GenerationSegment;
    layer?: PassageTextLayer;
    oninspect: (
      item: GenerationSegment,
      layer: PassageTextLayer,
      boundary: PassageBoundary,
      anchor?: HTMLButtonElement,
      activate?: boolean
    ) => void;
  } = $props();
  const text = $derived(
    layer === 'speech' ? item.optimized_text || item.text : item.text
  );
  const mapping = $derived(item.passage_structure?.layers[layer]);
  const pieces = $derived(passagePieces(text, mapping));
</script>

<!-- Keep adjacent control blocks adjacent: whitespace here changes copied narration. -->
<!-- prettier-ignore -->
<span class="passage-text" data-passage-segment={item.id}>
  {#if pieces}
    {#each pieces as piece, index (piece.boundary?.id ?? `tail-${index}`)}<span
        >{piece.text}</span
      >{#if piece.boundary}{@const boundary = piece.boundary}<button
          type="button"
          class="passage-dot"
          class:unfinished={!boundary.natural}
          class:informational={!boundary.split_allowed}
          aria-label={boundaryLabel(boundary)}
          aria-haspopup="menu"
          aria-expanded="false"
          onpointerenter={(event) => {
            if (event.pointerType === 'mouse') oninspect(item, layer, boundary, event.currentTarget, false);
          }}
          onkeydown={(event) => {
            if (event.key !== 'ArrowDown') return;
            event.preventDefault();
            event.stopPropagation();
            oninspect(item, layer, boundary, event.currentTarget, true);
          }}
          onclick={(event) => {
            event.stopPropagation();
            oninspect(item, layer, boundary, event.currentTarget, true);
          }}
        ></button>{/if}{/each}
  {:else}
    {text}<span class="passage-note" role="status"
      >{mapping?.status === 'mapped'
        ? 'Text changed; passage markers are hidden until the mapping is verified.'
        : mapping?.message || 'No verified passage mapping available.'}</span
    >
  {/if}
</span>

<style>
  .passage-text {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .passage-dot {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.15em;
    min-height: 1.5em;
    vertical-align: baseline;
    padding: 0;
    margin: 0 0.04em;
    border-radius: 0.35em;
    border: 0;
    background: var(--accent-soft);
    color: var(--accent);
    cursor: pointer;
    user-select: none;
    -webkit-user-select: none;
  }
  /* Interface-only glyph: no bullet enters copied text, editing or TTS. */
  .passage-dot::after {
    content: '\00b7';
    font-weight: 800;
  }
  .passage-dot.unfinished {
    color: var(--muted);
    background: transparent;
    outline: 1px dotted var(--line);
  }
  .passage-dot.informational {
    background: transparent;
  }
  .passage-dot:hover,
  .passage-dot:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  .passage-note {
    display: block;
    color: var(--muted);
    font-family: var(--font-sans, sans-serif);
    font-size: 0.7rem;
    line-height: 1.5;
    margin-top: 0.35rem;
  }
</style>
