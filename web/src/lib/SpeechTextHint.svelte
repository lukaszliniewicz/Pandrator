<script lang="ts">
  import { onDestroy, tick } from 'svelte';
  import { helpPopover } from './help-popover';
  import SpeechDirectionBadges from './SpeechDirectionBadges.svelte';
  import type { SpeechCapabilities } from './speech-support';
  import { deliveryDescription } from './speech-annotations';

  let {
    text,
    label = '',
    delivery = {},
    capabilities,
    enabled = true,
    scope = 'phrase',
    voiced = false,
    directed = false,
    speakerColor = 'var(--accent)',
    directionColor = 'var(--accent)',
    start,
    end,
    focus = 'delivery',
    disabled = false,
    oninspect
  }: {
    text: string;
    label?: string;
    delivery?: Record<string, unknown>;
    capabilities?: SpeechCapabilities;
    enabled?: boolean;
    scope?: 'segment' | 'phrase' | 'unresolved';
    voiced?: boolean;
    directed?: boolean;
    speakerColor?: string;
    directionColor?: string;
    start: number;
    end: number;
    focus?: 'speaker' | 'delivery' | 'instruction' | 'emotion';
    disabled?: boolean;
    oninspect?: (anchor: HTMLButtonElement) => void;
  } = $props();
  const description = $derived(deliveryDescription(delivery));
  const annotated = $derived(Boolean(label || description));
  let anchor = $state<HTMLButtonElement>();
  let popup = $state<HTMLDivElement>();
  let container = $state<HTMLSpanElement>();
  let pointerType = '';
  let active = $state(false);
  let alive = true;
  const hintId = $props.id();
  const help = helpPopover(
    () => anchor,
    () => popup,
    () => container
  );
  async function show() {
    if (!annotated) return;
    active = true;
    await tick();
    if (alive) help.show();
  }
  onDestroy(() => {
    alive = false;
    help.hide();
  });
</script>

<!-- No formatting whitespace: this component participates in exact speech offsets. -->
<!-- prettier-ignore -->
<span bind:this={container} class="hint-container">{#if annotated}<button bind:this={anchor} type="button" class="hint-text" class:voiced class:directed style:--speaker-color={speakerColor} style:--direction-color={directionColor} title={[label, description, description && !enabled ? 'Delivery off' : ''].filter(Boolean).join(' · ')} aria-label={[text, label, description].filter(Boolean).join(' · ')} aria-haspopup="dialog" aria-expanded={active} aria-controls={active ? hintId : undefined} data-speech-start={start} data-speech-end={end} data-speech-focus={focus} onpointerenter={(event) => { if (event.pointerType === 'mouse' && event.buttons === 0) void show(); }} onfocus={(event) => { if (event.currentTarget.matches(':focus-visible')) void show(); }} onpointerleave={help.leave} onfocusout={help.focusout} onkeydown={help.escape} onpointerdown={(event) => { pointerType = event.pointerType; if (event.pointerType === 'mouse') help.hide(); }} onclick={(event) => { if (window.getSelection()?.isCollapsed === false) { event.stopPropagation(); return; } if (event.detail === 0) { event.stopPropagation(); help.hide(); if (!disabled) oninspect?.(event.currentTarget); } else if (pointerType === 'touch' || pointerType === 'pen') { event.stopPropagation(); void show(); } }}>{text}</button>{:else}<span>{text}</span>{/if}{#if active}<div bind:this={popup} id={hintId} popover="manual" class="annotation-hint" data-speech-decoration role="dialog" tabindex="-1" aria-label="Speech annotation details" onpointerenter={help.show} onpointerleave={help.leave} onfocusout={help.focusout} onkeydown={help.escape} ontoggle={(event) => { if (event.newState === 'closed') active = false; }}>{#if label}<strong>{label}</strong>{/if}{#if description}<p class="scope-caption">{scope === 'segment' ? 'Whole-segment direction' : 'Direction for these words'}</p><SpeechDirectionBadges {delivery} {capabilities} {scope} {enabled} />{/if}{#if oninspect}<button type="button" class="edit-annotation" {disabled} data-speech-start={start} data-speech-end={end} data-speech-focus={focus} onclick={(event) => { event.stopPropagation(); help.hide(); if (anchor) oninspect?.(anchor); }}>Edit {focus === 'speaker' ? 'speaker and voice' : 'direction'}</button>{/if}</div>{/if}</span>

<style>
  .hint-container {
    display: inline;
  }
  .hint-text {
    display: inline;
    padding: 0;
    border: 0;
    background: none;
    color: inherit;
    font: inherit;
    text-align: inherit;
    white-space: inherit;
    user-select: text;
    cursor: text;
  }
  .voiced {
    text-decoration: underline var(--speaker-color) 2px;
    text-underline-offset: 0.25em;
  }
  .directed {
    text-decoration-line: underline;
    text-decoration-style: double;
    text-decoration-color: var(--direction-color);
    text-underline-offset: 0.25em;
  }
  .voiced.directed {
    box-shadow: inset 0 -1px 0 var(--speaker-color);
  }
  .hint-text:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
    border-radius: 0.15rem;
  }
  .annotation-hint {
    position: fixed;
    inset: auto;
    margin: 0;
    max-width: min(24rem, calc(100vw - 16px));
    max-height: min(18rem, calc(100dvh - 16px));
    overflow: auto;
    padding: 0.75rem;
    border: 1px solid var(--line);
    border-radius: 0.65rem;
    background: var(--paper-strong);
    color: var(--ink);
    box-shadow: var(--shadow);
    font: 400 0.78rem/1.5 var(--font-body, sans-serif);
    white-space: normal;
  }
  .scope-caption {
    margin: 0.25rem 0;
    color: var(--muted);
    font-size: 0.7rem;
  }
  .edit-annotation {
    display: block;
    margin-top: 0.6rem;
    padding: 0;
    color: var(--accent);
    font-size: 0.75rem;
    text-decoration: underline;
  }
</style>
