<script lang="ts">
  import { onDestroy } from 'svelte';
  import { Mic, Square } from '@lucide/svelte';
  import { createAudioRecorder, openMicrophone } from './audio-recorder';
  import { errorMessage } from './errors';

  let {
    disabled = false,
    onrecord,
    onbusy
  }: {
    disabled?: boolean;
    onrecord: (file: File) => void;
    onbusy: (value: boolean) => void;
  } = $props();
  let recording = $state(false);
  let starting = $state(false);
  let seconds = $state(0);
  let error = $state('');
  let devices = $state<MediaDeviceInfo[]>([]);
  let deviceId = $state('');
  let recorder: MediaRecorder | null = null;
  let stream: MediaStream | null = null;
  let timer: ReturnType<typeof setInterval> | undefined;
  let destroyed = false;

  function release() {
    if (timer) clearInterval(timer);
    timer = undefined;
    stream?.getTracks().forEach((track) => track.stop());
    stream = null;
    recording = false;
    starting = false;
    onbusy(false);
  }

  async function start() {
    error = '';
    starting = true;
    onbusy(true);
    try {
      stream = await openMicrophone(deviceId);
      if (destroyed) {
        release();
        return;
      }
      devices = (await navigator.mediaDevices.enumerateDevices()).filter(
        (device) => device.kind === 'audioinput'
      );
      if (destroyed) {
        release();
        return;
      }
      const next = createAudioRecorder(stream);
      recorder = next;
      const chunks: Blob[] = [];
      let bytes = 0;
      let failed = false;
      next.ondataavailable = (event) => {
        if (!event.data.size) return;
        bytes += event.data.size;
        if (bytes > 256 * 1024 * 1024) {
          failed = true;
          error =
            'Recording reached the 256 MiB limit. Please record a shorter clip.';
          stop();
        } else chunks.push(event.data);
      };
      next.onerror = () => {
        failed = true;
        error =
          'The microphone recording failed. Check the microphone and try again.';
        stop();
        release();
      };
      next.onstop = () => {
        release();
        if (destroyed || failed) return;
        const type = next.mimeType || chunks[0]?.type || 'audio/webm';
        const extension = type.includes('ogg')
          ? 'ogg'
          : type.includes('mp4')
            ? 'm4a'
            : 'webm';
        const blob = new Blob(chunks, { type });
        if (!blob.size) {
          error = 'The recording was empty. Try another microphone.';
          return;
        }
        onrecord(new File([blob], `recording.${extension}`, { type }));
      };
      next.start(250);
      recording = true;
      starting = false;
      seconds = 0;
      timer = setInterval(() => {
        seconds += 1;
        if (seconds >= 7200) stop();
      }, 1000);
    } catch (caught) {
      error = errorMessage(caught);
      release();
    }
  }

  function stop() {
    if (recorder && recorder.state !== 'inactive') recorder.stop();
  }

  onDestroy(() => {
    destroyed = true;
    stop();
    release();
  });
</script>

<div class="space-y-4">
  <p class="muted text-sm">
    Record here, listen back, then transcribe. Audio is uploaded only when you
    select Transcribe.
  </p>
  {#if devices.length > 1}
    <label class="block text-sm"
      >Microphone
      <select
        class="mt-2 w-full"
        bind:value={deviceId}
        disabled={disabled || starting || recording}
      >
        <option value="">System default</option>
        {#each devices as device}<option value={device.deviceId}
            >{device.label || 'Microphone'}</option
          >{/each}
      </select>
    </label>
  {/if}
  <div class="flex flex-wrap items-center gap-4">
    {#if recording}
      <button
        class="btn btn-primary inline-flex items-center gap-2"
        onclick={stop}><Square size={16} /> Stop recording</button
      >
      <span
        class="font-mono text-sm"
        role="timer"
        aria-label="Recording duration"
        >{Math.floor(seconds / 60)}:{String(seconds % 60).padStart(
          2,
          '0'
        )}</span
      >
    {:else}
      <button
        class="btn btn-secondary inline-flex items-center gap-2"
        disabled={disabled || starting}
        onclick={start}
        ><Mic size={16} />
        {starting ? 'Connecting microphone…' : 'Record audio'}</button
      >
    {/if}
  </div>
  {#if error}<p class="text-sm text-[var(--danger)]" role="alert">
      {error}
    </p>{/if}
</div>
