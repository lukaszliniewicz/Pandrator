import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { after, beforeEach, test } from 'node:test';
import { build } from 'esbuild';

const bundle = await build({
  entryPoints: ['src/lib/project-link-context.ts'],
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'browser'
});
const context = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);
const saved = new Map();
const original = {
  window: globalThis.window,
  location: globalThis.location,
  sessionStorage: globalThis.sessionStorage,
  BroadcastChannel: globalThis.BroadcastChannel
};
Object.assign(globalThis, {
  window: {},
  location: new URL(
    'https://pandrator.example/sessions/22222222-2222-4222-8222-222222222222/text'
  ),
  sessionStorage: {
    getItem: (key) => saved.get(key) ?? null,
    setItem: (key, value) => saved.set(key, value),
    removeItem: (key) => saved.delete(key)
  },
  BroadcastChannel: undefined
});
const selected = {
  id: '11111111-1111-4111-8111-111111111111',
  csrf: 'guest-csrf-not-an-authentication-cookie',
  mode: 'manage_project',
  projectId: 'project',
  rootSessionId: 'root',
  label: 'Temporary collaboration'
};
beforeEach(() => {
  context.clearProjectLink();
  saved.clear();
});
after(() => {
  context.clearProjectLink();
  Object.assign(globalThis, original);
});

test('normal requests and native delivery are unchanged without a selected link', () => {
  const headers = new Headers({ 'X-CSRF-Token': 'owner-csrf' });
  context.projectLinkHeaders(headers, true);
  assert.equal(headers.get('X-CSRF-Token'), 'owner-csrf');
  assert.equal(headers.has('X-Pandrator-Project-Link'), false);
  assert.equal(
    context.projectLinkUrl('/api/v1/artifacts/file/content?v=hash'),
    '/api/v1/artifacts/file/content?v=hash'
  );
  assert.equal(saved.size, 0);
});
test('guest writes use their own CSRF and an explicit selector without bearer credentials', () => {
  context.selectProjectLink(selected);
  const headers = new Headers({ 'X-CSRF-Token': 'owner-csrf' });
  context.projectLinkHeaders(headers, true);
  assert.equal(headers.get('X-CSRF-Token'), selected.csrf);
  assert.equal(headers.get('X-Pandrator-Project-Link'), selected.id);
  assert.equal(headers.has('Authorization'), false);
  assert.deepEqual(context.activeProjectLink(), selected);
});
test('native URLs carry a nonsecret selector and preserve version, download and fragment values', () => {
  context.selectProjectLink(selected);
  const value = context.projectLinkUrl(
    '/api/v1/artifacts/file/content?v=hash&download=1#page=2'
  );
  const url = new URL(value, globalThis.location.origin);
  assert.equal(url.searchParams.get('project_link_id'), selected.id);
  assert.equal(url.searchParams.get('v'), 'hash');
  assert.equal(url.searchParams.get('download'), '1');
  assert.equal(url.hash, '#page=2');
  assert.equal(value.includes(selected.csrf), false);
  assert.equal(
    context.projectLinkUrl(
      'https://other.example/api/v1/artifacts/file/content'
    ),
    'https://other.example/api/v1/artifacts/file/content'
  );
  assert.equal(
    context.projectLinkUrl('/_app/immutable/file.js'),
    '/_app/immutable/file.js'
  );
});
test('processing choices use the current project language session rather than a workspace catalogue', () => {
  context.selectProjectLink(selected);
  const value = context.projectLinkApiPath(
    '/api/v1/services/tts?workspace_id=private-workspace&view=compact'
  );
  const url = new URL(value, globalThis.location.origin);
  assert.equal(
    url.pathname,
    `/api/v1/public-links/${selected.id}/services/tts`
  );
  assert.equal(url.searchParams.has('workspace_id'), false);
  assert.equal(
    url.searchParams.get('session_id'),
    '22222222-2222-4222-8222-222222222222'
  );
  assert.equal(url.searchParams.get('view'), 'compact');
  assert.equal(context.projectLinkApiPath('/api/v1/voices'), '/api/v1/voices');
});
test('leaving a link removes its tab selector without altering normal request credentials', () => {
  context.selectProjectLink(selected);
  context.clearProjectLink();
  const headers = new Headers({ 'X-CSRF-Token': 'owner-csrf' });
  context.projectLinkHeaders(headers, true);
  assert.equal(context.activeProjectLink(), null);
  assert.equal(saved.size, 0);
  assert.equal(headers.get('X-CSRF-Token'), 'owner-csrf');
  assert.equal(headers.has('X-Pandrator-Project-Link'), false);
});
