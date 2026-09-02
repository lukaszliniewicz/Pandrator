<script lang="ts">
  import { errorMessage } from './errors';
  import { page } from '$app/state';
  import {
    ArrowLeft,
    AudioLines,
    CheckCircle2,
    CircleAlert,
    CloudUpload,
    Library,
    Link2,
    LoaderCircle,
    Mic,
    Pencil,
    Play,
    Plus,
    Save,
    Settings2,
    Square,
    Trash2,
    Unlink,
    Volume2,
    WandSparkles
  } from '@lucide/svelte';
  import { diagnosticsApi, speechServiceApi, voiceApi } from './admin-api';
  import type {
    RuntimeCapabilities,
    TtsService,
    VoiceRecord,
    VoiceProviderRegistration
  } from './api-models';
  import { jobApi } from './domain-api';
  import { onDestroy, onMount } from 'svelte';
  import GuidedTour from './GuidedTour.svelte';
  import SettingsModal from './SettingsModal.svelte';
  import PrebuiltVoiceLibrary from './PrebuiltVoiceLibrary.svelte';
  import { modalFocus } from './modal-focus';

  type Voice = VoiceRecord;
  type Sample = {
    id: string;
    artifact_id: string;
    transcript?: string;
    transcript_language?: string;
    transcript_reviewed: boolean;
    file_status?: 'ready' | 'missing' | 'unsafe';
    available?: boolean;
    voice_revision?: number;
  };

  let {
    onback,
    initialView,
    initialService = '',
    initialVoice = '',
    embedded = false,
    onvoicepublished
  }: {
    onback: () => void;
    initialView?: 'references' | 'prebuilt';
    initialService?: string;
    initialVoice?: string;
    embedded?: boolean;
    onvoicepublished?: (providerVoiceId: string) => void;
  } = $props();
  let activeView = $state<'references' | 'prebuilt'>('references');
  const requestedService = $derived(
    initialService || page.url.searchParams.get('service') || ''
  );
  let voices = $state<Voice[]>([]);
  let selected = $state<Voice | null>(null);
  let samples = $state<Sample[]>([]);
  let capabilities = $state<RuntimeCapabilities>({});
  let ttsServices = $state<TtsService[]>([]);
  let error = $state('');
  let notice = $state('');
  let newName = $state('');
  let newNameInput = $state<HTMLInputElement>();
  let sampleUploadInput = $state<HTMLInputElement>();
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
  let transcribing = $state<Record<string, boolean>>({});
  let transcribingMissing = $state(false);
  let publishing = $state(false);
  let uploadingSample = $state(false);
  let removingProviders = $state<Record<string, boolean>>({});
  let deleteDialogOpen = $state(false);
  let deleteProviderSelection = $state<Record<string, boolean>>({});
  let editingVoice = $state(false);
  let savingVoice = $state(false);
  let deletingVoice = $state(false);
  let deletingSamples = $state<Record<string, boolean>>({});
  let replacingSamples = $state<Record<string, boolean>>({});
  let editName = $state('');
  let editLanguage = $state('');
  let editDescription = $state('');

  const tourSteps = [
    {
      section: 'Voices',
      title: 'References stay reviewable',
      body: 'Each voice can contain multiple playable samples and an editable, explicitly reviewed transcript.'
    },
    {
      section: 'Recording',
      title: 'Record in the browser',
      body: 'Microphone access is requested only when you enable recording. Preview locally, then save; FFmpeg normalizes the sample to mono PCM WAV.'
    },
    {
      section: 'Transcription',
      title: 'Local STT is optional',
      body: 'CrispASR runs Whisper, Parakeet, or native-speaker MOSS and retains word timing metadata. Nothing is saved until you review it.'
    }
  ];
  const canTranscribe = $derived(Boolean(capabilities?.stt?.crispasr));
  const canRecord = $derived(
    Boolean(
      capabilities?.ffmpeg?.available &&
      capabilities?.recording?.browser_media_recorder !== false
    )
  );
  // Pre-built-only commercial services (Vertex, Gemini, OpenAI) must never
  // offer the reference-upload flow: those APIs do not clone voices.
  const providerTarget = $derived(
    ttsServices.find(
      (service) =>
        service.id === requestedService &&
        service.supports_voice_cloning === true
    )
  );
  const providerRegistration = $derived(
    selected?.metadata_json?.providers?.[providerTarget?.id ?? '']
  );
  const isLinkedRegistration = (
    registration: VoiceProviderRegistration | undefined,
    service?: TtsService
  ) =>
    registration?.resource_kind === 'linked_reference' ||
    service?.adapter === 'audio_cpp';
  const providerUsesLinkedReferences = $derived(
    isLinkedRegistration(providerRegistration, providerTarget)
  );
  const providerNeedsReviewedTranscript = $derived(
    providerTarget?.voice_reference_text === 'required' &&
      !selected?.preferred_sample_transcript_reviewed
  );
  const providerRegistrations = $derived(
    Object.entries(selected?.metadata_json?.providers ?? {}).map(
      ([serviceId, registration]) => ({
        serviceId,
        registration: registration as VoiceProviderRegistration,
        service: ttsServices.find((item) => item.id === serviceId)
      })
    )
  );
  const transcribingCount = $derived(
    Object.values(transcribing).filter(Boolean).length
  );
  const sttModelInfo = $derived(capabilities?.stt?.models?.[engine] ?? {});

  const sttModelLabel = (
    modelId: 'whisper' | 'parakeet' | 'moss',
    label: string
  ) => {
    const info = capabilities?.stt?.models?.[modelId] ?? {};
    if (info.default)
      return `${label} · default${info.installed ? '' : ' · downloads on first use'}`;
    return `${label}${info.installed ? ' · ready' : ' · downloads on first use'}`;
  };

  const sttEngineName = () =>
    engine === 'moss' ? 'MOSS' : engine === 'parakeet' ? 'Parakeet' : 'Whisper';

  function chooseSttEngine() {
    modelQuantization = String(
      capabilities?.stt?.models?.[engine]?.precision ??
        (engine === 'moss' ? 'q8_0' : 'f16')
    );
    if (engine === 'moss') vadEnabled = false;
  }

  function report(caught: unknown, prefix = '') {
    error = `${prefix}${errorMessage(caught)}`;
    notice = '';
  }

  async function loadVoices() {
    const result = await voiceApi.list<Voice>();
    voices = result.items;
    if (selected)
      selected = voices.find((voice) => voice.id === selected?.id) ?? null;
  }

  async function choose(voice: Voice) {
    stopPlayback();
    selected = voice;
    editingVoice = false;
    editName = voice.name;
    editLanguage = voice.language ?? '';
    editDescription = voice.description ?? '';
    const result = await voiceApi.samples<Sample>(voice.id);
    samples = result.items;
    transcripts = Object.fromEntries(
      samples.map((sample) => [sample.id, sample.transcript ?? ''])
    );
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
      const voice = await voiceApi.create<Voice>({
        name: newName.trim(),
        language
      });
      newName = '';
      await loadVoices();
      await choose(voice);
    } catch (caught) {
      report(caught);
    }
  }

  async function saveVoice() {
    if (!selected || selected.bundled || savingVoice || !editName.trim())
      return;
    savingVoice = true;
    error = '';
    try {
      const updated = await voiceApi.update<Voice>(
        selected.id,
        selected.revision,
        {
          name: editName.trim(),
          language: editLanguage.trim() || null,
          description: editDescription.trim() || null
        }
      );
      notice = 'Voice details saved.';
      await loadVoices();
      await choose(updated);
    } catch (caught) {
      report(caught);
    } finally {
      savingVoice = false;
    }
  }

  function requestDeleteVoice() {
    if (!selected || selected.bundled || deletingVoice) return;
    deleteProviderSelection = Object.fromEntries(
      providerRegistrations.map(({ serviceId }) => [serviceId, false])
    );
    deleteDialogOpen = true;
  }

  async function removeProviderCopy(
    serviceId: string,
    announce = true
  ): Promise<void> {
    if (!selected || removingProviders[serviceId]) return;
    const service = ttsServices.find((item) => item.id === serviceId);
    removingProviders = { ...removingProviders, [serviceId]: true };
    try {
      const job = await voiceApi.unpublish(
        selected.id,
        serviceId,
        selected.revision
      );
      await waitJob(job.id);
      await loadVoices();
      if (selected) await choose(selected);
      if (announce)
        notice = isLinkedRegistration(
          selected?.metadata_json?.providers?.[serviceId],
          service
        )
          ? `${service?.name ?? serviceId} reference link removed.`
          : `${service?.name ?? serviceId} provider copy removed.`;
    } finally {
      const next = { ...removingProviders };
      delete next[serviceId];
      removingProviders = next;
    }
  }

  async function removeProvider(serviceId: string) {
    const service = ttsServices.find((item) => item.id === serviceId);
    const registration = selected?.metadata_json?.providers?.[serviceId];
    const linked = isLinkedRegistration(registration, service);
    if (
      !window.confirm(
        linked
          ? `Unlink this local reference from ${service?.name ?? serviceId}? Existing generated audio will remain, but future generation cannot use the link until you add it again.`
          : `Remove this managed voice copy from ${service?.name ?? serviceId}? Existing generated audio will remain, but future generation with this provider voice will stop working until it is uploaded again.`
      )
    )
      return;
    error = '';
    try {
      await removeProviderCopy(serviceId);
    } catch (caught) {
      report(caught, 'Could not remove the provider copy: ');
    }
  }

  async function deleteVoice() {
    if (!selected || selected.bundled || deletingVoice) return;
    deletingVoice = true;
    error = '';
    try {
      for (const serviceId of Object.keys(deleteProviderSelection).filter(
        (id) => deleteProviderSelection[id]
      ))
        await removeProviderCopy(serviceId, false);
      if (!selected) throw new Error('The local voice is no longer available.');
      await voiceApi.delete(selected.id, selected.revision);
      stopPlayback();
      selected = null;
      samples = [];
      deleteDialogOpen = false;
      notice = 'Voice and its managed local samples were deleted.';
      await loadVoices();
    } catch (caught) {
      report(
        caught,
        'The local voice was retained because cleanup did not finish: '
      );
    } finally {
      deletingVoice = false;
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
        if (!window.isSecureContext)
          throw new Error(
            'Microphone access requires HTTPS or a local browser session.'
          );
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: true
        });
        stream.getTracks().forEach((track) => track.stop());
        microphoneReady = true;
      }
      devices = (await navigator.mediaDevices.enumerateDevices()).filter(
        (device) => device.kind === 'audioinput'
      );
      if (!devices.some((device) => device.deviceId === deviceId))
        deviceId = devices[0]?.deviceId ?? '';
      if (requestAccess && !devices.length)
        throw new Error('No microphone input was found.');
      if (requestAccess)
        notice = `${devices.length} microphone${devices.length === 1 ? '' : 's'} available.`;
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
      const requestedAudio: MediaTrackConstraints | boolean = deviceId
        ? { deviceId: { exact: deviceId } }
        : true;
      try {
        activeStream = await navigator.mediaDevices.getUserMedia({
          audio: requestedAudio
        });
      } catch (caught) {
        // Device IDs can change after reconnecting a microphone. Retry with the
        // browser default instead of leaving the Record button mysteriously dead.
        if (!deviceId) throw caught;
        activeStream = await navigator.mediaDevices.getUserMedia({
          audio: true
        });
      }
      microphoneReady = true;
      await refreshMicrophones(false);
      const preferred = [
        'audio/webm;codecs=opus',
        'audio/ogg;codecs=opus',
        'audio/mp4',
        'audio/webm'
      ].find((type) => MediaRecorder.isTypeSupported(type));
      const next = new MediaRecorder(
        activeStream,
        preferred ? { mimeType: preferred } : undefined
      );
      chunks = [];
      next.ondataavailable = (event) => {
        if (event.data.size) chunks.push(event.data);
      };
      next.onerror = (event) =>
        report(
          (event as Event & { error?: DOMException }).error ??
            new Error('The browser recorder failed.')
        );
      next.onstop = () => {
        if (timer) window.clearInterval(timer);
        timer = undefined;
        activeStream?.getTracks().forEach((track) => track.stop());
        activeStream = null;
        const type = next.mimeType || chunks[0]?.type || 'audio/webm';
        const blob = new Blob(chunks, { type });
        if (!blob.size) {
          report(
            new Error(
              'The browser returned an empty recording. Please try another microphone.'
            )
          );
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
      timer = window.setInterval(() => (seconds += 1), 1000);
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
    if (!selected || selected.bundled || !recordingBlob || savingRecording)
      return;
    savingRecording = true;
    error = '';
    const body = new FormData();
    const extension = recordingBlob.type.includes('ogg')
      ? 'ogg'
      : recordingBlob.type.includes('mp4')
        ? 'm4a'
        : 'webm';
    body.set('file', recordingBlob, `recording.${extension}`);
    body.set('expected_revision', String(selected.revision));
    try {
      const job = await voiceApi.uploadSample(
        selected.id,
        selected.revision,
        body
      );
      await waitJob(job.id);
      clearRecording();
      await loadVoices();
      if (selected) await choose(selected);
      if (!(await maybePublishRequestedVoice()))
        notice = providerNeedsReviewedTranscript
          ? `The sample is ready. Review its transcript before using it with ${providerTarget?.name ?? 'this provider'}.`
          : 'The normalized voice sample was saved.';
    } catch (caught) {
      report(caught);
    } finally {
      savingRecording = false;
    }
  }

  async function uploadReference(event: Event) {
    if (!selected || selected.bundled || uploadingSample) return;
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    const body = new FormData();
    body.set('file', file);
    body.set('expected_revision', String(selected.revision));
    error = '';
    uploadingSample = true;
    try {
      const job = await voiceApi.uploadSample(
        selected.id,
        selected.revision,
        body
      );
      await waitJob(job.id);
      await loadVoices();
      if (selected) await choose(selected);
      if (!(await maybePublishRequestedVoice()))
        notice = providerNeedsReviewedTranscript
          ? `The sample is ready. Review its transcript before using it with ${providerTarget?.name ?? 'this provider'}.`
          : 'Voice sample saved.';
    } catch (caught) {
      report(caught);
    } finally {
      uploadingSample = false;
      input.value = '';
    }
  }

  async function replaceReference(sample: Sample, event: Event) {
    if (!selected || selected.bundled || replacingSamples[sample.id]) return;
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    const body = new FormData();
    body.set('file', file);
    replacingSamples = { ...replacingSamples, [sample.id]: true };
    error = '';
    try {
      const job = await voiceApi.replaceSample(
        selected.id,
        sample.id,
        selected.revision,
        body
      );
      await waitJob(job.id);
      await loadVoices();
      if (selected) await choose(selected);
      if (!(await maybePublishRequestedVoice()))
        notice =
          'Reference audio replaced. Review or transcribe its new transcript.';
    } catch (caught) {
      report(caught);
    } finally {
      const next = { ...replacingSamples };
      delete next[sample.id];
      replacingSamples = next;
      input.value = '';
    }
  }

  async function deleteSample(sample: Sample) {
    if (!selected || selected.bundled || deletingSamples[sample.id]) return;
    if (!window.confirm('Delete this local reference sample?')) return;
    deletingSamples = { ...deletingSamples, [sample.id]: true };
    error = '';
    try {
      await voiceApi.deleteSample(selected.id, sample.id, selected.revision);
      if (playingKey === sample.id) stopPlayback();
      notice = 'Reference sample deleted.';
      await loadVoices();
      if (selected) await choose(selected);
    } catch (caught) {
      report(caught);
    } finally {
      const next = { ...deletingSamples };
      delete next[sample.id];
      deletingSamples = next;
    }
  }

  async function waitJob(id: string) {
    for (let attempt = 0; attempt < 240; attempt += 1) {
      const job = await jobApi.get(id);
      if (job.status === 'succeeded') return job;
      if (['failed', 'canceled', 'interrupted'].includes(job.status))
        throw new Error(job.error_message || `Job ${job.status}`);
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    throw new Error('The operation is still running. Check the job queue.');
  }

  async function transcribe(sample: Sample) {
    if (!selected || transcribing[sample.id]) return;
    error = '';
    transcribing = { ...transcribing, [sample.id]: true };
    notice = `Transcribing sample with ${sttEngineName()}${sttModelInfo.download_on_demand ? ' (the model will download first)' : ''}…`;
    try {
      const job = await voiceApi.transcribeSample(selected.id, sample.id, {
        stt_engine: engine,
        stt_backend: engine,
        stt_compute_backend: computeBackend,
        stt_model_quantization: modelQuantization,
        stt_language: language,
        moss_max_chunk_seconds: 120,
        moss_vad_enabled: engine === 'moss' ? vadEnabled : false,
        moss_ctc_alignment_enabled: true,
        moss_ctc_padding_seconds: 0.5,
        crispasr_vad_enabled: vadEnabled,
        crispasr_vad_threshold: vadThreshold
      });
      const completed = await waitJob(job.id);
      transcripts[sample.id] = String(completed.result_json?.transcript ?? '');
      notice = 'Transcript ready for review. Save it when the text is correct.';
    } catch (caught) {
      report(caught);
    } finally {
      const next = { ...transcribing };
      delete next[sample.id];
      transcribing = next;
    }
  }

  async function transcribeMissing() {
    if (transcribingMissing) return;
    transcribingMissing = true;
    try {
      for (const sample of samples.filter((item) => !item.transcript_reviewed))
        await transcribe(sample);
    } finally {
      transcribingMissing = false;
    }
  }

  async function publishVoice() {
    if (!selected || !providerTarget || !samples.length || publishing) return;
    publishing = true;
    error = '';
    notice = providerUsesLinkedReferences
      ? `Linking ${selected.name} to ${providerTarget.name}…`
      : `Uploading ${selected.name} to ${providerTarget.name}…`;
    try {
      const job = await voiceApi.publish(
        selected.id,
        providerTarget.id,
        selected.revision
      );
      const completed = await waitJob(job.id);
      const providerVoiceId = String(
        completed.result_json?.provider_voice_id ?? ''
      );
      if (!providerVoiceId)
        throw new Error(`${providerTarget.name} did not return a voice ID.`);
      await loadVoices();
      if (selected) await choose(selected);
      notice = providerUsesLinkedReferences
        ? `${selected?.name ?? 'Voice'} is linked to its newest local sample for ${providerTarget.name}.`
        : `${selected?.name ?? 'Voice'} is ready in ${providerTarget.name} as “${providerVoiceId}”.`;
      onvoicepublished?.(providerVoiceId);
    } catch (caught) {
      report(
        caught,
        providerUsesLinkedReferences
          ? `Could not link the voice to ${providerTarget.name}: `
          : `Could not upload the voice to ${providerTarget.name}: `
      );
    } finally {
      publishing = false;
    }
  }

  async function maybePublishRequestedVoice(): Promise<boolean> {
    if (
      !onvoicepublished ||
      !providerTarget ||
      providerNeedsReviewedTranscript ||
      providerTarget.available === false ||
      !samples.some((sample) => sample.available !== false)
    )
      return false;
    await publishVoice();
    return true;
  }

  async function saveTranscript(sample: Sample) {
    if (!selected || !transcripts[sample.id]?.trim()) return;
    try {
      await voiceApi.reviewTranscript<Sample>(selected.id, sample.id, {
        transcript: transcripts[sample.id].trim(),
        language,
        expected_voice_revision: selected.revision
      });
      notice = 'Reviewed transcript saved.';
      await loadVoices();
      if (selected) await choose(selected);
      await maybePublishRequestedVoice();
    } catch (caught) {
      report(caught);
    }
  }

  function stopPlayback() {
    if (playbackAudio) {
      playbackAudio.pause();
      try {
        playbackAudio.currentTime = 0;
      } catch {
        /* not seekable yet */
      }
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
    activeView =
      initialView ??
      (page.url.searchParams.get('view') === 'prebuilt'
        ? 'prebuilt'
        : 'references');
    try {
      const [capabilityPayload, servicesPayload] = await Promise.all([
        diagnosticsApi.capabilities(),
        speechServiceApi.catalogue(Boolean(requestedService)),
        loadVoices()
      ]);
      capabilities = capabilityPayload;
      ttsServices = servicesPayload.services ?? [];
      engine = String(capabilities?.stt?.default_engine ?? 'whisper');
      modelQuantization = String(
        capabilities?.stt?.default_model_quantization ?? 'f16'
      );
      if (engine === 'moss') vadEnabled = false;
      const requestedVoice = voices.find((voice) => voice.id === initialVoice);
      if (requestedVoice) await choose(requestedVoice);
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

<audio
  bind:this={playbackAudio}
  preload="metadata"
  class="sr-only"
  onended={() => (playingKey = '')}
  onerror={() => {
    if (playingKey)
      error = 'Playback failed: the audio file could not be decoded or loaded.';
    playingKey = '';
  }}
></audio>

<div class="voice-manager mx-auto flex w-full max-w-7xl flex-col">
  {#if !embedded}<button
      onclick={onback}
      class="muted mb-6 flex shrink-0 items-center gap-2 self-start text-sm font-semibold"
      ><ArrowLeft size={17} /> Workspace</button
    >
    <header
      class="mb-5 flex shrink-0 flex-wrap items-end justify-between gap-4"
    >
      <div>
        <div class="eyebrow">Voices</div>
        <h1 class="mt-2 text-4xl font-semibold">Voice Library</h1>
        <p class="muted mt-2 text-sm">
          Manage voice-cloning references and compare provider voices in one
          workspace.
        </p>
      </div>
      {#if activeView === 'references'}<div class="flex gap-2">
          <button
            onclick={() => (tourOpen = true)}
            class="rounded-xl border border-[var(--line)] px-4 py-2 text-sm font-semibold"
            >Tour</button
          >
        </div>{/if}
    </header>
  {/if}
  <div class="mb-6 flex shrink-0 gap-2 border-b border-[var(--line)]">
    <button
      onclick={() => (activeView = 'references')}
      class:active={activeView === 'references'}
      class="library-tab"><Library size={16} /> Reference samples</button
    ><button
      onclick={() => (activeView = 'prebuilt')}
      class:active={activeView === 'prebuilt'}
      class="library-tab"><AudioLines size={16} /> Pre-built voices</button
    >
  </div>
  {#if activeView === 'references'}
    {#if error}<div
        role="alert"
        class="mb-4 flex items-start gap-2 rounded-xl border border-red-400/40 bg-red-500/10 px-4 py-3 text-sm"
      >
        <CircleAlert class="mt-0.5 shrink-0" size={16} /><span>{error}</span>
      </div>{/if}
    {#if notice}<div
        role="status"
        class="mb-4 rounded-xl border border-[var(--line)] bg-[var(--accent-soft)] px-4 py-3 text-sm"
      >
        {notice}
      </div>{/if}

    <div class="grid items-start gap-5 lg:grid-cols-[20rem_1fr]">
      <aside class="surface flex flex-col rounded-3xl p-4">
        <div class="relative flex gap-2">
          <input
            bind:this={newNameInput}
            bind:value={newName}
            oninput={() => (nameRequired = false)}
            aria-label="New voice name"
            aria-invalid={nameRequired}
            aria-describedby={nameRequired ? 'voice-name-required' : undefined}
            placeholder="New voice"
            class:border-red-500={nameRequired}
            class="min-w-0 flex-1 rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-sm"
          /><button
            onclick={createVoice}
            aria-label="Add voice"
            title="Add voice"
            class="btn btn-icon btn-primary"><Plus size={17} /></button
          >{#if nameRequired}<div
              id="voice-name-required"
              role="tooltip"
              class="absolute left-1 top-[calc(100%+.45rem)] z-10 rounded-lg bg-[var(--ink)] px-3 py-2 text-xs font-semibold text-[var(--paper-strong)] shadow-lg"
            >
              Enter a voice name first.<span
                class="absolute -top-1 left-4 size-2 rotate-45 bg-[var(--ink)]"
              ></span>
            </div>{/if}
        </div>
        <div class="mt-4 space-y-1">
          {#each voices as voice}<button
              onclick={() => choose(voice)}
              class:active={selected?.id === voice.id}
              class="flex w-full items-center gap-3 rounded-xl px-3 py-3 text-left"
              ><Library size={17} /><span
                class="min-w-0 flex-1 truncate font-semibold">{voice.name}</span
              ><span class="muted text-xs">{voice.language}</span></button
            >{:else}<p class="muted p-5 text-center text-sm">
              Create a voice to add samples.
            </p>{/each}
        </div>
      </aside>

      <main class="surface rounded-3xl p-5 sm:p-7">
        {#if selected}
          <div class="mb-6 flex flex-wrap items-center justify-between gap-4">
            <div>
              <div class="flex flex-wrap items-center gap-2">
                <h2 class="text-2xl font-semibold">{selected.name}</h2>
                {#if selected.bundled}<span
                    class="rounded-full bg-[var(--accent-soft)] px-2 py-1 text-[0.65rem] font-bold uppercase tracking-wide text-[var(--accent)]"
                    >Bundled</span
                  >{/if}
              </div>
              <p class="muted text-sm">
                Review playback, transcripts, and recordings in one place.
              </p>
            </div>
            <div class="flex flex-wrap items-center justify-end gap-2">
              {#if !selected.bundled}<button
                  onclick={() => (editingVoice = !editingVoice)}
                  class="stt-control font-semibold"
                  ><Pencil size={15} /> Edit voice</button
                ><button
                  onclick={requestDeleteVoice}
                  disabled={deletingVoice}
                  class="stt-control font-semibold text-red-600 disabled:opacity-40"
                  ><Trash2 size={15} />
                  {deletingVoice ? 'Deleting…' : 'Delete voice'}</button
                >{/if}
            </div>
          </div>

          {#if editingVoice && !selected.bundled}<section
              class="mb-5 grid gap-3 rounded-2xl border border-[var(--line)] p-4 sm:grid-cols-2"
            >
              <label class="text-xs font-semibold"
                >Name<input
                  bind:value={editName}
                  class="mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-sm font-normal"
                /></label
              ><label class="text-xs font-semibold"
                >Language<input
                  bind:value={editLanguage}
                  placeholder="en"
                  class="mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-sm font-normal"
                /></label
              ><label class="text-xs font-semibold sm:col-span-2"
                >Description<textarea
                  bind:value={editDescription}
                  rows="2"
                  class="mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-sm font-normal"
                ></textarea></label
              >
              <div class="flex justify-end gap-2 sm:col-span-2">
                <button onclick={() => (editingVoice = false)} class="btn"
                  >Cancel</button
                ><button
                  onclick={saveVoice}
                  disabled={savingVoice || !editName.trim()}
                  class="btn btn-primary disabled:opacity-40"
                  ><Save size={15} />
                  {savingVoice ? 'Saving…' : 'Save voice'}</button
                >
              </div>
            </section>{/if}

          <section
            class="mb-5 flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-[var(--line)] p-4"
            aria-busy={uploadingSample}
          >
            <div>
              <h3 class="font-semibold">Samples</h3>
              <p class="muted mt-1 text-xs">
                {selected.available_sample_count ??
                  samples.filter((sample) => sample.available !== false).length}
                readable sample{(selected.available_sample_count ??
                  samples.length) === 1
                  ? ''
                  : 's'} · add clean speech references for reusable voices.
              </p>
            </div>
            {#if !selected.bundled}<button
                type="button"
                onclick={() => sampleUploadInput?.click()}
                disabled={uploadingSample}
                class="btn btn-primary disabled:opacity-40"
                ><CloudUpload size={16} />
                {uploadingSample ? 'Uploading…' : 'Upload sample'}</button
              ><input
                bind:this={sampleUploadInput}
                type="file"
                accept="audio/*"
                disabled={uploadingSample}
                onchange={uploadReference}
                class="sr-only"
              />{/if}
          </section>

          <div class="mb-5 flex flex-wrap items-center justify-end gap-3">
            <div class="stt-toolbar">
              <select
                bind:value={engine}
                onchange={chooseSttEngine}
                disabled={!canTranscribe || transcribingCount > 0}
                aria-label="Transcription model"
                ><option value="whisper"
                  >{sttModelLabel('whisper', 'Whisper large-v3')}</option
                ><option value="parakeet"
                  >{sttModelLabel('parakeet', 'Parakeet 0.6B v3')}</option
                ><option value="moss"
                  >{sttModelLabel('moss', 'MOSS Diarize 0.9B')}</option
                ></select
              ><select
                bind:value={modelQuantization}
                disabled={!canTranscribe || transcribingCount > 0}
                aria-label="Transcription model precision"
                ><option value="f16">FP16</option
                >{#if engine === 'whisper'}<option value="q5_0">Q5_0</option
                  >{:else if engine === 'parakeet'}<option value="q8_0"
                    >Q8_0</option
                  ><option value="q5_0">Q5_0</option><option value="q4_k"
                    >Q4_K</option
                  >{:else}<option value="q8_0">Q8_0 · recommended</option
                  ><option value="q4_k">Q4_K</option>{/if}</select
              ><select
                bind:value={computeBackend}
                disabled={!canTranscribe || transcribingCount > 0}
                aria-label="Transcription compute backend"
                ><option value="auto">Automatic compute</option><option
                  value="cpu">CPU</option
                ><option value="cuda">CUDA</option><option value="vulkan"
                  >Vulkan</option
                ><option value="metal">Metal</option></select
              ><label class="stt-control"
                ><input
                  bind:checked={vadEnabled}
                  disabled={transcribingCount > 0}
                  type="checkbox"
                  class="accent-[var(--accent)]"
                /><span>VAD</span></label
              ><label
                class:opacity-45={!vadEnabled}
                class="stt-control vad-threshold"
                ><span>VAD threshold</span><input
                  bind:value={vadThreshold}
                  aria-label="VAD threshold"
                  type="range"
                  min="0"
                  max="1"
                  step="0.05"
                  disabled={!vadEnabled || transcribingCount > 0}
                /><output>{Number(vadThreshold).toFixed(2)}</output></label
              ><button
                onclick={transcribeMissing}
                disabled={!canTranscribe ||
                  transcribingMissing ||
                  transcribingCount > 0 ||
                  !samples.some((item) => !item.transcript_reviewed)}
                class="stt-control font-semibold disabled:opacity-40"
                >{#if transcribingMissing}<LoaderCircle
                    class="animate-spin"
                    size={16}
                  />{:else}<WandSparkles size={16} />{/if}
                {transcribingMissing
                  ? 'Transcribing…'
                  : 'Transcribe missing'}</button
              >
            </div>
          </div>

          <div class="mb-4 flex justify-end">
            <button
              onclick={() => (sttSettingsOpen = true)}
              class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-3 py-2 text-xs font-semibold"
              ><Settings2 size={15} /> All speech recognition and VAD defaults</button
            >
          </div>
          {#if providerTarget}
            <section
              class="mb-5 flex flex-wrap items-center gap-4 rounded-2xl border border-[var(--accent)]/35 bg-[var(--accent-soft)] p-4"
            >
              <div class="min-w-0 flex-1">
                <div class="flex items-center gap-2 font-semibold">
                  {#if providerRegistration?.status === 'ready'}<CheckCircle2
                      size={17}
                    />{:else if providerUsesLinkedReferences}<Link2
                      size={17}
                    />{:else}<CloudUpload size={17} />{/if} Use with {providerTarget.name}
                </div>
                <p class="muted mt-1 text-xs">
                  {#if providerRegistration?.status === 'ready' && providerUsesLinkedReferences}Linked
                    to the newest readable local sample. Replacing the sample
                    automatically updates what audio.cpp uses; the audio is
                    encoded only when synthesis starts.{:else if providerRegistration?.status === 'ready'}Uploaded
                    as “{providerRegistration.voice_id}”. The provider copy
                    matches the current reference.{:else if providerRegistration?.status === 'stale'}Uploaded
                    as “{providerRegistration.voice_id}”, but the local
                    reference changed. Upload again to refresh it.{:else if providerNeedsReviewedTranscript}This
                    provider needs an accurate reviewed transcript for the
                    newest sample. Transcribe or enter it below, then save the
                    review.{:else if providerUsesLinkedReferences && samples.some((sample) => sample.available !== false)}Links
                    this voice to its newest normalized local sample. A reviewed
                    transcript improves Qwen cloning and is required when you
                    synthesize with OmniVoice.{:else if samples.some((sample) => sample.available !== false)}Uploads
                    the newest normalized sample and stores the exact provider
                    voice ID returned by the API.{:else}Add, replace, or record
                    a readable sample first; local library names are not sent to
                    synthesis until the provider accepts them.{/if}
                </p>
                {#if providerTarget.available === false}<p
                    class="mt-1 text-xs text-[var(--warning)]"
                  >
                    {providerUsesLinkedReferences
                      ? 'You can create the local link now; start audio.cpp before synthesis.'
                      : providerTarget.availability_reason ||
                        'Start this service before uploading.'}
                  </p>{/if}
              </div>
              <button
                onclick={publishVoice}
                disabled={!samples.some(
                  (sample) => sample.available !== false
                ) ||
                  publishing ||
                  providerNeedsReviewedTranscript ||
                  (providerTarget.available === false &&
                    !providerUsesLinkedReferences)}
                class="btn btn-primary disabled:opacity-40"
                >{#if publishing}<LoaderCircle
                    class="animate-spin"
                    size={16}
                  />{:else if providerUsesLinkedReferences}<Link2
                    size={16}
                  />{:else}<CloudUpload size={16} />{/if}
                {publishing
                  ? providerUsesLinkedReferences
                    ? 'Linking…'
                    : 'Uploading…'
                  : providerRegistration?.status === 'ready'
                    ? providerUsesLinkedReferences
                      ? 'Refresh link'
                      : 'Update provider voice'
                    : providerUsesLinkedReferences
                      ? `Link to ${providerTarget.name}`
                      : `Upload to ${providerTarget.name}`}</button
              >
            </section>
          {/if}
          {#if providerRegistrations.length}
            <section class="mb-5 rounded-2xl border border-[var(--line)] p-4">
              <h3 class="font-semibold">Provider links and copies</h3>
              <p class="muted mt-1 text-xs">
                Links keep using your newest local sample; copies live in the
                provider. Removing either does not delete the local voice or
                previously generated audio.
              </p>
              <div class="mt-3 space-y-2">
                {#each providerRegistrations as item}
                  <div
                    class="flex flex-wrap items-center gap-3 rounded-xl bg-[var(--paper)] px-3 py-3"
                  >
                    <div class="min-w-0 flex-1">
                      <div
                        class="flex flex-wrap items-center gap-2 text-sm font-semibold"
                      >
                        <span>{item.service?.name ?? item.serviceId}</span>
                        <span
                          class="rounded-full bg-[var(--accent-soft)] px-2 py-0.5 text-[0.65rem] uppercase tracking-wide"
                          >{item.registration.status ?? 'registered'}</span
                        >
                      </div>
                      <p
                        class="muted mt-1 truncate text-xs"
                        title={item.registration.voice_id}
                      >
                        {isLinkedRegistration(item.registration, item.service)
                          ? `Local reference link: ${item.registration.voice_id ?? 'unknown'}`
                          : `Provider ID: ${item.registration.voice_id ?? 'unknown'}`}
                      </p>
                    </div>
                    {#if item.registration.managed_by === 'pandrator' && (item.service?.supports_voice_deletion || isLinkedRegistration(item.registration, item.service))}
                      <button
                        type="button"
                        onclick={() => removeProvider(item.serviceId)}
                        disabled={Boolean(removingProviders[item.serviceId])}
                        class="flex items-center gap-2 rounded-xl border border-red-400/50 px-3 py-2 text-xs font-semibold text-red-600 disabled:opacity-40"
                      >
                        {#if removingProviders[item.serviceId]}<LoaderCircle
                            size={14}
                            class="animate-spin"
                          />{:else if isLinkedRegistration(item.registration, item.service)}<Unlink
                            size={14}
                          />{:else}<Trash2 size={14} />{/if}
                        {removingProviders[item.serviceId]
                          ? 'Removing…'
                          : isLinkedRegistration(
                                item.registration,
                                item.service
                              )
                            ? 'Unlink'
                            : 'Remove from provider'}
                      </button>
                    {:else}
                      <span class="muted max-w-52 text-right text-xs">
                        {item.registration.managed_by !== 'pandrator'
                          ? 'Legacy copy—remove it in the provider.'
                          : 'This provider cannot remove voices remotely.'}
                      </span>
                    {/if}
                  </div>
                {/each}
              </div>
            </section>
          {/if}
          <section class="mb-7 rounded-2xl border border-[var(--line)] p-4">
            <div class="mb-3">
              <h3 class="font-semibold">Record a reference</h3>
              <p class="muted mt-1 text-xs">
                Permission is requested only when you enable the microphone. The
                recording remains local until you save it.
              </p>
            </div>
            <div class="flex flex-wrap items-center gap-3">
              {#if !microphoneReady}<button
                  onclick={() => refreshMicrophones(true)}
                  disabled={selected.bundled ||
                    !canRecord ||
                    checkingMicrophone}
                  class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-4 py-2 text-sm font-semibold disabled:opacity-40"
                  ><Mic size={16} />
                  {checkingMicrophone
                    ? 'Checking…'
                    : 'Enable microphone'}</button
                >{/if}
              <select
                bind:value={deviceId}
                aria-label="Microphone"
                disabled={!microphoneReady ||
                  !devices.length ||
                  recording ||
                  stopping}
                class="min-w-48 rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-sm"
                ><option value="">Default microphone</option
                >{#each devices as device}<option value={device.deviceId}
                    >{device.label ||
                      `Microphone ${devices.indexOf(device) + 1}`}</option
                  >{/each}</select
              >
              {#if !recording && !stopping}<button
                  onclick={startRecording}
                  disabled={selected.bundled ||
                    !microphoneReady ||
                    !devices.length ||
                    !canRecord}
                  class="flex items-center gap-2 rounded-xl bg-red-500 px-4 py-2 font-semibold text-white disabled:opacity-40"
                  ><Mic size={16} /> Record</button
                >{:else}<button
                  onclick={stopRecording}
                  disabled={stopping}
                  class="flex items-center gap-2 rounded-xl bg-red-500 px-4 py-2 font-semibold text-white disabled:opacity-60"
                  ><Square size={15} />
                  {stopping ? 'Finishing…' : `Stop · ${seconds}s`}</button
                >{/if}
              {#if recordingUrl}
                <button
                  aria-label={playingKey === 'recording'
                    ? 'Stop recording playback'
                    : 'Play recording'}
                  onclick={() => togglePlayback('recording', recordingUrl)}
                  class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-3 py-2 text-sm font-semibold"
                  >{#if playingKey === 'recording'}<Square size={15} /> Stop{:else}<Play
                      size={16}
                    /> Preview{/if}</button
                >
                <button
                  onclick={discard}
                  aria-label="Discard recording"
                  class="rounded-xl border border-[var(--line)] p-2"
                  ><Trash2 size={16} /></button
                >
                <button
                  onclick={saveRecording}
                  disabled={selected.bundled || savingRecording}
                  class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2 font-semibold text-white disabled:opacity-50"
                  ><Save size={16} />
                  {savingRecording ? 'Normalizing…' : 'Save sample'}</button
                >
              {/if}
            </div>
            {#if !capabilities?.ffmpeg?.available}<p
                class="mt-2 text-xs text-[var(--warning)]"
              >
                Recording is disabled until FFmpeg is available.
              </p>{/if}
          </section>

          <div class="space-y-4">
            {#each samples as sample, sampleIndex}
              <article class="rounded-2xl border border-[var(--line)] p-4">
                <div class="mb-3 flex flex-wrap items-center gap-2">
                  <h4 class="font-semibold">Sample {sampleIndex + 1}</h4>
                  <span
                    class="rounded-full bg-[var(--accent-soft)] px-2 py-0.5 text-[0.65rem] font-semibold uppercase tracking-wide"
                    >{sample.available === false
                      ? 'Missing audio'
                      : 'Ready'}</span
                  >
                  {#if selected.preferred_sample_id === sample.id}<span
                      class="muted text-xs"
                      >Used for the next provider upload</span
                    >{/if}
                </div>
                <div class="flex flex-wrap items-center gap-3">
                  <button
                    aria-label={playingKey === sample.id
                      ? 'Stop sample playback'
                      : sample.available === false
                        ? 'Sample file missing'
                        : 'Play sample'}
                    disabled={sample.available === false}
                    onclick={() =>
                      togglePlayback(
                        sample.id,
                        `/api/v1/artifacts/${sample.artifact_id}/content`
                      )}
                    class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-3 py-2 text-sm font-semibold disabled:opacity-40"
                    >{#if playingKey === sample.id}<Square size={15} /> Stop{:else}<Volume2
                        size={16}
                      /> Play sample{/if}</button
                  >
                  <button
                    onclick={() => transcribe(sample)}
                    disabled={!canTranscribe ||
                      sample.available === false ||
                      Boolean(transcribing[sample.id]) ||
                      transcribingMissing}
                    aria-busy={Boolean(transcribing[sample.id])}
                    class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-3 py-2 text-sm font-semibold disabled:opacity-40"
                    >{#if transcribing[sample.id]}<LoaderCircle
                        class="animate-spin"
                        size={15}
                      />{:else}<WandSparkles size={15} />{/if}
                    {transcribing[sample.id]
                      ? 'Transcribing…'
                      : 'Transcribe'}</button
                  >
                  <span class="muted text-xs" aria-live="polite"
                    >{sample.available === false
                      ? 'Audio file missing · replace or remove this entry'
                      : transcribing[sample.id]
                        ? 'Speech recognition is running'
                        : sample.transcript_reviewed
                          ? 'Transcript reviewed'
                          : 'Transcript not reviewed'}</span
                  >
                  {#if !selected.bundled}<span
                      class="ml-auto flex items-center gap-2"
                      ><label
                        class:pointer-events-none={Boolean(
                          replacingSamples[sample.id]
                        )}
                        class="cursor-pointer rounded-xl border border-[var(--line)] px-3 py-2 text-xs font-semibold"
                        >{replacingSamples[sample.id]
                          ? 'Replacing…'
                          : 'Replace audio'}<input
                          type="file"
                          accept="audio/*"
                          onchange={(event) => replaceReference(sample, event)}
                          class="sr-only"
                        /></label
                      ><button
                        onclick={() => deleteSample(sample)}
                        disabled={Boolean(deletingSamples[sample.id])}
                        aria-label="Delete voice sample"
                        class="flex items-center gap-1.5 rounded-xl border border-[var(--line)] px-3 py-2 text-xs font-semibold text-red-600 disabled:opacity-40"
                        ><Trash2 size={15} />
                        {deletingSamples[sample.id]
                          ? 'Removing…'
                          : 'Remove'}</button
                      ></span
                    >{/if}
                </div>
                <details
                  class="mt-3 rounded-xl border border-[var(--line)] px-3 py-2"
                >
                  <summary class="cursor-pointer text-sm font-semibold">
                    Transcript and recognition · {sample.transcript_reviewed
                      ? 'reviewed'
                      : 'needs review'}
                  </summary>
                  <textarea
                    bind:value={transcripts[sample.id]}
                    disabled={selected.bundled}
                    rows="3"
                    placeholder="Transcript will remain unsaved until you review it."
                    class="mt-3 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] p-3 text-sm"
                  ></textarea>
                  <div class="mt-2 flex justify-end">
                    <button
                      onclick={() => saveTranscript(sample)}
                      disabled={selected.bundled ||
                        !transcripts[sample.id]?.trim()}
                      class="flex items-center gap-2 rounded-lg bg-[var(--accent)] px-3 py-2 text-xs font-semibold text-white disabled:opacity-40"
                      ><Save size={14} /> Save reviewed transcript</button
                    >
                  </div>
                </details>
              </article>
            {:else}
              <div
                class="muted rounded-2xl border border-dashed border-[var(--line)] p-10 text-center"
              >
                <Play class="mx-auto mb-2" size={22} /> Add the first clean voice
                sample by recording above or uploading a file.
                {#if !selected.bundled}<button
                    type="button"
                    onclick={() => sampleUploadInput?.click()}
                    disabled={uploadingSample}
                    class="btn btn-primary mx-auto mt-4 w-fit disabled:opacity-40"
                    ><CloudUpload size={16} />
                    {uploadingSample ? 'Uploading…' : 'Upload sample'}</button
                  >{/if}
              </div>
            {/each}
          </div>
        {:else}
          <div class="grid h-full min-h-96 place-items-center text-center">
            <div>
              <Library class="mx-auto text-[var(--accent)]" size={30} />
              <h2 class="mt-3 text-xl font-semibold">Select a voice</h2>
              <p class="muted mt-1">Or create one in the left panel.</p>
            </div>
          </div>
        {/if}
      </main>
    </div>
  {:else}
    <div>
      <PrebuiltVoiceLibrary initialService={requestedService} />
    </div>
  {/if}
</div>
{#if deleteDialogOpen && selected}<div
    class="fixed inset-0 z-[80] grid place-items-center bg-black/45 p-4 backdrop-blur-sm"
    role="presentation"
  >
    <div
      use:modalFocus={{ onclose: () => (deleteDialogOpen = false) }}
      class="surface w-full max-w-xl rounded-3xl p-6 shadow-2xl"
      role="dialog"
      aria-modal="true"
      aria-labelledby="delete-voice-title"
    >
      <h2 id="delete-voice-title" class="text-xl font-semibold">
        Delete “{selected.name}”?
      </h2>
      <p class="muted mt-2 text-sm leading-relaxed">
        The local voice and {samples.length} sample{samples.length === 1
          ? ''
          : 's'} will be deleted. Existing generated audio is not affected.
      </p>
      {#if providerRegistrations.length}
        <fieldset class="mt-5 space-y-2">
          <legend class="mb-2 text-sm font-semibold">
            Also remove provider links and managed copies (optional)
          </legend>
          {#each providerRegistrations as item}
            {@const removable =
              item.registration.managed_by === 'pandrator' &&
              (item.service?.supports_voice_deletion === true ||
                isLinkedRegistration(item.registration, item.service))}
            <label
              class="flex items-start gap-3 rounded-xl border border-[var(--line)] p-3"
              class:opacity-60={!removable}
            >
              <input
                type="checkbox"
                bind:checked={deleteProviderSelection[item.serviceId]}
                disabled={!removable || deletingVoice}
                class="mt-1"
              />
              <span class="min-w-0">
                <span class="block text-sm font-semibold"
                  >{item.service?.name ?? item.serviceId}</span
                >
                <span class="muted mt-0.5 block text-xs">
                  {removable
                    ? isLinkedRegistration(item.registration, item.service)
                      ? 'Remove the local audio.cpp link before deleting this voice.'
                      : 'Remove the Pandrator-managed copy before deleting locally.'
                    : item.registration.managed_by !== 'pandrator'
                      ? 'Legacy registration: ownership cannot be verified, so remote deletion is disabled.'
                      : 'This provider does not support remote voice deletion.'}
                </span>
              </span>
            </label>
          {/each}
        </fieldset>
      {/if}
      <p class="muted mt-4 text-xs">
        If selected link or provider cleanup fails, the local voice will be
        retained so you can retry without losing its samples.
      </p>
      <div class="mt-6 flex justify-end gap-2">
        <button
          type="button"
          onclick={() => (deleteDialogOpen = false)}
          disabled={deletingVoice}
          class="rounded-xl border border-[var(--line)] px-4 py-2 text-sm font-semibold disabled:opacity-40"
          >Cancel</button
        >
        <button
          type="button"
          onclick={deleteVoice}
          disabled={deletingVoice}
          class="flex items-center gap-2 rounded-xl bg-red-600 px-4 py-2 text-sm font-semibold text-white disabled:opacity-40"
          >{#if deletingVoice}<LoaderCircle
              size={16}
              class="animate-spin"
            />{/if}
          {deletingVoice ? 'Deleting…' : 'Delete local voice'}</button
        >
      </div>
    </div>
  </div>{/if}
<GuidedTour tourId="voices" steps={tourSteps} bind:open={tourOpen} />
{#if sttSettingsOpen}<SettingsModal
    section="stt"
    title="Speech recognition and VAD defaults"
    description="These defaults are reused for voice-reference transcription and new session transcription runs. Per-operation controls can still override them."
    onclose={() => (sttSettingsOpen = false)}
  />{/if}

<style>
  aside button.active {
    background: var(--accent-soft);
    color: var(--accent);
  }
  .library-tab {
    display: inline-flex;
    align-items: center;
    gap: 0.45rem;
    border-bottom: 2px solid transparent;
    padding: 0.75rem 1rem;
    color: var(--muted);
    font-size: 0.82rem;
    font-weight: 700;
  }
  .library-tab.active {
    border-color: var(--accent);
    color: var(--ink);
  }
  .stt-toolbar {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 0.5rem;
  }
  .stt-toolbar select,
  .stt-control {
    display: inline-flex;
    min-height: 2.75rem;
    align-items: center;
    gap: 0.5rem;
    border: 1px solid var(--line);
    border-radius: 0.75rem;
    background: var(--paper);
    padding: 0.55rem 0.75rem;
    color: var(--ink);
    font-size: 0.8rem;
    line-height: 1.2;
  }
  .vad-threshold {
    display: grid;
    grid-template-columns: auto 6rem 2.25rem;
    align-items: center;
  }
  .vad-threshold input {
    width: 100%;
    accent-color: var(--accent);
  }
  .vad-threshold output {
    text-align: right;
    font-size: 0.72rem;
    font-variant-numeric: tabular-nums;
    font-weight: 700;
  }
</style>
