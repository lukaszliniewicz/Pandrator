<script lang="ts">
  import { speechRecognitionApi } from './admin-api';
  import type { SttService, TtsService, TtsCatalogue } from './api-models';
  import { fetchTtsCatalogue } from './speech-catalogue-api';
  import { errorMessage } from './errors';
  import { speechCatalogueKey } from './tts-catalogue-cache';

  let {
    workspaceId,
    workspaceName,
    kind
  }: {
    workspaceId?: string;
    workspaceName: string;
    kind: 'tts' | 'stt';
  } = $props();
  let services = $state<Array<TtsService | SttService>>([]);
  let loading = $state(true);
  let error = $state('');

  $effect(() => {
    const scope = { workspaceId };
    const scopeKey = speechCatalogueKey(scope);
    let active = true;
    loading = true;
    error = '';
    const request =
      kind === 'tts'
        ? fetchTtsCatalogue<TtsCatalogue>(false, scope, 'summary')
        : speechRecognitionApi.catalogue(scope);
    void request
      .then((catalogue) => {
        if (active && scopeKey === speechCatalogueKey(scope))
          services = catalogue.services;
      })
      .catch((caught) => {
        if (active) error = errorMessage(caught);
      })
      .finally(() => {
        if (active) loading = false;
      });
    return () => {
      active = false;
    };
  });
</script>

<section
  aria-label={kind === 'tts'
    ? 'Available speech providers'
    : 'Available transcription providers'}
>
  <h2 class="text-2xl font-semibold">
    {kind === 'tts' ? 'Speech providers' : 'Transcription providers'}
  </h2>
  <p class="muted mt-2 text-sm">
    Providers available to projects in {workspaceName}. Workspace managers
    manage connections.
  </p>
  {#if loading}<p class="muted mt-5" role="status">Loading providers…</p>
  {:else if error}<p class="error-banner mt-5" role="alert">{error}</p>
  {:else if !services.length}<p class="muted mt-5">
      No providers are configured for this workspace.
    </p>
  {:else}<ul class="mt-5 grid gap-4 sm:grid-cols-2">
      {#each services as service (service.id)}
        <li class="surface min-w-0 p-5">
          <h3 class="break-words font-semibold">{service.name}</h3>
          <p class="muted mt-2 break-words text-sm">
            {service.models?.length ?? 0}
            {service.models?.length === 1
              ? 'model'
              : 'models'}{service.default_model
              ? ` · default ${service.default_model}`
              : ''}
          </p>
        </li>
      {/each}
    </ul>{/if}
</section>
