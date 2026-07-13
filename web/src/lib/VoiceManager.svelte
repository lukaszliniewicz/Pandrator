<script lang="ts">
  import { page } from '$app/state';
  import { ArrowLeft, AudioLines, CircleAlert, Library, Mic, Play, Plus, Save, Settings2, Square, Trash2, Volume2, WandSparkles } from '@lucide/svelte';
  import { api, type JobRecord } from './api';
  import { onDestroy, onMount } from 'svelte';
  import GuidedTour from './GuidedTour.svelte';
  import SettingsModal from './SettingsModal.svelte';
  import PrebuiltVoiceLibrary from './PrebuiltVoiceLibrary.svelte';

  type Voice = { id: string; name: string; language?: string; description?: string };
  type Sample = { id: string; artifact_id: string; transcript?: string; transcript_language?: string; transcript_reviewed: boolean };

  let { onback, initialView }: { onback: () => void; initialView?: 'references' | 'prebuilt' } = $props();
  let activeView = $state<'references' | 'prebuilt'>('references');
  const initialService = page.url.searchParams.get('service') ?? '';
  let voices = $state<Voice[]>([]);
  let selected = $state<Voice | null>(null);
  let samples = $state<Sample[]>([]);
  let capabilities = $state<any>({});
  let error = $state('');
  let notice = $state('');
  let newName = $state('');
  let newNameInput = $state<HTMLInputElement>();
  let nameRequired = $state(false);
  let language = $state('en');
  let engine = $state('whisper');
  let computeBackend = $state('auto');
  let modelQuantization = $state('f16');
  let vadEnabled = $state(true);
  let vadThreshold = $state(0.5);
  let transcripts = $state<Record<string, string>>({});
  let devices = $state<MediaDeviceInfo[]>([]);
  let deviceId = $state('');
  let microphoneReady = $state(false);
  let checkingMicrophone = $state(false);
  let recorder = $state<MediaRecorder | null>(null);
  let activeStream: MediaStream | null = null;
  let chunks: Blob[] = [];
  let recording = $state(false);
  let stopping = $state(false);
  let seconds = $state(0);
  let timer: number | undefined;
  let recordingBlob = $state<Blob | null>(null);
  let recordingUrl = $state('');
  let savingRecording = $state(false);
  let playbackAudio: HTMLAudioElement;
  let playingKey = $state('');
  let tourOpen = $state(false);
  let sttSettingsOpen = $state(false);

  const tourSteps = [
    { section: 'Voices', title: 'References stay reviewable', body: 'Each voice can contain multiple playable samples and an editable, explicitly reviewed transcript.' },
    { section: 'Recording', title: 'Record in the browser', body: 'Microphone access is requested only when you enable recording. Preview locally, then save; FFmpeg normalizes the sample to mono PCM WAV.' },
    { section: 'Transcription', title: 'Local STT is optional', body: 'CrispASR runs full-precision Whisper or Parakeet and retains word timing metadata. Nothing is saved until you review it.' }
  ];
  const canTranscribe = $derived(Boolean(capabilities?.stt?.crispasr));
  const canRecord = $derived(Boolean(capabilities?.ffmpeg?.available && capabilities?.recording?.browser_media_recorder !== false));

  function report(caught: unknown, prefix = '') {
    error = `${prefix}${caught instanceof Error ? caught.message : String(caught)}`;
    notice = '';
  }

  async function loadVoices() {
    const result = await api<{ items: Voice[] }>('/voices');
    voices = result.items;
    if (selected) selected = voices.find((voice) => voice.id === selected?.id) ?? null;
  }

  async function choose(voice: Voice) {
    stopPlayback();
    selected = voice;
    const result = await api<{ items: Sample[] }>(`/voices/${voice.id}/samples`);
    samples = result.items;
    transcripts = Object.fromEntries(samples.map((sample) => [sample.id, sample.transcript ?? '']));
  }

  async function createVoice() {
    if (!newName.trim()) {
      nameRequired = true;
      newNameInput?.focus();
      return;
    }
    nameRequired = false;
    error = '';
    try {
      const voice = await api<Voice>('/voices', { method: 'POST', body: JSON.stringify({ name: newName.trim(), language }) });
      newName = '';
      await loadVoices();
      await choose(voice);
    } catch (caught) {
      report(caught);
    }
  }

  async function refreshMicrophones(requestAccess = false) {
    error = '';
    checkingMicrophone = true;
    try {
      if (!navigator.mediaDevices?.enumerateDevices || !window.MediaRecorder) {
        throw new Error('This browser does not expose microphone recording.');
      }
      if (requestAccess) {
        if (!window.isSecureContext) throw new Error('Microphone access requires HTTPS or a local browser session.');
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        stream.getTracks().forEach((track) => track.stop());
        microphoneReady = true;
      }
      devices = (await navigator.mediaDevices.enumerateDevices()).filter((device) => device.kind === 'audioinput');
      if (!devices.some((device) => device.deviceId === deviceId)) deviceId = devices[0]?.deviceId ?? '';
      if (requestAccess && !devices.length) throw new Error('No microphone input was found.');
      if (requestAccess) notice = `${devices.length} microphone${devices.length === 1 ? '' : 's'} available.`;
    } catch (caught) {
      microphoneReady = false;
      report(caught, 'Microphone unavailable: ');
    } finally {
      checkingMicrophone = false;
    }
  }

  function clearRecording() {
    if (playingKey === 'recording') stopPlayback();
    if (recordingUrl) URL.revokeObjectURL(recordingUrl);
    recordingUrl = '';
    recordingBlob = null;
    chunks = [];
  }

  async function startRecording() {
    if (!canRecord || recording || stopping) return;
    error = '';
    notice = '';
    stopPlayback();
    clearRecording();
    try {
      const requestedAudio: MediaTrackConstraints | boolean = deviceId ? { deviceId: { exact: deviceId } } : true;
      try {
        activeStream = await navigator.mediaDevices.getUserMedia({ audio: requestedAudio });
      } catch (caught) {
        // Device IDs can change after reconnecting a microphone. Retry with the
        // browser default instead of leaving the Record button mysteriously dead.
        if (!deviceId) throw caught;
        activeStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      }
      microphoneReady = true;
      await refreshMicrophones(false);
      const preferred = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4', 'audio/webm'].find((type) => MediaRecorder.isTypeSupported(type));
      const next = new MediaRecorder(activeStream, preferred ? { mimeType: preferred } : undefined);
      chunks = [];
      next.ondataavailable = (event) => { if (event.data.size) chunks.push(event.data); };
      next.onerror = (event) => report((event as any).error ?? new Error('The browser recorder failed.'));
      next.onstop = () => {
        if (timer) window.clearInterval(timer);
        timer = undefined;
        activeStream?.getTracks().forEach((track) => track.stop());
        activeStream = null;
        const type = next.mimeType || chunks[0]?.type || 'audio/webm';
        const blob = new Blob(chunks, { type });
        if (!blob.size) {
          report(new Error('The browser returned an empty recording. Please try another microphone.'));
        } else {
          recordingBlob = blob;
          recordingUrl = URL.createObjectURL(blob);
          notice = 'Recording ready to preview. It is not saved yet.';
        }
        recording = false;
        stopping = false;
        recorder = null;
      };
      next.start(250);
      recorder = next;
      recording = true;
      seconds = 0;
      timer = window.setInterval(() => seconds += 1, 1000);
    } catch (caught) {
      activeStream?.getTracks().forEach((track) => track.stop());
      activeStream = null;
      recording = false;
      stopping = false;
      report(caught, 'Could not start recording: ');
    }
  }

  function stopRecording() {
    if (!recorder || recorder.state === 'inactive') return;
    stopping = true;
    recorder.requestData();
    recorder.stop();
  }

  function discard() {
    if (recording || stopping) return;
    stopPlayback();
    clearRecording();
    notice = '';
  }

  async function saveRecording() {
    if (!selected || !recordingBlob || savingRecording) return;
    savingRecording = true;
    error = '';
    const body = new FormData();
    const extension = recordingBlob.type.includes('ogg') ? 'ogg' : recordingBlob.type.includes('mp4') ? 'm4a' : 'webm';
    body.set('file', recordingBlob, `recording.${extension}`);
    try {
      const job = await api<JobRecord>(`/voices/${selected.id}/samples`, { method: 'POST', body });
      await waitJob(job.id);
      clearRecording();
      notice = 'The normalized voice sample was saved.';
      await choose(selected);
    } catch (caught) {
      report(caught);
    } finally {
      savingRecording = false;
    }
  }

  async function uploadReference(event: Event) {
    if (!selected) return;
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    const body = new FormData();
    body.set('file', file);
    error = '';
    try {
      const job = await api<JobRecord>(`/voices/${selected.id}/samples`, { method: 'POST', body });
      await waitJob(job.id);
      notice = 'Voice sample saved.';
      await choose(selected);
    } catch (caught) {
      report(caught);
    } finally {
      input.value = '';
    }
  }

  async function waitJob(id: string) {
    for (let attempt = 0; attempt < 240; attempt += 1) {
      const job = await api<JobRecord>(`/jobs/${id}`);
      if (job.status === 'succeeded') return job;
      if (['failed', 'canceled'].includes(job.status)) throw new Error(job.error_message || `Job ${job.status}`);
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    throw new Error('The operation is still running. Check the job queue.');
  }

  async function transcribe(sample: Sample) {
    if (!selected) return;
    error = '';
    try {
      const job = await api<JobRecord>(`/voices/${selected.id}/samples/${sample.id}/transcribe`, {
        method: 'POST',
        body: JSON.stringify({
          stt_engine: engine,
          stt_backend: engine,
          stt_compute_backend: computeBackend,
          stt_model_quantization: modelQuantization,
          stt_language: language,
          crispasr_vad_enabled: vadEnabled,
          crispasr_vad_threshold: vadThreshold
        })
      });
      const completed = await waitJob(job.id);
      transcripts[sample.id] = String(completed.result_json?.transcript ?? '');
      notice = 'Transcript ready for review. Save it when the text is correct.';
    } catch (caught) {
      report(caught);
    }
  }

  async function transcribeMissing() {
    for (const sample of samples.filter((item) => !item.transcript_reviewed)) await transcribe(sample);
  }

  async function saveTranscript(sample: Sample) {
    if (!selected || !transcripts[sample.id]?.trim()) return;
    try {
      await api(`/voices/${selected.id}/samples/${sample.id}/transcript`, { method: 'PATCH', body: JSON.stringify({ transcript: transcripts[sample.id].trim(), language }) });
      notice = 'Reviewed transcript saved.';
      await choose(selected);
    } catch (caught) {
      report(caught);
    }
  }

  function stopPlayback() {
    if (playbackAudio) {
      playbackAudio.pause();
      try { playbackAudio.currentTime = 0; } catch { /* not seekable yet */ }
    }
    playingKey = '';
  }

  async function togglePlayback(key: string, source: string) {
    error = '';
    if (playingKey === key && playbackAudio && !playbackAudio.paused) {
      stopPlayback();
      return;
    }
    stopPlayback();
    try {
      if (playbackAudio.src !== new URL(source, window.location.href).href) {
        playbackAudio.src = source;
        playbackAudio.load();
      }
      await playbackAudio.play();
      playingKey = key;
    } catch (caught) {
      stopPlayback();
      report(caught, 'Playback failed: ');
    }
  }

  onMount(async () => {
    activeView = initialView ?? (page.url.searchParams.get('view') === 'prebuilt' ? 'prebuilt' : 'references');
    try {
      [capabilities] = await Promise.all([api('/capabilities'), loadVoices()]);
      await refreshMicrophones(false);
    } catch (caught) {
      report(caught);
    }
  });

  onDestroy(() => {
    if (recorder && recorder.state !== 'inactive') recorder.stop();
    activeStream?.getTracks().forEach((track) => track.stop());
    if (timer) window.clearInterval(timer);
    stopPlayback();
    if (recordingUrl) URL.revokeObjectURL(recordingUrl);
  });
</script>

<audio bind:this={playbackAudio} preload="metadata" class="sr-only" onended={() => playingKey = ''} onerror={() => { if (playingKey) error = 'Playback failed: the audio file could not be decoded or loaded.'; playingKey = ''; }}></audio>

<div class="mx-auto flex max-w-7xl flex-col">
  <button onclick={onback} class="muted mb-6 flex items-center gap-2 self-start text-sm font-semibold"><ArrowLeft size={17}/> Workspace</button>
  <header class="mb-5 flex flex-wrap items-end justify-between gap-4">
    <div><div class="eyebrow">Voices</div><h1 class="mt-2 text-4xl font-semibold">Voice Library</h1><p class="muted mt-2 text-sm">Manage voice-cloning references and compare provider voices in one workspace.</p></div>
    {#if activeView === 'references'}<div class="flex gap-2"><label class:pointer-events-none={!selected} class:opacity-40={!selected} class="cursor-pointer rounded-xl border border-[var(--line)] px-4 py-2 text-sm font-semibold">Upload sample<input type="file" accept="audio/*" onchange={uploadReference} class="sr-only"/></label><button onclick={() => tourOpen = true} class="rounded-xl border border-[var(--line)] px-4 py-2 text-sm font-semibold">Tour</button></div>{/if}
  </header>
  <div class="mb-6 flex gap-2 border-b border-[var(--line)]"><button onclick={() => activeView = 'references'} class:active={activeView === 'references'} class="library-tab"><Library size={16}/> Reference samples</button><button onclick={() => activeView = 'prebuilt'} class:active={activeView === 'prebuilt'} class="library-tab"><AudioLines size={16}/> Pre-built voices</button></div>
  {#if activeView === 'references'}
    {#if error}<div role="alert" class="mb-4 flex items-start gap-2 rounded-xl border border-red-400/40 bg-red-500/10 px-4 py-3 text-sm"><CircleAlert class="mt-0.5 shrink-0" size={16}/><span>{error}</span></div>{/if}
    {#if notice}<div role="status" class="mb-4 rounded-xl border border-[var(--line)] bg-[var(--accent-soft)] px-4 py-3 text-sm">{notice}</div>{/if}

  <div class="grid min-h-0 flex-1 gap-5 lg:grid-cols-[20rem_1fr]">
    <aside class="surface flex min-h-0 flex-col rounded-3xl p-4">
      <div class="relative flex gap-2"><input bind:this={newNameInput} bind:value={newName} oninput={() => nameRequired = false} aria-label="New voice name" aria-invalid={nameRequired} aria-describedby={nameRequired ? 'voice-name-required' : undefined} placeholder="New voice" class:border-red-500={nameRequired} class="min-w-0 flex-1 rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-sm"/><button onclick={createVoice} aria-label="Add voice" title="Add voice" class="btn btn-icon btn-primary"><Plus size={17}/></button>{#if nameRequired}<div id="voice-name-required" role="tooltip" class="absolute left-1 top-[calc(100%+.45rem)] z-10 rounded-lg bg-[var(--ink)] px-3 py-2 text-xs font-semibold text-[var(--paper-strong)] shadow-lg">Enter a voice name first.<span class="absolute -top-1 left-4 size-2 rotate-45 bg-[var(--ink)]"></span></div>{/if}</div>
      <div class="mt-4 min-h-0 flex-1 space-y-1 overflow-auto">{#each voices as voice}<button onclick={() => choose(voice)} class:active={selected?.id === voice.id} class="flex w-full items-center gap-3 rounded-xl px-3 py-3 text-left"><Library size={17}/><span class="min-w-0 flex-1 truncate font-semibold">{voice.name}</span><span class="muted text-xs">{voice.language}</span></button>{:else}<p class="muted p-5 text-center text-sm">Create a voice to add samples.</p>{/each}</div>
    </aside>

    <main class="surface min-h-0 overflow-auto rounded-3xl p-5 sm:p-7">
      {#if selected}
        <div class="mb-6 flex flex-wrap items-center justify-between gap-4">
          <div><h2 class="text-2xl font-semibold">{selected.name}</h2><p class="muted text-sm">Review playback, transcripts, and recordings in one place.</p></div>
          <div class="stt-toolbar"><select bind:value={engine} onchange={() => modelQuantization = 'f16'} disabled={!canTranscribe} aria-label="Transcription model"><option value="whisper">Whisper large-v3</option><option value="parakeet">Parakeet 0.6B v3</option></select><select bind:value={modelQuantization} disabled={!canTranscribe} aria-label="Transcription model precision"><option value="f16">FP16</option>{#if engine === 'whisper'}<option value="q5_0">Q5_0</option>{:else}<option value="q8_0">Q8_0</option><option value="q5_0">Q5_0</option><option value="q4_k">Q4_K</option>{/if}</select><select bind:value={computeBackend} disabled={!canTranscribe} aria-label="Transcription compute backend"><option value="auto">Automatic compute</option><option value="cpu">CPU</option><option value="cuda">CUDA</option><option value="vulkan">Vulkan</option><option value="metal">Metal</option></select><label class="stt-control"><input bind:checked={vadEnabled} type="checkbox" class="accent-[var(--accent)]"/><span>VAD</span></label><label class:opacity-45={!vadEnabled} class="stt-control vad-threshold"><span>VAD threshold</span><input bind:value={vadThreshold} aria-label="VAD threshold" type="range" min="0" max="1" step="0.05" disabled={!vadEnabled}/><output>{Number(vadThreshold).toFixed(2)}</output></label><button onclick={transcribeMissing} disabled={!canTranscribe} class="stt-control font-semibold disabled:opacity-40"><WandSparkles size={16}/> Transcribe missing</button></div>
        </div>

        <div class="mb-4 flex justify-end"><button onclick={()=>sttSettingsOpen=true} class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-3 py-2 text-xs font-semibold"><Settings2 size={15}/> All speech recognition and VAD defaults</button></div>
        <section class="mb-7 rounded-2xl border border-[var(--line)] p-4">
          <div class="mb-3"><h3 class="font-semibold">Record a reference</h3><p class="muted mt-1 text-xs">Permission is requested only when you enable the microphone. The recording remains local until you save it.</p></div>
          <div class="flex flex-wrap items-center gap-3">
            {#if !microphoneReady}<button onclick={() => refreshMicrophones(true)} disabled={!canRecord || checkingMicrophone} class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-4 py-2 text-sm font-semibold disabled:opacity-40"><Mic size={16}/> {checkingMicrophone ? 'Checking…' : 'Enable microphone'}</button>{/if}
            <select bind:value={deviceId} aria-label="Microphone" disabled={!microphoneReady || !devices.length || recording || stopping} class="min-w-48 rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-sm"><option value="">Default microphone</option>{#each devices as device}<option value={device.deviceId}>{device.label || `Microphone ${devices.indexOf(device) + 1}`}</option>{/each}</select>
            {#if !recording && !stopping}<button onclick={startRecording} disabled={!microphoneReady || !devices.length || !canRecord} class="flex items-center gap-2 rounded-xl bg-red-500 px-4 py-2 font-semibold text-white disabled:opacity-40"><Mic size={16}/> Record</button>{:else}<button onclick={stopRecording} disabled={stopping} class="flex items-center gap-2 rounded-xl bg-red-500 px-4 py-2 font-semibold text-white disabled:opacity-60"><Square size={15}/> {stopping ? 'Finishing…' : `Stop · ${seconds}s`}</button>{/if}
            {#if recordingUrl}
              <button aria-label={playingKey === 'recording' ? 'Stop recording playback' : 'Play recording'} onclick={() => togglePlayback('recording', recordingUrl)} class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-3 py-2 text-sm font-semibold">{#if playingKey === 'recording'}<Square size={15}/> Stop{:else}<Play size={16}/> Preview{/if}</button>
              <button onclick={discard} aria-label="Discard recording" class="rounded-xl border border-[var(--line)] p-2"><Trash2 size={16}/></button>
              <button onclick={saveRecording} disabled={savingRecording} class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2 font-semibold text-white disabled:opacity-50"><Save size={16}/> {savingRecording ? 'Normalizing…' : 'Save sample'}</button>
            {/if}
          </div>
          {#if !capabilities?.ffmpeg?.available}<p class="mt-2 text-xs text-[var(--warning)]">Recording is disabled until FFmpeg is available.</p>{/if}
        </section>

        <div class="space-y-4">
          {#each samples as sample}
            <article class="rounded-2xl border border-[var(--line)] p-4">
              <div class="flex flex-wrap items-center gap-3">
                <button aria-label={playingKey === sample.id ? 'Stop sample playback' : 'Play sample'} onclick={() => togglePlayback(sample.id, `/api/v1/artifacts/${sample.artifact_id}/content`)} class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-3 py-2 text-sm font-semibold">{#if playingKey === sample.id}<Square size={15}/> Stop{:else}<Volume2 size={16}/> Play sample{/if}</button>
                <button onclick={() => transcribe(sample)} disabled={!canTranscribe} class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-3 py-2 text-sm font-semibold disabled:opacity-40"><WandSparkles size={15}/> Transcribe</button>
                <span class="muted text-xs">{sample.transcript_reviewed ? 'Transcript reviewed' : 'Transcript not reviewed'}</span>
              </div>
              <textarea bind:value={transcripts[sample.id]} rows="3" placeholder="Transcript will remain unsaved until you review it." class="mt-3 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] p-3 text-sm"></textarea>
              <div class="mt-2 flex justify-end"><button onclick={() => saveTranscript(sample)} disabled={!transcripts[sample.id]?.trim()} class="flex items-center gap-2 rounded-lg bg-[var(--accent)] px-3 py-2 text-xs font-semibold text-white disabled:opacity-40"><Save size={14}/> Save reviewed transcript</button></div>
            </article>
          {:else}
            <div class="muted rounded-2xl border border-dashed border-[var(--line)] p-10 text-center"><Play class="mx-auto mb-2" size={22}/> Record or upload the first sample above.</div>
          {/each}
        </div>
      {:else}
        <div class="grid h-full min-h-96 place-items-center text-center"><div><Library class="mx-auto text-[var(--accent)]" size={30}/><h2 class="mt-3 text-xl font-semibold">Select a voice</h2><p class="muted mt-1">Or create one in the left panel.</p></div></div>
      {/if}
    </main>
  </div>
  {:else}
    <PrebuiltVoiceLibrary {initialService}/>
  {/if}
</div>
<GuidedTour tourId="voices" steps={tourSteps} bind:open={tourOpen}/>
{#if sttSettingsOpen}<SettingsModal section="stt" title="Speech recognition and VAD defaults" description="These defaults are reused for voice-reference transcription and new session transcription runs. Per-operation controls can still override them." onclose={()=>sttSettingsOpen=false}/>{/if}
<style>
  aside button.active{background:var(--accent-soft);color:var(--accent)}
  .library-tab{display:inline-flex;align-items:center;gap:.45rem;border-bottom:2px solid transparent;padding:.75rem 1rem;color:var(--muted);font-size:.82rem;font-weight:700}.library-tab.active{border-color:var(--accent);color:var(--ink)}
  .stt-toolbar{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:.5rem}.stt-toolbar select,.stt-control{display:inline-flex;min-height:2.75rem;align-items:center;gap:.5rem;border:1px solid var(--line);border-radius:.75rem;background:var(--paper);padding:.55rem .75rem;color:var(--ink);font-size:.8rem;line-height:1.2}.vad-threshold{display:grid;grid-template-columns:auto 6rem 2.25rem;align-items:center}.vad-threshold input{width:100%;accent-color:var(--accent)}.vad-threshold output{text-align:right;font-size:.72rem;font-variant-numeric:tabular-nums;font-weight:700}
</style>
