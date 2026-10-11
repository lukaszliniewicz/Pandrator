<script lang="ts">
  import type { ControlField } from './speech-markup-editor';
  import type { SpeechDelivery } from './speech-inspector';
  import { deliverySupport, type SpeechCapabilities } from './speech-support';
  import EmotionPicker from './EmotionPicker.svelte';
  import EmotionIntensity from './EmotionIntensity.svelte';
  let {
    value,
    label = 'Delivery instruction',
    mixed = [],
    disabled = false,
    advanced = false,
    mode = 'all',
    capabilities,
    scope = 'segment',
    enabled = true,
    showInstructionSupport = true,
    onchange,
    onintensity
  }: {
    value: Partial<SpeechDelivery>;
    label?: string;
    mixed?: string[];
    disabled?: boolean;
    advanced?: boolean;
    mode?: 'all' | 'instruction' | 'emotion';
    capabilities?: SpeechCapabilities;
    scope?: 'segment' | 'phrase';
    enabled?: boolean;
    showInstructionSupport?: boolean;
    onchange: (field: ControlField, value: string) => void;
    onintensity?: (value: number | null) => void;
  } = $props();
  const instructionSupport = $derived(
    deliverySupport(capabilities, 'instruction', value.instruction, scope)
  );
</script>

{#if mode !== 'emotion'}
  <label class="block text-sm font-medium">
    {label}
    {#if mixed.includes('instruction')}<span class="muted text-xs font-normal">
        · Mixed</span
      >{/if}
    <textarea
      class="input mt-1 w-full"
      rows="3"
      maxlength="1200"
      {disabled}
      value={value.instruction ?? ''}
      placeholder={mixed.includes('instruction')
        ? 'Different directions; leave unchanged or replace them.'
        : 'For example: quietly, with dry amusement'}
      data-delivery-input="instruction"
      oninput={(event) => onchange('instruction', event.currentTarget.value)}
    ></textarea>
  </label>
  {#if showInstructionSupport}<p
      class="support-note"
      data-support={instructionSupport.status}
    >
      <strong>{instructionSupport.label}</strong> · {instructionSupport.description}
      {#if !enabled}<span class="block"
          >Delivery is off; this instruction remains saved.</span
        >{/if}
    </p>{/if}
{/if}
{#if mode === 'emotion'}
  <EmotionPicker
    value={value.emotion ?? ''}
    {capabilities}
    {scope}
    {enabled}
    {disabled}
    mixed={mixed.includes('emotion')}
    onchange={(next) => onchange('emotion', next)}
  />
  <EmotionIntensity
    value={value.emotion_intensity}
    emotion={value.emotion ?? ''}
    {capabilities}
    {scope}
    {enabled}
    {disabled}
    onchange={onintensity}
  />
{:else if mode === 'all'}
  <details open={advanced} class="text-sm">
    <summary class="muted cursor-pointer py-2"
      >Emotion, pace and emphasis</summary
    >
    <div class="mt-2 space-y-3">
      <div>
        <EmotionPicker
          value={value.emotion ?? ''}
          {capabilities}
          {scope}
          {enabled}
          {disabled}
          mixed={mixed.includes('emotion')}
          onchange={(next) => onchange('emotion', next)}
        />
        <EmotionIntensity
          value={value.emotion_intensity}
          emotion={value.emotion ?? ''}
          {capabilities}
          {scope}
          {enabled}
          {disabled}
          onchange={onintensity}
        />
      </div>
      <div class="grid gap-3 sm:grid-cols-2">
        {#each ['pace', 'cadence', 'emphasis'] as name}
          {@const field = name as ControlField}
          {@const support = deliverySupport(
            capabilities,
            field,
            value[field],
            scope
          )}
          <label class="block text-sm">
            {name[0].toUpperCase() +
              name.slice(1)}{#if mixed.includes(field)}<span
                class="muted text-xs"
              >
                · Mixed</span
              >{/if}
            <select
              class="input mt-1 w-full"
              {disabled}
              value={value[field] ?? ''}
              onchange={(event) => onchange(field, event.currentTarget.value)}
            >
              <option value=""
                >{mixed.includes(field)
                  ? 'Mixed · leave unchanged'
                  : 'Inherit the default'}</option
              >
              {#each field === 'pace' ? [['natural', 'Natural'], ['slower', 'Slow'], ['brisk', 'Fast']] : field === 'cadence' ? [['continuing', 'Continuing'], ['concluding', 'Concluding'], ['questioning', 'Questioning'], ['contrast', 'Contrast']] : [['light', 'Light'], ['moderate', 'Moderate'], ['strong', 'Strong']] as option}
                <option value={option[0]}>{option[1]}</option>
              {/each}
            </select>
            <span
              class="support-note"
              data-support={support.status}
              title={support.description}
              >{support.label}{!enabled ? ' · Delivery off' : ''}</span
            >
          </label>
        {/each}
      </div>
    </div>
  </details>
{/if}

<style>
  .support-note {
    display: block;
    margin-top: 0.35rem;
    font-size: 0.7rem;
    line-height: 1.5;
    color: var(--muted);
    font-weight: 400;
  }
  .support-note[data-support='unsupported'],
  .support-note[data-support='unknown'] {
    color: color-mix(in srgb, var(--ink) 65%, #b8842e);
  }
</style>
