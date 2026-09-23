<script lang="ts">
  import {
    Pencil,
    GitMerge,
    RotateCcw,
    Scissors,
    Trash2,
    WandSparkles
  } from '@lucide/svelte';
  import type { GenerationSegment } from './api-models';
  import type { GenerationSegmentChanges } from './domain-api';
  import type { PlayableTake } from './generation-view-models';
  import type { SettingOption } from './settings-fields';
  import type { VoiceDescriptor } from './voice-catalog';
  import SegmentAudioPreview from './SegmentAudioPreview.svelte';
  import { tick, onMount } from 'svelte';
  import { SvelteMap } from 'svelte/reactivity';
  import SpeechBoundaryMarker from './SpeechBoundaryMarker.svelte';
  import PassageText from './PassageText.svelte';
  import SpeechAnnotationText from './SpeechAnnotationText.svelte';
  import {
    textareaSpeechRange,
    speakerColors,
    type SpeechSelection,
    type SpeechPreview
  } from './speech-annotations';
  import type { PassageBoundary, PassageTextLayer } from './passage-structure';
  import SegmentRegenerationMenu from './SegmentRegenerationMenu.svelte';
  import SegmentOptionsMenu from './SegmentOptionsMenu.svelte';
  import {
    selectVirtualWindow,
    slotHeights,
    buildOffsets
  } from './generation-virtual-window';

  let {
    items,
    selectedRows,
    loading,
    speechOptionsLoading,
    selectedTtsServiceName,
    selectedTtsModel,
    inheritedVoice,
    inheritedLanguage,
    onselect,
    onpatch,
    onreview,
    onvoices,
    onvoicelabel,
    onlanguages,
    onlanguagelabel,
    onlanguagechange,
    onactivetake,
    onselecttake,
    ontakelabel,
    onverificationtitle,
    onregenerate,
    onregeneratewith,
    onmerge,
    onsplit,
    compactRows = false,
    showPassageBoundaries = false,
    showSpeechAnnotations = false,
    speechPreviews = {},
    onspeech,
    onselection,
    onpassage,
    topologyDisabled = false,
    textMode = 'display',
    previewSegmentId = '',
    onpreviewrequest,
    scrollRoot = null,
    overscan = 8,
    estimatedRowHeight = 280,
    boundaryRowHeight = 13,
    virtualizeThreshold = 50,
    activeSegmentId = '',
    onshowallchange,
    showAll = $bindable(false)
  }: {
    items: GenerationSegment[];
    selectedRows: string[];
    loading: boolean;
    speechOptionsLoading: boolean;
    selectedTtsServiceName?: string | null;
    selectedTtsModel: string;
    inheritedVoice: string;
    inheritedLanguage: string;
    onselect: (item: GenerationSegment, event: MouseEvent) => void;
    onpatch: (
      item: GenerationSegment,
      changes: GenerationSegmentChanges
    ) => unknown;
    onreview: (item: GenerationSegment) => void;
    onvoices: (item: GenerationSegment) => VoiceDescriptor[];
    onvoicelabel: (voice: VoiceDescriptor) => string;
    onlanguages: (item: GenerationSegment) => SettingOption[];
    onlanguagelabel: (value: string) => string;
    onlanguagechange: (item: GenerationSegment, language: string) => unknown;
    onactivetake: (item: GenerationSegment) => PlayableTake | undefined;
    onselecttake: (item: GenerationSegment, takeId: string) => unknown;
    ontakelabel: (take: GenerationSegment['takes'][number]) => string;
    onverificationtitle: (take: GenerationSegment['takes'][number]) => string;
    onregenerate: (item: GenerationSegment) => unknown;
    onregeneratewith: (item: GenerationSegment) => unknown;
    onmerge: (left: GenerationSegment, right: GenerationSegment) => unknown;
    onsplit: (
      item: GenerationSegment,
      textLayer: 'display' | 'speech',
      cursor: number
    ) => unknown;
    previewSegmentId?: string;
    onpreviewrequest?: (item: GenerationSegment) => void;
    compactRows?: boolean;
    showPassageBoundaries?: boolean;
    showSpeechAnnotations?: boolean;
    speechPreviews?: Record<string, SpeechPreview>;
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
    textMode?: 'display' | 'speech';
    /** Explicit scroll host. Defaults to the closest scrollable ancestor. */
    scrollRoot?: HTMLElement | null;
    /** Rendered rows kept above/below the viewport on each side. */
    overscan?: number;
    /** Slot height estimate until a row is measured. */
    estimatedRowHeight?: number;
    /** Height folded into each slot for its boundary row. */
    boundaryRowHeight?: number;
    /** Item counts at or below this render fully (no virtualization). */
    virtualizeThreshold?: number;
    /** Playing row pinned alongside the preview row (playlist controller). */
    activeSegmentId?: string;
    onshowallchange?: (showAll: boolean) => void;
    /** Accessible non-virtualized fallback; drawer may bind to own it. */
    showAll?: boolean;
  } = $props();
  const annotationColors = $derived(speakerColors(items, speechPreviews));

  type CursorState = {
    layer: 'display' | 'speech';
    offset: number;
    value: string;
  };

  let cursorBySegment = $state<Record<string, CursorState>>({});
  let editingPassageId = $state('');
  function inspectSelection(
    node: HTMLTextAreaElement,
    item: GenerationSegment,
    layer: 'display' | 'speech'
  ) {
    const selected = textareaSpeechRange(node, item, layer);
    if (selected) onselection?.(selected);
  }

  function codePointOffset(value: string, codeUnitOffset: number) {
    return Array.from(value.slice(0, codeUnitOffset)).length;
  }

  function rememberCursor(
    item: GenerationSegment,
    layer: 'display' | 'speech',
    node: HTMLTextAreaElement
  ) {
    cursorBySegment[item.id] = {
      layer,
      offset: codePointOffset(node.value, node.selectionStart ?? 0),
      value: node.value
    };
  }

  function storedLayerText(
    item: GenerationSegment,
    layer: 'display' | 'speech'
  ) {
    return layer === 'speech'
      ? String(item.optimized_text ?? item.text)
      : String(item.text);
  }

  function validCursor(item: GenerationSegment) {
    const cursor = cursorBySegment[item.id];
    if (!cursor || cursor.layer !== textMode) return false;
    const stored = storedLayerText(item, cursor.layer);
    return (
      cursor.value === stored &&
      cursor.offset > 0 &&
      cursor.offset < Array.from(stored).length &&
      Array.from(stored).slice(0, cursor.offset).join('').trim().length > 0 &&
      Array.from(stored).slice(cursor.offset).join('').trim().length > 0
    );
  }

  function splitTitle(item: GenerationSegment) {
    if (topologyDisabled)
      return 'Return to the current plan to edit its blocks';
    const cursor = cursorBySegment[item.id];
    if (!cursor || cursor.layer !== textMode)
      return 'Place the text cursor where this block should split';
    if (cursor.value !== storedLayerText(item, cursor.layer))
      return 'Save the text edit first, then place the cursor again';
    if (!validCursor(item)) return 'Place the cursor between non-empty text';
    return `Split ${textMode} text at cursor`;
  }

  function autoExpand(node: HTMLTextAreaElement) {
    if (CSS.supports('field-sizing', 'content')) return;

    let frame: number | undefined;
    const adjust = () => {
      if (frame !== undefined) cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        frame = undefined;
        node.style.height = 'auto';
        node.style.height = `${Math.max(node.scrollHeight, 36)}px`;
      });
    };
    adjust();
    node.addEventListener('input', adjust);
    return {
      update() {
        adjust();
      },
      destroy() {
        if (frame !== undefined) cancelAnimationFrame(frame);
        node.removeEventListener('input', adjust);
      }
    };
  }

  // ---- Viewport virtualization (bounded DOM, variable heights) ----
  //
  // The drawer can load hundreds of segments; rendering every row costs
  // tens of thousands of DOM nodes. Only the scrolled window plus overscan
  // is mounted. Window math lives in generation-virtual-window.ts (unit
  // tested under plain node); this component owns scroll listeners,
  // measured row heights, and rendering with top/bottom spacer rows so the
  // scrollbar and total counts keep loaded-data semantics.
  let tableEl = $state<HTMLElement | null>(null);
  let tbodyEl = $state<HTMLElement | null>(null);
  let hostEl = $state<HTMLElement | null>(null);
  let scrollPos = $state(0);
  let viewportH = $state(0);
  let focusedSegmentId = $state('');
  let revealId = $state('');
  // Reveal epoch: rapid ArrowDown/search calls overlap the async reveal
  // below. Each call takes a token; stale calls bail before touching the
  // viewport, and only the newest call may clear the pin.
  let revealToken = 0;
  let selectedSegmentId = $state('');
  // SvelteMap: plain Map.set/delete is not reactive under Svelte 5 runes,
  // so measurements must live in a reactive map for offsets to recompute.
  const measured = new SvelteMap<string, number>();
  let rowObserver: ResizeObserver | undefined;
  // Actions run before onMount creates the observer: park nodes here so the
  // first viewport still gets measured instead of staying estimated.
  const pendingMeasureNodes = new Map<string, HTMLTableRowElement>();
  let scrollRaf = 0;

  const virtualEnabled = $derived(
    items.length > virtualizeThreshold && !showAll
  );

  const indexById = $derived(
    new Map(items.map((item, index) => [item.id, index] as const))
  );

  const pinnedIndexes = $derived.by(() => {
    const ids = [
      previewSegmentId,
      activeSegmentId,
      editingPassageId,
      focusedSegmentId,
      selectedSegmentId,
      revealId
    ];
    const pinned = new Set<number>();
    for (const id of ids) {
      if (!id) continue;
      const index = indexById.get(id);
      if (index !== undefined) pinned.add(index);
    }
    return [...pinned];
  });

  const offsets = $derived(
    buildOffsets(
      slotHeights(
        items.length,
        (index) => measured.get(items[index].id),
        estimatedRowHeight,
        compactRows ? 0 : boundaryRowHeight
      )
    )
  );

  const selection = $derived(
    selectVirtualWindow(
      items.length,
      offsets,
      scrollPos,
      viewportH,
      overscan,
      virtualEnabled ? pinnedIndexes : []
    )
  );

  const visibleIndexes = $derived(
    virtualEnabled ? selection.indexes : items.map((_, index) => index)
  );

  function findScrollHost(node: HTMLElement | null): HTMLElement | null {
    let ancestor = node?.parentElement ?? null;
    while (ancestor) {
      const overflowY = getComputedStyle(ancestor).overflowY;
      if (overflowY === 'auto' || overflowY === 'scroll') return ancestor;
      ancestor = ancestor.parentElement;
    }
    return null;
  }

  function updateViewport() {
    if (!tbodyEl) return;
    const host = scrollRoot ?? hostEl;
    if (host) {
      const hostRect = host.getBoundingClientRect();
      const bodyRect = tbodyEl.getBoundingClientRect();
      scrollPos = Math.max(0, hostRect.top - bodyRect.top);
      viewportH = host.clientHeight;
    } else if (typeof window !== 'undefined') {
      scrollPos = Math.max(0, -tbodyEl.getBoundingClientRect().top);
      viewportH = window.innerHeight;
    }
  }

  function handleViewportScroll() {
    if (scrollRaf) return;
    scrollRaf = requestAnimationFrame(() => {
      scrollRaf = 0;
      updateViewport();
    });
  }

  function observeRowNode(id: string, node: HTMLTableRowElement) {
    node.dataset.measuredId = id;
    if (rowObserver) rowObserver.observe(node, { box: 'border-box' });
    else pendingMeasureNodes.set(id, node);
  }

  function forgetRowNode(node: HTMLTableRowElement) {
    rowObserver?.unobserve(node);
    const id = node.dataset.measuredId;
    if (id) pendingMeasureNodes.delete(id);
  }

  function measureRow(node: HTMLTableRowElement, id: string) {
    observeRowNode(id, node);
    return {
      update(next: string) {
        if (node.dataset.measuredId !== next) {
          forgetRowNode(node);
          observeRowNode(next, node);
        }
      },
      destroy() {
        forgetRowNode(node);
      }
    };
  }

  function rowIdOf(element: Element | null): string {
    const row = element?.closest?.('tr[data-segment-id]');
    return row?.getAttribute('data-segment-id') ?? '';
  }

  function handleSelectionChange() {
    // Pin the row holding a live text selection so scrolling cannot unmount
    // it mid-select/copy. Textarea selections are not part of the document
    // selection, so check the focused field first, then DOM ranges.
    const active = document.activeElement as
      HTMLTextAreaElement | HTMLInputElement | null;
    if (
      active &&
      tableEl?.contains(active) &&
      typeof active.selectionStart === 'number' &&
      typeof active.selectionEnd === 'number' &&
      active.selectionStart !== active.selectionEnd
    ) {
      const id = rowIdOf(active);
      if (id) {
        selectedSegmentId = id;
        return;
      }
    }
    const domSelection = document.getSelection();
    if (
      domSelection &&
      !domSelection.isCollapsed &&
      domSelection.rangeCount > 0 &&
      tableEl?.contains(domSelection.anchorNode?.parentElement ?? null)
    ) {
      const id = rowIdOf(
        domSelection.anchorNode instanceof Element
          ? domSelection.anchorNode
          : (domSelection.anchorNode?.parentElement ?? null)
      );
      if (id) {
        selectedSegmentId = id;
        return;
      }
    }
    if (selectedSegmentId) selectedSegmentId = '';
  }

  function handleFocusIn(event: FocusEvent) {
    const row = (event.target as HTMLElement | null)?.closest?.(
      'tr[data-segment-id]'
    );
    const id = row?.getAttribute('data-segment-id');
    if (id) focusedSegmentId = id;
  }

  function handleFocusOut(event: FocusEvent) {
    const next = event.relatedTarget as HTMLElement | null;
    if (!next || !tableEl?.contains(next)) focusedSegmentId = '';
  }

  function setShowAll(value: boolean) {
    showAll = value;
    onshowallchange?.(value);
  }

  // Reveal API for the drawer (keyboard arrows, playlist navigation):
  // call scrollToSegment(id) BEFORE falling back to querySelector, because
  // offscreen rows are not mounted until revealed. The target row is pinned
  // while scrolling so measurement passes cannot unmount it mid-reveal.
  // Returns false when the id is not loaded.
  export async function scrollToSegment(
    segmentId: string,
    options: {
      align?: 'center' | 'nearest' | 'start' | 'end';
      focus?: string | boolean;
    } = {}
  ): Promise<boolean> {
    const index = indexById.get(segmentId);
    if (index === undefined) return false;
    const { align = 'center', focus } = options;
    const token = (revealToken += 1);
    revealId = segmentId;
    const current = () => token === revealToken;
    try {
      if (virtualEnabled) {
        const host = scrollRoot ?? hostEl ?? findScrollHost(tableEl);
        const rowTop = offsets[index];
        const rowHeight = offsets[index + 1] - offsets[index];
        const view = viewportH || 600;
        let target: number;
        if (align === 'start') target = rowTop;
        else if (align === 'end') target = rowTop + rowHeight - view;
        else if (align === 'nearest') {
          const min = rowTop + rowHeight - view;
          target =
            scrollPos < min ? min : scrollPos > rowTop ? rowTop : scrollPos;
        } else target = rowTop + rowHeight / 2 - view / 2;
        if (host) host.scrollTop = Math.max(0, target);
        else if (typeof window !== 'undefined')
          window.scrollTo({
            top: window.scrollY + target - scrollPos
          });
        updateViewport();
        await tick();
        await new Promise((resolve) => requestAnimationFrame(resolve));
        await tick();
        // A newer reveal started while awaiting: stop before moving the
        // viewport back or focusing a stale row.
        if (!current()) return false;
      }
      const row = tbodyEl?.querySelector(
        `tr[data-segment-id="${segmentId.replace(/"/g, '\\"')}"]`
      );
      if (!current()) return false;
      (row as HTMLElement | null)?.scrollIntoView({
        block:
          align === 'center'
            ? 'center'
            : align === 'nearest'
              ? 'nearest'
              : align,
        behavior: 'auto'
      });
      if (!current()) return Boolean(row);
      if (typeof focus === 'string')
        (row?.querySelector(focus) as HTMLElement | null)?.focus({
          preventScroll: true
        });
      else if (focus) (row as HTMLElement | null)?.focus?.();
      return Boolean(row);
    } finally {
      // Only the newest reveal clears the pin; a stale finally must not
      // unmount the row a newer call just pinned.
      if (current()) revealId = '';
    }
  }

  // Profiler/test hook: ids currently mounted (window union pinned rows).
  export function getRenderedSegmentIds(): string[] {
    return visibleIndexes
      .map((index) => items[index]?.id)
      .filter((id): id is string => Boolean(id));
  }

  $effect(() => {
    // Bound the measurement cache across corpus reloads.
    const ids = new Set(items.map((item) => item.id));
    if (measured.size > ids.size + 50) {
      for (const key of measured.keys()) {
        if (!ids.has(key)) measured.delete(key);
      }
    }
  });

  onMount(() => {
    rowObserver = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const id = (entry.target as HTMLElement).dataset.measuredId;
        if (!id) continue;
        // Border-box: the spacer math must account for padding/border, not
        // just content height.
        const box = Array.isArray(entry.borderBoxSize)
          ? entry.borderBoxSize[0]
          : undefined;
        const height = Math.round(
          box?.blockSize ??
            (entry.target as HTMLElement).getBoundingClientRect().height
        );
        if (height > 0 && Math.abs((measured.get(id) ?? 0) - height) > 1) {
          measured.set(id, height);
        }
      }
    });
    // Actions parked rows here before the observer existed: pick them up so
    // the first viewport measures instead of staying estimated.
    for (const [id, node] of pendingMeasureNodes) {
      if (node.isConnected) {
        node.dataset.measuredId = id;
        rowObserver.observe(node, { box: 'border-box' });
      }
    }
    pendingMeasureNodes.clear();
    document.addEventListener('selectionchange', handleSelectionChange);
    updateViewport();
    return () => {
      if (scrollRaf) cancelAnimationFrame(scrollRaf);
      document.removeEventListener('selectionchange', handleSelectionChange);
      pendingMeasureNodes.clear();
      rowObserver?.disconnect();
      rowObserver = undefined;
    };
  });

  $effect(() => {
    // Resolve lazily so late drawer layout still finds its scroll host.
    if (!scrollRoot && !hostEl && tableEl) {
      hostEl = findScrollHost(tableEl);
    }
  });

  $effect(() => {
    // Scroll/resize listeners follow the resolved host (explicit prop wins).
    // Host layout changes (drawer panels opening, window zoom) do not fire
    // window resize, so the host gets its own ResizeObserver.
    const host = scrollRoot ?? hostEl;
    host?.addEventListener('scroll', handleViewportScroll, { passive: true });
    window.addEventListener('resize', handleViewportScroll);
    const hostResize = new ResizeObserver(handleViewportScroll);
    if (host) hostResize.observe(host);
    if (!host)
      window.addEventListener('scroll', handleViewportScroll, {
        passive: true
      });
    updateViewport();
    return () => {
      host?.removeEventListener('scroll', handleViewportScroll);
      window.removeEventListener('resize', handleViewportScroll);
      if (!host) window.removeEventListener('scroll', handleViewportScroll);
      hostResize.disconnect();
    };
  });
</script>

<table
  bind:this={tableEl}
  data-testid="generation-segment-table"
  data-loaded-count={items.length}
  data-rendered-count={visibleIndexes.length}
  data-virtualized={virtualEnabled ? 'true' : 'false'}
  aria-rowcount={items.length
    ? items.length * (compactRows ? 1 : 2) + (compactRows ? 1 : 0)
    : 1}
  onfocusin={handleFocusIn}
  onfocusout={handleFocusOut}
  class="w-full table-fixed border-collapse text-sm"
>
  <caption class="sr-only">
    Generation segments: showing {visibleIndexes.length} of {items.length} loaded
    rows{virtualEnabled ? ' (virtualized)' : ''}.
  </caption>
  <thead class="sticky top-0 z-10 bg-[var(--paper-strong)]">
    <tr aria-rowindex={1}>
      <th class="w-12">Mark</th>
      <th class="w-14">#</th>
      <th class="text-left">Generation text and delivery</th>
      <th class="w-52">Audio take</th>
      <th class="w-24">Status</th>
    </tr>
  </thead>
  <tbody bind:this={tbodyEl}>
    {#if virtualEnabled && selection.topGap > 0}
      <tr class="virtual-spacer" aria-hidden="true">
        <td
          colspan="5"
          style="height: {Math.round(
            selection.topGap
          )}px; padding: 0; border: 0;"
        ></td>
      </tr>
    {/if}
    {#each visibleIndexes as vi (items[vi].id)}
      {@const item = items[vi]}
      {@const itemIndex = vi}
      {@const gapHeight = virtualEnabled ? (selection.gaps.get(vi) ?? 0) : 0}
      {#if gapHeight > 0}
        <tr class="virtual-spacer" aria-hidden="true">
          <td
            colspan="5"
            style="height: {Math.round(gapHeight)}px; padding: 0; border: 0;"
          ></td>
        </tr>
      {/if}
      {@const selectedTake = onactivetake(item)}
      {@const hasPassages = Boolean(
        item.passage_structure?.layers[textMode].boundaries.length
      )}
      {#if itemIndex > 0 && !compactRows}
        <tr class="boundary-row" aria-rowindex={itemIndex * 2 + 1}>
          <td colspan="5">
            <SpeechBoundaryMarker
              left={items[itemIndex - 1]}
              right={item}
              compact
              disabled={topologyDisabled ||
                items[itemIndex - 1].ordinal + 1 !== item.ordinal}
              {onmerge}
            />
          </td>
        </tr>
      {/if}
      <tr
        use:measureRow={item.id}
        onclick={(event) => onselect(item, event)}
        class:selected={selectedRows.includes(item.id)}
        class:removed={item.removed}
        data-segment-id={item.id}
        data-segment-ordinal={item.ordinal}
        aria-rowindex={itemIndex * (compactRows ? 1 : 2) + 2}
      >
        <td>
          <input
            type="checkbox"
            class="mark-toggle"
            checked={item.marked}
            aria-label={`Mark segment ${item.ordinal + 1}`}
            onclick={(event) => event.stopPropagation()}
            onchange={(event) =>
              onpatch(item, { marked: event.currentTarget.checked })}
          />
        </td>
        <td class="muted font-mono text-xs">{item.ordinal + 1}</td>
        <td class="narrative-cell">
          {#if item.speaker}
            <span
              class="mb-1 inline-flex rounded-full bg-[var(--accent-soft)] px-2 py-0.5 text-[.62rem] font-semibold text-[var(--accent)]"
              >{item.speaker}</span
            >
          {/if}
          {#if showSpeechAnnotations && (item.speech_annotation_xml || item.speech_plan?.speech_xml || speechPreviews[item.id]) && editingPassageId !== item.id}
            <div class="passage-row p-2 text-sm leading-relaxed">
              <SpeechAnnotationText
                {item}
                colors={annotationColors}
                layer={textMode}
                preview={speechPreviews[item.id]}
                oninspect={onspeech ?? (() => {})}
                {onselection}
              />
              <button
                type="button"
                class="passage-edit"
                aria-label={`Edit text for segment ${item.ordinal + 1}`}
                title="Edit text"
                onclick={async (event) => {
                  event.stopPropagation();
                  const cell = event.currentTarget.closest('td');
                  editingPassageId = item.id;
                  await tick();
                  cell?.querySelector('textarea')?.focus();
                }}><Pencil size={14} /></button
              >
            </div>
          {:else if showPassageBoundaries && hasPassages && editingPassageId !== item.id}
            <div class="passage-row p-2 text-sm leading-relaxed">
              <PassageText
                {item}
                layer={textMode}
                oninspect={onpassage ?? (() => {})}
              />
              <button
                type="button"
                class="passage-edit"
                aria-label={`Edit text for segment ${item.ordinal + 1}`}
                title="Edit text"
                onclick={async (event) => {
                  event.stopPropagation();
                  const cell = event.currentTarget.closest('td');
                  editingPassageId = item.id;
                  await tick();
                  cell?.querySelector('textarea')?.focus();
                }}><Pencil size={14} /></button
              >
            </div>
          {:else}
            {#if textMode === 'speech'}
              <div
                class="mb-1 flex items-center gap-1.5 text-[.65rem] font-medium text-[var(--accent)]"
              >
                <span class="rounded bg-[var(--accent-soft)] px-1.5 py-0.5"
                  >Spoken override (TTS only)</span
                >
                <span class="muted max-w-md truncate">
                  {item.optimized_text
                    ? 'Subtitles stay unchanged.'
                    : 'Starts from the script; edits affect speech only.'}
                </span>
                {#if item.optimized_text}
                  <button
                    type="button"
                    class="muted ml-auto underline decoration-dotted underline-offset-2"
                    onclick={(event) => {
                      event.stopPropagation();
                      onpatch(item, { optimized_text: null });
                    }}>Reset to script</button
                  >
                {/if}
              </div>
              <textarea
                use:autoExpand
                value={item.optimized_text ?? item.text}
                aria-label={`Spoken override for segment ${item.ordinal + 1}`}
                data-generation-search-index={itemIndex}
                onselect={(event) =>
                  rememberCursor(item, 'speech', event.currentTarget)}
                onkeyup={(event) => {
                  rememberCursor(item, 'speech', event.currentTarget);
                  if (event.key === 'Shift')
                    inspectSelection(event.currentTarget, item, 'speech');
                }}
                onpointerup={(event) =>
                  inspectSelection(event.currentTarget, item, 'speech')}
                oninput={(event) =>
                  rememberCursor(item, 'speech', event.currentTarget)}
                onclick={(event) => {
                  event.stopPropagation();
                  rememberCursor(item, 'speech', event.currentTarget);
                }}
                onblur={(event) => {
                  const text = event.currentTarget.value.trim();
                  if (!text) {
                    event.currentTarget.value = item.text;
                    if (item.optimized_text)
                      onpatch(item, { optimized_text: null });
                    return;
                  }
                  const current = (item.optimized_text ?? item.text).trim();
                  if (text !== current) onpatch(item, { optimized_text: text });
                }}
                rows="1"
                class="segment-text w-full rounded-lg border border-[var(--accent-soft)] bg-transparent p-2 focus:border-[var(--accent)]"
              ></textarea>
            {:else}
              <textarea
                use:autoExpand
                value={item.text}
                aria-label={`Script text for segment ${item.ordinal + 1}`}
                data-generation-search-index={itemIndex}
                onselect={(event) =>
                  rememberCursor(item, 'display', event.currentTarget)}
                onkeyup={(event) => {
                  rememberCursor(item, 'display', event.currentTarget);
                  if (event.key === 'Shift')
                    inspectSelection(event.currentTarget, item, 'display');
                }}
                onpointerup={(event) =>
                  inspectSelection(event.currentTarget, item, 'display')}
                oninput={(event) =>
                  rememberCursor(item, 'display', event.currentTarget)}
                onclick={(event) => {
                  event.stopPropagation();
                  rememberCursor(item, 'display', event.currentTarget);
                }}
                onblur={(event) => {
                  const text = event.currentTarget.value.trim();
                  if (text !== item.text.trim()) onpatch(item, { text });
                }}
                rows="1"
                class="segment-text w-full rounded-lg border border-transparent bg-transparent p-2 focus:border-[var(--line)]"
              ></textarea>
              {#if item.optimized_text && item.optimized_text !== item.text}
                <p
                  class="muted mt-0.5 mb-1 truncate text-[.65rem]"
                  title={`Spoken: ${item.optimized_text}`}
                >
                  <span class="font-medium text-[var(--accent)]">Spoken:</span>
                  {item.optimized_text}
                </p>
              {/if}
            {/if}
            {#if (showSpeechAnnotations || (showPassageBoundaries && hasPassages)) && editingPassageId === item.id}
              <button
                type="button"
                class="muted mb-2 text-xs underline underline-offset-2"
                onclick={(event) => {
                  event.stopPropagation();
                  editingPassageId = '';
                }}>Return to annotated text</button
              >
            {/if}
          {/if}
          <div
            class="segment-actions flex min-w-0 flex-wrap items-center justify-end gap-1"
          >
            <div
              class="inline-flex shrink-0 items-center gap-1"
              role="group"
              aria-label={`Actions for segment ${item.ordinal + 1}`}
            >
              <button
                onmousedown={(event) => event.preventDefault()}
                onclick={(event) => {
                  event.stopPropagation();
                  const cursor = cursorBySegment[item.id];
                  if (cursor && validCursor(item))
                    onsplit(item, cursor.layer, cursor.offset);
                }}
                disabled={loading || topologyDisabled || !validCursor(item)}
                class="action icon-action"
                title={splitTitle(item)}
                aria-label={`Split segment ${item.ordinal + 1} at text cursor`}
              >
                <Scissors size={14} />
              </button>
              <SegmentRegenerationMenu
                segmentNumber={item.ordinal + 1}
                disabled={loading || item.removed}
                onregenerate={() => onregenerate(item)}
                onregeneratewith={() => onregeneratewith(item)}
              />
              <button
                onclick={(event) => {
                  event.stopPropagation();
                  onpatch(item, { removed: !item.removed });
                }}
                class="action icon-action"
                aria-label={item.removed ? 'Restore segment' : 'Remove segment'}
              >
                {#if item.removed}<RotateCcw size={14} />{:else}<Trash2
                    size={14}
                  />{/if}
              </button>
              <button
                type="button"
                class="action icon-action"
                aria-label={`Merge segment ${item.ordinal + 1} with next`}
                title="Merge with next segment"
                disabled={loading ||
                  topologyDisabled ||
                  item.removed ||
                  !items[itemIndex + 1] ||
                  items[itemIndex + 1].removed ||
                  items[itemIndex + 1].ordinal !== item.ordinal + 1}
                onclick={(event) => {
                  event.stopPropagation();
                  const next = items[itemIndex + 1];
                  if (next && next.ordinal === item.ordinal + 1)
                    onmerge(item, next);
                }}><GitMerge size={14} /></button
              >
              <SegmentOptionsMenu segmentNumber={item.ordinal + 1}>
                {#if item.optimized_text || selectedTake?.llm_optimized}
                  <button
                    onclick={(event) => {
                      event.stopPropagation();
                      onreview(item);
                    }}
                    class="mb-2 flex max-w-full items-center gap-1.5 rounded-lg bg-[var(--accent-soft)] px-2.5 py-1.5 text-left text-[.68rem] font-semibold text-[var(--accent)]"
                  >
                    <WandSparkles size={12} />
                    <span class="truncate"
                      >{item.speech_plan?.version
                        ? 'Review speech plan'
                        : 'Compare speech optimization'}</span
                    >
                    <span
                      class="rounded-full bg-[var(--paper)] px-1.5 py-0.5 text-[.58rem] uppercase"
                      >{item.speech_plan?.mode_used ??
                        item.optimization_status ??
                        'generated'}</span
                    >
                    {#if item.speech_plan?.proposals?.length}
                      <span
                        class="rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[.58rem] uppercase text-amber-700"
                        >{item.speech_plan.proposals.length} proposed</span
                      >
                    {/if}
                  </button>
                {/if}

                {#if onspeech && !showSpeechAnnotations}<button
                    type="button"
                    class="mini"
                    aria-label={`Inspect voices and delivery for segment ${item.ordinal + 1}`}
                    onclick={(event) => {
                      event.stopPropagation();
                      onspeech?.(item, 0, event.currentTarget, true);
                    }}>Voices &amp; delivery</button
                  >{/if}
                <label
                  >Role
                  <select
                    value={item.node_kind ?? 'paragraph'}
                    onchange={(event) =>
                      onpatch(item, {
                        node_kind: event.currentTarget
                          .value as GenerationSegment['node_kind']
                      })}
                    aria-label="Segment role"
                    class="mini"
                  >
                    <option value="paragraph">Paragraph</option>
                    <option value="heading">Heading</option>
                    <option value="chapter_marker">Chapter start</option>
                    <option value="subtitle_cue">Subtitle cue</option>
                  </select></label
                >
                <label
                  >Voice
                  <select
                    value={item.voice ?? ''}
                    onchange={(event) =>
                      onpatch(item, {
                        voice: event.currentTarget.value || null
                      })}
                    aria-label={`Voice for segment ${item.ordinal + 1}`}
                    title={`${selectedTtsServiceName ?? 'TTS service'} · ${selectedTtsModel || 'default model'}`}
                    disabled={speechOptionsLoading}
                    class="mini max-w-52"
                  >
                    <option value="">
                      Inherited{inheritedVoice
                        ? ` · ${onvoices(item).find((voice) => voice.id.toLowerCase() === inheritedVoice.toLowerCase())?.name ?? inheritedVoice}`
                        : ' · service default'}
                    </option>
                    {#each onvoices(item) as voice}
                      <option value={voice.id}>{onvoicelabel(voice)}</option>
                    {/each}
                  </select></label
                >
                <label
                  >Language
                  <select
                    value={item.language ?? ''}
                    onchange={(event) =>
                      onlanguagechange(item, event.currentTarget.value)}
                    aria-label={`Language for segment ${item.ordinal + 1}`}
                    disabled={speechOptionsLoading}
                    class="mini max-w-48"
                  >
                    <option value=""
                      >Inherited · {onlanguagelabel(inheritedLanguage)}</option
                    >
                    {#each onlanguages(item) as language}
                      <option value={language.value}>{language.label}</option>
                    {/each}
                  </select></label
                >
              </SegmentOptionsMenu>
            </div>
          </div>
        </td>
        <td>
          {#if selectedTake}
            <SegmentAudioPreview
              take={selectedTake}
              segmentNumber={item.ordinal + 1}
              takeLabel={ontakelabel(selectedTake)}
              active={previewSegmentId === item.id}
              onrequest={() => onpreviewrequest?.(item)}
            />
            <select
              value={selectedTake.id}
              onchange={(event) =>
                onselecttake(item, event.currentTarget.value)}
              class="mini mt-1 w-full"
            >
              {#each item.takes as take}
                <option value={take.id}
                  >{ontakelabel(take)} · {take.status}</option
                >
              {/each}
            </select>
            {#if selectedTake.audio_verification}
              <span
                class="verification-badge {selectedTake.audio_verification
                  .status}"
                title={onverificationtitle(selectedTake)}
              >
                Signal check: {selectedTake.audio_verification.status}
              </span>
            {/if}
          {:else}
            <span class="muted text-xs">Not generated</span>
          {/if}
        </td>
        <td>
          <span class="status">{item.status}</span>
          {#if ['generation_settings_changed', 'voice_reference_changed'].includes(item.audio_reuse_reason ?? '')}
            <span class="mt-1 block text-xs text-[var(--warning)]"
              >Audio settings changed</span
            >
          {:else if item.audio_reuse_reason === 'performance_changed'}
            <span class="mt-1 block text-xs text-[var(--warning)]"
              >Speech direction or context changed</span
            >
          {:else if item.audio_reuse_reason === 'audio_identity_unknown'}
            <span
              class="mt-1 block text-xs text-[var(--warning)]"
              title="Recording made with earlier settings, or its saved settings could not be verified. Continue unfinished audio keeps it; Refresh changed audio regenerates it with the current settings."
              >Older audio · kept unless refreshed</span
            >
          {/if}
        </td>
      </tr>
    {/each}
    {#if virtualEnabled && selection.bottomGap > 0}
      <tr class="virtual-spacer" aria-hidden="true">
        <td
          colspan="5"
          style="height: {Math.round(
            selection.bottomGap
          )}px; padding: 0; border: 0;"
        ></td>
      </tr>
    {/if}
  </tbody>
  {#if items.length > virtualizeThreshold}
    <tfoot>
      <tr>
        <td colspan="5" class="border-0 py-2 text-center">
          {#if showAll}
            <button type="button" class="mini" onclick={() => setShowAll(false)}
              >Virtualize rows (faster)</button
            >
          {:else}
            <button type="button" class="mini" onclick={() => setShowAll(true)}
              >Show all {items.length} loaded rows (enables find-in-page; slower)</button
            >
          {/if}
          <span class="muted ml-2 text-xs"
            >Showing {visibleIndexes.length} of {items.length} loaded</span
          >
        </td>
      </tr>
    </tfoot>
  {/if}
</table>

<style>
  table {
    min-width: 680px;
  }
  .mark-toggle {
    appearance: none;
    display: inline-grid;
    place-content: center;
    width: 1.15rem;
    height: 1.15rem;
    border: 1.5px solid var(--line);
    border-radius: 50%;
    background: var(--paper);
    cursor: pointer;
    vertical-align: top;
  }
  .mark-toggle:checked {
    border-color: var(--accent);
    background: var(--accent);
  }
  .mark-toggle:checked::after {
    content: '';
    width: 0.3rem;
    height: 0.55rem;
    border: solid white;
    border-width: 0 2px 2px 0;
    transform: translateY(-1px) rotate(45deg);
  }
  .mark-toggle:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 3px;
  }
  th,
  td {
    border-bottom: 1px solid var(--line);
    padding: 0.55rem;
    text-align: center;
    vertical-align: top;
  }
  td.narrative-cell,
  th:nth-child(3) {
    min-width: 0;
    overflow-wrap: anywhere;
    text-align: start;
  }
  td {
    overflow-wrap: anywhere;
  }
  td:first-child,
  td:nth-child(2) {
    padding-top: 1rem;
  }
  .mark-toggle {
    display: block;
    margin: 0 auto;
  }
  .segment-actions {
    margin-top: 0.2rem;
  }
  .passage-row {
    display: flex;
    align-items: flex-start;
    gap: 0.5rem;
  }
  .passage-row :global(.passage-text),
  .passage-row :global(.annotated-text) {
    flex: 1;
    min-width: 0;
  }
  .passage-edit {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    padding: 0.35rem;
    border-radius: 0.4rem;
    color: var(--muted);
  }
  .passage-edit:hover,
  .passage-edit:focus-visible {
    background: var(--accent-soft);
    color: var(--accent);
  }
  .passage-edit:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  tr.boundary-row td {
    height: 0.8rem;
    border-bottom: 0;
    padding: 0;
  }
  tr.virtual-spacer td {
    line-height: 0;
    font-size: 0;
  }
  tr.removed {
    opacity: 0.42;
  }
  tr.selected {
    background: var(--accent-soft);
  }
  .status {
    font-size: 0.68rem;
    text-transform: uppercase;
    color: var(--muted);
  }
  .mini {
    border: 1px solid var(--line);
    border-radius: 0.45rem;
    background: var(--paper);
    padding: 0.3rem 0.45rem;
    font-size: 0.68rem;
  }
  .verification-badge {
    display: inline-flex;
    margin-top: 0.35rem;
    border-radius: 999px;
    padding: 0.2rem 0.45rem;
    font-size: 0.6rem;
    font-weight: 750;
    text-transform: uppercase;
    background: color-mix(in srgb, var(--accent) 12%, transparent);
    color: var(--accent);
  }
  .verification-badge.warning {
    background: rgba(245, 158, 11, 0.13);
    color: #b45309;
  }
  .verification-badge.failed {
    background: rgba(239, 68, 68, 0.13);
    color: #dc2626;
  }
  .action {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    border: 1px solid var(--line);
    border-radius: 0.55rem;
    padding: 0.4rem 0.6rem;
    font-size: 0.7rem;
    font-weight: 700;
  }
  .action:disabled {
    opacity: 0.35;
  }
  .icon-action {
    padding: 0.42rem;
  }
  .segment-text {
    /* Content-sized textareas must grow vertically, not widen the table. */
    min-width: 0;
    max-width: 100%;
    overflow-wrap: anywhere;
    field-sizing: content;
    resize: none;
    overflow-y: hidden;
    min-height: 2.2rem;
    line-height: 1.45;
  }
</style>
