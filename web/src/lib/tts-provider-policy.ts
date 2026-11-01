import type { TtsService } from './api-models';

function serviceIdentity(value: unknown) {
  const normalized = String(value ?? '')
    .trim()
    .toLowerCase()
    .replace(/[-\s]/g, '_');
  const aliases: Record<string, string> = {
    qwen: 'kobold_qwen',
    qwen3: 'kobold_qwen',
    qwen3_tts: 'kobold_qwen',
    kobold_qwen3: 'kobold_qwen',
    'audio.cpp': 'audio_cpp',
    audiocpp: 'audio_cpp'
  };
  return aliases[normalized] ?? normalized;
}

export function serviceMatches(service: TtsService, value: unknown): boolean {
  const selected = serviceIdentity(value);
  return [service.id, service.name].some(
    (item) => serviceIdentity(item) === selected
  );
}

/** Unchecked connections can be selected; execution validates them on the server. */
export function ttsServiceUsable(service: TtsService | undefined): boolean {
  return Boolean(service && service.available !== false);
}

export function selectableTtsServices(
  services: TtsService[],
  current: unknown,
  includeCompatibility = false
): TtsService[] {
  return services.filter(
    (service) =>
      service.catalogue_role !== 'compatibility' ||
      serviceMatches(service, current) ||
      includeCompatibility
  );
}

export function preferredTtsService(
  services: TtsService[]
): TtsService | undefined {
  return services.find(
    (service) =>
      service.available === true && service.catalogue_role !== 'compatibility'
  );
}

export function hasPrebuiltVoices(service: TtsService | undefined): boolean {
  if (!service) return false;
  return Boolean(
    service.supports_prebuilt_voices ||
    (!service.supports_voice_cloning &&
      (service.voices?.length ||
        Object.values(service.voice_catalogues ?? {}).some(
          (voices) => voices.length
        )))
  );
}
