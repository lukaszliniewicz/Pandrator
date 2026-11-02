<script lang="ts">
  import { appState } from './app-state.svelte';

  let { value = $bindable('silero') }: { value: string } = $props();
  const models = ['silero', 'firered', 'marblenet', 'whisper-vad', 'webrtc'];
  const custom = $derived(!models.includes(value));
</script>

<label class="text-xs font-semibold">
  Speech detector
  <select
    value={custom ? 'custom' : value}
    onchange={(event) => {
      const next = event.currentTarget.value;
      value = next === 'custom' ? (custom ? value : '') : next;
    }}
    class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
  >
    <option value="silero">Silero · recommended</option>
    <option value="firered">FireRedVAD · robust</option>
    <option value="marblenet">MarbleNet · compact</option>
    <option value="whisper-vad">Whisper VAD · experimental</option>
    <option value="webrtc">WebRTC · no model file</option>
    {#if appState.isAdministrator || custom}
      <option value="custom"
        >{appState.isAdministrator
          ? 'Existing local model file…'
          : 'Configured local file'}</option
      >
    {/if}
  </select>
</label>
{#if custom && appState.isAdministrator}
  <label class="text-xs font-semibold">
    Existing model file on the Pandrator computer
    <input
      type="text"
      bind:value
      class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
      placeholder="Full path to an existing VAD model"
    />
    <span class="muted mt-1 block text-xs font-normal"
      >Use a model supported by CrispASR. This selects the file in place.</span
    >
  </label>
{/if}
