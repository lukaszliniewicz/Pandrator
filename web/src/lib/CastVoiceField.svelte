<script lang="ts">
  import type { VoiceRecord } from './api-models';
  import type { VoiceBinding } from './generation-controls';
  import type VoiceLibraryModalType from './VoiceLibraryModal.svelte';
  import { voiceLibraryApi, type CatalogVoice } from './voice-library-api';
  import { readinessLabel, bestBinding, setupHref } from './voice-presentation';
  import AudioPlayer from './AudioPlayer.svelte';
  let VoiceLibraryModal = $state<typeof VoiceLibraryModalType | null>(null);
  let libraryOpen = $state(false);
  let libraryInitialVoice = $state('');
  let catalogVoice = $state<CatalogVoice | null>(null);
  let checking = $state(false);
  let checkFailed = $state(false);
  let playing = $state(false);
  let {
    label,
    value,
    inherited = null,
    inheritedLabel = 'Session voice',
    voices = [],
    suggestions = [],
    service = '',
    model = '',
    disabled = false,
    onchange
  }: {
    label: string;
    value?: VoiceBinding | null;
    inherited?: VoiceBinding | null;
    inheritedLabel?: string;
    voices?: VoiceRecord[];
    suggestions?: string[];
    service?: string;
    model?: string;
    disabled?: boolean;
    onchange: (value: VoiceBinding | null) => void;
  } = $props();
  const listId = $props.id();
  const effective = $derived(value ?? inherited);
  const managed = $derived(
    voices.find((voice) => voice.id === effective?.voice_id)
  );
  const voiceLabel = $derived(
    catalogVoice?.name ||
      managed?.name ||
      (effective?.voice_id
        ? 'Saved reference voice'
        : effective?.voice ||
          (effective?.voice_description
            ? 'Voice designed from description'
            : 'Service default'))
  );
  const mismatched = $derived(
    Boolean(
      (effective?.service && service && effective.service !== service) ||
      (effective?.model && model && effective.model !== model)
    )
  );
  const binding = $derived(
    catalogVoice?.compatibility.find(
      (item) =>
        (!service || item.service_id === service) &&
        (!model || item.model === model)
    ) ?? (!service && catalogVoice ? bestBinding(catalogVoice) : undefined)
  );
  const status = $derived(
    mismatched
      ? 'Assigned to a different service or model'
      : checking
        ? 'Checking voice setup…'
        : checkFailed
          ? 'Could not check voice setup'
          : catalogVoice
            ? readinessLabel(binding)
            : effective?.voice_description
              ? 'Requires a model with voice design support'
              : effective?.voice_id
                ? 'Reference not found in the library'
                : effective?.voice
                  ? 'Check service availability before generation'
                  : 'Uses the session’s default voice'
  );
  $effect(() => {
    const reference = effective;
    const targetService = reference?.service || service,
      targetModel = reference?.model || model;
    playing = false;
    catalogVoice = null;
    checkFailed = false;
    if (!reference?.voice_id && !reference?.voice) {
      checking = false;
      return;
    }
    let stale = false;
    checking = true;
    void voiceLibraryApi
      .query(
        reference.voice_id
          ? { query: reference.voice_id, kind: 'managed', limit: 1 }
          : {
              query: reference.voice!,
              kind: 'provider',
              service_id: targetService,
              model: targetModel,
              limit: 200
            }
      )
      .then((result) => {
        if (!stale)
          catalogVoice =
            result.items.find((item) =>
              reference.voice_id
                ? item.id === reference.voice_id
                : item.reference.kind === 'provider' &&
                  item.reference.voice === reference.voice &&
                  (!targetService ||
                    item.reference.service_id === targetService) &&
                  (!targetModel || item.reference.model === targetModel)
            ) ?? null;
      })
      .catch(() => {
        if (!stale) checkFailed = true;
      })
      .finally(() => {
        if (!stale) checking = false;
      });
    return () => {
      stale = true;
    };
  });
  function selectCatalogVoice(voice: CatalogVoice) {
    const reference = voice.reference;
    onchange(
      reference.kind === 'managed'
        ? {
            voice_id: reference.voice_id,
            voice: '',
            service: service || null,
            model: model || null
          }
        : {
            voice_id: null,
            voice: reference.voice,
            service: reference.service_id,
            model: reference.model
          }
    );
    libraryOpen = false;
  }
  async function browse(current = false) {
    libraryInitialVoice = current ? effective?.voice_id || '' : '';
    VoiceLibraryModal = (await import('./VoiceLibraryModal.svelte')).default;
    libraryOpen = true;
  }
  function update(key: keyof VoiceBinding, text: string) {
    const next = {
      ...value,
      [key]: text,
      service: value?.service || service || null,
      model: value?.model || model || null
    };
    if (key === 'voice') next.voice_id = null;
    if (key === 'voice_id') next.voice = '';
    onchange(
      next.voice || next.voice_id || next.voice_description ? next : null
    );
  }
</script>

<div class="min-w-0 space-y-2">
  <div class="flex flex-wrap items-center justify-between gap-2">
    <p class="text-sm font-semibold">{label}</p>
    <span class="muted text-xs"
      >{value ? 'Assigned here' : `Inherited · ${inheritedLabel}`}</span
    >
  </div>
  <div class="rounded-xl border border-[var(--line)] bg-[var(--paper)] p-3">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div class="min-w-0 flex-1">
        <p class="break-words font-semibold">{voiceLabel}</p>
        <p class="muted mt-1 text-xs" role="status">{status}</p>
      </div>
      <button
        type="button"
        class="btn btn-sm"
        {disabled}
        onclick={() => void browse()}
        aria-label={`Choose ${label}`}>Choose voice</button
      >
    </div>
    {#if effective?.voice_description}<p
        class="muted mt-2 line-clamp-2 text-xs"
      >
        {effective.voice_description}
      </p>{/if}
    {#if catalogVoice?.preview_artifact_id}<button
        class="btn btn-sm mt-2"
        aria-expanded={playing}
        onclick={() => (playing = !playing)}
        >{playing ? 'Hide preview' : 'Listen to reference'}</button
      >{/if}
    {#if !checking && catalogVoice && !binding?.ready && !mismatched}
      {#if catalogVoice.kind === 'managed'}<button
          class="btn btn-sm mt-2"
          {disabled}
          onclick={() => void browse(true)}>Samples &amp; setup</button
        >{:else}<a
          class="mt-2 inline-block text-xs text-[var(--accent)] underline"
          href={setupHref(binding)}>Check service and model</a
        >{/if}
    {/if}
    {#if playing && catalogVoice?.preview_artifact_id}<div class="mt-3">
        <AudioPlayer
          src={`/api/v1/artifacts/${catalogVoice.preview_artifact_id}/content`}
          label={`${voiceLabel} reference`}
        />
      </div>{/if}
  </div>
  <div class="flex flex-wrap items-start justify-between gap-2">
    <details class="min-w-0 flex-1 text-xs">
      <summary class="muted cursor-pointer py-2">Manual voice settings</summary>
      <div class="mt-2 space-y-3">
        <label class="block"
          >Provider voice ID<input
            class="input mt-1 w-full"
            {disabled}
            list={listId}
            value={value?.voice ?? ''}
            placeholder="Use the inherited voice"
            oninput={(event) => update('voice', event.currentTarget.value)}
          /></label
        >
        <datalist id={listId}
          >{#each suggestions as voice}<option value={voice}
            ></option>{/each}</datalist
        >
        <label class="block"
          >Managed reference<select
            class="input mt-1 w-full"
            {disabled}
            value={value?.voice_id ?? ''}
            onchange={(event) => update('voice_id', event.currentTarget.value)}
            ><option value="">No reference</option
            >{#each voices as voice}<option value={voice.id}
                >{voice.name}</option
              >{/each}</select
          ></label
        >
        <label class="block"
          >Stable voice description <span class="muted"
            >· for models that design a voice</span
          ><textarea
            class="input mt-1 w-full"
            rows="2"
            maxlength="4000"
            {disabled}
            value={value?.voice_description ?? ''}
            oninput={(event) =>
              update('voice_description', event.currentTarget.value)}
          ></textarea></label
        >
      </div>
    </details>
    {#if value}<button
        class="btn btn-sm"
        {disabled}
        onclick={() => onchange(null)}>Use inherited voice</button
      >{/if}
  </div>
</div>

{#if libraryOpen && VoiceLibraryModal}
  <VoiceLibraryModal
    initialService={service}
    initialModel={model}
    initialVoice={libraryInitialVoice}
    castingLabel={label}
    onselect={selectCatalogVoice}
    onclose={() => (libraryOpen = false)}
  />
{/if}
