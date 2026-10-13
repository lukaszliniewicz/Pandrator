<script lang="ts">
  import { Download } from '@lucide/svelte';
  import { soundAudioUrl, type SoundCandidate } from './sound-api';

  let {
    candidate,
    label = 'audio'
  }: { candidate: SoundCandidate; label?: string } = $props();
</script>

{#if candidate.status === 'ready' && candidate.artifact_id}
  <a
    class="btn btn-secondary"
    href={soundAudioUrl(candidate.artifact_id)}
    download
    aria-label={`Download ${label}`}
    title="Download original audio file"><Download size={14} /> Download</a
  >
{/if}
