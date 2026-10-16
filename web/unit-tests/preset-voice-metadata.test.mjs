import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { test } from 'node:test';
import { build } from 'esbuild';

const bundle = await build({
  entryPoints: ['src/lib/voice-catalog.ts'],
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'browser'
});
const {
  describeVoice,
  voiceSupportsLanguage,
  defaultVoiceForLanguage,
  languagesForService
} = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);

test('Voxtral locale describes the preset while cross-language synthesis stays selectable', () => {
  const french = describeVoice('audio_cpp', 'fr_female', {
    pandrator_preset: true,
    display_name: 'French · Female',
    language_code: 'fr',
    language_label: 'French preset · Multilingual',
    gender: 'female',
    multilingual: true
  });
  assert.equal(french.id, 'fr_female');
  assert.equal(french.name, 'French · Female');
  assert.equal(french.gender, 'Female');
  assert.match(french.language, /French/);
  assert.equal(voiceSupportsLanguage(french, 'de'), true);
  assert.equal(voiceSupportsLanguage(french, 'fr-FR'), true);
  const general = describeVoice('voxtral', 'casual_male');
  assert.equal(general.languageCode, '');
  assert.equal(general.language, 'Multilingual');
  assert.equal(voiceSupportsLanguage(general, 'hi'), true);
  const standalone = describeVoice('voxtral', 'fr_female');
  assert.equal(standalone.name, 'French · Female');
  assert.equal(voiceSupportsLanguage(standalone, 'en'), true);
});

test('locale-bound presets filter correctly and keep their original native IDs', () => {
  const french = describeVoice('audio_cpp', 'ff_siwis', {
    pandrator_preset: true,
    display_name: 'Siwis',
    language_code: 'fr',
    language_label: 'French',
    gender: 'female',
    multilingual: false
  });
  assert.equal(french.id, 'ff_siwis');
  assert.equal(french.name, 'Siwis');
  assert.equal(voiceSupportsLanguage(french, 'fr-FR'), true);
  assert.equal(voiceSupportsLanguage(french, 'en'), false);
  assert.deepEqual(languagesForService('audio_cpp', [french]), []);
  const englishCosette = describeVoice('audio_cpp', 'cosette', {
    pandrator_preset: true,
    display_name: 'Cosette',
    language_code: 'en',
    language_label: 'English',
    multilingual: false
  });
  assert.equal(voiceSupportsLanguage(englishCosette, 'fr'), false);
  const american = describeVoice('audio_cpp', 'af_heart', {
    pandrator_preset: true,
    display_name: 'Heart',
    language_code: 'en-US',
    language_label: 'American English',
    multilingual: false
  });
  assert.equal(voiceSupportsLanguage(american, 'en'), true);
  assert.equal(voiceSupportsLanguage(american, 'en-GB'), false);
});

test('provider names and explicit metadata work with arbitrary configured service IDs', () => {
  const voice = describeVoice('custom-tts', 'Native/Case:42', {
    display_name: 'My narrator',
    language_code: 'fr-FR',
    gender: 'female'
  });
  assert.equal(voice.id, 'Native/Case:42');
  assert.equal(voice.name, 'My narrator');
  assert.equal(voiceSupportsLanguage(voice, 'fr'), true);
  assert.equal(voiceSupportsLanguage(voice, 'de'), false);
  assert.equal(
    describeVoice('custom-tts', 'unknown').language,
    'Language not specified'
  );
});

test('defaults choose exact locales before language fallbacks and preserve explicit choices', () => {
  const service = {
    default_voices_by_language: {
      voxtral: { fr: 'fr_female', 'fr-CA': 'casual_female', de: 'de_male' }
    },
    default_voices: { voxtral: 'neutral_male' },
    default_voice: 'casual_male'
  };
  assert.equal(
    defaultVoiceForLanguage(service, 'voxtral', 'fr_FR'),
    'fr_female'
  );
  assert.equal(
    defaultVoiceForLanguage(service, 'voxtral', 'fr-CA'),
    'casual_female'
  );
  assert.equal(defaultVoiceForLanguage(service, 'voxtral', 'de-DE'), 'de_male');
  assert.equal(
    defaultVoiceForLanguage(service, 'voxtral', 'en'),
    'neutral_male'
  );
  assert.equal(defaultVoiceForLanguage(service, 'other', 'fr'), 'casual_male');
  service.default_voices_by_language.voxtral.fr = '';
  assert.equal(defaultVoiceForLanguage(service, 'voxtral', 'fr'), '');
});
