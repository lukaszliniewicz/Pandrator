<script lang="ts">
  import { Play } from '@lucide/svelte';
  import AudioPlayer from './AudioPlayer.svelte';
  import type { PlayableTake } from './generation-view-models';

  // Deferred per-row audio preview.
  //
  // Rendering one full AudioPlayer (native <audio> element plus controls)
  // per completed row costs thousands of DOM nodes and hundreds of media
  // elements on multi-voice audiobooks. This component renders a lightweight
  // Play button until its row is requested, then mounts the single shared
  // AudioPlayer for the active row. Autoplay is set on the mounted player
  // so the requesting click gesture leads directly to playback; if the
  // browser still blocks it, the player's own Play button remains as a
  // fallback with identical error handling.
  let {
    take,
    segmentNumber,
    takeLabel,
    active,
    onrequest
  }: {
    take: PlayableTake;
    segmentNumber: number;
    takeLabel: string;
    active: boolean;
    onrequest: () => void;
  } = $props();

  // The single mounted player instance. The outer component persists while
  // sibling rows activate, so onDestroy does NOT run on row switch: pause
  // via effect cleanup instead, capturing the active element so a switch,
  // take replacement, or unmount always pauses the previous element.
  let audioElement = $state<HTMLAudioElement | undefined>();
  $effect(() => {
    const current = active ? audioElement : undefined;
    return () => current?.pause();
  });

  let container = $state<HTMLDivElement | undefined>();
  let wasActive = false;
  $effect(() => {
    // Transfer keyboard focus to the mounted player's transport button on
    // activation only: the requesting Play button unmounts, otherwise focus
    // is lost to <body>. Take switches remount the player (key below)
    // without stealing focus from the take dropdown.
    const becameActive = active && !wasActive;
    wasActive = active;
    if (!becameActive || !container) return;
    container.querySelector('button')?.focus({ preventScroll: true });
  });
</script>

{#if active}
  <div bind:this={container}>
    {#key take.artifact_id}
      <AudioPlayer
        compact
        preload="none"
        autoplay
        bind:element={audioElement}
        src={`/api/v1/artifacts/${take.artifact_id}/content`}
        label={`Segment ${segmentNumber}`}
      />
    {/key}
  </div>
{:else}
  <button
    type="button"
    onclick={onrequest}
    onkeydown={(event) => {
      // Let the button keep its own Enter/Space activation: the drawer's
      // global shortcuts would otherwise steal the keypress (playing the
      // selected row via the playlist controller) and cancel this click.
      if (event.key === 'Enter' || event.key === ' ') event.stopPropagation();
    }}
    class="preview-request"
    aria-label={`Play audio for segment ${segmentNumber}: ${takeLabel}`}
    title={`Play audio for segment ${segmentNumber}`}
  >
    <span class="preview-transport"><Play size={13} fill="currentColor" /></span
    >
    <span class="preview-label">{takeLabel}</span>
  </button>
{/if}

<style>
  .preview-request {
    display: flex;
    min-width: 11.5rem;
    max-width: 100%;
    align-items: center;
    gap: 0.45rem;
    border: 1px solid var(--line);
    border-radius: 0.7rem;
    background: var(--paper-strong);
    padding: 0.3rem 0.45rem;
    text-align: start;
  }
  .preview-transport {
    display: grid;
    flex: 0 0 auto;
    height: 1.55rem;
    width: 1.55rem;
    place-items: center;
    border-radius: 999px;
    background: var(--action-bg);
    color: white;
  }
  .preview-request:hover .preview-transport {
    background: var(--action-hover);
  }
  .preview-request:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  .preview-label {
    min-width: 0;
    overflow: hidden;
    font-size: 0.65rem;
    font-weight: 600;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
</style>
