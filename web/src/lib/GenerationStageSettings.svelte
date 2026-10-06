<script lang="ts">
  import {
    CheckCircle2,
    CloudUpload,
    Library,
    Link2,
    LoaderCircle,
    RefreshCw
  } from '@lucide/svelte';
  import LocalModelPicker from './LocalModelPicker.svelte';
  import LanguageSelect from './LanguageSelect.svelte';
  import ParameterLabel from './ParameterLabel.svelte';
  import {
    type TtsSettingsSelection,
    type GenerationSettingsActivity
  } from './tts-settings-selection.svelte';
  import { type StageSettingsDraft } from './stage-settings-draft.svelte';
  import { type TtsCatalogueState } from './tts-catalogue-state.svelte';
  import type { VoiceRecord, XttsModel } from './api-models';
  import { LANGUAGE_OPTIONS } from './settings-fields';
  import { voiceSupportsLanguage } from './voice-catalog';

  let {
    draft,
    ttsSelection,
    activity,
    speechCatalogues,
    actions
  }: {
    draft: StageSettingsDraft;
    ttsSelection: TtsSettingsSelection;
    activity: GenerationSettingsActivity;
    speechCatalogues: TtsCatalogueState;
    actions: {
      chooseTtsService: (value: string) => Promise<void>;
      refreshSpeechServices: () => Promise<void>;
      openTtsServices: () => Promise<void>;
      chooseTtsModel: (value: string) => void;
      loadXttsModels: () => Promise<void>;
      removeXttsModel: (model: XttsModel) => Promise<void>;
      chooseXttsModelFiles: (files: FileList | null) => void;
      uploadXttsModel: () => Promise<void>;
      openVoiceLibrary: (
        view: 'references' | 'prebuilt',
        service?: string,
        initialVoice?: string
      ) => Promise<void>;
      useLibraryVoice: (voice: VoiceRecord) => Promise<void>;
    };
  } = $props();
</script>

{#if ttsSelection.selectedTtsService?.catalogue_role === 'compatibility'}
  <div class="rounded-xl border border-[var(--line)] p-4 text-sm">
    <strong>Compatibility provider</strong>
    <p class="muted mt-1">
      This session keeps its existing engine. For new generation, you can switch
      to the matching audio.cpp model and review its voice and settings.
    </p>
    <button
      type="button"
      class="btn btn-secondary mt-3"
      onclick={() =>
        actions.chooseTtsService(
          String(ttsSelection.selectedTtsService?.replacement_service_id)
        )}
      disabled={!speechCatalogues.catalogue.services.some(
        (item) =>
          item.id === ttsSelection.selectedTtsService?.replacement_service_id &&
          item.available
      )}>Switch to audio.cpp</button
    >
  </div>
{/if}
{#if ttsSelection.ttsSwitchSource}
  <div
    class="rounded-xl border border-[var(--accent)] p-4 text-sm"
    role="status"
  >
    <strong>Review switch from {ttsSelection.ttsSwitchSource.name}</strong>
    <p class="muted mt-1">
      Check the model and voice below. Only an existing target voice or ready
      reference link is reused. Old engine options and reference settings will
      be cleared when you save. Existing takes and the original engine remain
      available.
    </p>
    <p class="muted mt-2">
      Preview the selected voice in the Voice Library before starting a long
      generation.
    </p>
    <label class="mt-3 flex items-start gap-2"
      ><input
        type="checkbox"
        bind:checked={ttsSelection.ttsSwitchReviewed}
        class="mt-1"
      /> I reviewed the target model, voice, and settings reset.</label
    >
  </div>
{/if}
{#if speechCatalogues.error}<p role="alert" class="text-sm text-red-600">
    Could not refresh speech services: {speechCatalogues.error}
  </p>{/if}
<div class="grid gap-2">
  <div class="flex items-center justify-between gap-3">
    <span class="text-sm font-semibold">TTS service</span><button
      type="button"
      onclick={actions.refreshSpeechServices}
      disabled={activity.refreshingTtsServices}
      class="flex items-center gap-2 rounded-lg border border-[var(--line)] px-3 py-2 text-xs font-semibold disabled:opacity-50"
      ><RefreshCw
        size={14}
        class={activity.refreshingTtsServices ? 'animate-spin' : ''}
      /> Refresh service availability</button
    >
  </div>
  <select
    value={draft.ttsService}
    onchange={(event) => actions.chooseTtsService(event.currentTarget.value)}
    class="w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
    aria-label="TTS service"
    >{#if ttsSelection.availableTtsServices.length}<optgroup label="Available"
        >{#each ttsSelection.availableTtsServices as service}<option
            value={service.id}>{service.name} · available</option
          >{/each}</optgroup
      >{/if}{#if ttsSelection.unavailableTtsServices.length}<optgroup
        label="Unavailable"
        >{#each ttsSelection.unavailableTtsServices as service}<option
            value={service.id}
            disabled
            class="text-[var(--muted)]">{service.name} · unavailable</option
          >{/each}</optgroup
      >{/if}</select
  >{#if ttsSelection.selectedTtsService && !ttsSelection.selectedTtsServiceAvailable}<span
      class="text-xs font-semibold text-red-500"
      role="status"
      >{ttsSelection.selectedTtsService.availability_reason ||
        `${ttsSelection.selectedTtsService.name} is unavailable. Refresh availability or choose another provider.`}</span
    >{/if}<span class="muted text-xs"
    >Available means Pandrator can use the provider. Cloud providers can be
    available without a local process.</span
  >
</div>
{#if ttsSelection.selectedTtsServiceId === 'audio_cpp'}
  <p class="muted text-xs">
    audio.cpp runs several local speech models through one provider. Choose the
    model below. You can install more model packages under Providers &amp;
    services.
  </p>
{/if}
<div class="flex flex-wrap items-center justify-between gap-3">
  <p class="muted text-xs">
    The preferred available service is selected automatically. Your selected
    unavailable service remains visible until it is ready.
  </p>
  <button
    type="button"
    onclick={actions.openTtsServices}
    class="text-xs font-semibold text-[var(--accent)]">Manage services</button
  >
</div>
{#if ttsSelection.selectedTtsServiceId === 'audio_cpp' && ttsSelection.selectedTtsService?.model_catalog?.some( (item) => Boolean(item.family) )}
  <LocalModelPicker
    id="session-tts-model"
    label="TTS model"
    value={draft.ttsModel}
    catalog={ttsSelection.selectedTtsService.model_catalog}
    listedIds={ttsSelection.ttsModels}
    modelVoiceModes={ttsSelection.selectedTtsService.model_voice_modes ?? {}}
    onchange={actions.chooseTtsModel}
  />
{:else}
  <label class="text-sm font-semibold"
    >{ttsSelection.selectedTtsServiceId === 'kobold_qwen'
      ? 'Voice type'
      : 'Model'}<select
      value={draft.ttsModel}
      onchange={(event) => actions.chooseTtsModel(event.currentTarget.value)}
      class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
      aria-label="TTS model"
      >{#each ttsSelection.ttsModels as item}<option value={item}>{item}</option
        >{/each}</select
    ></label
  >
{/if}
{#if ttsSelection.ttsModelAcquisitionHint}
  <p class="muted -mt-2 text-xs leading-relaxed">
    {ttsSelection.ttsModelAcquisitionHint}
  </p>
{/if}
{#if ttsSelection.supportsXttsModelUpload}
  <section
    class="rounded-xl border border-[var(--line)] bg-[var(--accent-soft)] p-4"
    aria-labelledby="xtts-model-upload-title"
  >
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h3 id="xtts-model-upload-title" class="text-sm font-semibold">
          XTTS model management
        </h3>
        <p class="muted mt-1 max-w-xl text-xs leading-relaxed">
          Select any listed model for this generation. Local complete bundles
          can be removed here; the built-in XTTS model is protected. Add a
          fine-tuned model with the four-file bundle below—Pandrator installs it
          in stable user data.
        </p>
      </div>
      <button
        type="button"
        onclick={actions.loadXttsModels}
        disabled={activity.xttsModelsLoading}
        class="flex items-center gap-2 rounded-lg border border-[var(--line)] px-3 py-2 text-xs font-semibold disabled:opacity-50"
        ><RefreshCw
          size={14}
          class={activity.xttsModelsLoading ? 'animate-spin' : ''}
        /> Refresh models</button
      >
    </div>
    {#if activity.xttsModelsCompatibility}<p
        class="mt-3 rounded-lg border border-amber-300 bg-amber-50 p-3 text-xs text-amber-950"
        role="status"
      >
        {activity.xttsModelsCompatibility}
      </p>{/if}
    {#if activity.xttsModels.length}<div
        class="mt-3 overflow-hidden rounded-lg border border-[var(--line)] bg-[var(--paper)]"
      >
        {#each activity.xttsModels as model}<div
            class="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--line)] p-3 last:border-b-0"
          >
            <div class="min-w-0">
              <button
                type="button"
                onclick={() => actions.chooseTtsModel(model.id)}
                class="max-w-full truncate text-left text-xs font-semibold text-[var(--accent)]"
                aria-label={`Select XTTS model ${model.id}`}>{model.id}</button
              >
              <p class="muted mt-1 text-xs">
                {model.is_default
                  ? 'Built-in protected model'
                  : model.removable
                    ? 'Local model bundle'
                    : model.lifecycle_supported
                      ? 'Local model (not removable)'
                      : 'Model lifecycle requires an XTTS update'}
              </p>
              {#if model.id === draft.ttsModel}<span
                  class="mt-1 inline-block rounded bg-[var(--accent-soft)] px-2 py-0.5 text-[11px] font-semibold"
                  >Selected for this generation</span
                >{/if}
            </div>
            <div class="flex items-center gap-2">
              <button
                type="button"
                onclick={() => actions.chooseTtsModel(model.id)}
                class="rounded-lg border border-[var(--line)] px-3 py-2 text-xs font-semibold"
                >Select</button
              >{#if model.removable && activity.xttsModelsLifecycleSupported}<button
                  type="button"
                  onclick={() => actions.removeXttsModel(model)}
                  disabled={Boolean(activity.deletingXttsModelId)}
                  class="rounded-lg border border-red-300 px-3 py-2 text-xs font-semibold text-red-700 disabled:opacity-50"
                  >{activity.deletingXttsModelId === model.id
                    ? 'Removing…'
                    : 'Remove'}</button
                >{:else if !model.is_default}<span class="muted text-xs"
                  >Removal unavailable</span
                >{/if}
            </div>
          </div>{/each}
      </div>{/if}
    <div class="mt-3 grid gap-3 sm:grid-cols-2">
      <label class="text-xs font-semibold"
        >Model ID<input
          bind:value={activity.xttsModelId}
          placeholder="custom/my-narrator-v1"
          disabled={activity.uploadingXttsModel}
          class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-sm font-normal"
        /></label
      ><label class="text-xs font-semibold"
        >Bundle files<input
          type="file"
          multiple
          accept=".json,.pth"
          disabled={activity.uploadingXttsModel}
          onchange={(event) =>
            actions.chooseXttsModelFiles(event.currentTarget.files)}
          class="mt-1 block w-full text-sm font-normal"
        /></label
      >
    </div>
    <p class="muted mt-3 text-xs">
      Required: <code>config.json</code>, <code>model.pth</code>,
      <code>speakers_xtts.pth</code>, and <code>vocab.json</code>.
      {#if activity.xttsModelFiles.length}
        Selected: {activity.xttsModelFiles.map((file) => file.name).join(', ')}.
      {/if}
    </p>
    <div class="mt-3 flex flex-wrap items-center gap-3">
      <button
        type="button"
        onclick={actions.uploadXttsModel}
        disabled={activity.uploadingXttsModel ||
          !ttsSelection.selectedTtsServiceAvailable}
        class="flex items-center gap-2 rounded-lg bg-[var(--accent)] px-3 py-2 text-xs font-semibold text-white disabled:opacity-50"
        >{#if activity.uploadingXttsModel}<LoaderCircle
            class="animate-spin"
            size={14}
          /> Uploading model…{:else}<CloudUpload size={14} /> Upload and select{/if}</button
      >
      {#if activity.xttsModelUploadError}<p
          class="basis-full rounded-lg border border-red-300 bg-red-50 p-3 text-xs text-red-800"
          role="alert"
        >
          {activity.xttsModelUploadError}
        </p>{/if}
      {#if activity.xttsModelUploadMessage && !activity.uploadingXttsModel}<span
          class="text-xs"
          role="status"
          aria-live="polite">{activity.xttsModelUploadMessage}</span
        >{/if}
    </div>
    {#if activity.uploadingXttsModel}<div class="mt-3">
        <div class="mb-1.5 flex items-center justify-between gap-3 text-xs">
          <span class="muted"
            >{activity.xttsModelUploadPhase === 'installing'
              ? 'Upload transferred; Pandrator is installing the model…'
              : 'Uploading the XTTS model…'}</span
          ><span class="muted tabular-nums"
            >{Math.round(activity.xttsModelUploadProgress * 100)}%</span
          >
        </div>
        <progress
          class="h-2 w-full overflow-hidden rounded-full accent-[var(--accent)]"
          max="1"
          value={activity.xttsModelUploadProgress}
          aria-label="XTTS model upload progress"
          >{Math.round(activity.xttsModelUploadProgress * 100)}%</progress
        >
      </div>{/if}
  </section>
{/if}
<LanguageSelect
  label="Speech language"
  bind:value={draft.targetLanguage}
  options={ttsSelection.ttsLanguages.length
    ? ttsSelection.ttsLanguages
    : LANGUAGE_OPTIONS.filter((item) => item.value !== 'auto')}
  allowCustom={!ttsSelection.selectedTtsService?.model_catalog?.find(
    (item) => item.id === draft.ttsModel
  )?.language_support ||
    ttsSelection.selectedTtsService?.model_catalog?.find(
      (item) => item.id === draft.ttsModel
    )?.language_support?.coverage !== 'exact'}
  onchange={(language) => {
    const selected = ttsSelection.ttsVoiceDescriptors.find(
      (voice) => voice.id === draft.voiceName
    );
    if (selected && !voiceSupportsLanguage(selected, language))
      draft.voiceName =
        ttsSelection.ttsVoiceDescriptors.find((voice) =>
          voiceSupportsLanguage(voice, language)
        )?.id ?? '';
  }}
/>
{#if ttsSelection.ttsLanguageIssue}<p class="text-sm text-red-600" role="alert">
    {ttsSelection.ttsLanguageIssue}
  </p>{:else if !ttsSelection.selectedTtsLanguageSupport || ttsSelection.selectedTtsLanguageSupport.coverage !== 'exact'}
  <p class="muted text-xs" role="note">
    {ttsSelection.selectedTtsLanguageSupport?.coverage === 'subset'
      ? 'The listed languages are a verified subset.'
      : ttsSelection.selectedTtsLanguageSupport?.coverage === 'claim'
        ? 'These languages are documented claims.'
        : 'Language coverage for this model is unverified.'} You can retain or enter
    another code; test a short sample before full generation.
  </p>
{/if}
{#if ttsSelection.supportsPrebuiltVoices || ttsSelection.showClonedVoices}
  {#if !ttsSelection.audioCppLinkedReferences || !ttsSelection.showClonedVoices}
    <label class="text-sm font-semibold"
      >Voice<select
        bind:value={draft.voiceName}
        class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
        >{#if !ttsSelection.showClonedVoices || ttsSelection.selectedModelAllowsReferenceFree}<option
            value=""
            disabled={!ttsSelection.selectedModelAllowsReferenceFree &&
              ttsSelection.defaultVoiceLanguageMismatch}
            >{ttsSelection.selectedModelAllowsReferenceFree
              ? 'Design from instructions · no reference'
              : 'Service default'}</option
          >{/if}{#if ttsSelection.supportsPrebuiltVoices}<optgroup
            label={`${LANGUAGE_OPTIONS.find((item) => item.value === draft.targetLanguage)?.label ?? draft.targetLanguage} · pre-built voices`}
            >{#each ttsSelection.filteredPrebuiltVoices as voice}<option
                value={voice.id}
                >{voice.name}{voice.gender ? ` · ${voice.gender}` : ''} ·
                {voice.language}</option
              >{/each}</optgroup
          >{/if}{#if ttsSelection.showClonedVoices}{#each ttsSelection.clonedVoiceGroups as group}
            <optgroup label={group.label}
              >{#each group.voices as voice}<option value={voice.id}
                  >{voice.name}</option
                >{/each}</optgroup
            >{/each}{/if}</select
      ></label
    >
    {#if ttsSelection.supportsPrebuiltVoices}
      <p class="muted text-xs" role="status">
        {ttsSelection.selectedVoiceLanguageMismatch
          ? 'Choose a voice that supports the selected language before saving.'
          : ttsSelection.filteredPrebuiltVoices.length
            ? `${ttsSelection.filteredPrebuiltVoices.length} pre-built ${ttsSelection.filteredPrebuiltVoices.length === 1 ? 'voice supports' : 'voices support'} the selected language.`
            : 'No pre-built voices are listed for this language. Choose another language or a provider-ready voice.'}
      </p>
    {/if}
    <div class="flex flex-wrap items-center justify-between gap-3">
      <p class="muted text-xs">
        {ttsSelection.showClonedVoices
          ? ttsSelection.selectedModelAllowsReferenceFree
            ? 'Leave “Design from instructions” selected to follow the speech direction, or choose a linked local voice to clone it.'
            : ttsSelection.audioCppLinkedReferences
              ? 'Linked local voices can be selected above. Qwen benefits from a reviewed transcript; OmniVoice requires one.'
              : 'Provider-ready voices can be selected above. Local voices can be prepared in one click below.'
          : 'Only voices supported by the selected model are shown.'}
      </p>
      {#if ttsSelection.showClonedVoices}<button
          type="button"
          onclick={() =>
            actions.openVoiceLibrary(
              'references',
              ttsSelection.selectedTtsServiceId
            )}
          class="flex items-center gap-1.5 text-xs font-semibold text-[var(--accent)]"
          ><Library size={14} /> Manage Voice Library</button
        >{:else}<button
          type="button"
          onclick={() => actions.openVoiceLibrary('prebuilt')}
          class="flex items-center gap-1.5 text-xs font-semibold text-[var(--accent)]"
          ><Library size={14} /> Browse pre-built voices</button
        >{/if}
    </div>
  {/if}
  {#if ttsSelection.showClonedVoices}
    <details
      class="rounded-2xl border border-[var(--line)] bg-[var(--paper)] p-4"
      open={ttsSelection.audioCppLinkedReferences &&
        !draft.voiceName &&
        !ttsSelection.selectedModelAllowsReferenceFree}
    >
      <summary class="cursor-pointer text-sm font-semibold"
        >{ttsSelection.audioCppLinkedReferences
          ? 'Voice'
          : 'Available from your Voice Library'}
        <span class="muted font-normal"
          >· {#if ttsSelection.audioCppLinkedReferences}{ttsSelection.localVoiceChoices.find(
              (choice) => choice.registration?.voice_id === draft.voiceName
            )?.voice.name ||
              draft.voiceName ||
              (ttsSelection.selectedModelAllowsReferenceFree
                ? 'Speech direction · no reference'
                : 'Choose a voice')}{:else}{ttsSelection.localVoiceChoices
              .length}
            {ttsSelection.localVoiceChoices.length === 1
              ? 'voice'
              : 'voices'}{/if}</span
        ></summary
      >
      <div class="mt-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <p class="muted mt-1 text-xs">
            {ttsSelection.audioCppLinkedReferences
              ? 'Choose a local voice; Pandrator links its newest sample without making a provider-side copy, then selects it automatically.'
              : 'Choose a local voice; Pandrator uploads or refreshes only that voice, then selects it automatically.'}
          </p>
        </div>
        <button
          type="button"
          onclick={() =>
            actions.openVoiceLibrary(
              'references',
              ttsSelection.selectedTtsServiceId
            )}
          class="text-xs font-semibold text-[var(--accent)]"
          >Manage Voice Library</button
        >
      </div>
      {#if ttsSelection.audioCppLinkedReferences && ttsSelection.selectedModelAllowsReferenceFree}
        <button
          type="button"
          class="btn btn-secondary mt-3 w-full justify-between"
          onclick={() => (draft.voiceName = '')}
        >
          Use speech direction without a reference
          {#if !draft.voiceName}<CheckCircle2 size={16} />{/if}
        </button>
      {/if}
      {#if ttsSelection.localVoiceChoices.length}
        <div class="mt-3 space-y-2">
          {#each ttsSelection.localVoiceChoices as choice}
            {@const ready =
              choice.registration?.status === 'ready' &&
              Boolean(choice.registration.voice_id)}
            {@const preparing =
              activity.publishingLibraryVoiceId === choice.voice.id}
            <article
              class="flex min-w-0 items-center gap-3 rounded-xl border border-[var(--line)] px-3 py-3"
            >
              <div class="min-w-0 flex-1">
                <div class="break-words text-sm font-semibold">
                  {choice.voice.name}
                </div>
                <div class="muted mt-0.5 text-xs">
                  {choice.voice.language || 'Language not set'} · {ready
                    ? ttsSelection.audioCppLinkedReferences
                      ? 'linked to newest sample'
                      : 'ready in provider'
                    : choice.registration?.status === 'stale'
                      ? ttsSelection.audioCppLinkedReferences
                        ? 'link needs refresh'
                        : 'provider copy needs update'
                      : !choice.hasSample
                        ? 'sample needed'
                        : choice.needsTranscript
                          ? 'reviewed transcript needed'
                          : ttsSelection.audioCppLinkedReferences
                            ? 'ready to link'
                            : 'ready to upload'}
                </div>
              </div>
              <button
                type="button"
                onclick={() => actions.useLibraryVoice(choice.voice)}
                disabled={preparing ||
                  (Boolean(activity.publishingLibraryVoiceId) && !preparing) ||
                  (ttsSelection.selectedTtsService?.available === false &&
                    !ttsSelection.audioCppLinkedReferences &&
                    !ready &&
                    choice.hasSample &&
                    !choice.needsTranscript)}
                class:btn-primary={!ready}
                class="btn shrink-0 disabled:opacity-40"
              >
                {#if preparing}<LoaderCircle
                    size={15}
                    class="animate-spin"
                  />{:else if ready}<CheckCircle2
                    size={15}
                  />{:else if ttsSelection.audioCppLinkedReferences}<Link2
                    size={15}
                  />{:else}<CloudUpload size={15} />{/if}
                {preparing
                  ? ttsSelection.audioCppLinkedReferences
                    ? 'Linking…'
                    : 'Uploading…'
                  : ready
                    ? draft.voiceName === choice.registration?.voice_id
                      ? 'Selected'
                      : 'Use'
                    : !choice.hasSample
                      ? 'Add sample'
                      : choice.needsTranscript
                        ? 'Review text'
                        : choice.registration?.status === 'stale'
                          ? ttsSelection.audioCppLinkedReferences
                            ? 'Refresh & use'
                            : 'Update & use'
                          : ttsSelection.audioCppLinkedReferences
                            ? 'Link & use'
                            : 'Upload & use'}
              </button>
            </article>
          {/each}
        </div>
      {:else}
        <p
          class="muted mt-3 rounded-xl border border-dashed border-[var(--line)] p-4 text-sm"
        >
          No local voices yet. Add one once, then reuse it across compatible
          speech services.
        </p>
      {/if}
      {#if activity.voicePublishStatus}<p
          class="mt-3 text-xs font-semibold text-[var(--accent)]"
          role="status"
        >
          {activity.voicePublishStatus}
        </p>{/if}
    </details>
  {/if}
{/if}
{#if ttsSelection.supportsGenerationPrompt}
  <label class="text-sm font-semibold"
    ><ParameterLabel
      section="tts"
      name="generation_prompt"
      label="Speech direction"
    /><textarea
      bind:value={draft.generationPrompt}
      rows="4"
      placeholder="For example: Warm, intimate narration with measured pacing and subtle excitement."
      class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
    ></textarea><span class="muted mt-2 block text-xs"
      >Sent with every segment as performance guidance. It does not rewrite the
      transcript and should not be spoken aloud.</span
    ></label
  >
{:else if ttsSelection.generationPromptModels.length}
  <p class="muted rounded-xl bg-[var(--accent-soft)] p-3 text-xs">
    {draft.ttsModel || 'This model'} does not accept speech-direction prompts. Choose
    an instruction-capable model to add one.
  </p>
{/if}
{#if ttsSelection.selectedTtsService?.supports_parallel_synthesis}
  <label class="text-sm font-semibold">
    Concurrent TTS requests
    <input
      type="number"
      min="1"
      max="8"
      step="1"
      bind:value={draft.ttsConcurrentRequests}
      class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
    />
    <span class="muted mt-2 block text-xs font-normal">
      Send 1–8 segment requests at once. Start with 2 if your provider quota
      allows it. Completed groups become playable in segment order. The current
      group may finish before a pause takes effect; higher values can increase
      rate-limit retries.
    </span>
  </label>
{/if}
{#if ttsSelection.supportsBatchSynthesis}
  <div class="rounded-xl border border-[var(--line)] p-4">
    <div class="text-sm font-semibold">Streaming generation batches</div>
    <p class="muted mt-1 text-xs leading-relaxed">
      Keep the speech engine continuously occupied while completed segments
      become playable one by one. Use 1 to disable batching.
    </p>
    <label class="mt-3 block text-xs font-semibold"
      ><ParameterLabel
        section="tts"
        name="tts_batch_size"
        label="Segments per batch"
        compact
      /><input
        type="number"
        min="1"
        max={ttsSelection.maximumTtsBatchSize}
        bind:value={draft.ttsBatchSize}
        class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
      /></label
    >
  </div>
{/if}
