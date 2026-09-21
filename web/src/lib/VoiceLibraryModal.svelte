<script lang="ts">
  import { X } from '@lucide/svelte';
  import VoiceCatalogLibrary from './VoiceCatalogLibrary.svelte';
  import type { CatalogVoice } from './voice-library-api';
  import { modalFocus } from './modal-focus';

  let {
    onclose,
    initialView = 'references',
    initialService = '',
    initialModel = '',
    initialVoice = '',
    castingLabel = '',
    onselect,
    onvoicepublished
  }: {
    onclose: () => void;
    initialView?: 'references' | 'prebuilt';
    initialService?: string;
    initialModel?: string;
    initialVoice?: string;
    castingLabel?: string;
    onselect?: (voice: CatalogVoice) => void;
    onvoicepublished?: (providerVoiceId: string) => void;
  } = $props();
  let library: VoiceCatalogLibrary;
  function close() {
    if (library) library.requestClose(onclose);
    else onclose();
  }
</script>

<div
  class="fixed inset-0 z-[60] bg-black/40 p-3 backdrop-blur-sm sm:p-5"
  role="presentation"
>
  <div
    use:modalFocus={{ onclose: close }}
    class="surface mx-auto flex h-[calc(100vh-1.5rem)] max-w-[92rem] flex-col overflow-hidden rounded-[1.7rem] sm:h-[calc(100vh-2.5rem)]"
    role="dialog"
    aria-modal="true"
    aria-label={castingLabel ? `Choose ${castingLabel}` : 'Voice Library'}
  >
    <header
      class="flex shrink-0 items-center justify-between border-b border-[var(--line)] px-5 py-3"
    >
      <div>
        <h2 class="font-semibold">
          {castingLabel ? `Choose ${castingLabel}` : 'Voice library'}
        </h2>
        <p class="muted mt-1 text-xs">
          Browse saved voices, compare candidates, or design a voice for this
          role.
        </p>
      </div>
      <button
        onclick={close}
        class="rounded-xl p-2"
        aria-label="Close Voice Library"><X size={19} /></button
      >
    </header>
    <div class="modal-scroll min-h-0 flex-1 p-4 sm:p-6">
      <VoiceCatalogLibrary
        bind:this={library}
        {initialView}
        {initialService}
        {initialModel}
        {initialVoice}
        embedded
        {onselect}
        {onvoicepublished}
      />
    </div>
  </div>
</div>
