<script lang="ts">
  import { RefreshCw, WandSparkles } from '@lucide/svelte';

  let {
    segmentNumber,
    disabled = false,
    compact = false,
    onregenerate,
    onregeneratewith
  }: {
    segmentNumber: number;
    disabled?: boolean;
    compact?: boolean;
    onregenerate: () => unknown;
    onregeneratewith: () => unknown;
  } = $props();

  const id = $props.id();
  let trigger: HTMLButtonElement;
  let menu: HTMLSpanElement;
  let expanded = $state(false);

  function close() {
    menu.hidePopover();
    trigger.focus();
  }

  function toggle(event: MouseEvent | KeyboardEvent) {
    event.stopPropagation();
    if (menu.matches(':popover-open')) {
      close();
      return;
    }
    const rect = trigger.getBoundingClientRect();
    const width = Math.min(248, window.innerWidth - 16);
    menu.style.left = `${Math.max(8, Math.min(rect.right - width, window.innerWidth - width - 8))}px`;
    menu.showPopover();
    const height = menu.getBoundingClientRect().height;
    menu.style.top = `${rect.bottom + height + 13 <= window.innerHeight ? rect.bottom + 5 : Math.max(8, rect.top - height - 5)}px`;
    menu.querySelector('button')?.focus();
  }

  function navigate(event: KeyboardEvent) {
    event.stopPropagation();
    if (event.key === 'Escape' || event.key === 'Tab') {
      if (event.key === 'Escape') event.preventDefault();
      close();
      return;
    }
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const buttons = Array.from(menu.querySelectorAll('button'));
    const current = buttons.indexOf(
      document.activeElement as HTMLButtonElement
    );
    const next =
      event.key === 'Home'
        ? 0
        : event.key === 'End'
          ? buttons.length - 1
          : (current + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) %
            buttons.length;
    buttons[next]?.focus();
  }
</script>

<button
  bind:this={trigger}
  type="button"
  class="regenerate-trigger"
  class:compact
  {disabled}
  aria-label={`Regenerate segment ${segmentNumber}`}
  title="Regeneration options"
  aria-haspopup="menu"
  aria-controls={id}
  aria-expanded={expanded}
  onclick={toggle}
  onkeydown={(event) => {
    if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return;
    event.preventDefault();
    toggle(event);
    if (event.key === 'ArrowUp')
      menu.querySelector<HTMLButtonElement>('button:last-child')?.focus();
  }}
>
  <RefreshCw size={compact ? 13 : 14} />
</button>
<span
  bind:this={menu}
  {id}
  popover="auto"
  role="menu"
  tabindex="-1"
  aria-label={`Regeneration options for segment ${segmentNumber}`}
  class="regenerate-menu font-sans"
  ontoggle={() => (expanded = menu.matches(':popover-open'))}
  onkeydown={navigate}
>
  <button
    type="button"
    role="menuitem"
    onclick={(event) => {
      event.stopPropagation();
      close();
      onregenerate();
    }}><RefreshCw size={14} />Regenerate</button
  >
  <button
    type="button"
    role="menuitem"
    onclick={(event) => {
      event.stopPropagation();
      close();
      onregeneratewith();
    }}><WandSparkles size={14} />Regenerate with different settings…</button
  >
</span>

<style>
  .regenerate-trigger {
    display: grid;
    place-items: center;
    border: 1px solid var(--line);
    border-radius: 0.55rem;
    padding: 0.42rem;
    color: var(--ink);
  }
  .regenerate-trigger.compact {
    width: 1.8rem;
    height: 1.8rem;
    border: 0;
    border-radius: 0.45rem;
    color: var(--muted);
  }
  .regenerate-trigger:hover:not(:disabled),
  .regenerate-trigger[aria-expanded='true'] {
    background: var(--accent-soft);
    color: var(--accent);
  }
  .regenerate-trigger:disabled {
    opacity: 0.35;
  }
  .regenerate-menu {
    pointer-events: auto;
    position: fixed;
    margin: 0;
    width: min(248px, calc(100vw - 16px));
    max-height: calc(100vh - 16px);
    overflow-y: auto;
    border: 1px solid var(--line);
    border-radius: 0.65rem;
    padding: 0.25rem;
    background: var(--paper-strong);
    color: var(--ink);
    box-shadow: var(--shadow);
  }
  .regenerate-menu button {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    width: 100%;
    border-radius: 0.4rem;
    padding: 0.65rem 0.5rem;
    text-align: left;
    font-size: 0.72rem;
    font-weight: 600;
    line-height: 1.35;
  }
  .regenerate-menu button:hover,
  .regenerate-menu button:focus-visible {
    background: var(--accent-soft);
    color: var(--accent);
    outline: none;
  }
</style>
