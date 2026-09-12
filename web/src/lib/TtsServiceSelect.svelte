<script lang="ts">
  import { onMount } from 'svelte';
  import { speechServiceApi } from './admin-api';
  import type { TtsService } from './api-models';
  import { errorMessage } from './errors';
  import { selectableTtsServices, serviceMatches } from './tts-provider-policy';

  let {
    value,
    onchange,
    onloaded
  }: {
    value: string;
    onloaded?: (services: TtsService[]) => void;
    onchange: (value: string, resetSelection: boolean) => void;
  } = $props();
  let services = $state<TtsService[]>([]);
  let loading = $state(true);
  let error = $state('');
  const choices = $derived(selectableTtsServices(services, value));
  const selected = $derived(
    services.find((service) => serviceMatches(service, value))
  );

  onMount(async () => {
    try {
      services = (await speechServiceApi.catalogue()).services;
      onloaded?.(services);
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      loading = false;
    }
  });
</script>

<div class="min-w-0">
  <label class="block text-xs font-semibold">
    Service
    <select
      class="field mt-1 w-full"
      value={selected?.id ?? value}
      disabled={loading || Boolean(error)}
      onchange={(event) =>
        onchange(
          event.currentTarget.value,
          selected?.catalogue_role === 'compatibility' &&
            event.currentTarget.value === selected.replacement_service_id
        )}
    >
      {#if !selected}<option {value}>{value || 'Choose a provider'}</option
        >{/if}
      {#each choices as service}
        <option value={service.id}
          >{service.name}{service.catalogue_role === 'compatibility'
            ? ' · compatibility'
            : ''}</option
        >
      {/each}
    </select>
  </label>
  {#if selected?.id === 'audio_cpp'}
    <p class="muted mt-2 text-xs">
      audio.cpp is one provider with several local speech models. Install model
      packages in the Manager, then choose a model and its controls here.
    </p>
  {/if}
  {#if selected?.catalogue_role === 'compatibility'}
    <p class="muted mt-2 text-xs">
      This saved provider is kept for compatibility. For a saved session, use
      Generation settings to review the audio.cpp model and voice before
      switching. Changing the service here clears the old model and voice.
    </p>
  {/if}
  {#if error}<p class="mt-2 text-xs text-red-600" role="status">
      Could not load speech providers: {error}
    </p>{/if}
</div>

<style>
  .field {
    margin-top: 0.4rem;
    width: 100%;
    border: 1px solid var(--line);
    border-radius: 0.72rem;
    background: var(--paper);
    padding: 0.65rem 0.72rem;
    font-weight: 400;
    color: var(--ink);
  }
</style>
