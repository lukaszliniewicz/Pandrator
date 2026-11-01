<script lang="ts">
  import type { Snippet } from 'svelte';
  import { PanelRight, X } from '@lucide/svelte';
  import { modalDialog } from './modal-dialog';
  import { dialogBackdrop } from './dialog-backdrop';
  import WorkspaceMaximizeButton from './WorkspaceMaximizeButton.svelte';
  import WorkspacePane from './WorkspacePane.svelte';

  let {
    titleId,
    closeLabel = 'Close workspace',
    onclose,
    busy = false,
    maximized = $bindable(false),
    toolsOpen = $bindable(false),
    toolsTitle = 'Tools',
    toolsSide = 'right',
    toolsSummary = '',
    header,
    children,
    tools,
    notices,
    footer
  }: {
    titleId: string;
    closeLabel?: string;
    onclose: () => void;
    busy?: boolean;
    maximized?: boolean;
    toolsOpen?: boolean;
    toolsTitle?: string;
    toolsSide?: 'left' | 'right';
    toolsSummary?: string;
    header: Snippet;
    children: Snippet;
    tools?: Snippet;
    notices?: Snippet;
    footer?: Snippet;
  } = $props();
  let inspectorModal = $state(false);
  function requestClose() {
    if (!busy) onclose();
  }
</script>

<dialog
  use:modalDialog={{
    onclose: () => {
      if (toolsOpen) toolsOpen = false;
      else requestClose();
    }
  }}
  use:dialogBackdrop={requestClose}
  class="review-workspace"
  class:maximized
  aria-labelledby={titleId}
  aria-busy={busy}
  data-review-workspace
>
  <header class="workspace-header" inert={inspectorModal}>
    <div class="workspace-heading">{@render header()}</div>
    <div class="workspace-actions">
      {#if tools}
        <button
          class="btn btn-sm btn-secondary"
          aria-expanded={toolsOpen}
          onclick={() => (toolsOpen = !toolsOpen)}
          title={toolsSummary || toolsTitle}
          ><PanelRight size={16} />
          {toolsTitle}
          {#if toolsSummary}<span class="tools-summary">{toolsSummary}</span
            >{/if}</button
        >
      {/if}
      <WorkspaceMaximizeButton
        {maximized}
        ontoggle={() => (maximized = !maximized)}
      />
      <button
        class="btn btn-icon btn-secondary"
        disabled={busy}
        aria-label={closeLabel}
        onclick={requestClose}><X size={18} /></button
      >
    </div>
  </header>
  {#if notices}<div class="workspace-notices" inert={inspectorModal}>
      {@render notices()}
    </div>{/if}
  {#if tools}
    <WorkspacePane
      bind:open={toolsOpen}
      bind:modal={inspectorModal}
      title={toolsTitle}
      side={toolsSide}
      {tools}
    >
      {@render children()}
    </WorkspacePane>
  {:else}
    <div class="workspace-body">{@render children()}</div>
  {/if}
  {#if footer}<footer inert={inspectorModal}>{@render footer()}</footer>{/if}
</dialog>

<style>
  .review-workspace {
    width: calc(100vw - 2rem);
    height: min(64rem, calc(100dvh - 2rem));
    max-width: 96rem;
    max-height: none;
    margin: auto;
    padding: 0;
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 1.25rem;
    background: var(--paper);
    color: var(--ink);
    box-shadow: var(--shadow);
  }
  .review-workspace[open] {
    display: flex;
    flex-direction: column;
  }
  .review-workspace::backdrop {
    background: rgb(0 0 0 / 50%);
    backdrop-filter: blur(3px);
  }
  .review-workspace.maximized {
    width: 100vw;
    height: 100dvh;
    max-width: none;
    border-radius: 0;
  }
  .workspace-header {
    display: flex;
    flex: none;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 0.5rem 1rem;
    padding: 0.75rem 1rem;
    border-bottom: 1px solid var(--line);
  }
  .workspace-heading {
    display: flex;
    flex: 1;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.5rem 1rem;
    min-width: min(22rem, 100%);
  }
  .workspace-actions {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    margin-left: auto;
  }
  .tools-summary {
    max-width: 10rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--muted);
    font-size: 0.7rem;
  }
  .workspace-notices {
    flex: none;
    max-height: 30dvh;
    overflow: auto;
  }
  .workspace-body {
    display: flex;
    flex: 1;
    flex-direction: column;
    min-height: 0;
    overflow: hidden;
  }
  footer {
    flex: none;
  }
  @media (max-width: 600px) {
    .review-workspace {
      width: 100vw;
      height: 100dvh;
      border-radius: 0;
    }
    .workspace-header {
      padding: 0.5rem 0.75rem;
    }
    .tools-summary {
      max-width: 6rem;
    }
  }
</style>
