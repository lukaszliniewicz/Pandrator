<script lang="ts">
  import { page } from '$app/state';
  import CredentialManager from '$lib/CredentialManager.svelte';
  import LocalComponentsPanel from '$lib/LocalComponentsPanel.svelte';
  import ProviderManager from '$lib/ProviderManager.svelte';
  import ServiceManager from '$lib/ServiceManager.svelte';
  import SttServiceManager from '$lib/SttServiceManager.svelte';
  import WorkspaceSpeechServices from '$lib/WorkspaceSpeechServices.svelte';
  import { appState } from '$lib/app-state.svelte';
  const workspaceId = $derived(
    appState.collaborationEnabled
      ? appState.activeWorkspaceId || undefined
      : undefined
  );
  const workspaceName = $derived(
    appState.collaborationEnabled
      ? (appState.activeWorkspace?.name ?? 'this workspace')
      : 'your projects'
  );
  const canManageWorkspace = $derived(
    !appState.collaborationEnabled ||
      Boolean(appState.activeWorkspace?.allowed_actions.includes('manage'))
  );
  const providerScopeKey = $derived(
    `${appState.contentKey}:${appState.activeWorkspace?.role ?? ''}:${appState.activeWorkspace?.allowed_actions.join(',') ?? ''}`
  );
  const initialTab = page.url.searchParams.get('tab');
  let tab = $state<'llm' | 'speech' | 'credentials'>(
    initialTab === 'credentials'
      ? 'credentials'
      : initialTab === 'llm'
        ? 'llm'
        : 'speech'
  );
  let speechTab = $state<'local' | 'external'>(
    initialTab === 'tts' ||
      initialTab === 'external' ||
      page.url.searchParams.get('speech') === 'external'
      ? 'external'
      : 'local'
  );
  let externalSpeechTab = $state<'tts' | 'stt'>(
    page.url.searchParams.get('service') === 'stt' ? 'stt' : 'tts'
  );
</script>

<div class="mx-auto max-w-7xl">
  <header>
    <h1 class="mt-2 text-4xl font-semibold">Providers &amp; services</h1>
    <p class="muted mt-3">
      LLM connections and API keys for {workspaceName}. Local models and
      services are managed for this installation.
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
    {#if appState.collaborationEnabled && !appState.workspacesReady}
      <p class="muted" role="status">Loading workspace…</p>
    {:else}
      {#if tab === 'llm'}{#key providerScopeKey}<ProviderManager
            {workspaceId}
            {workspaceName}
            onback={() => history.back()}
          />{/key}{:else if tab === 'speech'}<section>
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
              {#key providerScopeKey}
                {#if !canManageWorkspace}<WorkspaceSpeechServices
                    {workspaceId}
                    {workspaceName}
                    kind={externalSpeechTab}
                  />
                {:else if externalSpeechTab === 'tts'}<ServiceManager
                    {workspaceId}
                    {workspaceName}
                  />{:else}<SttServiceManager
                    {workspaceId}
                    {workspaceName}
                  />{/if}
              {/key}{/if}
          </div>
        </section>{:else}{#key providerScopeKey}{#if canManageWorkspace}<CredentialManager
              {workspaceId}
            />{:else}<p class="muted">
              Workspace managers manage API keys for this workspace.
            </p>{/if}{/key}{/if}
    {/if}
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
