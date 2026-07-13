<script lang="ts">
  import { X } from '@lucide/svelte';
  import ServiceManager from './ServiceManager.svelte';
  let { onclose }: { onclose: () => void } = $props();
</script>

<div class="fixed inset-0 z-[80] grid place-items-center bg-black/45 p-3 backdrop-blur-sm" role="presentation" onclick={(event) => event.target === event.currentTarget && onclose()}>
  <div class="surface flex max-h-[95vh] w-full max-w-7xl flex-col overflow-hidden rounded-3xl" role="dialog" aria-modal="true" aria-labelledby="tts-services-modal-title">
    <header class="flex items-center justify-between border-b border-[var(--line)] px-6 py-4"><div><div class="eyebrow">Providers & services</div><h2 id="tts-services-modal-title" class="mt-1 text-xl font-semibold">TTS connections, defaults, and voices</h2></div><button onclick={onclose} class="btn btn-icon btn-secondary" aria-label="Close TTS services"><X size={18}/></button></header>
    <div class="min-h-0 flex-1 overflow-y-auto p-5 sm:p-7"><ServiceManager/></div>
  </div>
</div>
