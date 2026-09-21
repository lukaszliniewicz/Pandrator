import type { CatalogVoice, VoiceCompatibility } from './voice-library-api';

export function voiceLanguageName(code: string): string {
  try {
    return new Intl.DisplayNames(['en'], { type: 'language' }).of(code) ?? code;
  } catch {
    return code;
  }
}

export function readinessLabel(binding?: VoiceCompatibility): string {
  if (!binding) return 'No compatible model';
  if (binding.ready) return 'Ready to generate';
  return (
    {
      needs_reference: 'Add a reference sample',
      needs_link: 'Prepare reference for this service',
      unavailable: 'Service unavailable',
      model_unavailable: 'Model not installed',
      unknown: 'Availability not checked'
    }[binding.status] ?? 'Check voice setup'
  );
}

export function bestBinding(
  voice: CatalogVoice
): VoiceCompatibility | undefined {
  return (
    voice.compatibility.find((binding) => binding.ready) ??
    voice.compatibility[0]
  );
}

export function setupHref(binding?: VoiceCompatibility): string {
  return binding?.status === 'model_unavailable'
    ? '/models'
    : `/providers?tab=speech${binding?.service_id ? `&service=${encodeURIComponent(binding.service_id)}` : ''}`;
}
