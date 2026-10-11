<script lang="ts">
  import type { SoundPlayback } from './project-sounds-api';
  let {
    value = $bindable(),
    timed,
    disabled = false
  }: { value: SoundPlayback; timed: boolean; disabled?: boolean } = $props();
</script>

<label class="block text-sm"
  >Playback
  <select class="input mt-1 w-full" bind:value={value.mode} {disabled}>
    {#if !timed}<option value="insert">Insert a pause for this sound</option
      >{/if}
    <option value="overlay">Overlay · keep speech timing</option>
  </select>
</label>
<p class="muted text-xs">
  {value.mode === 'insert'
    ? 'Speech, chapters and generated captions move together. The sound does not replace speech.'
    : 'The sound plays over the existing audio. It must fit within its duration.'}
</p>
<details class="text-sm">
  <summary class="cursor-pointer">Gain, trim and fades</summary>
  <div class="mt-3 grid grid-cols-2 gap-3">
    <label
      >Gain (dB)<input
        class="input mt-1 w-full"
        type="number"
        min="-60"
        max="12"
        step="1"
        bind:value={value.gain_db}
        {disabled}
      /></label
    >
    <label
      >Offset (ms)<input
        class="input mt-1 w-full"
        type="number"
        min={value.mode === 'insert' ? 0 : -10000}
        max="10000"
        step="10"
        bind:value={value.offset_ms}
        {disabled}
      /></label
    >
    <label
      >Trim start (s)<input
        class="input mt-1 w-full"
        type="number"
        min="0"
        step="0.1"
        bind:value={value.trim_start_seconds}
        {disabled}
      /></label
    >
    <label
      >Trim end (s)<input
        class="input mt-1 w-full"
        type="number"
        min="0"
        step="0.1"
        placeholder="End of sound"
        bind:value={value.trim_end_seconds}
        {disabled}
      /></label
    >
    <label
      >Fade in (ms)<input
        class="input mt-1 w-full"
        type="number"
        min="0"
        max="1000"
        bind:value={value.fade_in_ms}
        {disabled}
      /></label
    >
    <label
      >Fade out (ms)<input
        class="input mt-1 w-full"
        type="number"
        min="0"
        max="1000"
        bind:value={value.fade_out_ms}
        {disabled}
      /></label
    >
  </div>
</details>
