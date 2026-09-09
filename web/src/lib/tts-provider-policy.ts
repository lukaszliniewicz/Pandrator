import type { TtsService } from './api-models';

export function serviceMatches(service: TtsService, value: unknown): boolean {
  const selected = String(value ?? '')
    .trim()
    .toLowerCase();
  return [service.id, service.name].some(
    (item) =>
      String(item ?? '')
        .trim()
        .toLowerCase() === selected
  );
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
