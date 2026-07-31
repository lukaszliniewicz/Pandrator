<script lang="ts">
  import { page } from '$app/state';
  import { ExternalLink, Mic2, RefreshCw, X } from '@lucide/svelte';
  import { appState } from '$lib/app-state.svelte';
  import { modalFocus } from '$lib/modal-focus';
  import SettingsPanel from '$lib/SettingsPanel.svelte';
  import VoiceManager from '$lib/VoiceManager.svelte';
  const sessionId = String(page.params.id);
  let voicesOpen = $state(false);
</script>

<div class="space-y-5">
  <div class="flex flex-wrap items-end justify-between gap-4">
    <div>
      <h2 class="text-2xl font-semibold">Voice and audio</h2>
      <p class="muted mt-2">
        Choose a detected service and voice, then reveal backend-specific
        controls when needed.
      </p>
    </div>
    <div class="flex flex-wrap gap-2">
      <a href="/providers?tab=tts" class="tool"
        ><ExternalLink size={16} /> Providers & services</a
      ><button onclick={() => appState.refreshCapabilities()} class="tool"
        ><RefreshCw size={16} /> Detect services</button
      ><button
        onclick={() => (voicesOpen = true)}
        class="tool bg-[var(--accent)] text-white"
        ><Mic2 size={16} /> Voice library</button
      >
    </div>
  </div>
  <SettingsPanel
    {sessionId}
    section="tts"
    title="Speech generation"
  /><SettingsPanel
    {sessionId}
    section="audio"
    title="Verification, silence, fades, and assembly"
    description="Optional signal verification examines each raw generated take before fades or future normalization, and marks suspicious segments for review."
  /><SettingsPanel {sessionId} section="rvc" title="RVC variants" />
</div>
{#if voicesOpen}<div
    use:modalFocus={{ onclose: () => (voicesOpen = false) }}
    class="fixed inset-0 z-[70] bg-[var(--paper)] p-4 sm:p-7"
    role="dialog"
    aria-modal="true"
    aria-label="Voice library"
  >
    <button
      onclick={() => (voicesOpen = false)}
      aria-label="Close voice library"
      class="fixed right-6 top-6 z-10 rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] p-2"
      ><X size={19} /></button
    >
    <div class="h-full">
      <VoiceManager onback={() => (voicesOpen = false)} />
    </div>
  </div>{/if}

<style>
  .tool {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    border: 1px solid var(--line);
    border-radius: 0.75rem;
    padding: 0.65rem 0.8rem;
    font-size: 0.78rem;
    font-weight: 700;
  }
</style>
