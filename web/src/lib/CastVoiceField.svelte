<script lang="ts">
  import type { VoiceRecord } from './api-models';
  import type { VoiceBinding } from './generation-controls';
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
      placeholder="Use inherited voice"
      oninput={(event) => update('voice', event.currentTarget.value)}
    />
  </label>
  <datalist id={listId}
    >{#each suggestions as voice}<option value={voice}
      ></option>{/each}</datalist
  >
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
