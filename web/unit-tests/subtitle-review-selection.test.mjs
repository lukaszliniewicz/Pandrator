import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { test } from 'node:test';
import { build } from 'esbuild';

const bundle = await build({
  entryPoints: ['src/lib/subtitle-review-selection.ts'],
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'node'
});
const { selectedDisplayArtifact } = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);

function document(stage, name, createdAt) {
  return {
    id: `document-${name}`,
    stage,
    active_revision_id: `revision-${name}`,
    revisions: [
      {
        id: `revision-${name}`,
        revision_number: 1,
        created_at: createdAt,
        artifact: { id: name, state: 'current' }
      }
    ]
  };
}
const documents = [
  document('translation', 'older', '2026-10-12T10:00:00Z'),
  document('translation', 'newer', '2026-10-13T10:00:00Z'),
  document('tts_optimization', 'speech', '2026-10-13T11:00:00Z')
];

test('selected artifact wins across documents with equal revision numbers, including an older deliberate selection', () => {
  for (const selected of ['older', 'newer']) {
    assert.equal(
      selectedDisplayArtifact(documents, {
        stages: [{ key: 'translate', selected_artifact_id: selected }]
      }),
      selected
    );
  }
});

test('fallback uses an active written revision and excludes speech optimization and inactive revisions', () => {
  const inactive = document('translation', 'inactive', '2026-10-13T12:00:00Z');
  inactive.active_revision_id = 'another-revision';
  assert.equal(
    selectedDisplayArtifact([...documents, inactive], { stages: [] }),
    'newer'
  );
});

test('deleted selected artifacts are excluded and an adopted subtitle source can be used', () => {
  const deleted = document('translation', 'deleted', '2026-10-13T12:00:00Z');
  deleted.revisions[0].artifact.state = 'deleted';
  assert.equal(
    selectedDisplayArtifact([...documents, deleted], {
      stages: [{ key: 'translate', selected_artifact_id: 'deleted' }]
    }),
    'newer'
  );
  assert.equal(
    selectedDisplayArtifact([document('transcription', 'imported', '')], {
      stages: [],
      subtitle_source: { subtitle_artifact_id: 'imported' }
    }),
    'imported'
  );
});
