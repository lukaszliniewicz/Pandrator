<script lang="ts">
  import { elevenLabsVoiceSettingsError } from './elevenlabs-settings';
  let {
    model,
    value,
    onchange
  }: {
    model: string;
    value: Record<string, unknown>;
    onchange: (value: Record<string, unknown>) => void;
  } = $props();
  const error = $derived(elevenLabsVoiceSettingsError(model, value));
  function change(key: string, event: Event) {
    const input = event.currentTarget as HTMLInputElement;
    const next = { ...value };
    if (input.value === '') delete next[key];
    else if (Number.isFinite(input.valueAsNumber))
      next[key] = input.valueAsNumber;
    onchange(next);
  }
</script>

<section
  class="rounded-xl border border-[var(--line)] p-4"
  aria-label="Eleven v4 voice settings"
>
  <h3 class="text-sm font-semibold">Voice settings</h3>
  <p class="muted mt-1 text-xs">
    {model === 'eleven_v4_turbo'
      ? 'The realtime route supports stability. Before/after text context is unavailable.'
      : 'The dialogue route supports stability and similarity. Before/after text context is limited to 100 characters on each side.'}
    Leave a control blank to use the provider default. Use Speech direction for inline
    delivery tags.
  </p>
  <div class="mt-3 grid gap-3 sm:grid-cols-2">
    {#each model === 'eleven_v4' ? ['stability', 'similarity_boost'] : ['stability'] as key}
      <label class="text-xs font-semibold">
        {key === 'stability' ? 'Stability' : 'Similarity'}
        <input
          class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
          type="number"
          min="0"
          max="1"
          step="0.05"
          value={typeof value[key] === 'number' ? Number(value[key]) : ''}
          placeholder={key === 'stability'
            ? 'Provider default (0.5)'
            : 'Provider default (0.75)'}
          oninput={(event) => change(key, event)}
        />
      </label>
    {/each}
  </div>
  {#if error}<p class="mt-3 text-xs text-red-500" role="alert">{error}</p>{/if}
  {#if Object.keys(value).length}
    <button
      type="button"
      class="mt-3 text-xs font-semibold text-[var(--accent)]"
      onclick={() => onchange({})}
      >Reset voice settings to provider defaults</button
    >
  {/if}
</section>
