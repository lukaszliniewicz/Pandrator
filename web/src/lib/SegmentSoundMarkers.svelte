<script lang="ts">
  import { Bell, Pencil } from '@lucide/svelte';
  import {
    soundPlacementLabel,
    type ProjectSoundCue
  } from './project-sounds-api';
  import { soundAudioUrl } from './sound-api';
  import AudioPlayer from './AudioPlayer.svelte';
  let {
    cues,
    previewOnly,
    onedit
  }: {
    cues: ProjectSoundCue[];
    previewOnly: boolean;
    onedit?: (cue: ProjectSoundCue) => void;
  } = $props();
  let opened = $state('');
</script>

{#if cues.length}<div
    class="my-2 flex flex-wrap gap-2"
    aria-label="Segment sound effects"
  >
    {#each cues as cue (cue.logical_id)}<div
        class="rounded-lg border border-[var(--line)] px-2 py-1 text-xs"
      >
        <button
          class="flex items-center gap-1"
          title={`${soundPlacementLabel(cue.anchor)} · ${cue.playback.mode}${!cue.included ? ' · Excluded' : ''}${cue.placement_status === 'needs_placement' ? ' · Needs placement' : ''}`}
          aria-expanded={opened === cue.logical_id}
          onclick={() =>
            (opened = opened === cue.logical_id ? '' : cue.logical_id)}
          ><Bell size={13} />{cue.sound
            .name}{#if cue.placement_status === 'needs_placement'}<span
              class="muted">· Pending</span
            >{/if}{#if !cue.included}<span class="muted">· Excluded</span
            >{/if}</button
        >
        {#if opened === cue.logical_id}<div class="mt-2 max-w-80 space-y-2">
            <p class="muted">
              {soundPlacementLabel(cue.anchor)} · {cue.playback.mode}
            </p>
            {#if cue.anchor.resolution?.reason}<p class="muted">
                {cue.anchor.resolution.reason}
              </p>{/if}
            {#if cue.sound.artifact_id}<AudioPlayer
                src={soundAudioUrl(cue.sound.artifact_id)}
                label={cue.sound.name}
                compact
                preload="none"
              />{/if}
            {#if !previewOnly && onedit}<button
                class="flex items-center gap-1 text-[var(--accent)]"
                onclick={() => onedit?.(cue)}
                ><Pencil size={13} />Edit cue</button
              >{/if}
          </div>{/if}
      </div>{/each}
  </div>{/if}
