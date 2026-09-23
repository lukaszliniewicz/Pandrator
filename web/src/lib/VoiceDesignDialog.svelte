<script lang="ts">
  import {
    CircleAlert,
    LoaderCircle,
    RefreshCw,
    Save,
    WandSparkles,
    X
  } from '@lucide/svelte';
  import { onDestroy, onMount } from 'svelte';
  import { beforeNavigate, goto } from '$app/navigation';
  import { speechServiceApi, voiceApi } from './admin-api';
  import type { JobRecord, TtsService, VoiceRecord } from './api-models';
  import { jobApi } from './domain-api';
  import { errorMessage } from './errors';
  import { modalFocus } from './modal-focus';
  import AudioPlayer from './AudioPlayer.svelte';
  import { languageName, readable } from './audio-cpp-catalogue';
  import { VOICE_DESIGN_SAMPLES } from './voice-design-samples';

  let {
    services,
    voices,
    initialVoiceId = '',
    initialPrompt = '',
    initialAccent = '',
    initialPitch = '',
    initialCategory = '',
    onclose,
    onsaved
  }: {
    services: TtsService[];
    voices: VoiceRecord[];
    initialVoiceId?: string;
    initialPrompt?: string;
    initialAccent?: string;
    initialPitch?: string;
    initialCategory?: string;
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
    model: string;
  };

  let catalogueServices = $state<TtsService[]>([]);
  let catalogueLoading = $state(true);
  let catalogueError = $state('');
  const availableVoices = $derived(voices.filter((voice) => !voice.bundled));
  const audioCpp = $derived(
    catalogueServices.find(
      (service) => service.id === 'audio_cpp' || service.adapter === 'audio_cpp'
    )
  );
  const designModelNames: Record<string, string> = $derived({
    qwen3_tts_1_7b_voicedesign_q8_0: 'Qwen3 VoiceDesign',
    breeze_tts_2_q8_0: 'BreezeTTS 2',
    ...Object.fromEntries(
      (audioCpp?.model_catalog ?? [])
        .filter(
          (model) =>
            model.catalogue_info?.pandrator_features?.voice_design ===
            'documented'
        )
        .map((model) => [model.id, model.label ?? model.id])
    )
  });
  const designLanguageNames: Record<string, string> = {
    en: 'English',
    zh: 'Mandarin Chinese',
    ja: 'Japanese',
    ko: 'Korean',
    de: 'German',
    fr: 'French',
    ru: 'Russian',
    pt: 'Portuguese',
    es: 'Spanish',
    it: 'Italian'
  };
  const designModels = $derived(
    (audioCpp?.models ?? []).filter((model) => model in designModelNames)
  );
  let designModel = $state('');
  const designInfo = $derived(
    (audioCpp?.model_catalog ?? []).find((model) => model.id === designModel)
  );
  const designLanguages = $derived(
    designInfo?.supported_languages?.length
      ? designInfo.supported_languages.filter((code) =>
          /^[a-z]{2,3}(-[A-Za-z0-9]+)*$/.test(code)
        )
      : designModel === 'breeze_tts_2_q8_0'
        ? ['en', 'zh']
        : Object.keys(designLanguageNames)
  );
  const languageChoices = $derived({
    ...designLanguageNames,
    ...Object.fromEntries(
      designLanguages.map((code) => [
        code,
        designLanguageNames[code] ?? languageName(code)
      ])
    )
  });
  $effect(() => {
    if (!designModels.includes(designModel)) {
      designModel = designModels.includes('qwen3_tts_1_7b_voicedesign_q8_0')
        ? 'qwen3_tts_1_7b_voicedesign_q8_0'
        : designModels[0] || '';
    }
  });

  let targetVoiceId = $state('');
  let targetInitialized = false;
  let voiceName = $state('');
  let language = $state('en');
  const designLanguageProblem = $derived(
    Boolean(
      designModel &&
      !designInfo?.catalogue_info?.unlisted_languages &&
      !designLanguages.includes(language)
    )
  );
  const canGenerate = $derived(
    Boolean(
      !catalogueLoading &&
      audioCpp &&
      designModels.includes(designModel) &&
      audioCpp.available !== false &&
      !designLanguageProblem
    )
  );
  let prompt = $state('');
  let sampleText = $state(VOICE_DESIGN_SAMPLES.en);
  let seed = $state(randomSeed());
  let fixedSeed = $state(false);
  let candidateCount = $state(3);
  let previews = $state<PreviewSnapshot[]>([]);
  let selectedPreviewId = $state('');
  const preview = $derived(
    previews.find((item) => item.artifactId === selectedPreviewId) ?? null
  );
  let generating = $state(false);
  let stopRequested = $state(false);
  let saving = $state(false);
  let activeJobId = $state('');
  let linkAfterSave = $state(true);
  let error = $state('');
  let progressDetail = $state('');
  let alive = true;
  let closing = false;
  let draftTouched = $state(false);
  let pendingDiscard = $state<(() => void) | null>(null);
  let step = $state<'brief' | 'auditions'>('brief');
  const hasDraft = $derived(draftTouched || previews.length > 0 || generating);
  beforeNavigate((event) => {
    if (closing || (!hasDraft && !saving)) return;
    event.cancel();
    if (!event.willUnload && !saving && event.to?.url) {
      const destination = event.to.url.href;
      pendingDiscard = () => void finishClose(destination);
    }
  });
  const validSeed = $derived(
    Number.isInteger(seed) && seed >= 0 && seed <= 4_294_967_295
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
    return Math.floor(Math.random() * 4_294_967_295);
  }

  function invalidatePreview() {
    previews = [];
    selectedPreviewId = '';
    error = '';
    progressDetail = '';
  }

  function chooseTarget() {
    const target = availableVoices.find((voice) => voice.id === targetVoiceId);
    const targetLanguage = String(target?.language ?? '').toLowerCase();
    if (targetLanguage) {
      language = targetLanguage.replaceAll('_', '-').split('-')[0];
      sampleText = VOICE_DESIGN_SAMPLES[language] ?? '';
    }
    invalidatePreview();
  }

  function chooseLanguage() {
    sampleText = VOICE_DESIGN_SAMPLES[language] ?? '';
    invalidatePreview();
  }

  function chooseAnotherSeed() {
    draftTouched = true;
    seed = randomSeed();
    invalidatePreview();
  }

  async function refreshCatalogue() {
    catalogueLoading = true;
    catalogueError = '';
    catalogueServices = services;
    try {
      const payload = await speechServiceApi.catalogue(true);
      if (alive) catalogueServices = payload.services ?? [];
    } catch (caught) {
      if (alive) catalogueError = errorMessage(caught);
    } finally {
      if (alive) catalogueLoading = false;
    }
  }

  async function waitJob(id: string, attempts = 4000): Promise<JobRecord> {
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      if (!alive || closing)
        throw new DOMException('Dialog closed', 'AbortError');
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
    if (
      generating ||
      saving ||
      !cleanPrompt ||
      !cleanText ||
      !audioCpp ||
      !canGenerate ||
      (fixedSeed && !validSeed)
    )
      return;
    const count = Math.max(1, Math.min(4, Number(candidateCount) || 1));
    const requestedModel = designModel;
    const requestedLanguage = language;
    const baseSeed = seed;
    const usedSeeds = new Set<number>();
    generating = true;
    stopRequested = false;
    closing = false;
    error = '';
    previews = [];
    selectedPreviewId = '';
    try {
      for (let index = 0; index < count; index += 1) {
        if (!alive || closing || stopRequested) break;
        let candidateSeed = fixedSeed
          ? (baseSeed + index) % 4_294_967_296
          : randomSeed();
        while (usedSeeds.has(candidateSeed))
          candidateSeed = (candidateSeed + 1) % 4_294_967_296;
        usedSeeds.add(candidateSeed);
        if (!fixedSeed) seed = candidateSeed;
        progressDetail = `Generating candidate ${index + 1} of ${count}…`;
        const queued = await speechServiceApi.preview(audioCpp.id, {
          text: cleanText,
          model: requestedModel,
          voice: '',
          language: requestedLanguage,
          generation_prompt: cleanPrompt,
          seed: candidateSeed
        });
        activeJobId = queued.id;
        if (!alive || closing || stopRequested) {
          await jobApi.cancel(queued.id).catch((caught) => {
            if (alive && !closing)
              error = `Could not stop the audition: ${errorMessage(caught)}. Follow it in Activity & logs.`;
          });
          break;
        }
        const complete = await waitJob(queued.id);
        if (!alive || closing) break;
        const artifactId = String(complete.result_json?.artifact_id ?? '');
        if (!artifactId)
          throw new Error(
            'The model finished without returning a playable preview artifact.'
          );
        previews = [
          ...previews,
          {
            artifactId,
            prompt: cleanPrompt,
            text: cleanText,
            language: requestedLanguage,
            seed: candidateSeed,
            model: requestedModel
          }
        ];
        selectedPreviewId ||= artifactId;
        activeJobId = '';
      }
      if (alive && !closing)
        progressDetail = `${previews.length} candidate${previews.length === 1 ? '' : 's'} ready. Listen and choose one to save.`;
    } catch (caught) {
      if (
        alive &&
        !closing &&
        !stopRequested &&
        (caught as { name?: string })?.name !== 'AbortError'
      )
        error = errorMessage(caught);
    } finally {
      if (alive) {
        if (stopRequested && !closing)
          progressDetail = previews.length
            ? `Audition stopped. ${previews.length} candidate${previews.length === 1 ? '' : 's'} ready to save.`
            : 'Audition stopped. Your voice brief is preserved.';
        activeJobId = '';
        generating = false;
      }
    }
  }

  async function cancelGeneration() {
    if (!generating || stopRequested) return;
    stopRequested = true;
    if (!activeJobId) return;
    try {
      await jobApi.cancel(activeJobId);
    } catch (caught) {
      if (alive) {
        stopRequested = false;
        error = `Could not stop the audition: ${errorMessage(caught)}`;
      }
    }
  }

  function closeDialog() {
    if (saving) return;
    if (hasDraft) pendingDiscard = () => void finishClose();
    else void finishClose();
  }

  async function finishClose(destination?: string) {
    closing = true;
    if (activeJobId) {
      progressDetail = 'Canceling voice design…';
      await jobApi.cancel(activeJobId).catch(() => null);
    }
    onclose();
    if (destination) await goto(destination);
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
          description: preview.prompt,
          voice_category:
            initialCategory === 'male' ||
            initialCategory === 'female' ||
            initialCategory === 'androgynous'
              ? initialCategory
              : 'unspecified',
          profile: {
            schema_version: 1,
            pitch:
              initialPitch === 'low' ||
              initialPitch === 'mid' ||
              initialPitch === 'high'
                ? initialPitch
                : null,
            languages: [
              {
                language: preview.language,
                accent: initialAccent || null,
                evidence: { source: 'design_request', status: 'requested' }
              }
            ],
            evidence: {
              pitch: { source: 'design_request', status: 'requested' },
              voice_category: { source: 'design_request', status: 'requested' }
            }
          }
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
      closing = true;
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

  onMount(() => {
    prompt = initialPrompt;
    void refreshCatalogue();
  });

  onDestroy(() => {
    alive = false;
  });
</script>

<div
  class="fixed inset-0 z-[90] grid place-items-center bg-black/45 sm:p-4 sm:backdrop-blur-sm"
  role="presentation"
  onclick={(event) =>
    !saving && event.target === event.currentTarget && void closeDialog()}
>
  <!-- svelte-ignore a11y_no_noninteractive_element_to_interactive_role -->
  <section
    use:modalFocus={{ onclose: () => !saving && void closeDialog() }}
    class="surface flex h-[100dvh] w-full max-w-3xl flex-col overflow-hidden shadow-2xl sm:h-auto sm:max-h-[94dvh] sm:rounded-[1.8rem]"
    role="dialog"
    aria-modal="true"
    aria-labelledby="voice-design-title"
    aria-busy={catalogueLoading || generating || saving}
  >
    <div
      class="modal-scroll min-h-0 flex-1 p-4 sm:p-6"
      oninput={() => (draftTouched = true)}
      onchange={() => (draftTouched = true)}
    >
      <header class="flex items-start justify-between gap-4">
        <div>
          <h2 id="voice-design-title" class="mt-1 text-2xl font-semibold">
            Design a reusable voice
          </h2>
          <p class="muted mt-2 max-w-2xl text-sm leading-relaxed">
            Describe your speaker, compare a few readings, and save your
            favorite as a reusable voice.
          </p>
        </div>
        <button
          type="button"
          onclick={() => void closeDialog()}
          disabled={saving}
          aria-label="Close voice designer"
          class="grid min-h-11 min-w-11 shrink-0 place-items-center rounded-xl p-2 disabled:opacity-40"
          ><X size={20} /></button
        >
      </header>

      <nav aria-label="Voice design stages" class="mt-5 flex gap-2">
        <button
          class="btn flex-1"
          class:btn-primary={step === 'brief'}
          aria-current={step === 'brief' ? 'step' : undefined}
          onclick={() => (step = 'brief')}>1. Voice brief</button
        >
        <button
          class="btn flex-1"
          class:btn-primary={step === 'auditions'}
          aria-current={step === 'auditions' ? 'step' : undefined}
          onclick={() => (step = 'auditions')}>2. Audition &amp; save</button
        >
      </nav>

      {#if error}<div
          role="alert"
          class="mt-5 flex items-start gap-2 rounded-xl border border-red-400/40 bg-red-500/10 px-4 py-3 text-sm"
        >
          <CircleAlert class="mt-0.5 shrink-0" size={16} /><span>{error}</span>
        </div>{/if}

      {#if catalogueLoading}<div
          role="status"
          class="mt-5 flex items-center gap-2 rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 text-sm"
        >
          <LoaderCircle class="animate-spin text-[var(--accent)]" size={16} />
          Checking the installed audio.cpp models…
        </div>{:else if catalogueError}<div
          role="alert"
          class="mt-5 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-[var(--warning)]/40 bg-[var(--warning)]/10 px-4 py-3 text-sm"
        >
          <span
            >Could not refresh the audio.cpp model list: {catalogueError}</span
          >
          <button
            type="button"
            onclick={refreshCatalogue}
            class="btn btn-sm btn-secondary"
            ><RefreshCw size={14} /> Retry</button
          >
        </div>{:else if !designModel}<div
          class="mt-5 rounded-xl border border-[var(--warning)]/40 bg-[var(--warning)]/10 px-4 py-3 text-sm"
        >
          Install a model with voice-design support under audio.cpp in the
          Manager before designing a voice.
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

      <div hidden={step !== 'brief'}>
        <div class="mt-6 grid gap-4 sm:grid-cols-2">
          <label class="text-sm font-semibold sm:col-span-2"
            >Design model
            <select
              bind:value={designModel}
              onchange={invalidatePreview}
              disabled={generating || saving || !designModels.length}
              class="mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2.5 font-normal"
            >
              {#if !designModels.length}<option value=""
                  >Install a voice-design model</option
                >{/if}
              {#each designModels as model}<option value={model}
                  >{designModelNames[model]}</option
                >{/each}
            </select>
          </label>

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
              <span class="muted block text-xs font-semibold"
                >Existing voice</span
              >
              <span class="mt-0.5 block font-semibold"
                >{availableVoices.find((voice) => voice.id === targetVoiceId)
                  ?.name}</span
              >
            </div>{/if}
          <label class="text-sm font-semibold"
            >Language{#if designInfo?.catalogue_info?.unlisted_languages}
              <input
                aria-label="Language code"
                bind:value={language}
                onchange={chooseLanguage}
                maxlength="40"
                placeholder="e.g. en, pl, ja"
                class="mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2.5 font-normal"
              />
              <span class="muted mt-1 block text-xs font-normal"
                >Upstream reports broad language coverage without a complete
                verified list. Enter a language code and review the preview.</span
              >
            {:else}<select
                bind:value={language}
                onchange={chooseLanguage}
                disabled={generating || saving}
                class="mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2.5 font-normal"
              >
                {#if !(language in languageChoices)}<option
                    value={language}
                    disabled>{language} · unsupported language</option
                  >{/if}
                {#each Object.entries(languageChoices) as [code, name]}<option
                    value={code}
                    disabled={!designLanguages.includes(code)}
                    >{name}{!designLanguages.includes(code)
                      ? ' · unsupported by this model'
                      : ''}</option
                  >{/each}
              </select>{/if}</label
          >
          <div class="text-sm font-semibold">
            <label
              >Candidates per audition<select
                bind:value={candidateCount}
                disabled={generating || saving}
                class="mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2.5 font-normal"
              >
                {#each [1, 2, 3, 4] as count}<option value={count}
                    >{count} candidate{count === 1 ? '' : 's'}</option
                  >{/each}
              </select></label
            >
          </div>
          <details
            class="rounded-xl border border-[var(--line)] p-3 sm:col-span-2"
          >
            <summary class="cursor-pointer text-sm font-semibold"
              >Variation settings <span class="muted font-normal"
                >· {fixedSeed
                  ? 'fixed seeds'
                  : 'new seeds every audition'}</span
              ></summary
            >
            <label class="my-3 flex items-center gap-2 text-sm"
              ><input
                type="checkbox"
                bind:checked={fixedSeed}
                disabled={generating || saving}
              />Use a fixed seed to repeat an audition</label
            >
            <label class="text-sm font-semibold"
              >Variation seed
              <div class="mt-1 flex gap-2">
                <input
                  bind:value={seed}
                  oninput={invalidatePreview}
                  type="number"
                  min="0"
                  max="4294967295"
                  aria-invalid={!validSeed}
                  disabled={generating || saving || !fixedSeed}
                  class="min-w-0 flex-1 rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2.5 font-mono font-normal"
                /><button
                  type="button"
                  onclick={chooseAnotherSeed}
                  disabled={generating || saving || !fixedSeed}
                  title="Choose another random seed"
                  aria-label="Choose another random seed"
                  class="rounded-xl border border-[var(--line)] px-3 disabled:opacity-40"
                  ><RefreshCw size={16} /></button
                >
              </div>
              {#if !validSeed}<span
                  class="mt-1 block text-xs font-normal text-red-600"
                  >Use a whole number from 0 to 4,294,967,295.</span
                >{/if}</label
            >
            <p class="muted mt-2 text-xs">
              Multiple candidates use consecutive seeds in fixed mode. Each
              candidate shows its seed for later reuse.
            </p>
          </details>
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
            >Describe identity, age, accent, texture, pace, emotion, and
            recording style. Avoid naming a real person.</span
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
            >These exact words are sent to the selected model and saved as the
            sample's transcript. The suggested passage is translated for each
            supported language; edit it freely.</span
          ></label
        >

        {#if designLanguageProblem}<p
            class="mt-3 text-sm text-red-600"
            role="alert"
          >
            {designModelNames[designModel]} does not support {language}. Choose
            a supported language or another model.
          </p>{/if}
      </div>
      <div hidden={step !== 'auditions'}>
        <div class="mt-5 rounded-2xl border border-[var(--line)] p-4">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h3 class="font-semibold">Audition</h3>
              <p class="muted mt-1 text-xs">
                {progressDetail ||
                  (fixedSeed
                    ? 'Generate a few variations, listen to each, and select your favorite. Fixed mode repeats the same seeds.'
                    : 'Generate a few variations, listen to each, and select your favorite. New seeds are used automatically.')}
              </p>
            </div>
          </div>
          {#if previews.length}<div
              class="mt-4 space-y-3"
              role="radiogroup"
              aria-label="Voice candidates"
            >
              {#each previews as candidate, index (candidate.artifactId)}
                <article
                  class="rounded-xl border p-4"
                  class:border-[var(--accent)]={selectedPreviewId ===
                    candidate.artifactId}
                >
                  <label
                    class="mb-3 flex cursor-pointer items-center gap-3 text-sm font-semibold"
                  >
                    <input
                      type="radio"
                      name="voice-candidate"
                      value={candidate.artifactId}
                      bind:group={selectedPreviewId}
                      disabled={saving}
                      class="accent-[var(--accent)]"
                    />
                    Candidate {index + 1}<span
                      class="muted ml-auto text-xs font-normal"
                      >Seed {candidate.seed}</span
                    >
                  </label>
                  <AudioPlayer
                    src={`/api/v1/artifacts/${candidate.artifactId}/content`}
                    label={`Voice candidate ${index + 1}`}
                  />
                </article>
              {/each}
            </div>{/if}
        </div>

        <label
          class="mt-5 flex items-start gap-3 rounded-xl bg-[var(--paper)] p-3"
          ><input
            type="checkbox"
            bind:checked={linkAfterSave}
            disabled={saving}
            class="mt-1 accent-[var(--accent)]"
          /><span class="text-sm"
            ><strong>Link the saved reference to audio.cpp</strong><span
              class="muted mt-0.5 block text-xs"
              >Recommended: this makes the new library voice immediately
              available for compatible audio.cpp cloning models. Qwen3
              VoiceDesign itself creates voices from descriptions.</span
            ></span
          ></label
        >
      </div>
      <div
        class="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-[var(--line)] pt-5"
      >
        <p class="muted max-w-lg text-xs leading-relaxed">
          Model licence: {designInfo?.license?.name ?? 'Not verified'}.
          Commercial use: {readable(
            designInfo?.catalogue_info?.license?.commercial_use
          )}.
          {#if designInfo?.license?.url}<a
              href={designInfo.license.url}
              target="_blank"
              rel="noreferrer"
              class="font-semibold text-[var(--accent)] underline"
              >Read the model license.</a
            >{/if}
        </p>
      </div>
    </div>
    <footer
      class="shrink-0 border-t border-[var(--line)] bg-[var(--paper-strong)] p-3 sm:px-6"
    >
      {#if step === 'brief'}
        <div class="flex items-center justify-between gap-3">
          <p class="muted text-xs">Next: listen before saving.</p>
          <button class="btn btn-primary" onclick={() => (step = 'auditions')}
            >Continue to auditions</button
          >
        </div>
      {:else}
        <div class="flex flex-wrap items-center justify-end gap-2">
          <button
            type="button"
            onclick={() => {
              draftTouched = true;
              void generatePreview();
            }}
            disabled={!canGenerate ||
              generating ||
              saving ||
              (fixedSeed && !validSeed) ||
              !prompt.trim() ||
              !sampleText.trim()}
            class="btn btn-primary disabled:opacity-40"
            >{#if generating}<LoaderCircle
                class="animate-spin"
                size={16}
              />{:else}<WandSparkles size={16} />{/if}
            {generating
              ? 'Designing…'
              : `Generate ${candidateCount} candidate${candidateCount === 1 ? '' : 's'}`}</button
          >
          <div class="flex flex-wrap gap-2">
            <button
              type="button"
              onclick={() =>
                generating ? void cancelGeneration() : void closeDialog()}
              disabled={saving || (generating && stopRequested)}
              class="btn btn-secondary disabled:opacity-40"
              >{generating
                ? stopRequested
                  ? 'Stopping…'
                  : 'Cancel generation'
                : 'Cancel'}</button
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
              {saving ? 'Saving…' : 'Save selected voice'}</button
            >
          </div>
        </div>
        <p class="muted mt-2 text-xs" role="status">
          {saving || generating
            ? progressDetail
            : !prompt.trim() || !sampleText.trim()
              ? 'Complete the description and sample text in Voice brief to generate candidates.'
              : !preview
                ? 'Generate candidates, then select one to save.'
                : !targetVoiceId && !voiceName.trim()
                  ? 'Add a voice name in Voice brief before saving.'
                  : 'The selected reading will become a reference sample.'}
        </p>
      {/if}
    </footer>
  </section>
</div>

{#if pendingDiscard}
  <div class="fixed inset-0 z-[95] grid place-items-center bg-black/50 p-4">
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="design-unsaved-title"
      use:modalFocus={{ onclose: () => (pendingDiscard = null) }}
      class="compact-confirmation surface w-full max-w-md rounded-2xl p-5"
    >
      <h2 id="design-unsaved-title" class="text-lg font-semibold">
        Keep your voice design?
      </h2>
      <p class="muted mt-2 text-sm">
        Your brief and unsaved candidates will be lost if you leave.{generating
          ? ' The current audition will also be canceled.'
          : ''}
      </p>
      <div class="mt-5 grid grid-cols-2 gap-2">
        <button class="btn btn-primary" onclick={() => (pendingDiscard = null)}
          >Keep editing</button
        >
        <button
          class="btn btn-secondary"
          onclick={() => {
            const discard = pendingDiscard;
            pendingDiscard = null;
            discard?.();
          }}>Discard design</button
        >
      </div>
    </div>
  </div>
{/if}
