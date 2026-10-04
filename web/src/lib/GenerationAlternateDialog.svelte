<script module lang="ts">
  export type GenerationAlternateDraft = {
    segmentIds: string[];
    sourceTts: Record<string, unknown>;
    sourceRvc: Record<string, unknown>;
    fallbackModel: string;
    fallbackVoice: string;
    fallbackLanguage: string;
    sourceLabel: string;
  };
</script>

<script lang="ts">
  import { untrack } from 'svelte';
  import LanguageSelect from './LanguageSelect.svelte';
  import { languageSupportProblem } from './language-registry';
  import { selectableTtsServices } from './tts-provider-policy';
  import { modalFocus } from './modal-focus';
  import { errorMessage } from './errors';
  import type { TtsCatalogue } from './api-models';
  import { LANGUAGE_OPTIONS } from './settings-fields';
  import {
    describeVoice,
    languagesForService,
    type VoiceDescriptor
  } from './voice-catalog';
  import type { GenerationSpeechOptionsState } from './generation-speech-options.svelte';
  import { generationApi } from './domain-api';

  let {
    draft,
    speechOptions,
    rvcModels,
    onvoicelabel,
    onstart,
    onclose
  }: {
    draft: GenerationAlternateDraft;
    speechOptions: GenerationSpeechOptionsState;
    rvcModels: string[];
    onvoicelabel: (voice: VoiceDescriptor) => string;
    onstart: (
      ids: string[],
      override: Record<string, unknown>,
      attempt: ReturnType<typeof generationApi.createStartAttempt>
    ) => Promise<{ accepted: boolean; error: string }>;
    onclose: () => void;
  } = $props();

  // A fresh mount is a fresh draft; catalogue refreshes must not overwrite edits.
  const alternateSegmentIds = untrack(() => draft.segmentIds);
  const normalizeId = (value: unknown) =>
    String(value ?? '')
      .trim()
      .toLowerCase()
      .replaceAll('-', '_');
  const initial = untrack(initialSettings);
  let alternateTts = $state<Record<string, unknown>>(initial.tts);
  let alternateRvc = $state<Record<string, unknown>>(initial.rvc);
  let alternateError = $state('');
  let submitting = $state(false);
  const alternateAttempt = generationApi.createStartAttempt();

  const alternateService = $derived.by(() => {
    const configured = String(
      alternateTts.service ?? alternateTts.tts_service ?? ''
    );
    return (
      speechOptions.catalogue.services.find((service) =>
        [service.id, service.name].some(
          (value) => normalizeId(value) === normalizeId(configured)
        )
      ) ?? null
    );
  });
  const alternateModels = $derived.by(() => {
    const service = alternateService;
    if (!service) return [] as string[];
    return Array.from(
      new Set([
        ...(service.models ?? []),
        ...(service.model_catalog ?? []).map((model) => String(model.id ?? '')),
        String(service.default_model ?? '')
      ])
    ).filter(Boolean);
  });
  function alternateUsesManagedReferences(
    service: TtsCatalogue['services'][number],
    model: string
  ) {
    const mode = service.model_voice_modes?.[model];
    if (mode) {
      return ['cloning', 'hybrid', 'optional_cloning'].includes(mode);
    }
    return Boolean(
      service.supports_voice_cloning && !service.supports_prebuilt_voices
    );
  }
  function alternateVoiceIds(
    service: TtsCatalogue['services'][number],
    model: string,
    language: string
  ) {
    const catalogue = Array.from(service.voice_catalogues?.[model] ?? []).map(
      String
    );
    const configured = catalogue.length
      ? catalogue
      : Array.from(service.voices ?? []).map(String);
    const managed = alternateUsesManagedReferences(service, model)
      ? speechOptions.voices.flatMap((voice) => {
          const registration =
            voice.metadata_json?.providers?.[normalizeId(service.id)];
          return registration?.status === 'ready' && registration?.voice_id
            ? [String(registration.voice_id)]
            : [];
        })
      : [];
    return Array.from(
      new Set([
        ...configured,
        ...(service.live_voices ?? []).map(String),
        ...managed,
        String(service.default_voices_by_language?.[model]?.[language] ?? ''),
        String(service.default_voices?.[model] ?? service.default_voice ?? '')
      ])
    ).filter(Boolean);
  }
  function preferredAlternateVoice(
    service: TtsCatalogue['services'][number],
    model: string,
    language: string
  ) {
    const preferred = String(
      service.default_voices_by_language?.[model]?.[language] ??
        service.default_voices?.[model] ??
        service.default_voice ??
        ''
    );
    return (
      alternateVoiceIds(service, model, language).find(
        (voice) => normalizeId(voice) === normalizeId(preferred)
      ) ?? ''
    );
  }
  function compatibleAlternateVoice(
    service: TtsCatalogue['services'][number],
    model: string,
    language: string,
    voice: string
  ) {
    const candidate = alternateVoiceIds(service, model, language).find(
      (item) => normalizeId(item) === normalizeId(voice)
    );
    if (!candidate) return false;
    const descriptor = describeVoice(
      service.id,
      candidate,
      service.voice_metadata?.[`${model}:${candidate}`]
    );
    return (
      !descriptor.languageCode ||
      normalizeId(descriptor.languageCode) === normalizeId(language)
    );
  }
  function setAlternateVoiceFor(
    service: TtsCatalogue['services'][number],
    model: string,
    language: string,
    current: string
  ) {
    return compatibleAlternateVoice(service, model, language, current)
      ? current
      : preferredAlternateVoice(service, model, language);
  }
  const alternateVoices = $derived.by(() => {
    const service = alternateService;
    if (!service) return [] as VoiceDescriptor[];
    const serviceId = normalizeId(service.id);
    const model = String(alternateTts.model ?? alternateTts.xtts_model ?? '');
    const language = String(
      alternateTts.language ?? alternateTts.target_language ?? ''
    );
    const managed = alternateUsesManagedReferences(service, model)
      ? speechOptions.voices.flatMap((voice) => {
          const registration = voice.metadata_json?.providers?.[serviceId];
          return registration?.status === 'ready' && registration?.voice_id
            ? [[String(registration.voice_id), voice.name] as const]
            : [];
        })
      : [];
    const names = new Map(
      managed.map(([id, name]) => [id.toLowerCase(), name])
    );
    return Array.from(new Set(alternateVoiceIds(service, model, language)))
      .filter(Boolean)
      .map((voice) => {
        const descriptor = describeVoice(
          service.id,
          voice,
          service.voice_metadata?.[`${model}:${voice}`]
        );
        return {
          ...descriptor,
          name: names.get(voice.toLowerCase()) ?? descriptor.name
        };
      });
  });
  const alternateLanguages = $derived.by(() => {
    const model = String(alternateTts.model ?? alternateTts.xtts_model ?? '');
    const discovered = languagesForService(
      String(alternateService?.id ?? ''),
      alternateVoices,
      {
        modelId: model,
        modelCatalog: alternateService?.model_catalog
      }
    );
    return discovered.length
      ? discovered
      : LANGUAGE_OPTIONS.filter((item) => item.value !== 'auto');
  });
  const alternateLanguageSupport = $derived(
    alternateService?.model_catalog?.find(
      (item) =>
        item.id === String(alternateTts.model ?? alternateTts.xtts_model ?? '')
    )?.language_support
  );
  const alternateLanguageIssue = $derived(
    languageSupportProblem(
      alternateLanguageSupport,
      String(alternateTts.language ?? '')
    )
  );
  const alternateIsChatterbox = $derived(
    normalizeId(alternateService?.id) === 'chatterbox'
  );
  const alternateCanStart = $derived(
    alternateSegmentIds.length > 0 &&
      Boolean(alternateService) &&
      !alternateLanguageIssue &&
      alternateService?.online !== false &&
      (alternateModels.length === 0 ||
        Boolean(String(alternateTts.model ?? ''))) &&
      (!alternateRvc.enabled || Boolean(String(alternateRvc.model ?? '')))
  );

  function initialSettings() {
    const { sourceTts, sourceRvc } = draft;
    const service = String(
      sourceTts.service ??
        sourceTts.tts_service ??
        speechOptions.settings.service ??
        speechOptions.catalogue.default_service ??
        ''
    );
    const model = String(
      sourceTts.model ?? sourceTts.xtts_model ?? draft.fallbackModel ?? ''
    );
    const voice = String(
      sourceTts.voice ?? sourceTts.speaker ?? draft.fallbackVoice ?? ''
    );
    const language = String(
      sourceTts.language ??
        sourceTts.target_language ??
        draft.fallbackLanguage ??
        'en'
    );
    const matchingService = speechOptions.catalogue.services.find((item) =>
      [item.id, item.name].some(
        (value) => normalizeId(value) === normalizeId(service)
      )
    );
    const compatibleVoice = matchingService
      ? setAlternateVoiceFor(matchingService, model, language, voice)
      : voice;
    const tts = {
      service,
      tts_service: service,
      model,
      voice: compatibleVoice,
      speaker: compatibleVoice,
      language,
      target_language: language,
      generation_prompt: String(sourceTts.generation_prompt ?? ''),
      chatterbox_exaggeration: Number(sourceTts.chatterbox_exaggeration ?? 0.5),
      chatterbox_cfg_weight: Number(sourceTts.chatterbox_cfg_weight ?? 0.5)
    };
    const rvc = {
      enabled: Boolean(sourceRvc.enabled),
      model: String(sourceRvc.model ?? sourceRvc.rvc_model ?? ''),
      rvc_model: String(sourceRvc.model ?? sourceRvc.rvc_model ?? ''),
      pitch: Number(sourceRvc.pitch ?? 0),
      f0_method: String(sourceRvc.f0_method ?? 'rmvpe'),
      index_rate: Number(sourceRvc.index_rate ?? 0.3)
    };
    return { tts, rvc };
  }

  async function submitAlternateRegeneration() {
    if (!alternateCanStart || submitting) return;
    submitting = true;
    alternateError = '';
    const voice = String(alternateTts.voice ?? '').trim();
    const language = String(alternateTts.language ?? '').trim();
    const tts = {
      ...alternateTts,
      voice: voice || null,
      speaker: voice || null,
      language: language || null,
      target_language: language || null
    };
    const rvc = {
      ...alternateRvc,
      enabled: Boolean(alternateRvc.enabled),
      model: String(alternateRvc.model ?? '').trim(),
      rvc_model: String(alternateRvc.model ?? '').trim()
    };
    try {
      const result = await onstart(
        alternateSegmentIds,
        { tts, rvc },
        alternateAttempt
      );
      if (result.accepted) onclose();
      else alternateError = result.error;
    } catch (caught) {
      alternateError = errorMessage(caught);
    } finally {
      submitting = false;
    }
  }

  function close() {
    if (!submitting) onclose();
  }
</script>

<div
  class="fixed inset-0 z-[80] grid place-items-center bg-black/40 p-5"
  role="presentation"
  onclick={(event) => {
    if (!submitting && event.currentTarget === event.target) close();
  }}
>
  <div
    class="surface max-h-[92vh] w-full max-w-3xl overflow-y-auto rounded-3xl p-6"
    role="dialog"
    aria-modal="true"
    aria-labelledby="alternate-regeneration-title"
    use:modalFocus={{
      onclose: () => close(),
      closeOnEscape: !submitting
    }}
  >
    <header class="flex items-start justify-between gap-4">
      <div>
        <h2
          id="alternate-regeneration-title"
          class="mt-1 text-xl font-semibold"
        >
          Regenerate {alternateSegmentIds.length} selected segment{alternateSegmentIds.length ===
          1
            ? ''
            : 's'} with…
        </h2>
        <p class="muted mt-1 text-xs">
          Uses {draft.sourceLabel} as the source, then saves these choices with the
          replacement takes in the same output run. Previous takes remain available.
        </p>
      </div>
      <button
        class="action"
        aria-label="Close alternate regeneration"
        disabled={submitting}
        onclick={() => close()}>Close</button
      >
    </header>

    <fieldset disabled={submitting}>
      <div class="mt-5 grid gap-4 sm:grid-cols-2">
        <label class="text-sm font-semibold"
          >Speech service
          <select
            class="field mt-1 w-full"
            value={String(alternateTts.service ?? '')}
            onchange={(event) => {
              const service = event.currentTarget.value;
              const next = speechOptions.catalogue.services.find(
                (item) => item.id === service
              );
              const language = String(
                alternateTts.language ?? alternateTts.target_language ?? 'en'
              );
              const model = String(
                next?.default_model ??
                  next?.models?.[0] ??
                  next?.model_catalog?.[0]?.id ??
                  ''
              );
              const voice = next
                ? preferredAlternateVoice(next, model, language)
                : '';
              alternateTts = {
                ...alternateTts,
                service,
                tts_service: service,
                model,
                voice,
                speaker: voice
              };
            }}
          >
            <option value="">Choose a service</option>
            {#each selectableTtsServices(speechOptions.catalogue.services, alternateTts.service) as service}
              <option value={service.id} disabled={service.online === false}
                >{service.name ?? service.id}{service.online === false
                  ? ' · unavailable'
                  : ''}</option
              >
            {/each}
          </select>
        </label>
        <label class="text-sm font-semibold"
          >Model
          <select
            class="field mt-1 w-full"
            value={String(alternateTts.model ?? '')}
            disabled={!alternateService ||
              alternateService.online === false ||
              !alternateModels.length}
            onchange={(event) => {
              const model = event.currentTarget.value;
              const language = String(
                alternateTts.language ?? alternateTts.target_language ?? 'en'
              );
              const voice = alternateService
                ? setAlternateVoiceFor(
                    alternateService,
                    model,
                    language,
                    String(alternateTts.voice ?? '')
                  )
                : '';
              alternateTts = {
                ...alternateTts,
                model,
                voice,
                speaker: voice
              };
            }}
          >
            {#if !alternateModels.length}<option value=""
                >Service default</option
              >{/if}
            {#each alternateModels as model}<option value={model}
                >{model}</option
              >{/each}
          </select>
        </label>
        <label class="text-sm font-semibold"
          >Voice / managed reference
          <select
            class="field mt-1 w-full"
            value={String(alternateTts.voice ?? '')}
            disabled={!alternateService || alternateService.online === false}
            onchange={(event) => {
              const voice = event.currentTarget.value;
              alternateTts = { ...alternateTts, voice, speaker: voice };
            }}
          >
            <option value="">Service default</option>
            {#each alternateVoices as voice}<option value={voice.id}
                >{onvoicelabel(voice)}</option
              >{/each}
          </select>
          <span class="muted mt-1 block text-[.67rem]"
            >Published voice references appear here when ready for this
            provider.</span
          >
        </label>
        <div>
          <LanguageSelect
            label="Speech language"
            value={String(alternateTts.language ?? '')}
            options={alternateLanguages}
            allowCustom={alternateLanguageSupport?.coverage !== 'exact'}
            disabled={!alternateService || alternateService.online === false}
            onchange={(language) => {
              const voice = alternateService
                ? setAlternateVoiceFor(
                    alternateService,
                    String(alternateTts.model ?? ''),
                    language,
                    String(alternateTts.voice ?? '')
                  )
                : '';
              alternateTts = {
                ...alternateTts,
                language,
                target_language: language,
                voice,
                speaker: voice
              };
            }}
          />
          {#if alternateLanguageIssue}<p
              class="mt-2 text-sm text-red-600"
              role="alert"
            >
              {alternateLanguageIssue}
            </p>{:else if alternateLanguageSupport?.coverage !== 'exact'}<p
              class="muted mt-2 text-xs"
            >
              Language coverage is {alternateLanguageSupport?.coverage ===
              'subset'
                ? 'a documented subset'
                : alternateLanguageSupport?.coverage === 'claim'
                  ? 'a documented claim'
                  : 'unverified'} for this model. Try a short sample before generating
              all selected passages.
            </p>{/if}
        </div>
      </div>

      <label class="mt-4 block text-sm font-semibold"
        >Generation prompt / instructions
        <textarea
          class="field mt-1 min-h-20 w-full"
          value={String(alternateTts.generation_prompt ?? '')}
          oninput={(event) => {
            alternateTts = {
              ...alternateTts,
              generation_prompt: event.currentTarget.value
            };
          }}
          placeholder="Provider-supported style or voice instructions"
        ></textarea>
      </label>

      {#if alternateIsChatterbox}
        <div
          class="mt-4 grid gap-4 rounded-xl bg-[var(--accent-soft)] p-4 sm:grid-cols-2"
        >
          <label class="text-sm font-semibold"
            >Chatterbox exaggeration
            <input
              class="field mt-1 w-full"
              type="number"
              min="0"
              max="2"
              step="0.05"
              value={Number(alternateTts.chatterbox_exaggeration ?? 0.5)}
              onchange={(event) =>
                (alternateTts = {
                  ...alternateTts,
                  chatterbox_exaggeration: Number(event.currentTarget.value)
                })}
            />
          </label>
          <label class="text-sm font-semibold"
            >Chatterbox CFG weight
            <input
              class="field mt-1 w-full"
              type="number"
              min="0"
              max="2"
              step="0.05"
              value={Number(alternateTts.chatterbox_cfg_weight ?? 0.5)}
              onchange={(event) =>
                (alternateTts = {
                  ...alternateTts,
                  chatterbox_cfg_weight: Number(event.currentTarget.value)
                })}
            />
          </label>
        </div>
      {/if}

      <div class="mt-4 rounded-xl border border-[var(--line)] p-4">
        <label class="flex items-center gap-2 text-sm font-semibold">
          <input
            type="checkbox"
            checked={Boolean(alternateRvc.enabled)}
            onchange={(event) =>
              (alternateRvc = {
                ...alternateRvc,
                enabled: event.currentTarget.checked
              })}
          />
          Convert the new take with RVC
        </label>
        {#if alternateRvc.enabled}
          <div class="mt-3 grid gap-3 sm:grid-cols-3">
            <label class="text-sm font-semibold"
              >RVC model
              <select
                class="field mt-1 w-full"
                value={String(alternateRvc.model ?? '')}
                onchange={(event) =>
                  (alternateRvc = {
                    ...alternateRvc,
                    model: event.currentTarget.value,
                    rvc_model: event.currentTarget.value
                  })}
              >
                <option value="">Choose a model</option>
                {#each rvcModels as model}<option value={model}>{model}</option
                  >{/each}
              </select>
            </label>
            <label class="text-sm font-semibold"
              >Pitch
              <input
                class="field mt-1 w-full"
                type="number"
                min="-24"
                max="24"
                value={Number(alternateRvc.pitch ?? 0)}
                onchange={(event) =>
                  (alternateRvc = {
                    ...alternateRvc,
                    pitch: Number(event.currentTarget.value)
                  })}
              />
            </label>
            <label class="text-sm font-semibold"
              >Index rate
              <input
                class="field mt-1 w-full"
                type="number"
                min="0"
                max="1"
                step="0.05"
                value={Number(alternateRvc.index_rate ?? 0.3)}
                onchange={(event) =>
                  (alternateRvc = {
                    ...alternateRvc,
                    index_rate: Number(event.currentTarget.value)
                  })}
              />
            </label>
          </div>
        {/if}
      </div>
      {#if alternateService?.online === false}<p
          class="mt-3 text-sm text-red-500"
        >
          This speech service is currently unavailable.
        </p>{/if}
      {#if alternateRvc.enabled && !rvcModels.length}<p
          class="mt-3 text-sm text-red-500"
        >
          No RVC models are available. Add one in RVC management first.
        </p>{/if}
    </fieldset>
    {#if alternateError}
      <p class="mt-3 text-sm text-red-600" role="alert">{alternateError}</p>
    {/if}
    <footer class="mt-6 flex justify-end gap-3">
      <button class="action" disabled={submitting} onclick={() => close()}
        >Cancel</button
      >
      <button
        class="action primary"
        disabled={!alternateCanStart || submitting}
        onclick={submitAlternateRegeneration}
        >Create alternate take{alternateSegmentIds.length === 1
          ? ''
          : 's'}</button
      >
    </footer>
  </div>
</div>

<style>
  .action {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    border: 1px solid var(--line);
    border-radius: 0.55rem;
    padding: 0.4rem 0.6rem;
    font-size: 0.7rem;
    font-weight: 700;
  }
  .action.primary {
    background: var(--action-bg);
    color: white;
  }
  .action.primary:hover {
    background: var(--action-hover);
  }
  .action:disabled {
    opacity: 0.35;
  }
</style>
