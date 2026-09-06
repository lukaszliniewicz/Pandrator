<script lang="ts">
  import {
    CircleAlert,
    LoaderCircle,
    RefreshCw,
    Save,
    WandSparkles,
    X
  } from '@lucide/svelte';
  import { onDestroy } from 'svelte';
  import { speechServiceApi, voiceApi } from './admin-api';
  import type { JobRecord, TtsService, VoiceRecord } from './api-models';
  import { jobApi } from './domain-api';
  import { errorMessage } from './errors';
  import { modalFocus } from './modal-focus';
  import AudioPlayer from './AudioPlayer.svelte';

  let {
    services,
    voices,
    initialVoiceId = '',
    onclose,
    onsaved
  }: {
    services: TtsService[];
    voices: VoiceRecord[];
    initialVoiceId?: string;
    onclose: () => void;
    onsaved: (
      voiceId: string,
      providerVoiceId?: string,
      warning?: string
    ) => void | Promise<void>;
  } = $props();

  type PreviewSnapshot = {
    artifactId: string;
    prompt: string;
    text: string;
    language: string;
    seed: number;
  };

  const availableVoices = $derived(voices.filter((voice) => !voice.bundled));
  const audioCpp = $derived(
    services.find(
      (service) => service.id === 'audio_cpp' || service.adapter === 'audio_cpp'
    )
  );
  const breezeModel = $derived(
    (audioCpp?.models ?? []).find(
      (model) => model.toLowerCase() === 'breeze_tts_2_q8_0'
    ) ?? ''
  );
  const breezeInfo = $derived(
    (audioCpp?.model_catalog ?? []).find((model) => model.id === breezeModel)
  );
  const canGenerate = $derived(
    Boolean(audioCpp && breezeModel && audioCpp.available !== false)
  );

  let targetVoiceId = $state('');
  let targetInitialized = false;
  let voiceName = $state('');
  let language = $state<'en' | 'zh'>('en');
  let prompt = $state('');
  let sampleText = $state(
    'At the edge of the quiet harbor, morning light moved across the water while the city slowly woke.'
  );
  let seed = $state(randomSeed());
  let preview = $state<PreviewSnapshot | null>(null);
  let generating = $state(false);
  let saving = $state(false);
  let activeJobId = $state('');
  let linkAfterSave = $state(true);
  let error = $state('');
  let progressDetail = $state('');
  let alive = true;
  const validSeed = $derived(
    Number.isInteger(seed) && seed >= 0 && seed <= 2_147_483_647
  );

  $effect(() => {
    if (targetInitialized) return;
    targetVoiceId = availableVoices.some((voice) => voice.id === initialVoiceId)
      ? initialVoiceId
      : '';
    chooseTarget();
    targetInitialized = true;
  });

  function randomSeed() {
    return Math.floor(Math.random() * 2_147_483_647);
  }

  function invalidatePreview() {
    preview = null;
    error = '';
    progressDetail = '';
  }

  function chooseTarget() {
    const target = availableVoices.find((voice) => voice.id === targetVoiceId);
    const targetLanguage = String(target?.language ?? '').toLowerCase();
    if (targetLanguage.startsWith('zh')) language = 'zh';
    else if (targetLanguage.startsWith('en')) language = 'en';
  }

  function chooseAnotherSeed() {
    seed = randomSeed();
    invalidatePreview();
  }

  async function waitJob(id: string, attempts = 4000): Promise<JobRecord> {
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      if (!alive) throw new DOMException('Dialog closed', 'AbortError');
      const job = await jobApi.get(id);
      progressDetail = job.progress_detail ?? '';
      if (job.status === 'succeeded') return job;
      if (['failed', 'canceled', 'interrupted'].includes(job.status))
        throw new Error(job.error_message || `Voice design ${job.status}.`);
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    throw new Error(
      'Voice design is still running. Its progress remains available in Activity & logs.'
    );
  }

  async function generatePreview() {
    const cleanPrompt = prompt.trim();
    const cleanText = sampleText.trim();
    if (!cleanPrompt || !cleanText || !audioCpp || !breezeModel || !validSeed)
      return;
    generating = true;
    error = '';
    preview = null;
    progressDetail = 'Starting Breeze voice design…';
    try {
      const queued = await speechServiceApi.preview(audioCpp.id, {
        text: cleanText,
        model: breezeModel,
        voice: '',
        language,
        generation_prompt: cleanPrompt,
        seed
      });
      activeJobId = queued.id;
      const complete = await waitJob(queued.id);
      const artifactId = String(complete.result_json?.artifact_id ?? '');
      if (!artifactId)
        throw new Error(
          'Breeze finished without returning a playable preview artifact.'
        );
      preview = {
        artifactId,
        prompt: cleanPrompt,
        text: cleanText,
        language,
        seed
      };
      progressDetail = 'Preview ready. Listen before saving it as a reference.';
    } catch (caught) {
      if (alive && (caught as { name?: string })?.name !== 'AbortError')
        error = errorMessage(caught);
    } finally {
      if (alive) {
        activeJobId = '';
        generating = false;
      }
    }
  }

  async function closeDialog() {
    if (saving) return;
    if (activeJobId) {
      progressDetail = 'Canceling voice design…';
      await jobApi.cancel(activeJobId).catch(() => null);
    }
    onclose();
  }

  async function saveDesign() {
    if (!preview || saving) return;
    const requestedName = voiceName.trim();
    if (!targetVoiceId && !requestedName) {
      error = 'Give the new voice a name before saving it.';
      return;
    }
    saving = true;
    error = '';
    progressDetail = 'Saving the designed sample…';
    let createdTarget: VoiceRecord | null = null;
    try {
      let target: VoiceRecord;
      if (targetVoiceId) {
        const latest = await voiceApi.list<VoiceRecord>();
        const found = latest.items.find((voice) => voice.id === targetVoiceId);
        if (!found || found.bundled)
          throw new Error('The selected library voice is no longer editable.');
        target = found;
      } else {
        target = await voiceApi.create<VoiceRecord>({
          name: requestedName,
          language: preview.language,
          description: preview.prompt
        });
        createdTarget = target;
      }

      const queued = await voiceApi.promoteDesignPreview(
        target.id,
        target.revision,
        {
          artifact_id: preview.artifactId,
          transcript: preview.text,
          language: preview.language,
          expected_voice_revision: target.revision
        }
      );
      const complete = await waitJob(queued.id);
      createdTarget = null;
      let providerVoiceId = '';
      let linkWarning = '';
      if (linkAfterSave && audioCpp) {
        try {
          const voiceRevision = Number(complete.result_json?.voice_revision);
          if (!Number.isInteger(voiceRevision) || voiceRevision < 1)
            throw new Error(
              'The new voice revision was not returned. Refresh the library before linking it to audio.cpp.'
            );
          progressDetail = 'Linking the new reference to audio.cpp…';
          const link = await voiceApi.publish(
            target.id,
            audioCpp.id,
            voiceRevision
          );
          const linked = await waitJob(link.id);
          providerVoiceId = String(linked.result_json?.provider_voice_id ?? '');
        } catch (caught) {
          linkWarning = `The designed sample was saved, but audio.cpp linking did not finish: ${errorMessage(caught)}`;
        }
      }
      await onsaved(
        target.id,
        providerVoiceId || undefined,
        linkWarning || undefined
      );
      onclose();
    } catch (caught) {
      if (alive && (caught as { name?: string })?.name !== 'AbortError') {
        let detail = errorMessage(caught);
        if (createdTarget) {
          try {
            await voiceApi.delete(createdTarget.id, createdTarget.revision);
          } catch {
            detail +=
              ' The empty library voice could not be removed automatically; you can delete it from the Voice Library.';
          }
        }
        error = detail;
      }
    } finally {
      if (alive) saving = false;
    }
  }

  onDestroy(() => {
    alive = false;
  });
</script>

<div
  class="fixed inset-0 z-[90] grid place-items-center bg-black/45 p-4 backdrop-blur-sm"
  role="presentation"
  onclick={(event) =>
    !saving && event.target === event.currentTarget && void closeDialog()}
>
  <!-- svelte-ignore a11y_no_noninteractive_element_to_interactive_role -->
  <section
    use:modalFocus={{ onclose: () => !saving && void closeDialog() }}
    class="surface max-h-[94vh] w-full max-w-3xl overflow-y-auto rounded-[1.8rem] p-6 shadow-2xl sm:p-8"
    role="dialog"
    aria-modal="true"
    aria-labelledby="voice-design-title"
    aria-busy={generating || saving}
  >
    <header class="flex items-start justify-between gap-4">
      <div>
        <div class="eyebrow">Breeze voice design</div>
        <h2 id="voice-design-title" class="mt-1 text-2xl font-semibold">
          Design a reusable voice
        </h2>
        <p class="muted mt-2 max-w-2xl text-sm leading-relaxed">
          Describe the speaker, provide the exact words to read, then audition
          the result. Saving promotes that audio to a normal reference sample;
          the sample text becomes its reviewed transcript.
        </p>
      </div>
      <button
        type="button"
        onclick={() => void closeDialog()}
        disabled={saving}
        aria-label="Close voice designer"
        class="rounded-xl p-2 disabled:opacity-40"><X size={20} /></button
      >
    </header>

    {#if error}<div
        role="alert"
        class="mt-5 flex items-start gap-2 rounded-xl border border-red-400/40 bg-red-500/10 px-4 py-3 text-sm"
      >
        <CircleAlert class="mt-0.5 shrink-0" size={16} /><span>{error}</span>
      </div>{/if}

    {#if !breezeModel}<div
        class="mt-5 rounded-xl border border-[var(--warning)]/40 bg-[var(--warning)]/10 px-4 py-3 text-sm"
      >
        Install the Breeze TTS 2 model for audio.cpp before designing a voice.
        <a
          href="/providers?tab=speech&speech=local#component-audio_cpp"
          class="ml-1 font-semibold text-[var(--accent)] underline"
          >Open local model settings.</a
        >
      </div>{:else if audioCpp?.available === false}<div
        class="mt-5 rounded-xl border border-[var(--warning)]/40 bg-[var(--warning)]/10 px-4 py-3 text-sm"
      >
        {audioCpp.availability_reason || 'Start audio.cpp to design a voice.'}
        <a
          href="/providers?tab=speech&speech=local#component-audio_cpp"
          class="ml-1 font-semibold text-[var(--accent)] underline"
          >Open local model settings.</a
        >
      </div>{/if}

    <div class="mt-6 grid gap-4 sm:grid-cols-2">
      <label class="text-sm font-semibold"
        >Save to<select
          bind:value={targetVoiceId}
          onchange={chooseTarget}
          disabled={generating || saving}
          class="mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2.5 font-normal"
          ><option value="">A new library voice</option
          >{#each availableVoices as voice}<option value={voice.id}
              >Existing · {voice.name}</option
            >{/each}</select
        ></label
      >
      {#if !targetVoiceId}<label class="text-sm font-semibold"
          >Voice name<input
            bind:value={voiceName}
            maxlength="255"
            disabled={generating || saving}
            placeholder="e.g. Measured lecturer"
            class="mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2.5 font-normal"
          /></label
        >{:else}<div
          class="rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2.5 text-sm"
        >
          <span class="muted block text-xs font-semibold">Existing voice</span>
          <span class="mt-0.5 block font-semibold"
            >{availableVoices.find((voice) => voice.id === targetVoiceId)
              ?.name}</span
          >
        </div>{/if}
      <label class="text-sm font-semibold"
        >Language<select
          bind:value={language}
          oninput={invalidatePreview}
          disabled={generating || saving}
          class="mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2.5 font-normal"
          ><option value="en">English</option><option value="zh"
            >Mandarin Chinese</option
          ></select
        ></label
      >
      <label class="text-sm font-semibold"
        >Variation seed
        <div class="mt-1 flex gap-2">
          <input
            bind:value={seed}
            oninput={invalidatePreview}
            type="number"
            min="0"
            max="2147483647"
            aria-invalid={!validSeed}
            disabled={generating || saving}
            class="min-w-0 flex-1 rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2.5 font-mono font-normal"
          /><button
            type="button"
            onclick={chooseAnotherSeed}
            disabled={generating || saving}
            title="Choose another random seed"
            aria-label="Choose another random seed"
            class="rounded-xl border border-[var(--line)] px-3 disabled:opacity-40"
            ><RefreshCw size={16} /></button
          >
        </div>
        {#if !validSeed}<span
            class="mt-1 block text-xs font-normal text-red-600"
            >Use a whole number from 0 to 2,147,483,647.</span
          >{/if}</label
      >
    </div>

    <label class="mt-4 block text-sm font-semibold"
      >Voice description<textarea
        bind:value={prompt}
        oninput={invalidatePreview}
        rows="3"
        maxlength="4000"
        disabled={generating || saving}
        placeholder="Warm, thoughtful middle-aged lecturer; intimate microphone; measured pace; gentle confidence…"
        class="mt-1 w-full resize-y rounded-xl border border-[var(--line)] bg-[var(--paper)] p-3 font-normal leading-relaxed"
      ></textarea><span class="muted mt-1 block text-xs font-normal"
        >Describe identity, age, accent, texture, pace, emotion, and recording
        style. Avoid naming a real person.</span
      ></label
    >

    <label class="mt-4 block text-sm font-semibold"
      >Sample text<textarea
        bind:value={sampleText}
        oninput={invalidatePreview}
        rows="4"
        maxlength="1000"
        disabled={generating || saving}
        class="mt-1 w-full resize-y rounded-xl border border-[var(--line)] bg-[var(--paper)] p-3 font-normal leading-relaxed"
      ></textarea><span class="muted mt-1 block text-xs font-normal"
        >These exact words are sent to Breeze and saved as the sample's
        transcript. A varied, natural 10–20 second passage usually makes a
        better cloning reference.</span
      ></label
    >

    <div class="mt-5 rounded-2xl border border-[var(--line)] p-4">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 class="font-semibold">Audition</h3>
          <p class="muted mt-1 text-xs">
            {progressDetail ||
              'Generate one candidate, listen, then change the seed or description if needed.'}
          </p>
        </div>
        <button
          type="button"
          onclick={generatePreview}
          disabled={!canGenerate ||
            generating ||
            saving ||
            !validSeed ||
            !prompt.trim() ||
            !sampleText.trim()}
          class="btn btn-primary disabled:opacity-40"
          >{#if generating}<LoaderCircle
              class="animate-spin"
              size={16}
            />{:else}<WandSparkles size={16} />{/if}
          {generating
            ? 'Designing…'
            : preview
              ? 'Regenerate'
              : 'Generate preview'}</button
        >
      </div>
      {#if preview}<div class="mt-4">
          <AudioPlayer
            src={`/api/v1/artifacts/${preview.artifactId}/content`}
            label="Designed Breeze voice preview"
          />
        </div>{/if}
    </div>

    <label class="mt-5 flex items-start gap-3 rounded-xl bg-[var(--paper)] p-3"
      ><input
        type="checkbox"
        bind:checked={linkAfterSave}
        disabled={saving}
        class="mt-1 accent-[var(--accent)]"
      /><span class="text-sm"
        ><strong>Link the saved reference to audio.cpp</strong><span
          class="muted mt-0.5 block text-xs"
          >Recommended: this makes the new library voice immediately available
          for Breeze cloning.</span
        ></span
      ></label
    >

    <div
      class="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-[var(--line)] pt-5"
    >
      <p class="muted max-w-lg text-xs leading-relaxed">
        Breeze TTS 2 is licensed for research and non-commercial use. {#if breezeInfo?.license?.url}<a
            href={breezeInfo.license.url}
            target="_blank"
            rel="noreferrer"
            class="font-semibold text-[var(--accent)] underline"
            >Read the model license.</a
          >{/if}
      </p>
      <div class="flex gap-2">
        <button
          type="button"
          onclick={() => void closeDialog()}
          disabled={saving}
          class="btn btn-secondary disabled:opacity-40"
          >{generating ? 'Cancel generation' : 'Cancel'}</button
        ><button
          type="button"
          onclick={saveDesign}
          disabled={!preview ||
            saving ||
            generating ||
            (!targetVoiceId && !voiceName.trim())}
          class="btn btn-primary disabled:opacity-40"
          >{#if saving}<LoaderCircle
              class="animate-spin"
              size={16}
            />{:else}<Save size={16} />{/if}
          {saving ? 'Saving…' : 'Save as reviewed sample'}</button
        >
      </div>
    </div>
  </section>
</div>
