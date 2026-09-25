<script lang="ts">
  let { value = $bindable('off') }: { value?: string } = $props();
</script>

<details
  class="rounded-xl border border-[var(--line)] p-4"
  data-testid="transcription-preprocessing"
>
  <summary class="cursor-pointer text-sm font-semibold"
    >Audio preprocessing{value !== 'off'
      ? ' · vocal isolation enabled'
      : ' · optional'}</summary
  >
  <label class="mt-3 block text-sm font-semibold"
    >Vocal isolation
    <select
      bind:value
      class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
    >
      <option value="off">Off · use the original audio</option>
      <option value="bs_roformer"
        >BS-RoFormer · download approximately 173 MB</option
      >
      <option value="mel_band_roformer"
        >Mel-RoFormer · download approximately 252 MB</option
      >
      {#if !['off', 'bs_roformer', 'mel_band_roformer'].includes(value)}<option
          {value}>{value} · saved selection</option
        >{/if}
    </select>
  </label>
  <p class="muted mt-2 text-xs">
    Reduce background music before transcription or caption alignment. This is
    not speaker separation and cannot reliably isolate one person from other
    voices.
  </p>
  <p class="muted mt-2 text-xs">
    Runs locally through audio.cpp. Only the selected model downloads on first
    use. The original media stays unchanged for playback and export, and the
    processed copy keeps its timeline.
  </p>
  {#if value !== 'off'}<p class="mt-2 text-xs" role="status">
      Vocal isolation processes overlapping windows and can take longer than the
      recording, especially on older GPUs. It can also remove useful speech
      details. Start with Off for clean recordings. A preprocessing failure
      stops the task rather than silently using untreated audio.
    </p>{/if}
</details>
