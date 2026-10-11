export function elevenLabsVoiceSettingsError(
  model: string,
  value: Record<string, unknown>
): string {
  if (!['eleven_v4', 'eleven_v4_turbo'].includes(model)) return '';
  const allowed =
    model === 'eleven_v4' ? ['stability', 'similarity_boost'] : ['stability'];
  for (const [key, setting] of Object.entries(value)) {
    if (!allowed.includes(key))
      return `${model} does not support ${key.replaceAll('_', ' ')} on this route. Reset the voice settings to continue.`;
    if (
      typeof setting !== 'number' ||
      !Number.isFinite(setting) ||
      setting < 0 ||
      setting > 1
    )
      return `${key.replaceAll('_', ' ')} must be a number between 0 and 1.`;
  }
  return '';
}
