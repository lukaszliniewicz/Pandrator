<script lang="ts">
  import {
    AudioLines,
    Bell,
    Ellipsis,
    MessageSquare,
    Smile,
    Users,
    X
  } from '@lucide/svelte';
  import type { SpeechSelection } from './speech-annotations';
  import type { SpeechInspectorTarget } from './speech-inspector';
  let {
    selection,
    multiVoice = false,
    onopen,
    onclose,
    onsound
  }: {
    selection: SpeechSelection;
    multiVoice?: boolean;
    onopen: (
      selection: SpeechSelection,
      focus: SpeechInspectorTarget['focus']
    ) => void;
    onclose: () => void;
    onsound?: (selection: SpeechSelection) => void;
  } = $props();
  let panel: HTMLDivElement;
  let active = $state(0);
  const cursor = $derived(selection.start === selection.end);
  const scope = $derived(
    selection.kind === 'segment' ? 'Block' : cursor ? 'Cursor' : 'Phrase'
  );
  function buttons() {
    return [
      ...panel.querySelectorAll<HTMLButtonElement>('button[data-action]')
    ];
  }
  function focusAction(index: number) {
    const choices = buttons();
    active = choices.length ? (index + choices.length) % choices.length : 0;
    choices[active]?.focus();
  }
  $effect(() => {
    if (!panel) return;
    panel.showPopover();
    const initialAnchor = selection.anchor.getBoundingClientRect();
    const position = () => {
      const size = panel.getBoundingClientRect();
      const anchor = selection.anchor.getBoundingClientRect();
      const left = selection.rect.left + anchor.left - initialAnchor.left;
      const selectionTop = selection.rect.top + anchor.top - initialAnchor.top;
      const selectionBottom =
        selection.rect.bottom + anchor.top - initialAnchor.top;
      panel.style.left = `${Math.max(8, Math.min(left, document.documentElement.clientWidth - size.width - 8))}px`;
      const top =
        selectionTop > size.height + 12
          ? selectionTop - size.height - 6
          : selectionBottom + 6;
      panel.style.top = `${Math.max(8, Math.min(top, innerHeight - size.height - 8))}px`;
    };
    position();
    const resize = new ResizeObserver(position);
    resize.observe(panel);
    window.addEventListener('resize', position);
    window.addEventListener('scroll', position, true);
    const keyboard = (event: KeyboardEvent) => {
      if (
        (event.key === 'Tab' && !event.shiftKey) ||
        (event.altKey && event.key === 'Enter')
      ) {
        event.preventDefault();
        event.stopPropagation();
        focusAction(0);
      } else if (event.key === 'Escape') {
        event.stopPropagation();
        onclose();
      }
    };
    selection.anchor.addEventListener('keydown', keyboard);
    const dismiss = (event: PointerEvent) => {
      if (
        event.target instanceof Node &&
        !panel.contains(event.target) &&
        !selection.anchor.contains(event.target)
      )
        onclose();
    };
    document.addEventListener('pointerdown', dismiss);
    return () => {
      resize.disconnect();
      window.removeEventListener('resize', position);
      window.removeEventListener('scroll', position, true);
      selection.anchor.removeEventListener('keydown', keyboard);
      document.removeEventListener('pointerdown', dismiss);
    };
  });
</script>

<div
  bind:this={panel}
  popover="manual"
  role="toolbar"
  aria-label={`${scope} speech actions`}
  aria-orientation="horizontal"
  tabindex="-1"
  class="selection-toolbar"
  onkeydown={(event) => {
    if (
      [
        'ArrowRight',
        'ArrowLeft',
        'ArrowDown',
        'ArrowUp',
        'Home',
        'End'
      ].includes(event.key)
    ) {
      event.preventDefault();
      event.stopPropagation();
      const index = buttons().indexOf(
        document.activeElement as HTMLButtonElement
      );
      focusAction(
        event.key === 'Home'
          ? 0
          : event.key === 'End'
            ? buttons().length - 1
            : index +
              (event.key === 'ArrowLeft' || event.key === 'ArrowUp' ? -1 : 1)
      );
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      onclose();
    }
  }}
>
  <span class="scope-label">{scope}</span>
  {#if !cursor}
    {#if multiVoice}<button
        data-action
        tabindex={active === 0 ? 0 : -1}
        onfocus={() => (active = 0)}
        onclick={() => onopen(selection, 'speaker')}
        ><Users size={15} /> Speaker &amp; voice</button
      >{/if}
    <button
      data-action
      tabindex={active === (multiVoice ? 1 : 0) ? 0 : -1}
      onfocus={() => (active = multiVoice ? 1 : 0)}
      onclick={() => onopen(selection, 'instruction')}
      ><MessageSquare size={15} /> Instruction</button
    >
    <button
      data-action
      tabindex={active === (multiVoice ? 2 : 1) ? 0 : -1}
      onfocus={() => (active = multiVoice ? 2 : 1)}
      onclick={() => onopen(selection, 'emotion')}
      ><Smile size={15} /> Emotion</button
    >
  {/if}
  <button
    data-action
    tabindex={active === (cursor ? 0 : multiVoice ? 3 : 2) ? 0 : -1}
    onfocus={() => (active = cursor ? 0 : multiVoice ? 3 : 2)}
    onclick={() => onopen(selection, 'cue')}
    ><AudioLines size={15} /> Vocal cue</button
  >
  {#if cursor}<button
      data-action
      tabindex={active === 1 ? 0 : -1}
      onfocus={() => (active = 1)}
      onclick={() =>
        onopen(
          {
            ...selection,
            start: 0,
            end: Array.from(
              selection.item.optimized_text ?? selection.item.text
            ).length,
            kind: 'segment'
          },
          'instruction'
        )}>Block instruction</button
    >
  {:else}
    <button
      data-action
      tabindex={active === (multiVoice ? 4 : 3) ? 0 : -1}
      onfocus={() => (active = multiVoice ? 4 : 3)}
      aria-label="More delivery controls"
      onclick={() => onopen(selection, 'delivery')}
      ><Ellipsis size={17} /></button
    >
  {/if}
  {#if onsound}<button
      data-action
      tabindex={active === (cursor ? 2 : multiVoice ? 5 : 4) ? 0 : -1}
      onfocus={() => (active = cursor ? 2 : multiVoice ? 5 : 4)}
      onclick={() => onsound?.(selection)}
      ><Bell size={15} />Sound effect</button
    >{/if}
  <button aria-label="Dismiss speech selection" onclick={onclose}
    ><X size={15} /></button
  >
</div>

<style>
  .selection-toolbar {
    position: fixed;
    margin: 0;
    padding: 0.3rem;
    display: flex;
    gap: 0.2rem;
    border: 1px solid var(--line);
    border-radius: 0.8rem;
    background: var(--paper-strong);
    color: var(--ink);
    box-shadow: var(--shadow);
    max-width: calc(100% - 16px);
  }
  .scope-label {
    align-self: center;
    padding: 0 0.4rem;
    border-right: 1px solid var(--line);
    color: var(--muted);
    font-size: 0.68rem;
  }
  .selection-toolbar button {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.55rem 0.65rem;
    border-radius: 0.5rem;
    font-size: 0.8rem;
  }
  .selection-toolbar button:hover {
    background: var(--accent-soft);
  }
  .selection-toolbar button:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: -2px;
  }
  @media (max-width: 560px) {
    .selection-toolbar {
      flex-wrap: wrap;
    }
    .selection-toolbar button {
      padding: 0.45rem;
    }
  }
</style>
