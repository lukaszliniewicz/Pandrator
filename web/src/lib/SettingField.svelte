<script lang="ts">
  import { Plus, Trash2 } from '@lucide/svelte';
  import { isMultiline, numberPresentation, optionsFor, settingLabel } from './settings-fields';

  let { section, keyName, value, onchange, compact = false }: { section: string; keyName: string; value: any; onchange: (value: any) => void; compact?: boolean } = $props();
  let newKey = $state('');
  const choices = $derived(optionsFor(section, keyName));
  const numberMeta = $derived(numberPresentation(keyName));
  const objectEntries = $derived(Object.entries(value && typeof value === 'object' && !Array.isArray(value) ? value : {}));

  function cast(raw: string) {
    if (typeof value === 'number') return Number(raw);
    return raw;
  }

  function setObject(key: string, next: string) {
    const current = { ...(value ?? {}) };
    const prior = current[key];
    current[key] = typeof prior === 'number' ? Number(next) : next;
    onchange(current);
  }

  function removeObject(key: string) {
    const current = { ...(value ?? {}) };
    delete current[key];
    onchange(current);
  }

  function addObject() {
    const key = newKey.trim();
    if (!key || Object.prototype.hasOwnProperty.call(value ?? {}, key)) return;
    onchange({ ...(value ?? {}), [key]: 0 });
    newKey = '';
  }
</script>

<label class:text-xs={compact} class="block text-sm font-semibold">{settingLabel(keyName)}
  {#if typeof value === 'boolean'}
    <span class="field flex min-h-11 items-center gap-2"><input type="checkbox" checked={value} onchange={(event) => onchange(event.currentTarget.checked)} class="accent-[var(--accent)]"/><span class="font-normal">{value ? 'Enabled' : 'Disabled'}</span></span>
  {:else if choices}
    <select class="field" value={value ?? ''} onchange={(event) => onchange(cast(event.currentTarget.value))}>
      {#if value && !choices.some((item) => String(item.value) === String(value))}<option value={value}>{value}</option>{/if}
      {#each choices as item}<option value={item.value}>{item.label}</option>{/each}
    </select>
  {:else if typeof value === 'number' && numberMeta.range}
    <span class="field range-field"><input type="range" value={value} min={numberMeta.min} max={numberMeta.max} step={numberMeta.step} oninput={(event) => onchange(Number(event.currentTarget.value))}/><output>{value}{numberMeta.suffix ?? ''}</output></span>
  {:else if typeof value === 'number'}
    <input class="field" type="number" value={value} min={numberMeta.min} max={numberMeta.max} step={numberMeta.step ?? 'any'} oninput={(event) => onchange(Number(event.currentTarget.value))}/>
  {:else if value && typeof value === 'object'}
    <span class="field block space-y-2">
      {#each objectEntries as [key, item]}
        <span class="grid grid-cols-[minmax(7rem,.7fr)_1fr_auto] items-center gap-2"><span class="truncate text-xs font-semibold">{settingLabel(key)}</span><input value={String(item ?? '')} oninput={(event) => setObject(key, event.currentTarget.value)} class="subfield"/><button type="button" onclick={() => removeObject(key)} class="btn btn-icon btn-quiet" aria-label={`Remove ${settingLabel(key)}`}><Trash2 size={13}/></button></span>
      {/each}
      <span class="flex gap-2"><input bind:value={newKey} placeholder="Add named value" class="subfield min-w-0 flex-1"/><button type="button" onclick={addObject} class="btn btn-sm btn-secondary"><Plus size={13}/> Add</button></span>
    </span>
  {:else if isMultiline(keyName)}
    <textarea class="field min-h-24 resize-y" value={value ?? ''} oninput={(event) => onchange(event.currentTarget.value)}></textarea>
  {:else}
    <input class="field" value={value ?? ''} oninput={(event) => onchange(event.currentTarget.value)}/>
  {/if}
</label>

<style>
  .field{margin-top:.4rem;width:100%;border:1px solid var(--line);border-radius:.72rem;background:var(--paper);padding:.65rem .72rem;font-weight:400;color:var(--ink)}
  .subfield{width:100%;border:1px solid var(--line);border-radius:.55rem;background:var(--paper-strong);padding:.45rem .55rem;font-size:.75rem;font-weight:400;color:var(--ink)}
  .range-field{display:grid;grid-template-columns:minmax(8rem,1fr) auto;align-items:center;gap:.8rem}.range-field input{width:100%;accent-color:var(--accent)}.range-field output{min-width:3.2rem;text-align:right;font-variant-numeric:tabular-nums;font-size:.75rem;font-weight:700}
</style>
