<script lang="ts">
  import type { VoiceRecord } from './api-models';
  import type { VoiceBinding } from './generation-controls';
  import type { Component } from 'svelte';
  let VoiceLibraryModal = $state<Component<any> | null>(null);
  import type { CatalogVoice } from './voice-library-api';
  let libraryOpen = $state(false);
  let selectedReference = $state<{ id: string; name: string } | null>(null);
  let {
    label,
    value,
    voices = [],
    suggestions = [],
    service = '',
    model = '',
    disabled = false,
    onchange
  }: {
    label: string;
    value?: VoiceBinding | null;
    voices?: VoiceRecord[];
    suggestions?: string[];
    service?: string;
    model?: string;
    disabled?: boolean;
    onchange: (value: VoiceBinding | null) => void;
  } = $props();
  const listId = $props.id();
  const managedLabel = $derived(
    value?.voice_id
      ? voices.find((v) => v.id === value.voice_id)?.name ||
          (selectedReference?.id === value.voice_id
            ? selectedReference.name
            : 'Saved reference voice')
      : ''
  );
  function selectCatalogVoice(voice: CatalogVoice) {
    const reference = voice.reference;
    if (reference.kind === 'managed')
      selectedReference = { id: reference.voice_id, name: voice.name };
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
  <label class="block text-sm"
    >{label}
    <input
      class="input mt-1 w-full"
      {disabled}
      list={listId}
      value={value?.voice ?? ''}
      placeholder={managedLabel || 'Use inherited voice'}
      oninput={(event) => update('voice', event.currentTarget.value)}
    />
  </label>
  <datalist id={listId}
    >{#each suggestions as voice}<option value={voice}
      ></option>{/each}</datalist
  >
  <button
    type="button"
    class="btn btn-sm"
    {disabled}
    onclick={async () => {
      VoiceLibraryModal = (await import('./VoiceLibraryModal.svelte')).default;
      libraryOpen = true;
    }}>Browse voice library</button
  >
  {#if managedLabel}<p class="text-xs break-words">
      Reference: <strong>{managedLabel}</strong>
    </p>{/if}
  {#if value?.service || value?.model}<p class="muted text-xs break-words">
      Bound to {value.service || service} · {value.model ||
        model ||
        'selected model'}
    </p>{/if}
  <details class="text-xs">
    <summary class="cursor-pointer muted"
      >Reference voice or voice description</summary
    >
    <div class="mt-2 space-y-2">
      <label class="block"
        >Managed reference
        <select
          class="input mt-1 w-full"
          {disabled}
          value={value?.voice_id ?? ''}
          onchange={(event) => update('voice_id', event.currentTarget.value)}
        >
          <option value="">No reference</option>
          {#each voices as voice}<option value={voice.id}
              >{voice.name} · {String(
                voice.metadata_json?.voice_category ?? 'unspecified'
              )}</option
            >{/each}
        </select>
      </label>
      <label class="block"
        >Stable voice description <span class="muted"
          >For models that design a voice</span
        >
        <textarea
          class="input mt-1 w-full"
          rows="2"
          maxlength="4000"
          {disabled}
          value={value?.voice_description ?? ''}
          oninput={(event) =>
            update('voice_description', event.currentTarget.value)}></textarea>
      </label>
      {#if value}<button class="btn" {disabled} onclick={() => onchange(null)}
          >Use inherited voice</button
        >{/if}
    </div>
  </details>
</div>

{#if libraryOpen && VoiceLibraryModal}
  <VoiceLibraryModal
    initialService={service}
    initialModel={model}
    onselect={selectCatalogVoice}
    onclose={() => (libraryOpen = false)}
  />
{/if}
