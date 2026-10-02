<script lang="ts">
  import { Search, X } from '@lucide/svelte';
  import {
    REGISTRY_LANGUAGE_OPTIONS,
    canonicalLanguageTag,
    languageSearchMatches
  } from './language-registry';
  import { translationLanguageName } from './translation-project-display';

  let {
    value = $bindable<string[]>([]),
    excluded = [],
    disabled = false,
    label = 'Target languages',
    limit = 20
  }: {
    value?: string[];
    excluded?: string[];
    disabled?: boolean;
    label?: string;
    limit?: number;
  } = $props();
  let search = $state('');
  let showAll = $state(false);
  const unavailable = $derived(
    new Set(
      excluded.map((code) => canonicalLanguageTag(code) || code.toLowerCase())
    )
  );
  const options = $derived(
    REGISTRY_LANGUAGE_OPTIONS.filter(
      (option) =>
        option.value !== 'auto' &&
        !unavailable.has(option.value) &&
        languageSearchMatches(option.value, search)
    )
  );
  const invalid = $derived(
    value.filter((code) =>
      unavailable.has(canonicalLanguageTag(code) || code.toLowerCase())
    )
  );
  const visibleOptions = $derived(
    showAll || search.trim() ? options : options.slice(0, 60)
  );
  const customCode = $derived(canonicalLanguageTag(search));
  const canAddCustom = $derived(
    Boolean(
      customCode &&
      !unavailable.has(customCode) &&
      !REGISTRY_LANGUAGE_OPTIONS.some((option) => option.value === customCode)
    )
  );
  function selected(code: string) {
    return value.some(
      (item) => (canonicalLanguageTag(item) || item.toLowerCase()) === code
    );
  }
  function toggle(code: string) {
    const canonical = canonicalLanguageTag(code) || code;
    value = selected(canonical)
      ? value.filter(
          (item) =>
            (canonicalLanguageTag(item) || item.toLowerCase()) !== canonical
        )
      : value.length < limit
        ? [...value, canonical]
        : value;
  }
</script>

<fieldset {disabled} class="min-w-0">
  <legend class="text-sm font-semibold">{label}</legend>
  <label
    class="mt-2 flex items-center gap-2 rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2"
  >
    <Search size={16} class="muted shrink-0" />
    <input
      type="search"
      bind:value={search}
      aria-label={`Search ${label.toLowerCase()}`}
      placeholder="Find a language…"
      class="min-w-0 flex-1 bg-transparent text-sm outline-none"
    />
  </label>
  {#if value.length}
    <ul class="mt-3 flex flex-wrap gap-2" aria-label="Selected languages">
      {#each value as code (code)}
        <li>
          <button
            type="button"
            onclick={() => toggle(code)}
            class="flex items-center gap-1.5 rounded-lg bg-[var(--accent-soft)] px-2.5 py-1.5 text-xs font-semibold"
            aria-label={`Remove ${translationLanguageName(code)}`}
          >
            {translationLanguageName(code)}<X size={13} />
          </button>
        </li>
      {/each}
    </ul>
  {/if}
  <div
    class="mt-3 grid max-h-48 grid-cols-1 gap-1 overflow-y-auto rounded-xl border border-[var(--line)] p-2 sm:grid-cols-2"
  >
    {#each visibleOptions as option (option.value)}
      {@const code = String(option.value).toLowerCase()}
      <label
        class="flex cursor-pointer items-center gap-2 rounded-lg px-2 py-2 text-sm hover:bg-[var(--accent-soft)]"
      >
        <input
          type="checkbox"
          checked={selected(code)}
          disabled={disabled || (!selected(code) && value.length >= limit)}
          onchange={() => toggle(code)}
        />
        <span>{option.label}</span>
      </label>
    {:else}<p class="muted px-2 py-3 text-sm sm:col-span-2">
        No matching languages.
      </p>{/each}
  </div>
  {#if !search.trim() && !showAll && options.length > visibleOptions.length}
    <button
      type="button"
      onclick={() => (showAll = true)}
      class="mt-2 text-xs font-semibold text-[var(--accent)]"
      >Show all {options.length} languages</button
    >
  {/if}
  {#if canAddCustom}
    <button
      type="button"
      disabled={disabled || value.length >= limit || selected(customCode)}
      onclick={() => toggle(customCode)}
      class="mt-2 text-xs font-semibold text-[var(--accent)]"
      >Add language code {customCode}</button
    >
    <p class="muted mt-1 text-xs">
      This code is outside the reviewed registry. Check the selected model’s
      coverage before generating speech.
    </p>
  {/if}
  <p class="muted mt-2 text-xs" role="status">
    {value.length} of {limit} languages selected.
  </p>
  {#if invalid.length}<p role="alert" class="mt-2 text-xs text-red-500">
      Remove {invalid.map(translationLanguageName).join(', ')}: a target cannot
      repeat the source or an existing language.
    </p>{/if}
</fieldset>
