<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { SvelteMap } from 'svelte/reactivity';
  import {
    buildOffsets,
    selectVirtualWindow
  } from './generation-virtual-window';
  import { Flag, Play, RotateCcw, Trash2 } from '@lucide/svelte';
  import type { GenerationSegment } from './api-models';
  import type { GenerationSegmentChanges } from './domain-api';
  import type { ReadingBlock } from './generation-view-models';
  import SpeechBoundaryMarker from './SpeechBoundaryMarker.svelte';
  import PassageText from './PassageText.svelte';
  import SpeechAnnotationText from './SpeechAnnotationText.svelte';
  import {
    type SpeechSelection,
    type SpeechPreview
  } from './speech-annotations';
  import type { SpeechCapabilities } from './speech-support';
  import type { PassageBoundary, PassageTextLayer } from './passage-structure';
  import SegmentRegenerationMenu from './SegmentRegenerationMenu.svelte';

  let {
    blocks,
    scrollRoot = null,
    pinnedSegmentIds = [],
    selectedRunLabel,
    textMode,
    loaded,
    total,
    activePlayingId,
    selectedRows,
    loading,
    ontextmode,
    onactivate,
    onactivatekeyboard,
    ontext,
    onhasaudio,
    onplay,
    onregenerate,
    onregeneratewith,
    onpatch,
    onmerge,
    showPassageBoundaries = false,
    showSpeechAnnotations = false,
    multiVoice = false,
    speakerNames = {},
    detailsVisible = false,
    speechPreviews = {},
    speechCapabilities,
    performanceEnabled = true,
    vocalizationsEnabled = false,
    onspeech,
    onselection,
    onpassage,
    topologyDisabled = false
  }: {
    blocks: ReadingBlock[];
    scrollRoot?: HTMLElement | null;
    pinnedSegmentIds?: string[];
    selectedRunLabel?: string | null;
    textMode: 'display' | 'speech';
    loaded: number;
    total: number;
    activePlayingId: string;
    selectedRows: string[];
    loading: boolean;
    ontextmode: (mode: 'display' | 'speech') => void;
    onactivate: (
      event: MouseEvent | KeyboardEvent,
      item: GenerationSegment
    ) => void;
    onactivatekeyboard: (event: KeyboardEvent, item: GenerationSegment) => void;
    ontext: (item: GenerationSegment) => string;
    onhasaudio: (item: GenerationSegment) => boolean;
    onplay: (item: GenerationSegment) => unknown;
    onregenerate: (item: GenerationSegment) => unknown;
    onregeneratewith: (item: GenerationSegment) => unknown;
    onpatch: (
      item: GenerationSegment,
      changes: GenerationSegmentChanges
    ) => unknown;
    onmerge: (left: GenerationSegment, right: GenerationSegment) => unknown;
    showPassageBoundaries?: boolean;
    showSpeechAnnotations?: boolean;
    multiVoice?: boolean;
    speakerNames?: Record<string, string>;
    detailsVisible?: boolean;
    speechPreviews?: Record<string, SpeechPreview>;
    speechCapabilities?: SpeechCapabilities;
    performanceEnabled?: boolean;
    vocalizationsEnabled?: boolean;
    onspeech?: (
      item: GenerationSegment,
      offset: number,
      anchor: HTMLButtonElement,
      activate: boolean
    ) => void;
    onselection?: (selection: SpeechSelection) => void;
    onpassage?: (
      item: GenerationSegment,
      layer: PassageTextLayer,
      boundary: PassageBoundary,
      anchor?: HTMLButtonElement,
      activate?: boolean
    ) => void;
    topologyDisabled?: boolean;
  } = $props();
  let body = $state<HTMLDivElement>();
  let scrollTop = $state(0);
  let viewportHeight = $state(0);
  let focusedId = $state('');
  let revealId = $state('');
  let selectionIds = $state<string[]>([]);
  let frame = 0;
  let revealEpoch = 0;
  let observer: ResizeObserver | undefined;
  const nodes = new Map<string, HTMLElement>();
  const measured = new SvelteMap<string, number>();
  const virtualEnabled = $derived(loaded > 50);
  const blockBySegment = $derived(
    new Map(
      blocks.flatMap((block, index) =>
        block.items.map((item) => [item.id, index] as const)
      )
    )
  );
  const offsets = $derived(
    buildOffsets(
      blocks.map(
        (block) =>
          measured.get(block.key) ??
          Math.max(
            60,
            (block.items.reduce(
              (length, item) => length + ontext(item).length,
              0
            ) /
              85) *
              30 +
              24
          )
      )
    )
  );
  const selection = $derived(
    selectVirtualWindow(
      blocks.length,
      offsets,
      scrollTop,
      viewportHeight,
      2,
      [
        ...new Set([
          activePlayingId,
          selectedRows.at(-1) ?? '',
          focusedId,
          revealId,
          ...pinnedSegmentIds,
          ...selectionIds
        ])
      ]
        .map((id) => blockBySegment.get(id))
        .filter((index): index is number => index !== undefined)
    )
  );
  const visibleIndexes = $derived(
    virtualEnabled ? selection.indexes : blocks.map((_, index) => index)
  );

  function updateViewport() {
    if (!body || !scrollRoot) return;
    scrollTop = Math.max(
      0,
      scrollRoot.getBoundingClientRect().top - body.getBoundingClientRect().top
    );
    viewportHeight = scrollRoot.clientHeight;
  }
  function scheduleViewport() {
    if (frame) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      updateViewport();
    });
  }
  function measure(node: HTMLElement, key: string) {
    nodes.set(key, node);
    node.dataset.readingBlock = key;
    observer?.observe(node);
    return {
      destroy() {
        nodes.delete(key);
        observer?.unobserve(node);
      }
    };
  }
  function focused(event: FocusEvent) {
    focusedId =
      (event.target as HTMLElement)?.closest<HTMLElement>('[data-segment-id]')
        ?.dataset.segmentId ?? '';
  }
  function selected() {
    const range = document.getSelection();
    const id = (node: Node | null) =>
      (node instanceof Element
        ? node
        : node?.parentElement
      )?.closest<HTMLElement>('[data-segment-id]')?.dataset.segmentId ?? '';
    selectionIds =
      range && !range.isCollapsed
        ? [id(range.anchorNode), id(range.focusNode)]
        : [];
  }
  export async function scrollToSegment(id: string) {
    const epoch = ++revealEpoch;
    if (!blockBySegment.has(id)) return false;
    revealId = id;
    await tick();
    if (epoch !== revealEpoch) return false;
    const node = body?.querySelector<HTMLElement>(
      `[data-segment-id="${CSS.escape(id)}"]`
    );
    node?.scrollIntoView({ block: 'nearest', behavior: 'auto' });
    updateViewport();
    await tick();
    if (epoch === revealEpoch) revealId = '';
    return Boolean(node);
  }
  onMount(() => {
    observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const node = entry.target as HTMLElement;
        const key = node.dataset.readingBlock;
        const height = node.getBoundingClientRect().height;
        if (
          key &&
          height > 0 &&
          Math.abs((measured.get(key) ?? 0) - height) > 1
        )
          measured.set(key, height);
      }
      scheduleViewport();
    });
    for (const node of nodes.values()) observer.observe(node);
    document.addEventListener('selectionchange', selected);
    updateViewport();
    return () => {
      if (frame) cancelAnimationFrame(frame);
      observer?.disconnect();
      document.removeEventListener('selectionchange', selected);
    };
  });
  $effect(() => {
    if (!scrollRoot) return;
    const host = scrollRoot;
    host.addEventListener('scroll', scheduleViewport, { passive: true });
    const resize = new ResizeObserver(scheduleViewport);
    resize.observe(host);
    updateViewport();
    return () => {
      host.removeEventListener('scroll', scheduleViewport);
      resize.disconnect();
    };
  });
  $effect(() => {
    const keys = new Set(blocks.map((block) => block.key));
    for (const key of measured.keys()) if (!keys.has(key)) measured.delete(key);
  });
</script>

<div
  class="reading-view mx-auto max-w-4xl px-5 py-7 sm:px-8"
  data-testid="generation-reading-view"
  data-loaded-count={loaded}
  data-rendered-count={visibleIndexes.reduce(
    (count, index) => count + blocks[index].items.length,
    0
  )}
  data-virtualized={virtualEnabled}
  onfocusin={focused}
  onfocusout={(event) => {
    if (
      !(event.relatedTarget instanceof Node) ||
      !body?.contains(event.relatedTarget)
    )
      focusedId = '';
  }}
>
  <div
    class="mb-7 flex flex-wrap items-end justify-between gap-3 border-b border-[var(--line)] pb-4"
  >
    <div>
      <h3 class="mt-1 text-xl font-semibold">Narration text</h3>
      <p class="muted mt-1 text-xs">
        Reviewing {selectedRunLabel ?? 'the active takes'}. Display text is
        shown by default; speech text is never used for subtitle or read-along
        copy.
      </p>
    </div>
    <div class="flex flex-wrap items-center justify-end gap-2">
      <div class="view-switch font-sans" aria-label="Reading text source">
        <button
          onclick={() => ontextmode('display')}
          class:active={textMode === 'display'}>Display</button
        >
        <button
          onclick={() => ontextmode('speech')}
          class:active={textMode === 'speech'}>Speech</button
        >
      </div>
      <span class="muted text-xs">Loaded {loaded} of {total}</span>
    </div>
  </div>

  <div bind:this={body}>
    {#if virtualEnabled && selection.topGap > 0}<div
        aria-hidden="true"
        style:height={`${selection.topGap}px`}
      ></div>{/if}
    {#each visibleIndexes as blockIndex (blocks[blockIndex].key)}
      {@const block = blocks[blockIndex]}
      {#if virtualEnabled && selection.gaps.has(blockIndex)}<div
          aria-hidden="true"
          style:height={`${selection.gaps.get(blockIndex)}px`}
        ></div>{/if}
      <div use:measure={block.key} style="display: flow-root">
        {#if ['heading', 'chapter_marker'].includes(block.kind)}
          <h4
            class:now-playing={block.items.some(
              (item) => item.id === activePlayingId
            )}
            class:selected-heading={block.items.some((item) =>
              selectedRows.includes(item.id)
            )}
            class="reading-heading"
          >
            {#each block.items as item}
              <button
                data-segment-id={item.id}
                onclick={(event) => onactivate(event, item)}
                class:removed={item.removed}>{item.text}</button
              >
            {/each}
          </h4>
        {:else}
          <p class:continues={!block.closed} class="reading-paragraph">
            {#each block.items as item, index}
              <span
                data-segment-id={item.id}
                class:now-playing={item.id === activePlayingId}
                class:selected-sentence={selectedRows.includes(item.id)}
                class:removed={item.removed}
                class="reading-segment"
              >
                {#if showSpeechAnnotations && (item.speech_annotation_xml || item.speech_annotation || item.speech_plan?.speech_xml || speechPreviews[item.id])}
                  <SpeechAnnotationText
                    {item}
                    capabilities={speechCapabilities}
                    {performanceEnabled}
                    {vocalizationsEnabled}
                    {multiVoice}
                    {speakerNames}
                    {detailsVisible}
                    layer={textMode}
                    preview={speechPreviews[item.id]}
                    oninspect={onspeech ?? (() => {})}
                    {onselection}
                  />
                {:else if showPassageBoundaries && item.passage_structure?.layers[textMode].boundaries.length}
                  <PassageText
                    {item}
                    layer={textMode}
                    oninspect={onpassage ?? (() => {})}
                  />
                {:else}
                  <span
                    role="button"
                    tabindex="0"
                    onclick={(event) => onactivate(event, item)}
                    onkeydown={(event) => onactivatekeyboard(event, item)}
                    class="reading-sentence"
                    title={onhasaudio(item)
                      ? `Play segment ${item.ordinal + 1}`
                      : 'Select segment actions'}>{ontext(item)}</span
                  >
                {/if}
                <span
                  class="reading-actions"
                  aria-label={`Actions for segment ${item.ordinal + 1}`}
                >
                  <button
                    onclick={(event) => {
                      event.stopPropagation();
                      onplay(item);
                    }}
                    disabled={!onhasaudio(item) || item.removed}
                    title="Play segment"
                    aria-label={`Play segment ${item.ordinal + 1}`}
                    ><Play size={13} /></button
                  >
                  <SegmentRegenerationMenu
                    segmentNumber={item.ordinal + 1}
                    disabled={loading || item.removed}
                    compact
                    onregenerate={() => onregenerate(item)}
                    onregeneratewith={() => onregeneratewith(item)}
                  />
                  <button
                    onclick={(event) => {
                      event.stopPropagation();
                      onpatch(item, { marked: !item.marked });
                    }}
                    class:active={item.marked}
                    title={item.marked
                      ? 'Unmark segment'
                      : 'Mark for bulk regeneration'}
                    aria-label={item.marked
                      ? `Unmark segment ${item.ordinal + 1}`
                      : `Mark segment ${item.ordinal + 1}`}
                    ><Flag size={13} /></button
                  >
                  <button
                    onclick={(event) => {
                      event.stopPropagation();
                      onpatch(item, { removed: !item.removed });
                    }}
                    title={item.removed ? 'Restore segment' : 'Remove segment'}
                    aria-label={item.removed
                      ? `Restore segment ${item.ordinal + 1}`
                      : `Remove segment ${item.ordinal + 1}`}
                  >
                    {#if item.removed}<RotateCcw size={13} />{:else}<Trash2
                        size={13}
                      />{/if}
                  </button>
                </span>
              </span>{#if index < block.items.length - 1}
                <SpeechBoundaryMarker
                  left={item}
                  right={block.items[index + 1]}
                  disabled={topologyDisabled ||
                    item.ordinal + 1 !== block.items[index + 1].ordinal}
                  {onmerge}
                />
                <span aria-hidden="true"> </span>
              {/if}
            {/each}
          </p>
        {/if}
      </div>
    {/each}
    {#if virtualEnabled && selection.bottomGap > 0}<div
        aria-hidden="true"
        style:height={`${selection.bottomGap}px`}
      ></div>{/if}
  </div>

  {#if !blocks.length}
    <p class="muted py-16 text-center">No segments match this filter.</p>
  {/if}
</div>

<style>
  .view-switch {
    display: flex;
    border: 1px solid var(--line);
    border-radius: 0.6rem;
    background: var(--paper);
    padding: 0.15rem;
  }
  .view-switch button {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    border-radius: 0.45rem;
    padding: 0.3rem 0.5rem;
    font-size: 0.68rem;
    font-weight: 700;
    color: var(--muted);
  }
  .view-switch button.active {
    background: var(--accent-soft);
    color: var(--ink);
  }
  .reading-view {
    font-family: Georgia, 'Times New Roman', serif;
  }
  .reading-heading {
    margin: 2rem 0 0.75rem;
    font-size: 1.32rem;
    font-weight: 700;
    line-height: 1.35;
  }
  .reading-heading button {
    border-radius: 0.3rem;
    text-align: left;
  }
  .reading-heading.selected-heading button {
    background: var(--accent-soft);
  }
  .reading-heading.now-playing button {
    color: var(--accent);
  }
  .reading-heading button.removed {
    text-decoration: line-through;
    opacity: 0.42;
  }
  .reading-paragraph {
    margin: 0 0 1.2rem;
    font-size: 1.02rem;
    line-height: 1.9;
    white-space: normal;
  }
  .reading-paragraph.continues {
    margin-bottom: 0;
  }
  .reading-segment {
    position: relative;
    display: inline;
  }
  .reading-sentence {
    display: inline;
    white-space: normal;
    border-radius: 0.28rem;
    cursor: pointer;
    text-align: left;
    transition:
      background 0.12s ease,
      color 0.12s ease;
  }
  .reading-sentence:focus-visible {
    outline: 3px solid color-mix(in srgb, var(--accent) 38%, transparent);
    outline-offset: 2px;
  }
  .reading-segment:hover .reading-sentence,
  .reading-segment:focus-within .reading-sentence,
  .reading-segment.selected-sentence .reading-sentence {
    background: var(--accent-soft);
  }
  .reading-segment.now-playing .reading-sentence {
    background: var(--action-bg);
    color: white;
    box-shadow: 0 0 0 0.16rem color-mix(in srgb, var(--accent) 18%, transparent);
  }
  .reading-segment.removed .reading-sentence {
    text-decoration: line-through;
    opacity: 0.42;
  }
  .reading-actions {
    position: absolute;
    bottom: calc(100% + 0.32rem);
    left: 50%;
    z-index: 25;
    display: flex;
    gap: 0.18rem;
    border: 1px solid var(--line);
    border-radius: 0.65rem;
    background: var(--paper-strong);
    padding: 0.22rem;
    box-shadow: var(--shadow);
    opacity: 0;
    pointer-events: none;
    transform: translate(-50%, 0.25rem);
    transition:
      opacity 0.12s ease,
      transform 0.12s ease;
  }
  .reading-segment:hover .reading-actions,
  .reading-segment:focus-within .reading-actions,
  .reading-segment:has(:global(.regenerate-trigger[aria-expanded='true']))
    .reading-actions {
    opacity: 1;
    pointer-events: auto;
    transform: translate(-50%, 0);
  }
  .reading-actions button {
    display: grid;
    height: 1.8rem;
    width: 1.8rem;
    place-items: center;
    border-radius: 0.45rem;
    color: var(--muted);
  }
  .reading-actions button:hover:not(:disabled),
  .reading-actions button:focus-visible,
  .reading-actions button.active {
    background: var(--accent-soft);
    color: var(--accent);
  }
  .reading-actions button:disabled {
    opacity: 0.35;
  }
</style>
