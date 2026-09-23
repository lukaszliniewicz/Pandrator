// Manager package-list grouping: definition rows + model_info merge,
// Installed derived from inspection data, checkbox selection preserved.
// Run: node unit-tests/local-model-manager-groups.test.mjs (from web/).
// Pure helper only: no ports, no server, no build, no downloads.
/*global process, console, setTimeout */
import assert from 'node:assert/strict';
import { buildSync } from 'esbuild';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const outdir = mkdtempSync(path.join(tmpdir(), 'local-model-manager-'));
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
  groupLocalModels,
  resolveLocalModels,
  summarizeLocalModel
} = await import(
  pathToFileURL(path.join(outdir, 'local-model-groups.bundle.mjs')).href
);

const unhandled = [];
process.on('unhandledRejection', (reason) => {
  unhandled.push(reason);
});

// Mirrors LocalComponentsPanel Definition rows: manager-curated fields on the
// record, catalogue metadata nested under model_info (no family/precision of
// its own).
function definitionRow(id, label, info = {}) {
  return {
    id,
    label,
    description: `${label} description`,
    estimated_download_bytes: 1024,
    model_info: { id, label, ...info }
  };
}

function managerEntries() {
  return [
    definitionRow(
      'qwen3_tts_1_7b_base_q8_0',
      'Qwen3 TTS 12Hz 1.7B Base Q8_0 GGUF',
      {
        family: 'qwen3_tts',
        recommended_for: 'Multilingual cloned narration',
        package_availability: { status: 'installable', reason: 'via manager' }
      }
    ),
    definitionRow(
      'qwen3_tts_1_7b_customvoice_q8_0',
      'Qwen3 TTS 12Hz 1.7B CustomVoice Q8_0 GGUF',
      {
        family: 'qwen3_tts',
        voice_mode: 'prebuilt',
        package_availability: { status: 'installable', reason: 'via manager' }
      }
    ),
    definitionRow('pocket_tts_german_q8_0', 'PocketTTS German Q8_0 GGUF', {
      family: 'pocket_tts',
      voice_mode: 'hybrid',
      supported_languages: ['de'],
      package_availability: { status: 'installable', reason: 'via manager' }
    }),
    definitionRow('pocket_tts_german_bf16', 'PocketTTS German BF16 GGUF', {
      family: 'pocket_tts',
      voice_mode: 'hybrid',
      supported_languages: ['de']
    })
  ];
}

function merged(entries) {
  return entries.map((model) => ({ ...(model.model_info ?? {}), ...model }));
}

function testUmbrellasAndCapabilitiesKeepExactIds() {
  const source = [
    { id: 'chatterbox_q8_0', family: 'chatterbox', capabilities: ['voice_cloning'] },
    { id: 'chatterbox_turbo_q8_0', family: 'chatterbox_turbo', capabilities: ['prebuilt_voices', 'vocal_events'] },
    { id: 'firered_audio_tts_q8_0', family: 'firered_audio', capabilities: ['voice_cloning'] },
    { id: 'fireredtts3_base_q8_0', family: 'fireredtts3', capabilities: ['voice_cloning'] },
    { id: 'fireredtts3_instruct_q8_0', family: 'fireredtts3', capabilities: ['voice_cloning', 'instructions'] }
  ];
  const groups = groupLocalModels(resolveLocalModels(source));
  assert.deepEqual(groups.map(group => group.label), ['Chatterbox', 'FireRed']);
  const ids = groups.flatMap(group => group.subgroups.flatMap(sub => sub.models.map(model => model.id)));
  assert.deepEqual([...ids].sort(), source.map(model => model.id).sort());
  const filtered = filterLocalGroups(groups, '', 'instructions');
  assert.equal(filtered.length, 1);
  assert.deepEqual(filtered[0].subgroups.flatMap(sub => sub.models.map(model => model.id)), ['fireredtts3_instruct_q8_0']);
  assert.equal(groups[1].total, 3);
}
testUmbrellasAndCapabilitiesKeepExactIds();

function testManagerRowsGroupLikeCatalogue() {
  const groups = groupLocalModels(
    resolveLocalModels(merged(managerEntries()), [], [], {
      installedIds: ['qwen3_tts_1_7b_base_q8_0']
    })
  );
  const qwen = groups.find((group) => group.family === 'qwen3_tts');
  assert.ok(qwen);
  assert.deepEqual(
    qwen.subgroups.map((subgroup) => subgroup.label),
    ['1.7B Base', '1.7B CustomVoice']
  );
  // Manager rows carry no voice_mode; the variant still names the capability.
  assert.equal(qwen.subgroups[0].detail, 'Voice cloning');
  assert.equal(qwen.subgroups[1].detail, 'Built-in voices');
  const pocket = groups.find((group) => group.family === 'pocket_tts');
  assert.ok(pocket);
  assert.deepEqual(
    pocket.subgroups.map((subgroup) => subgroup.label),
    ['German']
  );
  assert.equal(
    summarizeLocalModel(groups, 'pocket_tts_german_bf16'),
    'PocketTTS · German · BF16'
  );
}

function testInstalledComesFromInspectionOnly() {
  const installedIds = ['qwen3_tts_1_7b_base_q8_0'];
  // The checkbox selection (listed) is staged install intent, not proof.
  const listed = [
    'qwen3_tts_1_7b_base_q8_0',
    'qwen3_tts_1_7b_customvoice_q8_0',
    'pocket_tts_german_bf16'
  ];
  const resolved = resolveLocalModels(merged(managerEntries()), listed, [], {
    installedIds
  });
  const badges = Object.fromEntries(
    resolved.map((model) => [model.id, model.badge])
  );
  assert.equal(badges.qwen3_tts_1_7b_base_q8_0, 'installed');
  // Selected for install but absent from inspection: never Installed.
  assert.equal(badges.qwen3_tts_1_7b_customvoice_q8_0, 'installable');
  assert.equal(badges.pocket_tts_german_bf16, 'unknown');
  assert.equal(badgeLabelFor(badges.qwen3_tts_1_7b_base_q8_0), 'Installed');
}

function testLoadedBeatsInstalled() {
  const resolved = resolveLocalModels(merged(managerEntries()), [], [], {
    installedIds: ['qwen3_tts_1_7b_base_q8_0'],
    loadedIds: ['qwen3_tts_1_7b_base_q8_0']
  });
  assert.equal(resolved[0].badge, 'loaded');
}

function testRecommendedFilterNeverDropsSelection() {
  // The panel pre-filters to recommended/selected rows; resolve must still
  // surface a selected id that the filter removed.
  const entries = merged(managerEntries()).filter(
    (model) => model.id !== 'pocket_tts_german_bf16'
  );
  const resolved = resolveLocalModels(
    entries,
    ['pocket_tts_german_bf16'],
    [],
    {}
  );
  const kept = resolved.find((model) => model.id === 'pocket_tts_german_bf16');
  assert.ok(kept, 'staged checkbox selection survives filtering');
  assert.equal(kept.custom, true);
  assert.equal(kept.badge, 'unknown');
}

function testManagerSearchAcrossDescendants() {
  const groups = groupLocalModels(
    resolveLocalModels(merged(managerEntries()), [], [], {
      installedIds: ['qwen3_tts_1_7b_base_q8_0']
    })
  );
  const german = filterLocalGroups(groups, 'german');
  assert.equal(german.length, 1);
  assert.equal(german[0].family, 'pocket_tts');
  assert.equal(german[0].subgroups[0].models.length, 2);
  const voice = filterLocalGroups(groups, 'voice cloning');
  assert.ok(voice.some((group) => group.family === 'qwen3_tts'));
}

await testManagerRowsGroupLikeCatalogue();
await testInstalledComesFromInspectionOnly();
await testLoadedBeatsInstalled();
await testRecommendedFilterNeverDropsSelection();
await testManagerSearchAcrossDescendants();
await new Promise((resolve) => setTimeout(resolve, 20));
assert.deepEqual(unhandled, []);
console.log(
  'local-model-manager-groups: 5 inspection/selection/search cases passed, no unhandled rejections'
);
