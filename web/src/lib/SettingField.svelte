<script lang="ts">
  import { Plus, Trash2 } from '@lucide/svelte';
  import {
    isMultiline,
    numberPresentation,
    optionsFor,
    settingLabel
  } from './settings-fields';

  type SettingValue = unknown;

  let {
    section,
    keyName,
    value,
    onchange,
    compact = false
  }: {
    section: string;
    keyName: string;
    value: SettingValue;
    onchange: (value: SettingValue) => void;
    compact?: boolean;
  } = $props();
  let newKey = $state('');
  let rangeValue = $derived(Number(value ?? 0));
  const choices = $derived(optionsFor(section, keyName));
  const numberMeta = $derived(numberPresentation(keyName));
  const objectEntries = $derived(
    Object.entries(
      value && typeof value === 'object' && !Array.isArray(value) ? value : {}
    )
  );

  function objectValue(): Record<string, unknown> {
    return value && typeof value === 'object' && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : {};
  }
  function cast(raw: string) {
    if (typeof value === 'number') return Number(raw);
    return raw;
  }

  function setObject(key: string, next: string) {
    const current = { ...objectValue() };
    const prior = current[key];
    current[key] = typeof prior === 'number' ? Number(next) : next;
    onchange(current);
  }

  function removeObject(key: string) {
    const current = { ...objectValue() };
    delete current[key];
    onchange(current);
  }

  function addObject() {
    const key = newKey.trim();
    if (!key || Object.prototype.hasOwnProperty.call(objectValue(), key))
      return;
    onchange({ ...objectValue(), [key]: 0 });
    newKey = '';
  }
</script>

<label class:text-xs={compact} class="block text-sm font-semibold"
  >{settingLabel(keyName)}
  {#if typeof value === 'boolean'}
    <span class="field flex min-h-11 items-center gap-2"
      ><input
        type="checkbox"
        checked={value}
        onchange={(event) => onchange(event.currentTarget.checked)}
        class="accent-[var(--accent)]"
      /><span class="font-normal">{value ? 'Enabled' : 'Disabled'}</span></span
    >
  {:else if choices}
    <select
      class="field"
      value={value ?? ''}
      onchange={(event) => onchange(cast(event.currentTarget.value))}
    >
      {#if value && !choices.some((item) => String(item.value) === String(value))}<option
          {value}>{value}</option
        >{/if}
      {#each choices as item}<option value={item.value}>{item.label}</option
        >{/each}
    </select>
  {:else if typeof value === 'number' && numberMeta.range}
    <span class="field range-field"
      ><input
        type="range"
        bind:value={rangeValue}
        min={numberMeta.min}
        max={numberMeta.max}
        step={numberMeta.step}
        oninput={() => onchange(Number(rangeValue))}
      /><output>{value}{numberMeta.suffix ?? ''}</output></span
    >
  {:else if typeof value === 'number'}
    <input
      class="field"
      type="number"
      {value}
      min={numberMeta.min}
      max={numberMeta.max}
      step={numberMeta.step ?? 'any'}
      oninput={(event) => onchange(Number(event.currentTarget.value))}
    />
  {:else if value && typeof value === 'object'}
    <span class="field block space-y-2">
      {#each objectEntries as [key, item]}
        <span class="object-row"
          ><span class="min-w-0 text-xs font-semibold">{settingLabel(key)}</span
          ><input
            value={String(item ?? '')}
            oninput={(event) => setObject(key, event.currentTarget.value)}
            class="subfield"
          /><button
            type="button"
            onclick={() => removeObject(key)}
            class="btn btn-icon btn-quiet"
            aria-label={`Remove ${settingLabel(key)}`}
            ><Trash2 size={13} /></button
          ></span
        >
      {/each}
      <span class="flex gap-2"
        ><input
          bind:value={newKey}
          placeholder="Add named value"
          class="subfield min-w-0 flex-1"
        /><button
          type="button"
          onclick={addObject}
          class="btn btn-sm btn-secondary"><Plus size={13} /> Add</button
        ></span
      >
    </span>
  {:else if isMultiline(keyName)}
    <textarea
      class="field min-h-24 resize-y"
      value={String(value ?? '')}
      oninput={(event) => onchange(event.currentTarget.value)}></textarea>
  {:else}
    <input
      class="field"
      value={String(value ?? '')}
      oninput={(event) => onchange(event.currentTarget.value)}
    />
  {/if}
</label>

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
  .subfield {
    width: 100%;
    border: 1px solid var(--line);
    border-radius: 0.55rem;
    background: var(--paper-strong);
    padding: 0.45rem 0.55rem;
    font-size: 0.75rem;
    font-weight: 400;
    color: var(--ink);
  }
  .range-field {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: 0.65rem;
  }
  .range-field input {
    min-width: 0;
    width: 100%;
    accent-color: var(--accent);
  }
  .range-field output {
    min-width: 2.8rem;
    text-align: right;
    font-variant-numeric: tabular-nums;
    font-size: 0.75rem;
    font-weight: 700;
  }
  .object-row {
    display: grid;
    grid-template-columns: minmax(6rem, 0.7fr) minmax(0, 1fr) auto;
    align-items: center;
    gap: 0.5rem;
  }
  @media (max-width: 480px) {
    .object-row {
      grid-template-columns: minmax(0, 1fr) auto;
    }
    .object-row > span {
      grid-column: 1/-1;
    }
    .object-row input {
      grid-column: 1;
    }
    .object-row button {
      grid-column: 2;
    }
  }
</style>
