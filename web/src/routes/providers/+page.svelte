<script lang="ts">
  import { page } from '$app/state';
  import CredentialManager from '$lib/CredentialManager.svelte';
  import LocalComponentsPanel from '$lib/LocalComponentsPanel.svelte';
  import ProviderManager from '$lib/ProviderManager.svelte';
  import ServiceManager from '$lib/ServiceManager.svelte';
  import SttServiceManager from '$lib/SttServiceManager.svelte';
  const initialTab = page.url.searchParams.get('tab');
  let tab = $state<'llm' | 'speech' | 'credentials'>(
    ['tts', 'local', 'speech'].includes(String(initialTab))
      ? 'speech'
      : initialTab === 'credentials'
        ? 'credentials'
        : 'llm'
  );
  let speechTab = $state<'local' | 'external'>(
    initialTab === 'local' || page.url.searchParams.get('speech') === 'local'
      ? 'local'
      : 'external'
  );
  let externalSpeechTab = $state<'tts' | 'stt'>(
    page.url.searchParams.get('service') === 'stt' ? 'stt' : 'tts'
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
    <button
      class="top-tab"
      class:active={tab === 'llm'}
      onclick={() => (tab = 'llm')}>LLM providers</button
    ><button
      class="top-tab"
      class:active={tab === 'speech'}
      onclick={() => (tab = 'speech')}>Speech services</button
    ><button
      class="top-tab"
      class:active={tab === 'credentials'}
      onclick={() => (tab = 'credentials')}>Other API keys</button
    >
  </div>
  <div class="mt-7">
    {#if tab === 'llm'}<ProviderManager
        onback={() => history.back()}
      />{:else if tab === 'speech'}<section>
        <div
          class="inline-flex rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] p-1"
          aria-label="Speech service location"
        >
          <button
            class="speech-tab"
            class:active={speechTab === 'local'}
            onclick={() => (speechTab = 'local')}>Local</button
          ><button
            class="speech-tab"
            class:active={speechTab === 'external'}
            onclick={() => (speechTab = 'external')}>External</button
          >
        </div>
        <div class="mt-6">
          {#if speechTab === 'local'}<LocalComponentsPanel />{:else}<div
              class="mb-6 flex gap-2 border-b border-[var(--line)]"
              aria-label="External speech service type"
            >
              <button
                class="service-tab"
                class:active={externalSpeechTab === 'tts'}
                onclick={() => (externalSpeechTab = 'tts')}
                >Text to speech</button
              ><button
                class="service-tab"
                class:active={externalSpeechTab === 'stt'}
                onclick={() => (externalSpeechTab = 'stt')}
                >Transcription</button
              >
            </div>
            {#if externalSpeechTab === 'tts'}<ServiceManager
              />{:else}<SttServiceManager />{/if}{/if}
        </div>
      </section>{:else}<CredentialManager />{/if}
  </div>
</div>

<style>
  .top-tab {
    border-bottom: 2px solid transparent;
    padding: 0.75rem 1rem;
    color: var(--muted);
    font-size: 0.85rem;
    font-weight: 700;
  }
  .top-tab.active {
    border-color: var(--accent);
    color: var(--ink);
  }
  .speech-tab {
    border-radius: 0.55rem;
    padding: 0.55rem 1rem;
    color: var(--muted);
    font-size: 0.78rem;
    font-weight: 700;
  }
  .speech-tab.active {
    background: var(--accent-soft);
    color: var(--ink);
  }
  .service-tab {
    border-bottom: 2px solid transparent;
    padding: 0.65rem 0.85rem;
    color: var(--muted);
    font-size: 0.78rem;
    font-weight: 700;
  }
  .service-tab.active {
    border-color: var(--accent);
    color: var(--ink);
  }
</style>
