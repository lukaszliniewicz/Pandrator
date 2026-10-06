import type { RuntimeCapabilities } from './api-models';

export function detectedSpeechComponentCount(
  capabilities: RuntimeCapabilities
): number {
  const components = new Set(
    Object.entries(capabilities.services ?? {})
      .filter(([, detected]) => detected)
      .map(([id]) => id)
  );
  if (capabilities.stt?.crispasr) components.add('crispasr');
  if (capabilities.stt?.audio_cpp_tools?.runtime?.available)
    components.add('audio_cpp');
  // Recognizer weights and audio.cpp operations share these runtime identities.
  return components.size;
}
