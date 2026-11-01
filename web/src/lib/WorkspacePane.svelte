<script lang="ts">
  import { onMount, type Snippet } from 'svelte';
  import { X } from '@lucide/svelte';
  import { modalDialog } from './modal-dialog';
  import { dialogBackdrop } from './dialog-backdrop';

  let {
    open = $bindable(false),
    modal = $bindable(false),
    title = 'Tools',
    side = 'right',
    children,
    tools
  }: {
    open?: boolean;
    modal?: boolean;
    title?: string;
    side?: 'left' | 'right';
    children: Snippet;
    tools: Snippet;
  } = $props();
  const id = $props.id();
  let panel = $state<HTMLDialogElement>();
  let narrow = $state(false);

  onMount(() => {
    const query = window.matchMedia('(max-width: 900px)');
    const update = () => (narrow = query.matches);
    update();
    query.addEventListener('change', update);
    return () => query.removeEventListener('change', update);
  });
  $effect(() => {
    modal = narrow && open;
  });
  $effect(() => {
    if (!open || !panel) return;
    if (narrow) {
      const focus = modalDialog(panel, { onclose: () => (open = false) });
      return () => focus?.destroy?.();
    }
    const previous = document.activeElement;
    const currentPanel = panel;
    currentPanel.show();
    panel.focus({ preventScroll: true });
    return () => {
      currentPanel.close();
      if (previous instanceof HTMLElement && previous.isConnected)
        previous.focus({ preventScroll: true });
    };
  });
</script>

<div class="workspace-pane" data-workspace-layout>
  <div class="workspace-content" inert={modal}>
    {@render children()}
  </div>
  <dialog
    use:dialogBackdrop={() => (open = false)}
    bind:this={panel}
    class="workspace-inspector"
    class:open
    class:left={side === 'left'}
    class:overlay={narrow}
    aria-modal={modal ? 'true' : undefined}
    aria-labelledby={`${id}-title`}
    aria-hidden={!open}
    tabindex="-1"
    inert={!open}
    data-workspace-tools
  >
    <div class="inspector-header">
      <h3 id={`${id}-title`}>{title}</h3>
      <button
        class="btn btn-icon btn-secondary"
        aria-label={`Close ${title.toLowerCase()}`}
        onclick={() => (open = false)}><X size={17} /></button
      >
    </div>
    <div class="inspector-content">{@render tools()}</div>
  </dialog>
</div>

<style>
  .workspace-pane {
    position: relative;
    display: flex;
    flex: 1;
    min-height: 0;
    min-width: 0;
    overflow: hidden;
  }
  .workspace-content {
    display: flex;
    flex: 1;
    flex-direction: column;
    min-height: 0;
    min-width: 0;
    overflow: hidden;
  }
  .workspace-inspector {
    display: none;
    position: static;
    margin: 0;
    padding: 0;
    color: var(--ink);
    max-width: none;
    max-height: none;
    border: 0;
    flex: 0 0 min(22rem, 34%);
    min-width: 18rem;
    min-height: 0;
    overflow: hidden;
    border-left: 1px solid var(--line);
    background: var(--paper-strong);
    height: 100%;
  }
  .workspace-inspector.left {
    order: -1;
    border-left: 0;
    border-right: 1px solid var(--line);
  }
  .workspace-inspector.left.overlay {
    inset: 0 auto 0 0;
  }
  .workspace-inspector.open {
    display: flex;
    flex-direction: column;
  }
  .workspace-inspector.overlay {
    position: fixed;
    inset: 0 0 0 auto;
    z-index: 3;
    width: min(25rem, 94vw);
    height: 100dvh;
    min-width: 0;
    box-shadow: -1rem 0 3rem rgb(0 0 0 / 15%);
  }
  .inspector-header {
    display: flex;
    flex: none;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.75rem 1rem;
    border-bottom: 1px solid var(--line);
    font-size: 0.85rem;
    font-weight: 700;
  }
  .inspector-content {
    flex: 1;
    min-height: 0;
    overflow: auto;
    overscroll-behavior: contain;
    padding: 1rem;
  }
  .workspace-inspector::backdrop {
    background: rgb(0 0 0 / 30%);
  }
</style>
