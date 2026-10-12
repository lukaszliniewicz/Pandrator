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
const { describeVoice, voiceSupportsLanguage, languagesForService } =
  await import(
    `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
  );

for (const provider of ['gemini', 'vertex_ai']) {
  test(`${provider}: German generation offers German library voices and multilingual presets`, () => {
    const german = describeVoice(provider, 'de-de-advisor-1', {
      display_name: 'Authoritative Advisor 1',
      language_code: 'de-DE',
      accent: 'German',
      gender: 'female'
    });
    const french = describeVoice(provider, 'fr-fr-advisor-1', {
      language_code: 'fr-FR'
    });
    const preset = describeVoice(provider, 'Algenib');
    assert.equal(german.id, 'de-de-advisor-1');
    assert.equal(german.name, 'Authoritative Advisor 1');
    assert.match(german.language, /German/);
    assert.equal(voiceSupportsLanguage(german, 'de'), true);
    assert.equal(voiceSupportsLanguage(german, 'de-DE'), true);
    assert.equal(voiceSupportsLanguage(german, 'fr'), false);
    assert.deepEqual(
      [german, french, preset]
        .filter((voice) => voiceSupportsLanguage(voice, 'de'))
        .map((voice) => voice.id),
      ['de-de-advisor-1', 'Algenib']
    );
    assert.equal(preset.languageCode, '');
    assert.equal(voiceSupportsLanguage(preset, 'ja'), true);
  });
}

test('Google camelCase language metadata and explicit multilingual voices retain their meaning', () => {
  const native = describeVoice('vertex_ai', 'Voice:Exact/Case', {
    languageCode: 'fr-FR'
  });
  assert.equal(native.id, 'Voice:Exact/Case');
  assert.equal(voiceSupportsLanguage(native, 'fr'), true);
  assert.equal(voiceSupportsLanguage(native, 'de'), false);
  for (const metadata of [
    { multilingual: true, language_code: 'en-US' },
    { language_code: 'mul' },
    {}
  ]) {
    const multilingual = describeVoice('gemini', 'Shared', metadata);
    assert.equal(voiceSupportsLanguage(multilingual, 'de'), true);
    assert.equal(voiceSupportsLanguage(multilingual, 'fr'), true);
  }
});

test('voice language metadata does not establish model language coverage', () => {
  const german = describeVoice('vertex_ai', 'de-de-advisor-1', {
    language_code: 'de-DE'
  });
  assert.deepEqual(languagesForService('vertex_ai', [german]), []);
  assert.deepEqual(
    languagesForService('vertex_ai', [german], {
      modelId: 'model',
      modelCatalog: [{ id: 'model', supported_languages: ['de', 'fr'] }]
    }).map((language) => language.value),
    ['de', 'fr']
  );
});
