<script lang="ts">
  import { Search, X } from '@lucide/svelte';
  import { LANGUAGE_OPTIONS } from './settings-fields';
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
  const unavailable = $derived(
    new Set(excluded.map((code) => code.toLowerCase()))
  );
  const options = $derived(
    LANGUAGE_OPTIONS.filter(
      (option) =>
        option.value !== 'auto' &&
        !unavailable.has(String(option.value).toLowerCase()) &&
        `${option.label} ${option.value}`
          .toLowerCase()
          .includes(search.trim().toLowerCase())
    )
  );
  const invalid = $derived(
    value.filter((code) => unavailable.has(code.toLowerCase()))
  );
  function toggle(code: string) {
    value = value.includes(code)
      ? value.filter((item) => item !== code)
      : [...value, code];
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
    {#each options as option (option.value)}
      {@const code = String(option.value).toLowerCase()}
      <label
        class="flex cursor-pointer items-center gap-2 rounded-lg px-2 py-2 text-sm hover:bg-[var(--accent-soft)]"
      >
        <input
          type="checkbox"
          checked={value.includes(code)}
          disabled={disabled ||
            (!value.includes(code) && value.length >= limit)}
          onchange={() => toggle(code)}
        />
        <span>{option.label}</span>
      </label>
    {:else}<p class="muted px-2 py-3 text-sm sm:col-span-2">
        No matching languages.
      </p>{/each}
  </div>
  <p class="muted mt-2 text-xs" role="status">
    {value.length} of {limit} languages selected.
  </p>
  {#if invalid.length}<p role="alert" class="mt-2 text-xs text-red-500">
      Remove {invalid.map(translationLanguageName).join(', ')}: a target cannot
      repeat the source or an existing language.
    </p>{/if}
</fieldset>
