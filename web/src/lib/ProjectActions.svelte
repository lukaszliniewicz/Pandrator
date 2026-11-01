<script lang="ts">
  import { Folder, Settings2, Sparkles, Ellipsis, Crop } from '@lucide/svelte';
  let {
    onfolders,
    oncustomize,
    ontour,
    oneditpdf
  }: {
    onfolders?: () => void;
    oncustomize?: () => void;
    ontour?: () => void;
    oneditpdf?: () => void;
  } = $props();
  let menu = $state<HTMLDetailsElement>();
  function act(callback: () => void) {
    if (menu) menu.open = false;
    callback();
  }
</script>

<svelte:window
  onpointerdown={(event) => {
    if (
      menu?.open &&
      event.target instanceof Node &&
      !menu.contains(event.target)
    )
      menu.open = false;
  }}
  onkeydown={(event) => {
    if (event.key === 'Escape' && menu?.open) {
      menu.open = false;
      menu.querySelector('summary')?.focus();
    }
  }}
/>

<div class="project-tools">
  <div class="hidden flex-wrap gap-2 sm:flex" aria-label="Project tools">
    {#if onfolders}<button class="btn btn-secondary" onclick={onfolders}
        ><Folder size={16} />Folders</button
      >{/if}
    {#if oncustomize}<button class="btn btn-secondary" onclick={oncustomize}
        ><Settings2 size={16} />Customize workflow</button
      >{/if}
    {#if ontour}<button class="btn btn-secondary" onclick={ontour}
        ><Sparkles size={16} />Tour</button
      >{/if}
    {#if oneditpdf}<button class="btn btn-secondary" onclick={oneditpdf}
        ><Crop size={16} />Edit PDF</button
      >{/if}
  </div>
  <details bind:this={menu} class="relative sm:hidden">
    <summary
      class="btn btn-secondary list-none cursor-pointer"
      aria-label="Project tools"
      ><Ellipsis size={19} /><span>Tools</span></summary
    >
    <div
      class="absolute right-0 z-30 mt-2 grid min-w-56 gap-1 rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] p-2 shadow-lg"
    >
      {#if onfolders}<button
          class="btn justify-start"
          onclick={() => act(onfolders!)}><Folder size={16} />Folders</button
        >{/if}
      {#if oncustomize}<button
          class="btn justify-start"
          onclick={() => act(oncustomize!)}
          ><Settings2 size={16} />Customize workflow</button
        >{/if}
      {#if ontour}<button class="btn justify-start" onclick={() => act(ontour!)}
          ><Sparkles size={16} />Tour</button
        >{/if}
      {#if oneditpdf}<button
          class="btn justify-start"
          onclick={() => act(oneditpdf!)}><Crop size={16} />Edit PDF</button
        >{/if}
    </div>
  </details>
</div>
