<script lang="ts">
  import AudioPlayer from './AudioPlayer.svelte';
  import { soundAudioUrl, type SoundCandidate } from './sound-api';
  let {
    candidate,
    label = 'Sound preview'
  }: { candidate: SoundCandidate; label?: string } = $props();
</script>

{#if candidate.artifact_id && candidate.status === 'ready'}
  {#key candidate.id}
    <AudioPlayer
      src={soundAudioUrl(candidate.artifact_id)}
      {label}
      preload="none"
      compact
    />
  {/key}
{:else}
  <span class="muted text-sm"
    >{candidate.status === 'ready'
      ? 'Audio unavailable'
      : candidate.status}</span
  >
{/if}
