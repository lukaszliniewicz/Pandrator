<script lang="ts">
  import { AudioLines } from '@lucide/svelte';
  import type { GenerationSegment } from './api-models';
  import {
    annotationSpans,
    deliveryDescription,
    speakerLabel,
    speakerColorKey,
    selectedSpeechRange,
    type SpeechSelection,
    type SpeechPreview,
    type SpeechSpan
  } from './speech-annotations';

  let {
    item,
    layer = 'display',
    preview,
    colors = {},
    oninspect,
    onselection
  }: {
    item: GenerationSegment;
    layer?: 'display' | 'speech';
    preview?: SpeechPreview;
    colors?: Record<string, string>;
    oninspect: (
      item: GenerationSegment,
      offset: number,
      anchor: HTMLButtonElement,
      activate: boolean
    ) => void;
    onselection?: (selection: SpeechSelection) => void;
  } = $props();
  const spoken = $derived(item.optimized_text ?? item.text);
  const text = $derived(layer === 'speech' ? spoken : item.text);
  const mapped = $derived(text === spoken);
  const spans = $derived(
    preview?.text === text ? preview.spans : annotationSpans(item)
  );
  const letters = $derived(Array.from(text));
  function description(span: SpeechSpan) {
    return [speakerLabel(span), span.voice, deliveryDescription(span.delivery)]
      .filter(Boolean)
      .join(' · ');
  }
  function selectionActions(node: HTMLElement) {
    function inspectSelection() {
      const selected = selectedSpeechRange(node, item, layer);
      if (selected) onselection?.(selected);
    }
    function keyboard(event: KeyboardEvent) {
      if (event.key === 'Shift') inspectSelection();
    }
    node.addEventListener('pointerup', inspectSelection);
    node.addEventListener('keyup', keyboard);
    return {
      destroy() {
        node.removeEventListener('pointerup', inspectSelection);
        node.removeEventListener('keyup', keyboard);
      }
    };
  }
</script>

<!-- Adjoining spans preserve the exact text when copied; symbols are interface-only. -->
<!-- prettier-ignore -->
<span class="annotated-text" tabindex="-1" data-speech-annotations={item.id} use:selectionActions>{#if mapped}{#each spans as span, index}<button
      type="button"
      class="speech-span"
      style:--speaker-color={colors[speakerColorKey(span)] ?? 'var(--speaker-narrator)'}
      class:character={span.role !== 'narrator'}
      class:directed={Boolean(deliveryDescription(span.delivery))}
      aria-label={`${description(span)}. Inspect voice and delivery for segment ${item.ordinal + 1}, phrase ${index + 1}`}
      aria-haspopup="dialog"
      onpointerenter={(event) => { if (event.pointerType === 'mouse') oninspect(item, span.start, event.currentTarget, false); }}
      onfocus={(event) => oninspect(item, span.start, event.currentTarget, false)}
      onclick={(event) => { event.stopPropagation(); if (!window.getSelection()?.isCollapsed) return; oninspect(item, span.start, event.currentTarget, true); }}
    >{letters.slice(span.start, span.end).join('')}</button>{/each}{:else}{text}{/if}<button
    type="button"
    class="inspect-speech"
    aria-label={`Inspect voices and delivery for segment ${item.ordinal + 1}`}
    aria-haspopup="dialog"
    title={mapped ? 'Voices and delivery' : 'Voices and delivery apply to the speech text'}
    onpointerenter={(event) => { if (event.pointerType === 'mouse') oninspect(item, 0, event.currentTarget, false); }}
    onfocus={(event) => oninspect(item, 0, event.currentTarget, false)}
    onclick={(event) => { event.stopPropagation(); oninspect(item, 0, event.currentTarget, true); }}
  ><AudioLines size={14} aria-hidden="true" /></button></span>

<style>
  .annotated-text {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .speech-span {
    display: inline;
    border: 0;
    padding: 0;
    background: none;
    color: inherit;
    font: inherit;
    text-align: inherit;
    white-space: inherit;
    cursor: pointer;
    text-decoration: underline solid var(--speaker-color);
    text-underline-offset: 0.22em;
    text-decoration-thickness: 2.5px;
  }
  .speech-span.character {
    text-decoration-style: solid;
    text-decoration-color: var(--speaker-color);
  }
  .speech-span.directed {
    text-decoration-style: double;
  }
  .speech-span:hover,
  .speech-span:focus-visible {
    background: var(--accent-soft);
    border-radius: 0.15em;
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  .inspect-speech {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.8em;
    min-height: 1.8em;
    margin-left: 0.2em;
    padding: 0;
    vertical-align: middle;
    border: 0;
    border-radius: 0.35em;
    color: var(--accent);
    background: var(--accent-soft);
    user-select: none;
  }
  .inspect-speech:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
</style>
