<script lang="ts">
  import ParameterLabel from './ParameterLabel.svelte';
  import type { SoundParameter } from './sound-api';

  let {
    controls,
    values = $bindable(),
    disabled = false
  }: {
    controls: Record<string, SoundParameter>;
    values: Record<string, string | number | boolean>;
    disabled?: boolean;
  } = $props();

  function set(name: string, value: string | number | boolean | undefined) {
    const next = { ...values };
    if (value === undefined) delete next[name];
    else next[name] = value;
    values = next;
  }
  function helpText(control: SoundParameter) {
    const parts = [
      control.description ?? 'Leave blank to use the model’s default setting.'
    ];
    if (control.default !== undefined)
      parts.push(`Default: ${control.default}.`);
    if (control.exclusive_minimum !== undefined)
      parts.push(`Must be greater than ${control.exclusive_minimum}.`);
    if (control.minimum !== undefined && control.maximum !== undefined)
      parts.push(`Allowed range: ${control.minimum}–${control.maximum}.`);
    else if (control.minimum !== undefined)
      parts.push(`Minimum: ${control.minimum}.`);
    if (control.options?.length)
      parts.push(`Choices: ${control.options.join(', ')}.`);
    return parts.join(' ');
  }
</script>

<div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
  {#each Object.entries(controls) as [name, control] (name)}
    <label class="block text-sm">
      {#if control.type === 'boolean'}
        <span class="flex items-center gap-2 py-2">
          <input
            type="checkbox"
            checked={Boolean(values[name] ?? control.default)}
            onchange={(event) => set(name, event.currentTarget.checked)}
            {disabled}
          />
          <ParameterLabel
            section="sound_generation"
            {name}
            label={control.label}
            description={helpText(control)}
          />
        </span>
      {:else}
        <span class="font-medium">
          <ParameterLabel
            section="sound_generation"
            {name}
            label={control.label}
            description={helpText(control)}
          />
        </span>
        {#if control.options?.length}
          <select
            class="field mt-1"
            value={String(values[name] ?? control.default ?? '')}
            onchange={(event) =>
              set(name, event.currentTarget.value || undefined)}
            {disabled}
          >
            {#if control.default === undefined}
              <option value="">Runtime default</option>
            {/if}
            {#each control.options as option}
              <option value={option}>{option}</option>
            {/each}
          </select>
        {:else if control.type === 'string'}
          <input
            class="field mt-1"
            value={String(values[name] ?? '')}
            placeholder={String(control.default ?? '')}
            maxlength="2000"
            oninput={(event) =>
              set(name, event.currentTarget.value || undefined)}
            {disabled}
          />
        {:else}
          <input
            class="field mt-1"
            type="number"
            value={values[name] === undefined ? '' : Number(values[name])}
            placeholder={String(control.default ?? '')}
            min={control.minimum ?? control.exclusive_minimum}
            max={control.maximum}
            step={control.type === 'integer' ? 1 : 'any'}
            oninput={(event) =>
              set(
                name,
                event.currentTarget.value === ''
                  ? undefined
                  : event.currentTarget.valueAsNumber
              )}
            {disabled}
          />
        {/if}
      {/if}
    </label>
  {/each}
</div>

<style>
  .field {
    display: block;
    width: 100%;
    border: 1px solid var(--line);
    border-radius: 0.55rem;
    padding: 0.6rem 0.7rem;
    background: var(--paper-strong);
  }
</style>
