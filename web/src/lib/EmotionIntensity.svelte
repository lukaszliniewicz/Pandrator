<script lang="ts">
  import type { SpeechCapabilities } from './speech-support';
  let {
    value,
    emotion,
    capabilities,
    scope,
    enabled,
    disabled,
    onchange
  }: {
    value?: number | null;
    emotion: string;
    capabilities?: SpeechCapabilities;
    scope: 'segment' | 'phrase';
    enabled: boolean;
    disabled: boolean;
    onchange?: (value: number | null) => void;
  } = $props();
  const native = $derived(capabilities?.emotion?.intensity);
  const active = $derived(value !== undefined && value !== null);
  const applicable = $derived(
    Boolean(native && scope === 'segment' && emotion.trim())
  );
</script>

{#if (native || active) && onchange}<div
    class="mt-3 rounded-lg border border-[var(--line)] p-3 text-sm"
  >
    <label class="flex items-center gap-2"
      ><input
        type="checkbox"
        checked={active}
        disabled={disabled || !applicable}
        onchange={(event) =>
          onchange?.(event.currentTarget.checked ? native!.default : null)}
      />{native?.label ?? 'Saved emotion intensity'}{#if active}<output
          class="ml-auto tabular-nums">{value}</output
        >{/if}</label
    >
    {#if native && applicable && active}<input
        class="mt-3 w-full"
        type="range"
        min={native.min}
        max={native.max}
        step="0.01"
        value={value ?? native.default}
        aria-label={native.label}
        {disabled}
        oninput={(event) => onchange?.(Number(event.currentTarget.value))}
      />
      <p class="muted flex justify-between text-xs">
        <span>{native.min}</span><span>Default {native.default}</span><span
          >{native.max}</span
        >
      </p>{/if}
    <p class="muted mt-2 text-xs">
      {!native
        ? 'Retained for a model with a native intensity control.'
        : scope === 'phrase'
          ? 'This model applies intensity to a complete voice request. A phrase-only value remains saved and is not sent.'
          : !emotion.trim()
            ? 'Choose an emotion first.'
            : native.semantics}{#if !enabled}
        Delivery is off; the value remains saved.{/if}
    </p>
    {#if active && native && (value! < native.min || value! > native.max)}<p
        class="mt-2 text-xs text-amber-700"
      >
        Outside this model’s range; retained, but not sent.
      </p>{/if}
  </div>{/if}
