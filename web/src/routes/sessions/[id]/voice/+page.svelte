<script lang="ts">
  import { page } from '$app/state';
  import { ExternalLink, Mic2, RefreshCw } from '@lucide/svelte';
  import { appState } from '$lib/app-state.svelte';
  import { onMount } from 'svelte';
  import { sessionApi } from '$lib/domain-api';
  import type { SettingsPayload } from '$lib/api-models';
  import { errorMessage } from '$lib/errors';
  import VoiceSetupCard from '$lib/VoiceSetupCard.svelte';
  import { useSessionContext } from '$lib/session-context';
  const context = useSessionContext();
  import SettingsPanel from '$lib/SettingsPanel.svelte';
  import VoiceLibraryModal from '$lib/VoiceLibraryModal.svelte';
  const sessionId = String(page.params.id);
  let voicesOpen = $state(false);
  let settings = $state<SettingsPayload | null>(null);
  let error = $state('');
  const service = $derived(String(settings?.effective.service ?? ''));
  const model = $derived(String(settings?.effective.model ?? ''));
  async function loadSettings() {
    try {
      settings = await sessionApi.settings(sessionId, 'tts');
      error = '';
    } catch (caught) {
      error = errorMessage(caught);
    }
  }
  onMount(() => {
    void loadSettings();
  });
</script>

<div class="space-y-5">
  <div class="flex flex-wrap items-end justify-between gap-4">
    <div>
      <h2 class="text-2xl font-semibold">Voice and audio</h2>
      <p class="muted mt-2">
        Review your cast, choose the speech model, and adjust generation and
        audio settings.
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
  {#if error}<p role="alert" class="text-sm text-red-600">{error}</p>{/if}
  {#if context.session && ['audiobook', 'voiceover'].includes(context.session.workflow_kind)}
    <VoiceSetupCard
      {sessionId}
      initialCastOpen={true}
      onchanged={loadSettings}
    />
  {/if}
  <SettingsPanel
    {sessionId}
    section="tts"
    title="Speech generation"
    onpersisted={(payload) => {
      settings = payload;
    }}
  /><SettingsPanel
    {sessionId}
    section="audio"
    title="Verification, silence, fades, and assembly"
    description="Optional signal verification examines each raw generated take before fades or future normalization, and marks suspicious segments for review."
  /><SettingsPanel {sessionId} section="rvc" title="RVC variants" />
</div>
{#if voicesOpen}<VoiceLibraryModal
    initialService={service}
    initialModel={model}
    onclose={() => (voicesOpen = false)}
  />{/if}

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
