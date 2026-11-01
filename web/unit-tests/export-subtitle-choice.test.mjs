import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { test } from 'node:test';
import { build } from 'esbuild';

const bundle = await build({
  entryPoints: ['src/lib/export-intent.ts'],
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'node'
});
const { selectedSubtitleTracks, subtitleSelectionIssue, exportIntent } =
  await import(
    `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
  );
const source = { artifact_id: 'source', role: 'correction' };
const translation = { artifact_id: 'translated', role: 'translation' };
const context = (...tracks) => ({ subtitle_tracks: tracks });

test('named choices never display another document as their contents', () => {
  assert.deepEqual(selectedSubtitleTracks(context(source), 'translation'), []);
  assert.deepEqual(selectedSubtitleTracks(context(translation), 'source'), []);
  assert.match(
    subtitleSelectionIssue(context(source), 'translation'),
    /translation|translated/
  );
  assert.match(
    subtitleSelectionIssue(context(source), 'dual'),
    /translation|translated/
  );
  assert.match(subtitleSelectionIssue(context(translation), 'dual'), /source/);
  assert.equal(
    subtitleSelectionIssue(context(source, translation), 'dual'),
    ''
  );
});

test('automatic selection shows every available document in source order', () => {
  for (const tracks of [[source], [translation], [source, translation]]) {
    assert.deepEqual(
      selectedSubtitleTracks(context(...tracks), 'auto'),
      tracks
    );
    assert.deepEqual(
      selectedSubtitleTracks(context(...tracks), undefined),
      tracks
    );
    assert.equal(subtitleSelectionIssue(context(...tracks), 'auto'), '');
  }
  assert.match(
    subtitleSelectionIssue(context(), 'auto'),
    /No subtitle document/
  );
});

test('attached SRT is a source and preferred corrected source is not duplicated', () => {
  const attached = {
    source_artifact_id: 'upload',
    source_name: 'captions.SRT'
  };
  assert.equal(
    selectedSubtitleTracks(attached, 'source')[0].artifact_id,
    'upload'
  );
  assert.deepEqual(
    selectedSubtitleTracks({ ...attached, ...context(source) }, 'auto'),
    [source]
  );
});

test('caption requirements follow the export target instead of incidental settings', () => {
  for (const export_mode of ['audio', 'media']) {
    const intent = exportIntent(
      'voiceover',
      { export_mode, subtitle_mode: 'none' },
      true,
      true
    );
    assert.equal(intent.includesSubtitles, false);
  }
  for (const subtitle_mode of ['soft', 'burned']) {
    assert.equal(
      exportIntent('voiceover', { subtitle_mode }, true, true)
        .includesSubtitles,
      true
    );
    assert.equal(
      exportIntent('voiceover', { subtitle_mode }, false, true)
        .includesSubtitles,
      false
    );
  }
  assert.equal(
    exportIntent('subtitles', { export_mode: 'text' }, false, false)
      .includesSubtitles,
    true
  );
});
