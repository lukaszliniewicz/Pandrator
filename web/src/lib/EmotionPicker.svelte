<script lang="ts">
  import { tick } from 'svelte';
  import { emotionChoices, emotionChoiceKind } from './speech-emotions';
  import { deliverySupport, type SpeechCapabilities } from './speech-support';
  let {
    value = '',
    capabilities,
    scope = 'segment',
    enabled = true,
    disabled = false,
    mixed = false,
    onchange
  }: {
    value?: string;
    capabilities?: SpeechCapabilities;
    scope?: 'segment' | 'phrase';
    enabled?: boolean;
    disabled?: boolean;
    mixed?: boolean;
    onchange: (value: string) => void;
  } = $props();
  let custom = $state(false);
  let customInput = $state<HTMLInputElement>();
  const choices = $derived(emotionChoices(capabilities));
  const normalized = $derived(value.trim().toLowerCase());
  const isCustom = $derived(
    custom || Boolean(value && !choices.includes(normalized))
  );
  const support = $derived(
    deliverySupport(capabilities, 'emotion', value, scope)
  );
  const id = $props.id();
  async function choose(next: string) {
    if (next === '__custom__') {
      custom = true;
      await tick();
      customInput?.focus();
    } else {
      custom = false;
      onchange(next);
    }
  }
</script>

<label class="emotion-label" for={id}
  >Emotion{#if mixed}<span class="muted"> · Mixed</span>{/if}</label
>
<select
  {id}
  class="input w-full"
  data-delivery-input="emotion"
  {disabled}
  value={isCustom ? '__custom__' : normalized}
  onchange={(event) => void choose(event.currentTarget.value)}
>
  <option value=""
    >{mixed ? 'Mixed · leave unchanged' : 'Inherit the default'}</option
  >
  {#each choices as emotion}<option value={emotion}
      >{emotion[0].toUpperCase() + emotion.slice(1)}</option
    >{/each}
  <option value="__custom__">Custom emotion…</option>
</select>
{#if isCustom}<label class="custom-label"
    >Custom emotion<input
      bind:this={customInput}
      class="input mt-1 w-full"
      {value}
      maxlength="80"
      {disabled}
      placeholder="For example: wistful, holding back tears"
      oninput={(event) => onchange(event.currentTarget.value)}
    /></label
  >{/if}
<p class="emotion-options-note">{emotionChoiceKind(capabilities)}</p>
<p class="emotion-support" data-support={support.status} role="status">
  <strong>{support.label}</strong> · {support.status === 'hint' &&
  scope === 'phrase'
    ? 'Sent as a hint for these words; nearby words may also be affected.'
    : support.description}
  {#if !enabled}<span class="delivery-off"
      >Delivery is off; this emotion is saved but omitted from generation.</span
    >{/if}
</p>

<style>
  .emotion-label {
    display: block;
    margin-bottom: 0.35rem;
    font-size: 0.82rem;
    font-weight: 600;
  }
  .custom-label {
    display: block;
    margin-top: 0.6rem;
    font-size: 0.78rem;
  }
  .emotion-options-note {
    margin-top: 0.35rem;
    color: var(--muted);
    font-size: 0.68rem;
  }
  .emotion-support {
    margin-top: 0.5rem;
    font-size: 0.72rem;
    line-height: 1.5;
    color: var(--muted);
  }
  .emotion-support[data-support='unsupported'],
  .emotion-support[data-support='unknown'] {
    color: color-mix(in srgb, var(--ink) 65%, #b8842e);
  }
  .delivery-off {
    display: block;
    margin-top: 0.25rem;
  }
</style>
