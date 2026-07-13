<script lang="ts">
  import { ArrowLeft, ArrowRight, Check, X } from '@lucide/svelte';
  type Step = { title: string; body: string; section?: string };
  let { tourId, steps, open = $bindable(false) }: { tourId: string; steps: Step[]; open?: boolean } = $props();
  let index = $state(0);
  const current = $derived(steps[index]);
  function close(completed = false) { if (completed) localStorage.setItem(`pandrator-tour-${tourId}`, 'complete'); index = 0; open = false; }
  function next() { if (index + 1 < steps.length) index += 1; else close(true); }
</script>

{#if open && current}
  <div class="fixed inset-0 z-[80] bg-black/30 backdrop-blur-[1px]" role="presentation">
    <div class="surface fixed right-5 bottom-5 w-[min(27rem,calc(100vw-2.5rem))] rounded-3xl p-6 shadow-2xl" role="dialog" aria-modal="true" aria-labelledby={`${tourId}-tour-title`}>
      <div class="flex items-start justify-between gap-4"><div><div class="eyebrow">{current.section ?? 'Guided tour'} · {index + 1}/{steps.length}</div><h2 id={`${tourId}-tour-title`} class="mt-2 text-xl font-semibold">{current.title}</h2></div><button onclick={() => close()} aria-label="Close tour" class="rounded-lg p-2 hover:bg-[var(--accent-soft)]"><X size={17}/></button></div>
      <p class="muted mt-3 leading-relaxed">{current.body}</p>
      <div class="mt-5 flex items-center gap-1">{#each steps as _, step}<span class:active={step===index} class="h-1.5 flex-1 rounded-full bg-[var(--line)]"></span>{/each}</div>
      <div class="mt-5 flex justify-between"><button onclick={() => index=Math.max(0,index-1)} disabled={index===0} class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-3 py-2 text-sm font-semibold disabled:opacity-30"><ArrowLeft size={15}/> Back</button><button onclick={next} class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2 text-sm font-semibold text-white">{index+1===steps.length?'Finish':'Next'}{#if index+1===steps.length}<Check size={15}/>{:else}<ArrowRight size={15}/>{/if}</button></div>
    </div>
  </div>
{/if}
<style>.active{background:var(--action-bg)}</style>
