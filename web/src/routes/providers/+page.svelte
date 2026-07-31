<script lang="ts">
  import { page } from '$app/state';
  import CredentialManager from '$lib/CredentialManager.svelte';
  import LocalComponentsPanel from '$lib/LocalComponentsPanel.svelte';
  import ProviderManager from '$lib/ProviderManager.svelte';
  import ServiceManager from '$lib/ServiceManager.svelte';
  const initialTab = page.url.searchParams.get('tab');
  let tab = $state<'llm' | 'tts' | 'local' | 'credentials'>(
    initialTab === 'tts'
      ? 'tts'
      : initialTab === 'local'
        ? 'local'
        : initialTab === 'credentials'
          ? 'credentials'
          : 'llm'
  );
</script>

<div class="mx-auto max-w-7xl">
  <header>
    <div class="eyebrow">Providers & services</div>
    <h1 class="mt-2 text-4xl font-semibold">Connections</h1>
    <p class="muted mt-3">
      Application-wide model, cost, endpoint, catalogue, credential, readiness,
      and local component settings.
    </p>
  </header>
  <div class="mt-7 flex gap-2 overflow-x-auto border-b border-[var(--line)]">
    <button class:active={tab === 'llm'} onclick={() => (tab = 'llm')}
      >LLM providers</button
    ><button class:active={tab === 'tts'} onclick={() => (tab = 'tts')}
      >TTS services</button
    ><button class:active={tab === 'local'} onclick={() => (tab = 'local')}
      >Local components</button
    ><button
      class:active={tab === 'credentials'}
      onclick={() => (tab = 'credentials')}>Other API keys</button
    >
  </div>
  <div class="mt-7">
    {#if tab === 'llm'}<ProviderManager
        onback={() => history.back()}
      />{:else if tab === 'tts'}<ServiceManager
      />{:else if tab === 'local'}<LocalComponentsPanel
      />{:else}<CredentialManager />{/if}
  </div>
</div>

<style>
  button {
    border-bottom: 2px solid transparent;
    padding: 0.75rem 1rem;
    color: var(--muted);
    font-size: 0.85rem;
    font-weight: 700;
  }
  button.active {
    border-color: var(--accent);
    color: var(--ink);
  }
</style>
