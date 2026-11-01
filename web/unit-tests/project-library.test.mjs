import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { test } from 'node:test';
import { build } from 'esbuild';

const bundle = await build({
  entryPoints: ['src/lib/project-library.ts'],
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'browser'
});
const {
  libraryProjects,
  folderMembershipSourceId,
  versionLanguageLabel,
  projectActivity,
  projectActivityStatus,
  folderDescendants,
  folderPath
} = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);

test('project identity survives a hidden source and keeps unrelated projects with the same name separate', () => {
  const membership = {
    id: 'aggregate',
    source_session_id: 'source',
    name: 'Course session'
  };
  const branch = {
    id: 'japanese',
    name: 'Old branch title',
    source_language: 'en',
    target_language: 'ja',
    translation_project: { ...membership, role: 'branch' }
  };
  const standalone = {
    id: 'other',
    name: 'Course session',
    source_language: 'en'
  };
  const groups = libraryProjects([branch, standalone]);
  assert.equal(groups.length, 2);
  assert.equal(groups[0].id, 'source');
  assert.equal(groups[0].name, 'Course session');
  assert.equal(groups[0].source, undefined);
  const restored = libraryProjects([
    branch,
    {
      id: 'source',
      name: 'Old source title',
      source_language: 'en',
      target_language: 'de',
      translation_project: { ...membership, role: 'source' }
    }
  ])[0];
  assert.deepEqual(
    restored.versions.map((item) => item.id),
    ['source', 'japanese']
  );
  assert.equal(versionLanguageLabel(restored.source), 'German');
});

test('language activity reflects a running job instead of an idle session', () => {
  const item = { id: 'japanese', status: 'idle' };
  const jobs = [
    { session_id: 'other', status: 'running' },
    {
      session_id: 'japanese',
      status: 'running',
      kind: 'tts.generate',
      progress_detail: 'Recording passage 3 of 8'
    }
  ];
  assert.equal(projectActivityStatus(item, jobs), 'running');
  assert.equal(projectActivity(item, jobs), 'Recording passage 3 of 8');
  const waiting = {
    ...jobs[1],
    status: 'queued',
    error_code: 'storage_pressure'
  };
  assert.equal(projectActivityStatus(item, [waiting]), 'queued');
  assert.equal(projectActivity(item, [waiting]), 'Waiting for storage');
  assert.equal(
    projectActivity(item, [{ ...waiting, status: 'running' }]),
    'Recording passage 3 of 8'
  );
  assert.equal(projectActivity(item, []), 'idle');
});

test('folder relationships use identities even when names contain path separators, and tolerate malformed cycles', () => {
  const folders = [
    { id: 'root', name: 'Course / archive', parent_id: null },
    { id: 'child', name: 'Sessions', parent_id: 'root' },
    { id: 'unrelated', name: 'Course / archive / Sessions', parent_id: null }
  ];
  assert.equal(folderPath(folders[1], folders), 'Course / archive / Sessions');
  assert.deepEqual([...folderDescendants('root', folders)], ['root', 'child']);
  const cycle = [
    { id: 'a', name: 'A', parent_id: 'b' },
    { id: 'b', name: 'B', parent_id: 'a' }
  ];
  assert.equal(folderPath(cycle[0], cycle), 'B / A');
  assert.deepEqual([...folderDescendants('a', cycle)], ['a', 'b']);
});

test('generic project identity and source navigation remain separate', () => {
  const membership = {
    id: 'translation-aggregate',
    source_session_id: 'source',
    name: 'Shared course'
  };
  const groups = libraryProjects([
    {
      id: 'branch',
      project_id: 'generic-project',
      name: 'German',
      source_language: 'en',
      target_language: 'de',
      translation_project: { ...membership, role: 'branch' }
    },
    {
      id: 'source',
      project_id: 'generic-project',
      name: 'Source',
      source_language: 'en',
      translation_project: { ...membership, role: 'source' }
    },
    {
      id: 'other',
      project_id: 'other-project',
      name: 'Shared course',
      source_language: 'en'
    }
  ]);
  assert.equal(groups.length, 2);
  assert.equal(groups[0].projectId, 'generic-project');
  assert.equal(groups[0].id, 'source');
  assert.equal(groups[0].sourceId, 'source');
  assert.deepEqual(
    groups[0].versions.map((item) => item.id),
    ['source', 'branch']
  );
  assert.equal(groups[1].projectId, 'other-project');
  assert.equal(
    folderMembershipSourceId({
      project_id: 'legacy',
      source_session_id: 'source',
      project_root_id: 'root'
    }),
    'source'
  );
  assert.equal(folderMembershipSourceId({ project_id: 'legacy' }), 'legacy');
});
