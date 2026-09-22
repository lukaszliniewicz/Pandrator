// Grouping, lifecycle badges, search, and summary for the local model chooser.
// Run: node unit-tests/local-model-groups.test.mjs (from web/).
// Pure helper only: no ports, no server, no build, no downloads.
/*global process, console, setTimeout */
import assert from 'node:assert/strict';
import { buildSync } from 'esbuild';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

// Bundle the helper (and its extensionless relative imports) with esbuild so
// the runner needs no tsconfig or loader changes.
const outdir = mkdtempSync(path.join(tmpdir(), 'local-model-groups-'));
buildSync({
  entryPoints: [path.join('src', 'lib', 'local-model-groups.ts')],
  bundle: true,
  platform: 'node',
  format: 'esm',
  outfile: path.join(outdir, 'local-model-groups.bundle.mjs'),
  logLevel: 'error'
});
const {
  badgeLabelFor,
  filterLocalGroups,
  findLocalModel,
  groupLocalModels,
  resolveLocalModels,
  summarizeLocalModel,
  voiceModeLabel
} = await import(
  pathToFileURL(path.join(outdir, 'local-model-groups.bundle.mjs')).href
);

const unhandled = [];
process.on('unhandledRejection', (reason) => {
  unhandled.push(reason);
});

function slim(id, label, family, voiceMode, languages = []) {
  return {
    id,
    label,
    family,
    voice_mode: voiceMode,
    supported_languages: languages
  };
}

function catalogFixture() {
  return [
    slim(
      'qwen3_tts_1_7b_base_q8_0',
      'Qwen3 TTS 12Hz 1.7B Base Q8_0 GGUF',
      'qwen3_tts',
      'cloning'
    ),
    slim(
      'qwen3_tts_1_7b_base_bf16',
      'Qwen3 TTS 12Hz 1.7B Base BF16 GGUF',
      'qwen3_tts',
      'cloning'
    ),
    slim(
      'qwen3_tts_0_6b_base_q8_0',
      'Qwen3 TTS 12Hz 0.6B Base Q8_0 GGUF',
      'qwen3_tts',
      'cloning'
    ),
    slim(
      'qwen3_tts_1_7b_customvoice_q8_0',
      'Qwen3 TTS 12Hz 1.7B CustomVoice Q8_0 GGUF',
      'qwen3_tts',
      'prebuilt'
    ),
    slim(
      'qwen3_tts_1_7b_voicedesign_q8_0',
      'Qwen3 TTS 12Hz 1.7B VoiceDesign Q8_0 GGUF',
      'qwen3_tts',
      'design'
    ),
    slim(
      'pocket_tts_english_q8_0',
      'PocketTTS English Q8_0 GGUF',
      'pocket_tts',
      'hybrid',
      ['en']
    ),
    slim(
      'pocket_tts_german_q8_0',
      'PocketTTS German Q8_0 GGUF',
      'pocket_tts',
      'hybrid',
      ['de']
    ),
    slim(
      'pocket_tts_german_bf16',
      'PocketTTS German BF16 GGUF',
      'pocket_tts',
      'hybrid',
      ['de']
    ),
    slim(
      'breeze_tts_2_q8_0',
      'BreezeTTS 2 Q8_0 GGUF',
      'breeze_tts',
      'optional_cloning',
      ['zh', 'en']
    ),
    { id: 'mystery_custom', label: 'Mystery Custom' }
  ];
}

function testQwenVariantsStayDistinct() {
  const groups = groupLocalModels(resolveLocalModels(catalogFixture()));
  const qwen = groups.find((group) => group.family === 'qwen3_tts');
  assert.ok(qwen, 'qwen family grouped under one model');
  assert.equal(qwen.label, 'Qwen3-TTS');
  const labels = qwen.subgroups.map((subgroup) => subgroup.label);
  assert.deepEqual(labels, [
    '1.7B Base',
    '1.7B CustomVoice',
    '1.7B VoiceDesign',
    '0.6B Base'
  ]);
  const base = qwen.subgroups[0];
  assert.deepEqual(
    base.models.map((model) => model.id),
    ['qwen3_tts_1_7b_base_q8_0', 'qwen3_tts_1_7b_base_bf16']
  );
  assert.equal(base.detail, 'Voice cloning');
  assert.equal(qwen.subgroups[1].detail, 'Built-in voices');
  assert.equal(qwen.subgroups[2].detail, 'Voice design');
  // Exact ids are preserved, never rewritten.
  for (const subgroup of qwen.subgroups) {
    for (const model of subgroup.models) {
      assert.match(model.id, /^[a-z0-9_]+$/);
    }
  }
}

function testPocketGroupedByLanguage() {
  const groups = groupLocalModels(resolveLocalModels(catalogFixture()));
  const pocket = groups.find((group) => group.family === 'pocket_tts');
  assert.ok(pocket);
  assert.equal(pocket.label, 'PocketTTS');
  const labels = pocket.subgroups.map((subgroup) => subgroup.label);
  assert.deepEqual(labels, ['English', 'German']);
  const german = pocket.subgroups.find(
    (subgroup) => subgroup.label === 'German'
  );
  assert.deepEqual(
    german.models.map((model) => model.precisionLabel),
    ['Q8_0', 'BF16']
  );
}

function testUnknownModelsFlatFallbackLast() {
  const groups = groupLocalModels(resolveLocalModels(catalogFixture()));
  const last = groups[groups.length - 1];
  assert.equal(last.id, '__unknown');
  assert.equal(last.label, 'Other models');
  assert.equal(last.subgroups.length, 1);
  assert.equal(last.subgroups[0].models[0].id, 'mystery_custom');
}

function testBadgesNeedEvidence() {
  const catalog = [
    { ...slim('a_loaded', 'A loaded', 'qwen3_tts', 'cloning'), loaded: true },
    {
      ...slim('b_installed', 'B installed', 'qwen3_tts', 'cloning'),
      status: { installed: true }
    },
    {
      ...slim('c_acquirable', 'C acquirable', 'qwen3_tts', 'cloning'),
      package_availability: { status: 'installable', reason: 'via manager' }
    },
    {
      ...slim('d_gated', 'D gated', 'qwen3_tts', 'cloning'),
      catalogue_info: {
        package_availability: { status: 'gated', reason: 'terms' }
      }
    },
    slim('e_bare', 'E bare', 'qwen3_tts', 'cloning')
  ];
  // Every id is in models[]: membership must NOT imply installed.
  const listed = [
    'a_loaded',
    'b_installed',
    'c_acquirable',
    'd_gated',
    'e_bare'
  ];
  const resolved = resolveLocalModels(catalog, listed);
  const badges = Object.fromEntries(
    resolved.map((model) => [model.id, model.badge])
  );
  assert.deepEqual(badges, {
    a_loaded: 'loaded',
    b_installed: 'installed',
    c_acquirable: 'installable',
    d_gated: 'gated',
    e_bare: 'unknown'
  });
  assert.equal(badgeLabelFor('unknown'), 'Unknown');
  assert.equal(badgeLabelFor('loaded'), 'Loaded');
  assert.equal(badgeLabelFor('installable'), 'Installable');
  assert.equal(badgeLabelFor('gated'), 'Gated');
  // absent vs installed vs loaded stay distinct
  assert.notEqual(badges.e_bare, badges.b_installed);
  assert.notEqual(badges.b_installed, badges.a_loaded);
}

function testLoadedIdsOverrideNeedsLiveEvidence() {
  const catalog = [
    slim('qwen3_tts_1_7b_base_q8_0', 'Qwen Base', 'qwen3_tts', 'cloning')
  ];
  const without = resolveLocalModels(catalog, ['qwen3_tts_1_7b_base_q8_0']);
  assert.equal(without[0].badge, 'unknown');
  const withLive = resolveLocalModels(
    catalog,
    ['qwen3_tts_1_7b_base_q8_0'],
    [],
    {
      loadedIds: ['qwen3_tts_1_7b_base_q8_0']
    }
  );
  assert.equal(withLive[0].badge, 'loaded');
}

function testSelectionNeverLost() {
  const catalog = [
    slim('qwen3_tts_1_7b_base_q8_0', 'Qwen Base', 'qwen3_tts', 'cloning')
  ];
  const resolved = resolveLocalModels(catalog, [], ['retired_custom_id']);
  const custom = resolved.find((model) => model.id === 'retired_custom_id');
  assert.ok(custom, 'custom selected id is preserved');
  assert.equal(custom.custom, true);
  assert.equal(custom.badge, 'unknown');
  const groups = groupLocalModels(resolved);
  assert.ok(findLocalModel(groups, 'retired_custom_id'));
  assert.equal(
    summarizeLocalModel(groups, 'retired_custom_id'),
    'retired_custom_id'
  );
}

function testSearchReachesCollapsedDescendants() {
  const groups = groupLocalModels(resolveLocalModels(catalogFixture()));
  const custom = filterLocalGroups(groups, 'customvoice');
  assert.equal(custom.length, 1);
  assert.equal(custom[0].family, 'qwen3_tts');
  assert.deepEqual(
    custom[0].subgroups.map((subgroup) => subgroup.label),
    ['1.7B CustomVoice']
  );
  const german = filterLocalGroups(groups, 'german');
  assert.equal(german.length, 1);
  assert.equal(german[0].subgroups.length, 1);
  assert.equal(german[0].subgroups[0].models.length, 2);
  const none = filterLocalGroups(groups, 'no-such-model');
  assert.deepEqual(none, []);
  // Inputs untouched.
  assert.equal(groups.find((group) => group.family === 'qwen3_tts').total, 5);
}

function testSummaryReadable() {
  const groups = groupLocalModels(resolveLocalModels(catalogFixture()));
  assert.equal(
    summarizeLocalModel(groups, 'qwen3_tts_1_7b_base_q8_0'),
    'Qwen3-TTS · 1.7B Base · Q8_0'
  );
  assert.equal(
    summarizeLocalModel(groups, 'pocket_tts_german_bf16'),
    'PocketTTS · German · BF16'
  );
  assert.equal(summarizeLocalModel(groups, ''), 'Choose a model');
  assert.equal(summarizeLocalModel(groups, '', 'Pick one'), 'Pick one');
}

function testVoiceModesNeverMerged() {
  const groups = groupLocalModels(resolveLocalModels(catalogFixture()));
  const qwen = groups.find((group) => group.family === 'qwen3_tts');
  const modes = new Set();
  for (const subgroup of qwen.subgroups) {
    for (const model of subgroup.models) modes.add(model.voiceMode);
  }
  assert.deepEqual([...modes].sort(), ['cloning', 'design', 'prebuilt']);
  assert.equal(voiceModeLabel('cloning'), 'Voice cloning');
  assert.equal(voiceModeLabel('prebuilt'), 'Built-in voices');
  assert.equal(voiceModeLabel('design'), 'Voice design');
  assert.equal(voiceModeLabel('hybrid'), 'Built-in + cloning');
}

function testHelpersArePure() {
  const catalog = catalogFixture();
  const snapshot = JSON.stringify(catalog);
  const groups = groupLocalModels(
    resolveLocalModels(catalog, ['qwen3_tts_1_7b_base_q8_0'])
  );
  filterLocalGroups(groups, 'qwen');
  assert.equal(
    JSON.stringify(catalog),
    snapshot,
    'catalogue input not mutated'
  );
}

await testQwenVariantsStayDistinct();
await testPocketGroupedByLanguage();
await testUnknownModelsFlatFallbackLast();
await testBadgesNeedEvidence();
await testLoadedIdsOverrideNeedsLiveEvidence();
await testSelectionNeverLost();
await testSearchReachesCollapsedDescendants();
await testSummaryReadable();
await testVoiceModesNeverMerged();
await testHelpersArePure();
await new Promise((resolve) => setTimeout(resolve, 20));
assert.deepEqual(unhandled, []);
console.log(
  'local-model-groups: 10 grouping/badge/search/summary cases passed, no unhandled rejections'
);
