import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { test } from 'node:test';
import { build } from 'esbuild';

const bundle = await build({
  entryPoints: ['src/lib/export-audio-version.ts'],
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'node'
});
const { exportAudioNote } = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);

const version = (overrides = {}) => ({
  assembly_id: 'assembly-3',
  artifact_id: 'artifact-3',
  generation_run_id: 'run-3',
  run_sequence_number: 3,
  plan_revision_id: 'revision-2',
  plan_revision_number: 2,
  active_revision: true,
  assembled_at: '2026-11-21T10:09:00+00:00',
  ...overrides
});
const labelOf = (item, knownRuns) =>
  exportAudioNote({ status: 'named', version: item, candidates: [] }, knownRuns)
    .versions[0];
const stamp = (iso) => new Date(iso).toLocaleString();
const runs = [
  { id: 'run-3', label: 'Run 3: regenerate · 12 segments' },
  { id: 'run-2', label: 'Run 2: generate · 5167 segments' }
];

test('a version is named by the page run label, time and plan revision', () => {
  assert.equal(
    labelOf(version(), runs),
    `Run 3: regenerate · 12 segments · assembled ${stamp('2026-11-21T10:09:00+00:00')} · plan revision 2 (active)`
  );
  assert.equal(
    labelOf(version({ active_revision: false, plan_revision_number: 1 }), runs),
    `Run 3: regenerate · 12 segments · assembled ${stamp('2026-11-21T10:09:00+00:00')} · plan revision 1`
  );
});

test('a run the page does not list falls back to its number, or to no run', () => {
  assert.match(
    labelOf(version({ generation_run_id: 'gone' }), runs),
    /^Run 3 · assembled/
  );
  assert.match(
    labelOf(
      version({ generation_run_id: null, run_sequence_number: null }),
      runs
    ),
    /^Assembled without a run · assembled/
  );
});

test('a named version says what the export will use', () => {
  const note = exportAudioNote(
    { status: 'named', version: version(), candidates: [] },
    runs
  );
  assert.equal(note.status, 'named');
  assert.match(note.headline, /will use/);
  assert.equal(note.versions.length, 1);
});

test('an ambiguous export lists every candidate and asks to select one', () => {
  const note = exportAudioNote(
    {
      status: 'ambiguous',
      version: null,
      candidates: [
        version(),
        version({
          generation_run_id: 'run-2',
          run_sequence_number: 2,
          active_revision: false,
          plan_revision_number: 1
        })
      ]
    },
    runs
  );
  assert.equal(note.status, 'ambiguous');
  assert.match(
    note.headline,
    /Select a run under Audio version.*Intermediate audio assemblies/
  );
  assert.equal(note.versions.length, 2);
  assert.match(note.versions[1], /^Run 2: generate/);
});

test('no assembly says there is none, and a missing read says nothing', () => {
  const none = exportAudioNote(
    { status: 'none', version: null, candidates: [] },
    runs
  );
  assert.equal(none.status, 'none');
  assert.deepEqual(none.versions, []);
  assert.equal(exportAudioNote(null, runs), null);
  assert.equal(exportAudioNote(undefined, runs), null);
});

test('a time that does not parse is shown as it came', () => {
  assert.match(
    labelOf(version({ assembled_at: 'last Tuesday' }), runs),
    / · assembled last Tuesday · /
  );
});

test('the none headline points to Generation', () => {
  const none = exportAudioNote(
    { status: 'none', version: null, candidates: [] },
    runs
  );
  assert.match(none.headline, /Assemble the current recordings in Generation/);
});
