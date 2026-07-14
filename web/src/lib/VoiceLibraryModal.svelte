<script lang="ts">
  import { X } from '@lucide/svelte';
  import VoiceManager from './VoiceManager.svelte';

  let {
    onclose,
    initialView = 'references',
    initialService = '',
    onvoicepublished
  }: {
    onclose: () => void;
    initialView?: 'references' | 'prebuilt';
    initialService?: string;
    onvoicepublished?: (providerVoiceId: string) => void;
  } = $props();
</script>

<div class="fixed inset-0 z-[60] bg-black/40 p-3 backdrop-blur-sm sm:p-5" role="presentation">
  <div class="surface mx-auto flex h-[calc(100vh-1.5rem)] max-w-[92rem] flex-col overflow-hidden rounded-[1.7rem] sm:h-[calc(100vh-2.5rem)]" role="dialog" aria-modal="true" aria-label="Voice Library">
    <header class="flex shrink-0 items-center justify-between border-b border-[var(--line)] px-5 py-3">
      <div><div class="eyebrow">Voice Library</div><p class="muted mt-1 text-xs">Choose a pre-built voice or manage reusable cloning references without leaving this session.</p></div>
      <button onclick={onclose} class="rounded-xl p-2" aria-label="Close Voice Library"><X size={19}/></button>
    </header>
    <div class="min-h-0 flex-1 overflow-hidden p-4 sm:p-6">
      <VoiceManager onback={onclose} {initialView} {initialService} {onvoicepublished}/>
    </div>
  </div>
</div>
