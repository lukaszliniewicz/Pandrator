<script lang="ts">
  import { Eye, Scissors } from '@lucide/svelte';
  import { passageTime, type PassageBoundary } from './passage-structure';

  let {
    anchor,
    activate = false,
    boundary,
    disabledReason = '',
    busy = false,
    onclose,
    onpreview,
    onsplit
  }: {
    anchor: HTMLButtonElement;
    activate?: boolean;
    boundary: PassageBoundary;
    disabledReason?: string;
    busy?: boolean;
    onclose: () => void;
    onpreview: () => void;
    onsplit: () => void | Promise<void>;
  } = $props();
  let menu: HTMLDivElement;
  let closeTimer: ReturnType<typeof setTimeout> | undefined;
  const reason = $derived(
    disabledReason || boundary.split_blocked_reason || ''
  );

  function cancelClose() {
    if (closeTimer) clearTimeout(closeTimer);
    closeTimer = undefined;
  }

  function close(restoreFocus = false) {
    cancelClose();
    if (restoreFocus && anchor.isConnected) anchor.focus();
    onclose();
  }

  function scheduleClose() {
    cancelClose();
    if (activate || menu?.contains(document.activeElement)) return;
    closeTimer = setTimeout(() => close(), 180);
  }

  function choose(action: 'preview' | 'split') {
    // Restore focus before opening the modal so Escape can return to the dot.
    anchor.focus();
    if (action === 'preview' || !boundary.natural) onpreview();
    else void onsplit();
  }

  function navigate(event: KeyboardEvent) {
    event.stopPropagation();
    if (event.key === 'Escape') {
      event.preventDefault();
      close(true);
      return;
    }
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const buttons = Array.from(
      menu.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')
    );
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

  $effect(() => {
    const target = anchor;
    const focusMenu = activate;
    if (!menu || !target.isConnected) return;
    const rect = target.getBoundingClientRect();
    menu.showPopover();
    const width = menu.getBoundingClientRect().width;
    menu.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - width - 8))}px`;
    const height = menu.getBoundingClientRect().height;
    menu.style.top = `${
      rect.bottom + height + 12 <= window.innerHeight
        ? rect.bottom + 4
        : Math.max(8, rect.top - height - 4)
    }px`;
    target.setAttribute('aria-expanded', 'true');
    if (focusMenu)
      menu.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus();
    const leave = () => scheduleClose();
    const enter = () => cancelClose();
    const scroll = (event: Event) => {
      if (!menu.contains(event.target as Node)) close();
    };
    target.addEventListener('pointerleave', leave);
    target.addEventListener('pointerenter', enter);
    document.addEventListener('scroll', scroll, true);
    return () => {
      cancelClose();
      target.setAttribute('aria-expanded', 'false');
      target.removeEventListener('pointerleave', leave);
      target.removeEventListener('pointerenter', enter);
      document.removeEventListener('scroll', scroll, true);
    };
  });
</script>

<svelte:window onresize={() => close()} />

<div
  bind:this={menu}
  popover="auto"
  role="menu"
  tabindex="-1"
  aria-label="Passage boundary actions"
  class="passage-actions font-sans"
  onpointerenter={cancelClose}
  onpointerleave={scheduleClose}
  onkeydown={navigate}
  onfocusout={(event) => {
    if (
      event.relatedTarget &&
      !menu.contains(event.relatedTarget as Node) &&
      event.relatedTarget !== anchor
    )
      close();
  }}
  ontoggle={(event) => {
    // A queued close event may outlive its virtualized row and bind:this.
    if (
      event.currentTarget.isConnected &&
      !event.currentTarget.matches(':popover-open')
    )
      close();
  }}
>
  <p class="timing">
    {passageTime(boundary.left_end_ms)} to {passageTime(
      boundary.right_start_ms
    )}
  </p>
  <div class="choices">
    <button
      type="button"
      role="menuitem"
      disabled={busy || Boolean(reason) || !boundary.split_allowed}
      onclick={() => choose('split')}
    >
      <Scissors size={15} />{boundary.natural ? 'Split here' : 'Review split…'}
    </button>
    <button
      type="button"
      role="menuitem"
      disabled={busy}
      onclick={() => choose('preview')}
    >
      <Eye size={15} />Preview
    </button>
  </div>
  <p class="hint">
    {reason ||
      (!boundary.natural
        ? 'Unfinished phrase: confirmation required.'
        : 'New plan revision; both blocks need new audio.')}
  </p>
</div>

<style>
  .passage-actions {
    position: fixed;
    margin: 0;
    width: min(290px, calc(100vw - 16px));
    max-height: calc(100vh - 16px);
    overflow: auto;
    padding: 0.6rem;
    border: 1px solid var(--line);
    border-radius: 0.7rem;
    background: var(--paper-strong);
    color: var(--ink);
    box-shadow: var(--shadow);
    text-align: left;
    white-space: normal;
  }
  .timing,
  .hint {
    font-size: 0.7rem;
    line-height: 1.5;
    color: var(--muted);
  }
  .timing {
    padding: 0 0.25rem 0.3rem;
    font-variant-numeric: tabular-nums;
  }
  .hint {
    padding: 0.35rem 0.25rem 0;
  }
  .choices {
    display: flex;
    gap: 0.35rem;
  }
  button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 0.4rem;
    flex: 1;
    padding: 0.55rem 0.4rem;
    border: 1px solid var(--line);
    border-radius: 0.45rem;
    font-size: 0.75rem;
    font-weight: 650;
    cursor: pointer;
  }
  button:hover:not(:disabled) {
    background: var(--accent-soft);
    color: var(--accent);
  }
  button:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  button:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }
</style>
