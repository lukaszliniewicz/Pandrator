<script lang="ts">
  import type { RuntimeCapabilities } from './api-models';
  import {
    qwenTimingExplanation,
    sttLanguageOptions
  } from './stt-language-policy';
  import LanguageSelect from './LanguageSelect.svelte';

  let {
    model = $bindable('qwen3_asr_0_6b'),
    language = $bindable('auto'),
    backend = $bindable('auto'),
    capabilities,
    requireWordTimestamps = true
  }: {
    model?: string;
    language?: string;
    backend?: string;
    capabilities: RuntimeCapabilities | null;
    requireWordTimestamps?: boolean;
  } = $props();
  const timing = $derived(
    requireWordTimestamps
      ? qwenTimingExplanation(capabilities, language)
      : 'Plain text uses recognition only. No forced aligner is downloaded and no subtitle timestamps are produced.'
  );
  const info = $derived(capabilities?.stt?.models?.qwen3);
  const availableBackends = $derived(
    Array.isArray(info?.compute_backends) && info.compute_backends.length
      ? (info.compute_backends as string[])
      : ['cpu']
  );
</script>

<section
  class="rounded-xl border border-[var(--line)] p-4 space-y-3"
  aria-label="Qwen3 transcription options"
>
  <div>
    <h3 class="text-sm font-semibold">Qwen3 speech recognition</h3>
    <p class="muted mt-1 text-xs">
      Runs locally through CrispASR. The selected model downloads on first use;
      your recording is not uploaded.
    </p>
  </div>
  <div class="grid gap-3 sm:grid-cols-2">
    <label class="text-sm font-semibold"
      >Qwen model size
      <select
        bind:value={model}
        class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
      >
        <option value="qwen3_asr_0_6b"
          >Qwen3 ASR 0.6B · smaller · Q8 · 1.01 GB</option
        >
        <option value="qwen3_asr_1_7b"
          >Qwen3 ASR 1.7B · larger · Q8 · 2.51 GB</option
        >
        {#if !['qwen3_asr_0_6b', 'qwen3_asr_1_7b'].includes(model)}<option
            value={model}>{model} · saved selection</option
          >{/if}
      </select>
    </label>
    <LanguageSelect
      bind:value={language}
      options={sttLanguageOptions(capabilities, 'qwen3', requireWordTimestamps)}
      label="Source language"
      allowAuto
      allowCustom
    />
  </div>
  <p class="text-sm" data-testid="qwen-timing-explanation">{timing}</p>
  <p class="muted text-xs">
    Recognition supports 30 languages. Qwen word alignment supports 11: Chinese,
    English, Cantonese, French, German, Italian, Japanese, Korean, Portuguese,
    Russian and Spanish. Other supported timing paths are explained above. The
    translation target does not determine alignment.
  </p>
  <details class="text-sm">
    <summary class="cursor-pointer font-semibold"
      >Local processing settings</summary
    >
    <label class="mt-3 block text-sm"
      >Compute backend
      <select
        bind:value={backend}
        class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2"
      >
        <option value="auto">Automatic</option>
        {#each availableBackends.filter((value) => value !== 'auto' && value !== 'best') as value}<option
            {value}>{value.toUpperCase()}</option
          >{/each}
        {#if backend !== 'auto' && !availableBackends.includes(backend)}<option
            value={backend}
            >{backend} · saved selection, availability not checked</option
          >{/if}
      </select>
    </label>
    <p class="muted mt-2 text-xs">
      Word alignment may download an additional model. Qwen's aligner is
      approximately 986 MB. Processing can be cancelled from Activity.
    </p>
  </details>
</section>
