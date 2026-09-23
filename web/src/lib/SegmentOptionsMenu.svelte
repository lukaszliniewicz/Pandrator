<script lang="ts">
  import { EllipsisVertical } from '@lucide/svelte';
  import type { Snippet } from 'svelte';

  let {
    segmentNumber,
    children
  }: { segmentNumber: number; children: Snippet } = $props();
  const id = $props.id();
  let trigger: HTMLButtonElement;
  let panel: HTMLDivElement;
  let expanded = $state(false);
  function toggle(event: MouseEvent) {
    event.stopPropagation();
    if (panel.matches(':popover-open')) {
      panel.hidePopover();
      return;
    }
    const rect = trigger.getBoundingClientRect();
    const width = Math.min(320, window.innerWidth - 16);
    panel.style.width = `${width}px`;
    panel.style.left = `${Math.max(8, Math.min(rect.right - width, window.innerWidth - width - 8))}px`;
    panel.showPopover();
    const height = panel.getBoundingClientRect().height;
    panel.style.top = `${rect.bottom + height + 8 <= window.innerHeight ? rect.bottom + 4 : Math.max(8, rect.top - height - 4)}px`;
    panel.querySelector<HTMLElement>('button, select, input')?.focus();
  }
</script>

<button
  bind:this={trigger}
  type="button"
  class="options-trigger"
  title="Segment options"
  aria-label={`Options for segment ${segmentNumber}`}
  aria-haspopup="dialog"
  aria-controls={id}
  aria-expanded={expanded}
  onclick={toggle}
>
  <EllipsisVertical size={16} />
</button>
<div
  bind:this={panel}
  {id}
  popover="auto"
  role="dialog"
  tabindex="-1"
  aria-label={`Options for segment ${segmentNumber}`}
  class="segment-options"
  ontoggle={(event) => {
    expanded =
      event.currentTarget.isConnected &&
      event.currentTarget.matches(':popover-open');
  }}
  onkeydown={(event) => {
    event.stopPropagation();
    if (event.key === 'Escape') {
      event.preventDefault();
      panel.hidePopover();
      trigger.focus();
    }
  }}
>
  <strong>Segment {segmentNumber}</strong>
  {@render children()}
</div>

<style>
  .options-trigger {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: 1px solid var(--line);
    border-radius: 0.5rem;
    padding: 0.4rem;
  }
  .segment-options {
    position: fixed;
    inset: auto;
    margin: 0;
    max-height: calc(100vh - 16px);
    overflow-y: auto;
    padding: 1rem;
    border: 1px solid var(--line);
    border-radius: 0.75rem;
    color: var(--ink);
    background: var(--paper-strong);
    box-shadow: 0 12px 32px #0003;
  }
  .segment-options:popover-open {
    display: grid;
    gap: 0.75rem;
  }
  .segment-options :global(label) {
    display: grid;
    gap: 0.25rem;
    font-size: 0.75rem;
    font-weight: 600;
  }
  .segment-options :global(select) {
    max-width: 100%;
    width: 100%;
  }
</style>
