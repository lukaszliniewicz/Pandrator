import type { SpeechCapabilities } from './speech-support';

// Authoring suggestions are not a promise of native model support.
const descriptiveEmotions = [
  'neutral',
  'happy',
  'sad',
  'angry',
  'fearful',
  'surprised',
  'calm',
  'excited',
  'serious',
  'wistful',
  'tender',
  'sarcastic'
];

export function emotionChoices(capabilities?: SpeechCapabilities) {
  const tags = capabilities?.emotion?.tags ?? [];
  return capabilities?.emotion?.mode === 'enum'
    ? [...new Set(tags)]
    : [...new Set([...tags, ...descriptiveEmotions])];
}

export function emotionChoiceKind(capabilities?: SpeechCapabilities) {
  if (!capabilities || capabilities.status === 'unknown')
    return 'Authoring suggestions · model support unverified';
  if (capabilities.emotion?.mode === 'enum')
    return 'Native emotion values · other values are saved but not sent';
  if (capabilities.emotion?.mode === 'text_field')
    return 'Suggested emotions · the model interprets custom descriptions';
  if (capabilities.emotion?.mode === 'open_description')
    return 'Suggested emotions · custom descriptions are welcome';
  return 'Authoring suggestions · check model support below';
}
