<script lang="ts">
  import { Pause, Play, RotateCcw, Volume2, VolumeX } from '@lucide/svelte';

  let {
    src,
    label = 'Audio preview',
    compact = false,
    preload = 'metadata',
    autoplay = false,
    element = $bindable<HTMLAudioElement | undefined>()
  }: {
    src: string;
    label?: string;
    compact?: boolean;
    preload?: 'none' | 'metadata' | 'auto';
    autoplay?: boolean;
    element?: HTMLAudioElement;
  } = $props();

  let audio: HTMLAudioElement;
  let playing = $state(false);
  let current = $state(0);
  let duration = $state(0);
  let volume = $state(1);
  let muted = $state(false);
  let failed = $state('');

  $effect(() => {
    // `element` is a bindable output; assigning it intentionally updates the parent.
    if (element !== audio) element = audio;
  });

  function clock(seconds: number) {
    if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
    const rounded = Math.floor(seconds);
    return `${Math.floor(rounded / 60)}:${String(rounded % 60).padStart(2, '0')}`;
  }

  function playbackError(fallback = 'Audio could not be played.') {
    const message =
      {
        1: 'Audio playback was interrupted.',
        2: 'The audio could not be loaded.',
        3: 'The browser could not decode this audio.',
        4: 'This audio format is not supported by the browser.'
      }[audio.error?.code ?? 0] ?? fallback;
    failed = message;
  }

  async function toggle() {
    failed = '';
    if (audio.paused) {
      try {
        await audio.play();
      } catch {
        playbackError();
      }
    } else audio.pause();
  }

  function seek(event: Event) {
    const value = Number((event.currentTarget as HTMLInputElement).value);
    if (Number.isFinite(value)) audio.currentTime = value;
  }

  function changeVolume(event: Event) {
    volume = Number((event.currentTarget as HTMLInputElement).value);
    audio.volume = volume;
    audio.muted = volume === 0;
    muted = audio.muted;
  }

  function toggleMute() {
    audio.muted = !audio.muted;
    muted = audio.muted;
  }
</script>

<div class:compact class:failed class="audio-player" aria-label={label}>
  <audio
    bind:this={audio}
    {src}
    {preload}
    {autoplay}
    onplay={() => (playing = true)}
    onpause={() => (playing = false)}
    onended={() => (playing = false)}
    onloadedmetadata={() =>
      (duration = Number.isFinite(audio.duration) ? audio.duration : 0)}
    ondurationchange={() =>
      (duration = Number.isFinite(audio.duration) ? audio.duration : 0)}
    ontimeupdate={() => (current = audio.currentTime)}
    onvolumechange={() => {
      volume = audio.volume;
      muted = audio.muted;
    }}
    onerror={() => playbackError()}
  ></audio>
  <button
    type="button"
    onclick={toggle}
    class="transport"
    aria-label={playing ? 'Pause' : 'Play'}
  >
    {#if playing}<Pause size={compact ? 13 : 15} />{:else}<Play
        size={compact ? 13 : 15}
        fill="currentColor"
      />{/if}
  </button>
  <span class="time">{clock(current)}</span>
  <input
    class="timeline"
    type="range"
    min="0"
    max={Math.max(duration, 0.01)}
    step="0.01"
    value={Math.min(current, duration || 0)}
    oninput={seek}
    aria-label="Playback position"
  />
  <span class="time duration">{clock(duration)}</span>
  {#if !compact}
    <button
      type="button"
      onclick={toggleMute}
      class="quiet"
      aria-label={muted ? 'Unmute' : 'Mute'}
      >{#if muted}<VolumeX size={15} />{:else}<Volume2 size={15} />{/if}</button
    >
    <input
      class="volume"
      type="range"
      min="0"
      max="1"
      step="0.05"
      value={muted ? 0 : volume}
      oninput={changeVolume}
      aria-label="Volume"
    />
  {/if}
  {#if failed}<span class="failure" role="alert">{failed}</span><button
      type="button"
      onclick={() => {
        audio.load();
        failed = '';
      }}
      class="quiet retry"
      title="Reload audio"
      aria-label="Reload audio"><RotateCcw size={14} /></button
    >{/if}
</div>

<style>
  audio {
    display: none;
  }
  .audio-player {
    display: flex;
    min-width: 15rem;
    align-items: center;
    gap: 0.55rem;
    border: 1px solid var(--line);
    border-radius: 0.85rem;
    background: var(--paper-strong);
    padding: 0.42rem 0.55rem;
    box-shadow: 0 1px 0 color-mix(in srgb, var(--ink) 4%, transparent);
  }
  .audio-player.compact {
    min-width: 11.5rem;
    gap: 0.38rem;
    border-radius: 0.7rem;
    padding: 0.3rem 0.4rem;
  }
  .transport {
    display: grid;
    flex: 0 0 auto;
    height: 1.9rem;
    width: 1.9rem;
    place-items: center;
    border-radius: 999px;
    background: var(--action-bg);
    color: white;
    box-shadow: 0 3px 9px color-mix(in srgb, var(--accent) 25%, transparent);
  }
  .transport:hover {
    background: var(--action-hover);
  }
  .compact .transport {
    height: 1.55rem;
    width: 1.55rem;
  }
  .quiet {
    display: grid;
    flex: 0 0 auto;
    place-items: center;
    color: var(--muted);
  }
  .timeline,
  .volume {
    height: 0.9rem;
    cursor: pointer;
    accent-color: var(--accent);
  }
  .timeline {
    min-width: 3rem;
    flex: 1;
  }
  .volume {
    width: 3.25rem;
  }
  .time {
    min-width: 2.35rem;
    font-variant-numeric: tabular-nums;
    font-size: 0.62rem;
    font-weight: 700;
    color: var(--muted);
  }
  .duration {
    text-align: right;
  }
  .compact .time {
    font-size: 0.57rem;
  }
  .compact .duration,
  .compact .volume {
    display: none;
  }
  .audio-player.failed {
    border-color: color-mix(in srgb, #ef4444 45%, var(--line));
  }
  .retry {
    color: #dc2626;
  }
  .failure {
    max-width: 15rem;
    color: #dc2626;
    font-size: 0.65rem;
    line-height: 1.2;
  }
</style>
