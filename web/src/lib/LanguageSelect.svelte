<script lang="ts">
  import {
    canonicalLanguageTag,
    LANGUAGE_OPTIONS,
    languageLabel,
    languageSearchMatches,
    type LanguageOption
  } from './language-registry';

  let {
    value = $bindable(''),
    options = LANGUAGE_OPTIONS,
    label = 'Language',
    allowAuto = false,
    allowCustom = false,
    disabled = false,
    onchange
  }: {
    value?: string;
    options?: readonly { value: string | number; label: string }[];
    label?: string;
    allowAuto?: boolean;
    allowCustom?: boolean;
    disabled?: boolean;
    onchange?: (value: string) => void;
  } = $props();
  let search = $state('');
  const choices = $derived.by(() => {
    const saved = String(value || '');
    const result: LanguageOption[] = options
      .map((option) => ({ value: String(option.value), label: option.label }))
      .filter(
        (option) =>
          (allowAuto || option.value !== 'auto') &&
          (option.value === saved ||
            languageSearchMatches(option.value, search))
      );
    if (saved && !result.some((option) => option.value === saved))
      result.unshift({
        value: saved,
        label: `${languageLabel(saved)} · current selection`
      });
    return result;
  });
  const customCode = $derived(canonicalLanguageTag(search, allowAuto));
  const customAvailable = $derived(
    allowCustom &&
      customCode &&
      !options.some((option) => String(option.value) === customCode)
  );
  function choose(code: string) {
    value = code;
    onchange?.(code);
  }
</script>

<div class="min-w-0">
  <label class="text-sm font-semibold"
    >{label}
    <input
      type="search"
      bind:value={search}
      {disabled}
      aria-label={`Search ${label.toLowerCase()}`}
      placeholder="Search name or code…"
      class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-sm font-normal"
    />
  </label>
  <select
    {disabled}
    {value}
    onchange={(event) => choose(event.currentTarget.value)}
    aria-label={label}
    class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 text-sm"
  >
    {#if !value}<option value="">Choose a language</option>{/if}
    {#each choices as option (option.value)}<option value={option.value}
        >{option.label} · {option.value}</option
      >{/each}
  </select>
  {#if customAvailable}
    <button
      type="button"
      {disabled}
      onclick={() => choose(customCode)}
      class="mt-2 text-xs font-semibold text-[var(--accent)]"
      >Use language code {customCode}</button
    >
  {/if}
</div>
