<script lang="ts">
  import type { GenerationSegment } from './api-models';
  import {
    annotationPresentation,
    deliveryDescription,
    selectedSpeechRange,
    speakerColor,
    speakerLabel,
    type SpeechSelection,
    type SpeechPreview
  } from './speech-annotations';
  import SpeechDirectionBadges from './SpeechDirectionBadges.svelte';
  import SpeechTextHint from './SpeechTextHint.svelte';
  import { eventSupport, type SpeechCapabilities } from './speech-support';
  import type { SpeechWordEdit } from './speech-word-edit';
  let {
    item,
    layer,
    capabilities,
    preview,
    performanceEnabled,
    vocalizationsEnabled,
    multiVoice = false,
    speakerNames = {},
    readOnly = false,
    detailsVisible = false,
    showAnnotations = true,
    disabled = false,
    onselection,
    oninspect,
    onedit
  }: {
    item: GenerationSegment;
    layer: 'display' | 'speech';
    capabilities?: SpeechCapabilities;
    preview?: SpeechPreview;
    performanceEnabled: boolean;
    vocalizationsEnabled: boolean;
    multiVoice?: boolean;
    speakerNames?: Record<string, string>;
    readOnly?: boolean;
    detailsVisible?: boolean;
    showAnnotations?: boolean;
    disabled?: boolean;
    onselection?: (selection: SpeechSelection) => void;
    onedit?: (edit: SpeechWordEdit, anchor: HTMLElement) => void;
    oninspect?: (
      item: GenerationSegment,
      offset: number,
      anchor: HTMLButtonElement,
      activate: boolean
    ) => void;
  } = $props();
  const spoken = $derived(item.optimized_text ?? item.text);
  const text = $derived(layer === 'speech' ? spoken : item.text);
  const mapped = $derived(text === spoken);
  const letters = $derived(Array.from(text));
  const presentation = $derived(annotationPresentation(item));
  const events = $derived(
    preview?.text === text ? preview.events : presentation.events
  );
  const modelCapabilities = $derived(preview?.capabilities ?? capabilities);
  const rootDelivery = $derived(
    Object.fromEntries(
      Object.entries(presentation.delivery).filter(
        ([key]) => key !== 'instruction'
      )
    )
  );
  const pieces = $derived.by(() => {
    const cuts = [
      ...new Set([
        0,
        letters.length,
        ...presentation.spans.flatMap((span) => [span.start, span.end]),
        ...presentation.scopes.flatMap((scope) => [scope.start, scope.end]),
        ...events.map((event) => event.offset)
      ])
    ].sort((a, b) => a - b);
    return cuts.slice(0, -1).map((start, index) => ({
      start,
      end: cuts[index + 1],
      span: presentation.spans.find(
        (span) => span.start <= start && span.end > start
      ),
      scope: presentation.scopes.findLast(
        (scope) => scope.start <= start && scope.end >= cuts[index + 1]
      )
    }));
  });
  function capabilitiesAt(offset: number, affinity = 'following') {
    const position =
      affinity === 'previous' || offset === letters.length
        ? Math.max(0, offset - 1)
        : offset;
    return (
      preview?.parts.find(
        (part) => part.start <= position && part.end > position
      )?.capabilities ?? modelCapabilities
    );
  }
  function capture(node: HTMLElement) {
    if (disabled || !mapped) return;
    const selection = selectedSpeechRange(node, item, layer, false);
    if (selection) onselection?.(selection);
  }
  function editWords(node: HTMLElement, inputType?: string, data?: string) {
    if (disabled || readOnly || !onedit) return;
    const selected = window.getSelection();
    let start = 0,
      end = 0;
    if (selected?.rangeCount) {
      const range = selected.getRangeAt(0);
      if (
        node.contains(range.startContainer) &&
        node.contains(range.endContainer)
      ) {
        const prefix = range.cloneRange();
        prefix.selectNodeContents(node);
        prefix.setEnd(range.startContainer, range.startOffset);
        const contents = (part: Range) => {
          const fragment = part.cloneContents();
          for (const decoration of fragment.querySelectorAll(
            '[data-speech-decoration]'
          ))
            decoration.remove();
          return fragment.textContent ?? '';
        };
        start = Array.from(contents(prefix)).length;
        end = start + Array.from(contents(range)).length;
      }
    }
    onedit({ start, end, ...(inputType ? { inputType, data } : {}) }, node);
  }
  function directionColor(index: number) {
    return speakerColor({
      start: 0,
      end: 0,
      role: 'speaker',
      speaker_id: `direction-${index}`,
      delivery: {}
    });
  }
  function nameFor(span: NonNullable<(typeof pieces)[number]['span']>) {
    return span.speaker_id && speakerNames[span.speaker_id]
      ? speakerNames[span.speaker_id]
      : speakerLabel(span);
  }
</script>

{#if mapped && showAnnotations && detailsVisible && deliveryDescription(rootDelivery)}
  <div class="block-direction">
    <span>Block direction</span>
    <button
      type="button"
      data-speech-direction="true"
      {disabled}
      onclick={(event) => oninspect?.(item, 0, event.currentTarget, true)}
      aria-label={`Edit block direction for segment ${item.ordinal + 1}`}
    >
      <SpeechDirectionBadges
        delivery={rootDelivery}
        capabilities={modelCapabilities}
        enabled={performanceEnabled}
      />
    </button>
  </div>
{/if}
<!-- Preview stays read-only; only double-click, F2 or the pencil opens wording. -->
<!-- prettier-ignore -->
<div class="speech-editing-text" class:read-only={readOnly} role="textbox" aria-readonly="true" aria-multiline="true" aria-label={`${layer === 'speech' ? 'Speech' : 'Subtitle'} text for segment ${item.ordinal + 1}`} aria-keyshortcuts={readOnly ? "Alt+Enter" : "Alt+Enter F2"} contenteditable="false" tabindex="0" data-speech-editing={item.id}
  onpointerup={(event) => { if (!(event.target instanceof Element && event.target.closest('[data-speech-decoration]'))) capture(event.currentTarget); }}
  onkeyup={(event) => { if (event.key === 'Shift' || event.key.startsWith('Arrow') || event.key === 'Home' || event.key === 'End') capture(event.currentTarget); }}
  onclick={(event) => { if (window.getSelection()?.isCollapsed === false) event.stopPropagation(); }}
  ondblclick={(event) => { event.stopPropagation(); editWords(event.currentTarget); }}
  onkeydown={(event) => { if (event.key === 'F2') { event.preventDefault(); event.stopPropagation(); editWords(event.currentTarget); } }}
  oncopy={(event) => { const range = selectedSpeechRange(event.currentTarget, item, layer); if (!range || !event.clipboardData) return; event.preventDefault(); event.clipboardData.setData('text/plain', letters.slice(range.start, range.end).join('')); }}
>{#if mapped}{#each pieces as piece}{#if showAnnotations}{#each events.filter(event => event.offset === piece.start) as event}{@const support = eventSupport(capabilitiesAt(event.offset, event.affinity), event.kind, vocalizationsEnabled, performanceEnabled)}<span contenteditable="false" data-speech-decoration class="vocal-cue" title={support.description}>{event.kind} · {support.label.toLowerCase()}</span>{/each}{/if}{@const voiceLabel = multiVoice && piece.span ? nameFor(piece.span) : ''}{@const direction = piece.scope?.delivery ?? presentation.delivery}{@const start = piece.scope?.start ?? (deliveryDescription(presentation.delivery) ? 0 : piece.span?.start ?? piece.start)}{@const end = piece.scope?.end ?? (deliveryDescription(presentation.delivery) ? letters.length : piece.span?.end ?? piece.end)}{#if showAnnotations}<SpeechTextHint text={letters.slice(piece.start, piece.end).join('')} label={voiceLabel} delivery={direction} capabilities={capabilitiesAt(piece.start)} enabled={performanceEnabled} scope={piece.scope ? 'phrase' : 'segment'} voiced={multiVoice && Boolean(piece.span && (piece.span.role !== 'narrator' || piece.span.voice))} directed={Boolean(piece.scope || deliveryDescription(presentation.delivery))} speakerColor={piece.span ? speakerColor(piece.span) : 'var(--line)'} directionColor={piece.scope ? directionColor(presentation.scopes.indexOf(piece.scope)) : 'var(--accent)'} {start} {end} focus={deliveryDescription(direction) ? direction.instruction ? 'instruction' : direction.emotion && !direction.pace && !direction.cadence && !direction.emphasis ? 'emotion' : 'delivery' : 'speaker'} {disabled} oninspect={oninspect ? (anchor) => oninspect?.(item, start, anchor, true) : undefined} />{:else}<span>{letters.slice(piece.start, piece.end).join('')}</span>{/if}{/each}{#if showAnnotations}{#each events.filter(event => event.offset === letters.length) as event}{@const support = eventSupport(capabilitiesAt(event.offset, event.affinity), event.kind, vocalizationsEnabled, performanceEnabled)}<span contenteditable="false" data-speech-decoration class="vocal-cue" title={support.description}>{event.kind} · {support.label.toLowerCase()}</span>{/each}{/if}{:else}{text}{/if}</div>

{#if mapped && showAnnotations && detailsVisible && presentation.scopes.length}<div
    class="phrase-notes"
    aria-label={`Phrase directions for segment ${item.ordinal + 1}`}
  >
    {#each presentation.scopes as scope, index}<button
        type="button"
        class="phrase-note"
        style:--direction-color={directionColor(index)}
        data-speech-direction="true"
        data-speech-start={scope.start}
        data-speech-end={scope.end}
        {disabled}
        onclick={(event) => {
          event.stopPropagation();
          oninspect?.(item, scope.start, event.currentTarget, true);
        }}
      >
        <q class="phrase-quote"
          >{letters.slice(scope.start, scope.end).join('')}</q
        >
        <SpeechDirectionBadges
          delivery={scope.delivery}
          capabilities={capabilitiesAt(scope.start)}
          scope="phrase"
          enabled={performanceEnabled}
        />
      </button>{/each}
  </div>{/if}

<style>
  .speech-editing-text {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    line-height: 1.9;
    padding: 0.65rem 0;
    border: 0;
    border-radius: 0.6rem;
    cursor: text;
  }
  .speech-editing-text:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
    background: var(--paper-strong);
  }
  .speech-editing-text.read-only {
    display: inline;
    padding: 0;
    border: 0;
    line-height: inherit;
  }
  .block-direction {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 0.4rem;
    margin-bottom: 0.2rem;
    font-size: 0.7rem;
    color: var(--muted);
  }
  .block-direction button {
    display: inline-flex;
    flex-wrap: wrap;
    padding: 0;
    background: none;
    vertical-align: baseline;
    text-align: left;
  }
  .phrase-notes {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem 0.75rem;
    margin: 0.15rem 0.65rem 0.5rem;
  }
  .phrase-note {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 0.4rem;
    max-width: 100%;
    padding: 0.2rem 0 0.2rem 0.5rem;
    border-left: 3px solid var(--direction-color);
    text-align: left;
  }
  .phrase-quote {
    font-size: 0.72rem;
    overflow-wrap: anywhere;
  }
  .phrase-note:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  .vocal-cue {
    display: inline;
    margin: 0 0.25rem;
    padding: 0.15rem 0.4rem;
    border: 1px dashed var(--line);
    border-radius: 0.4rem;
    background: var(--accent-soft);
    font-size: 0.7rem;
    user-select: none;
  }
</style>
