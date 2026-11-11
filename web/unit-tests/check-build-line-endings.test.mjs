import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  badTrackedWorkingTreeEols,
  generatedCrLfAssets,
  trackedEolProblems
} from '../scripts/check-build-line-endings.mjs';

function withScratch(run) {
  const root = mkdtempSync(join(tmpdir(), 'pandrator-lf-test-'));
  try {
    return run(root);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

test('detects both mixed and CRLF working tree files without rejecting LF or binary', () => {
  const listing =
    [
      'i/lf    w/lf    attr/text=auto eol=lf\tweb/clean.ts',
      'i/lf    w/mixed attr/text=auto eol=lf\tweb/mixed.ts',
      'i/lf    w/crlf  attr/text=auto eol=lf\tweb/crlf.ts',
      'i/-text w/-text attr/text=auto eol=lf\tweb/image.png'
    ].join('\0') + '\0';
  assert.deepEqual(badTrackedWorkingTreeEols(listing), [
    'web/mixed.ts',
    'web/crlf.ts'
  ]);
  assert.deepEqual(badTrackedWorkingTreeEols(''), []);
});

test('rejects a malformed git listing rather than reporting a clean checkout', () => {
  assert.throws(
    () => badTrackedWorkingTreeEols('malformed\0'),
    /Unexpected git ls-files/
  );
});

test('a broken git command is fatal for a checkout, but a source archive can omit git', () => {
  withScratch((root) => {
    const failingGit = () => {
      throw new Error('git failed to inspect the checkout');
    };
    assert.deepEqual(
      trackedEolProblems(root, ['web'], { git: failingGit }),
      []
    );
    mkdirSync(join(root, '.git'));
    assert.throws(
      () => trackedEolProblems(root, ['web'], { git: failingGit }),
      /git failed/
    );
  });
});

test('checks generated text assets, including untracked files, without probing binaries', () => {
  withScratch((root) => {
    mkdirSync(join(root, '_app'));
    writeFileSync(join(root, '_app', 'clean.js'), 'const a=1;\n');
    writeFileSync(join(root, '_app', 'broken.js'), 'const a=1;\r\n');
    writeFileSync(join(root, '_app', 'image.png'), Buffer.from([0, 13, 10, 4]));
    assert.deepEqual(generatedCrLfAssets(root), [join('_app', 'broken.js')]);
  });
});
