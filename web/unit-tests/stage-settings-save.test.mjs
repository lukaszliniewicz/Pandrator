/*global structuredClone, console */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { URL } from 'node:url';
import ts from 'typescript';

// Execute the actual editor functions with controlled API acknowledgments.
const source = readFileSync(
  new URL('../src/lib/SessionWorkspace.svelte', import.meta.url),
  'utf8'
);
const script = source.slice(
  source.indexOf('>') + 1,
  source.indexOf('</script>')
);
const ast = ts.createSourceFile(
  'editor.ts',
  script,
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.TS
);
const names = new Set([
  'saveSettings',
  'persistSection',
  'persistDefaultSection',
  'closeStageSettings',
  'captureStageSettings',
  'stageSectionUpdates',
  'clearSectionOverrides',
  'revertStageToDefaults'
]);
const functions = ast.statements.filter(
  (node) => ts.isFunctionDeclaration(node) && names.has(node.name?.text)
);
assert.equal(functions.length, names.size);
const code = ts.transpileModule(
  functions.map((node) => node.getText(ast)).join('\n'),
  {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.None
    }
  }
).outputText;
function gate() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
function fixture(key = 'translate') {
  const pending = gate(),
    started = gate();
  const writes = [],
    transformations = [],
    defaults = [];
  let gets = 0,
    loads = 0;
  const env = {
    structuredClone,
    $state: { snapshot: (value) => value },
    session: { id: 'fixture' },
    outcome: { revision: 7, value: { transformations: {} } },
    settingsStage: { key, enabled: true },
    settingsOpening: 1,
    settingsMutation: 0,
    settingsLoading: false,
    settingsSaving: false,
    disposed: false,
    settingsBases: {
      translation: {
        revision: 4,
        global_revision: 3,
        global: {},
        override: { unrelated: 'retained' }
      },
      text: { revision: 4, global_revision: 3, global: {}, override: {} }
    },
    stageSettings: {},
    stageMessage: '',
    error: '',
    ttsSwitchSource: null,
    translationSourceArtifactId: 'source',
    sourcePassages: { customized: false, valid: true },
    model: 'default',
    backend: 'llm',
    targetLanguage: 'en',
    reasoningEffort: '',
    instructions: 'submitted',
    optimizationConcurrent: 1,
    correctionBatchCharLimit: 6000,
    correctionBatchSegmentLimit: 40,
    contextBefore: 8,
    contextAfter: 2,
    preventSubtitleRemoval: false,
    timingContextMode: 'full',
    timingContextGap: 2000,
    webResearchEnabled: false,
    webResearchModel: '',
    webResearchMode: 'global',
    webResearchContextFraction: 0.8,
    speechAnnotationMode: 'off',
    optimizationTiming: 'generation',
    speechOptimizationMode: 'guarded',
    speechAnnotationOnly: false,
    optimizationEnabled: true,
    documentOptimizationEnabled: false,
    optimizationBatchSize: 3,
    documentOptimizationBatchSize: 8,
    optimizationMultiStage: false,
    optimizationPrompt: '',
    optimizationFirstPrompt: '',
    optimizationSecondPrompt: '',
    optimizationThirdPrompt: '',
    invalidateSpeechRequests() {},
    errorMessage: (caught) => caught.message,
    sectionDisplay: (section) => section,
    load: async () => {
      loads++;
    },
    run: async () => {},
    workflowStore: { snapshot: { stages: [] } },
    updateOutcomeTransformations: async (value, base) =>
      transformations.push({ value, base }),
    sessionApi: {
      settings: async () => {
        gets++;
        throw new Error('unexpected fresh revision');
      },
      saveDefaults: async (...args) => {
        defaults.push(args);
        return { revision: 4 };
      },
      saveSettings: async (...args) => {
        writes.push(args);
        started.resolve();
        return pending.promise;
      }
    }
  };
  vm.runInContext(code, vm.createContext(env));
  return {
    env,
    pending,
    started,
    writes,
    defaults,
    transformations,
    gets: () => gets,
    loads: () => loads
  };
}

for (const fail of [false, true]) {
  const f = fixture();
  const save = f.env.saveSettings();
  await f.started.promise;
  f.env.closeStageSettings();
  f.env.settingsStage = { key: 'correct' };
  f.env.settingsOpening++;
  if (fail) f.pending.reject(new Error('obsolete failure'));
  else f.pending.resolve({ revision: 5 });
  await save;
  assert.equal(f.env.settingsStage.key, 'correct');
  assert.equal(f.env.error, '');
  assert.equal(f.loads(), 0);
}
{
  const f = fixture('optimize_tts');
  const save = f.env.saveSettings();
  await f.started.promise;
  await f.env.saveSettings();
  assert.equal(f.writes.length, 1, 'duplicate submission is blocked');
  f.env.optimizationEnabled = false;
  f.env.documentOptimizationEnabled = true;
  f.pending.resolve({ revision: 5 });
  await save;
  assert.equal(f.writes[0][3].llm_tts_optimization, true);
  assert.equal(f.transformations[0].value.llm_tts_optimization, true);
  assert.equal(f.transformations[0].value.llm_tts_document_optimization, false);
  assert.equal(f.transformations[0].base.revision, 7);
}
{
  const f = fixture();
  const save = f.env.saveSettings();
  await f.started.promise;
  assert.equal(f.writes[0][2], 4, 'use revision from dialog opening');
  assert.equal(f.writes[0][3].unrelated, 'retained');
  f.pending.reject(
    new Error('Settings changed elsewhere. Reload before saving.')
  );
  await save;
  assert.equal(f.gets(), 0);
  assert.equal(f.env.settingsStage.key, 'translate');
  assert.match(f.env.error, /changed elsewhere/);
  assert.equal(f.env.settingsSaving, false);
  assert.equal(f.env.instructions, 'submitted');
}
{
  const f = fixture();
  const save = f.env.saveSettings('defaults');
  await f.started.promise;
  assert.equal(f.defaults[0][1], 3, 'global revision belongs to opening');
  assert.equal(f.writes[0][2], 4);
  assert.equal('source_artifact_id' in f.defaults[0][2], false);
  f.pending.reject(new Error('Session settings conflict'));
  await save;
  assert.match(
    f.env.error,
    /Saved translation defaults.*Remaining changes.*conflict/
  );
  assert.equal(f.env.settingsSaving, false);
}
{
  const f = fixture();
  f.env.settingsBases = {};
  await f.env.saveSettings();
  assert.equal(f.writes.length, 0);
  assert.match(f.env.error, /Reload/);
}
console.log('stage-settings-save: 6 ownership/snapshot/revision cases passed');
