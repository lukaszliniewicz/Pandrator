import { type StageSettingsDraft } from './stage-settings-draft.svelte';
import { languageSupportProblem } from './language-registry';
import {
  hasPrebuiltVoices,
  selectableTtsServices
} from './tts-provider-policy';
import { type TtsCatalogueState } from './tts-catalogue-state.svelte';
import type { TtsService, XttsModel } from './api-models';
import { LANGUAGE_OPTIONS } from './settings-fields';
import {
  describeVoice,
  languagesForService,
  voiceSupportsLanguage
} from './voice-catalog';

type VoiceLanguageGroup = {
  key: string;
  label: string;
  unspecified: boolean;
  voices: TtsSettingsSelection['clonedVoiceDescriptors'][number][];
};

/** Reactive voice and model selection policy shared by validation and generation controls. */
export class TtsSettingsSelection {
  constructor(
    readonly draft: StageSettingsDraft,
    readonly speechCatalogues: TtsCatalogueState
  ) {}
  ttsSwitchSource = $state<TtsService | null>(null);
  ttsSwitchReviewed = $state(false);
  selectedTtsService = $derived.by(() =>
    this.speechCatalogues.catalogue.services.find((item) =>
      [item.id, item.name]
        .map((value) => String(value ?? '').toLowerCase())
        .includes(this.draft.ttsService.toLowerCase())
    )
  );
  selectedTtsServiceAvailable = $derived.by(
    () => this.selectedTtsService?.available === true
  );
  compareLabels = (left: string, right: string) =>
    left.localeCompare(right, undefined, { sensitivity: 'base' });
  serviceLabel = (service: TtsService) => String(service.name || service.id);
  compareServices = (left: TtsService, right: TtsService) =>
    this.compareLabels(this.serviceLabel(left), this.serviceLabel(right)) ||
    this.compareLabels(left.id, right.id);
  availableTtsServices = $derived.by(() =>
    selectableTtsServices(
      this.speechCatalogues.catalogue.services,
      this.draft.ttsService
    )
      .filter((service) => service.available === true)
      .sort(this.compareServices)
  );
  unavailableTtsServices = $derived.by(() =>
    selectableTtsServices(
      this.speechCatalogues.catalogue.services,
      this.draft.ttsService
    )
      .filter((service) => service.available !== true)
      .sort(this.compareServices)
  );
  ttsModels = $derived.by(() => this.selectedTtsService?.models ?? []);
  selectedTtsLanguageSupport = $derived.by(
    () =>
      this.selectedTtsService?.model_catalog?.find(
        (item) => item.id === this.draft.ttsModel
      )?.language_support
  );
  ttsLanguageIssue = $derived.by(() =>
    languageSupportProblem(
      this.selectedTtsLanguageSupport,
      this.draft.targetLanguage
    )
  );
  selectedTtsDefaultVoice = $derived.by(
    () =>
      this.selectedTtsService?.default_voices_by_language?.[
        this.draft.ttsModel
      ]?.[this.draft.targetLanguage] ??
      this.selectedTtsService?.default_voices?.[this.draft.ttsModel] ??
      this.selectedTtsService?.default_voice ??
      ''
  );
  selectedTtsServiceId = $derived.by(() =>
    String(this.selectedTtsService?.id ?? this.draft.ttsService)
      .trim()
      .toLowerCase()
      .replaceAll('-', '_')
      .replaceAll(' ', '_')
  );
  audioCppLinkedReferences = $derived.by(
    () => this.selectedTtsService?.adapter === 'audio_cpp'
  );
  selectedModelNeedsReviewedTranscript = $derived.by(
    () =>
      this.selectedTtsService?.voice_reference_text === 'required' ||
      (this.audioCppLinkedReferences &&
        this.draft.ttsModel.toLowerCase().includes('omnivoice'))
  );
  supportsXttsModelUpload = $derived.by(
    () =>
      this.selectedTtsServiceId === 'xtts' &&
      Boolean(this.selectedTtsService?.supports_model_upload)
  );
  ttsModelAcquisitionHint = $derived.by(() =>
    this.selectedTtsServiceId === 'kobold_qwen'
      ? 'A Qwen voice family that was not prepared initially downloads automatically on first use. Model size and precision are configured in Speech services → Local.'
      : this.selectedTtsServiceId === 'chatterbox'
        ? 'Chatterbox downloads the selected model automatically on first use. The first generation can therefore take several minutes.'
        : this.selectedTtsServiceId === 'fishs2'
          ? 'Fish uses one S2 Pro model. Choose its quantization in Speech services → Local; the selected file is downloaded automatically when that configuration is applied.'
          : ''
  );
  generationPromptModels = $derived.by(() =>
    Array.from(this.selectedTtsService?.generation_prompt_models ?? []).map(
      (model) => String(model).toLowerCase()
    )
  );
  supportsGenerationPrompt = $derived.by(() =>
    this.generationPromptModels.includes(this.draft.ttsModel.toLowerCase())
  );
  invalidTtsConcurrency = $derived.by(
    () =>
      Boolean(this.selectedTtsService?.supports_parallel_synthesis) &&
      (!Number.isInteger(this.draft.ttsConcurrentRequests) ||
        this.draft.ttsConcurrentRequests < 1 ||
        this.draft.ttsConcurrentRequests > 8)
  );
  supportsBatchSynthesis = $derived.by(() =>
    Boolean(
      this.selectedTtsService?.supports_batch_synthesis &&
      this.selectedTtsService?.batch_synthesis?.streaming &&
      ['ndjson-v1', 'pandrator-ordered-serial-v1'].includes(
        this.selectedTtsService?.batch_synthesis?.protocol ?? ''
      )
    )
  );
  maximumTtsBatchSize = $derived.by(() =>
    Number(this.selectedTtsService?.batch_synthesis?.max_batch_size ?? 32)
  );
  selectedModelVoiceMode = $derived.by(
    () =>
      this.selectedTtsService?.model_voice_modes?.[this.draft.ttsModel] ?? ''
  );
  selectedModelAllowsReferenceFree = $derived.by(() =>
    ['optional_cloning', 'design'].includes(this.selectedModelVoiceMode)
  );
  selectedModelUsesReferences = $derived.by(
    () =>
      ['cloning', 'hybrid', 'optional_cloning'].includes(
        this.selectedModelVoiceMode
      ) ||
      (this.selectedTtsServiceId === 'kobold_qwen' &&
        this.draft.ttsModel.toLowerCase() === 'voice cloning')
  );
  selectedModelHasNoPrebuiltVoices = $derived.by(
    () =>
      ['cloning', 'optional_cloning', 'design'].includes(
        this.selectedModelVoiceMode
      ) ||
      (this.selectedTtsServiceId === 'kobold_qwen' &&
        this.draft.ttsModel.toLowerCase() === 'voice cloning')
  );
  supportsCloningVoices = $derived.by(() =>
    Boolean(this.selectedTtsService?.supports_voice_cloning)
  );
  supportsPrebuiltVoices = $derived.by(() =>
    Boolean(
      hasPrebuiltVoices(this.selectedTtsService) &&
      !this.selectedModelHasNoPrebuiltVoices
    )
  );
  selectedModelVoiceIds = $derived.by(
    () =>
      this.selectedTtsService?.voice_catalogues?.[this.draft.ttsModel] ??
      (this.supportsPrebuiltVoices
        ? (this.selectedTtsService?.voices ?? [])
        : [])
  );
  ttsVoiceDescriptors = $derived.by(() =>
    Array.from(new Set(this.selectedModelVoiceIds)).map((voice) =>
      describeVoice(
        String(this.selectedTtsService?.id ?? this.draft.ttsService),
        String(voice),
        this.selectedTtsService?.voice_metadata?.[
          `${this.draft.ttsModel}:${String(voice)}`
        ]
      )
    )
  );
  ttsLanguages = $derived.by(() =>
    languagesForService(
      String(this.selectedTtsService?.id ?? this.draft.ttsService),
      this.ttsVoiceDescriptors,
      {
        modelId: this.draft.ttsModel,
        modelCatalog: this.selectedTtsService?.model_catalog
      }
    )
  );
  filteredPrebuiltVoices = $derived.by(() =>
    this.ttsVoiceDescriptors.filter((voice) =>
      voiceSupportsLanguage(voice, this.draft.targetLanguage)
    )
  );
  defaultVoiceDescriptor = $derived.by(() =>
    this.ttsVoiceDescriptors.find(
      (voice) => voice.id === this.selectedTtsDefaultVoice
    )
  );
  defaultVoiceLanguageMismatch = $derived.by(() =>
    Boolean(
      this.defaultVoiceDescriptor &&
      !voiceSupportsLanguage(
        this.defaultVoiceDescriptor,
        this.draft.targetLanguage
      )
    )
  );
  selectedVoiceLanguageMismatch = $derived.by(
    () =>
      this.supportsPrebuiltVoices &&
      (this.draft.voiceName
        ? this.ttsVoiceDescriptors.some(
            (voice) =>
              voice.id === this.draft.voiceName &&
              !voiceSupportsLanguage(voice, this.draft.targetLanguage)
          )
        : this.defaultVoiceLanguageMismatch)
  );
  publishedProviderVoices = $derived.by(() =>
    this.speechCatalogues.voices.flatMap((voice) => {
      const registration =
        voice?.metadata_json?.providers?.[this.selectedTtsServiceId];
      return registration?.status === 'ready' && registration?.voice_id
        ? [String(registration.voice_id)]
        : [];
    })
  );
  managedProviderVoiceIds = $derived.by(
    () =>
      new Set(
        this.speechCatalogues.voices
          .map((voice) =>
            String(
              voice.metadata_json?.providers?.[this.selectedTtsServiceId]
                ?.voice_id ?? ''
            ).toLowerCase()
          )
          .filter(Boolean)
      )
  );
  readyManagedProviderVoiceIds = $derived.by(
    () =>
      new Set(this.publishedProviderVoices.map((voice) => voice.toLowerCase()))
  );
  prebuiltVoiceIds = $derived.by(
    () =>
      new Set(
        Array.from(
          this.selectedTtsService?.voice_catalogues?.['Prebuilt Voices'] ?? []
        ).map((voice) => String(voice).toLowerCase())
      )
  );
  clonedVoiceIds = $derived.by(() =>
    Array.from(
      new Set(
        [
          ...(this.selectedModelUsesReferences
            ? this.selectedModelVoiceIds
            : []),
          ...(this.selectedTtsService?.live_voices ?? []),
          ...this.publishedProviderVoices,
          ...(!this.selectedTtsService?.supports_prebuilt_voices
            ? (this.selectedTtsService?.voices ?? [])
            : [])
        ]
          .map((voice) => String(voice))
          .filter(
            (voice) =>
              voice &&
              !this.prebuiltVoiceIds.has(voice.toLowerCase()) &&
              (!this.managedProviderVoiceIds.has(voice.toLowerCase()) ||
                this.readyManagedProviderVoiceIds.has(voice.toLowerCase()))
          )
      )
    )
  );
  clonedVoiceDescriptors = $derived.by(() =>
    this.clonedVoiceIds.map((voice) => {
      const managed = this.speechCatalogues.voices.find(
        (item) =>
          String(
            item.metadata_json?.providers?.[this.selectedTtsServiceId]
              ?.voice_id ?? ''
          ).toLowerCase() === voice.toLowerCase()
      );
      const descriptor = describeVoice(this.selectedTtsServiceId, voice);
      return {
        ...descriptor,
        name: managed?.name ?? descriptor.name,
        managedLanguage: managed?.language
      };
    })
  );
  normalizeVoiceLanguage = (value: string | null | undefined) => {
    const raw = String(value ?? '').trim();
    if (!raw) return null;
    const normalized = raw
      .replaceAll(/\s+/g, '')
      .replaceAll('_', '-')
      .toLowerCase();
    const option = LANGUAGE_OPTIONS.find((item) => {
      const optionValue = String(item.value)
        .replaceAll(/\s+/g, '')
        .replaceAll('_', '-')
        .toLowerCase();
      const optionLabel = item.label.replaceAll(/\s+/g, '').toLowerCase();
      return optionValue === normalized || optionLabel === normalized;
    });
    if (option)
      return {
        key: String(option.value),
        label: option.label,
        unspecified: false
      };
    const baseCode = normalized.split('-')[0];
    const baseOption = LANGUAGE_OPTIONS.find(
      (item) => String(item.value).toLowerCase() === baseCode
    );
    if (baseOption)
      return {
        key: String(baseOption.value),
        label: baseOption.label,
        unspecified: false
      };
    return { key: normalized, label: raw, unspecified: false };
  };
  targetVoiceLanguage = $derived.by(
    () =>
      this.normalizeVoiceLanguage(this.draft.targetLanguage)?.key ??
      this.draft.targetLanguage
  );
  clonedVoiceGroups = $derived.by(() => {
    const groups = new Map<string, VoiceLanguageGroup>();
    for (const voice of this.clonedVoiceDescriptors) {
      const language = this.normalizeVoiceLanguage(voice.managedLanguage) ??
        this.normalizeVoiceLanguage(voice.languageCode) ?? {
          key: 'unspecified',
          label: 'Multilingual / language not set',
          unspecified: true
        };
      const existing = groups.get(language.key) ?? { ...language, voices: [] };
      existing.voices.push(voice);
      groups.set(language.key, existing);
    }
    return [...groups.values()]
      .map((group) => ({
        ...group,
        voices: [...group.voices].sort(
          (left, right) =>
            this.compareLabels(left.name, right.name) ||
            this.compareLabels(left.id, right.id)
        )
      }))
      .sort(
        (left, right) =>
          Number(left.key !== this.targetVoiceLanguage) -
            Number(right.key !== this.targetVoiceLanguage) ||
          Number(left.unspecified) - Number(right.unspecified) ||
          this.compareLabels(left.label, right.label) ||
          this.compareLabels(left.key, right.key)
      );
  });
  showClonedVoices = $derived.by(() =>
    Boolean(
      this.supportsCloningVoices &&
      this.selectedModelVoiceMode !== 'design' &&
      (!this.selectedTtsService?.supports_prebuilt_voices ||
        this.selectedModelUsesReferences)
    )
  );
  localVoiceChoices = $derived.by(() =>
    this.speechCatalogues.voices
      .map((voice) => {
        const registration =
          voice.metadata_json?.providers?.[this.selectedTtsServiceId];
        const hasSample = Number(voice.available_sample_count ?? 0) > 0;
        const needsTranscript = Boolean(
          this.selectedModelNeedsReviewedTranscript &&
          !voice.preferred_sample_transcript_reviewed
        );
        return { voice, registration, hasSample, needsTranscript };
      })
      .sort((left, right) => {
        const leftLanguage =
          left.voice.language === this.draft.targetLanguage ? 0 : 1;
        const rightLanguage =
          right.voice.language === this.draft.targetLanguage ? 0 : 1;
        return (
          leftLanguage - rightLanguage ||
          left.voice.name.localeCompare(right.voice.name)
        );
      })
  );
}

/** Transient catalogue, model-upload and voice-publication feedback. */
export class GenerationSettingsActivity {
  publishingLibraryVoiceId = $state('');
  voicePublishStatus = $state('');
  refreshingTtsServices = $state(false);
  xttsModelId = $state('');
  xttsModelFiles = $state<File[]>([]);
  uploadingXttsModel = $state(false);
  xttsModelUploadProgress = $state(0);
  xttsModelUploadPhase = $state<'idle' | 'transferring' | 'installing'>('idle');
  xttsModelUploadError = $state('');
  xttsModelUploadMessage = $state('');
  xttsModels = $state<XttsModel[]>([]);
  xttsModelsLoading = $state(false);
  xttsModelsLifecycleSupported = $state(false);
  xttsModelsCompatibility = $state('');
  deletingXttsModelId = $state('');
}
