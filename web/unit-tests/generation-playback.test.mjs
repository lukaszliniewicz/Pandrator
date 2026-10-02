import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath, URL } from 'node:url';
import { build, transformSync } from 'esbuild';
import { compileModule } from 'svelte/compiler';

// Exercise the real rune module after the same TS/Svelte compilation as the
// client. Audio is deterministic here so cancellation can happen in one turn.
const filename = 'generation-playback.svelte.ts';
const source = readFileSync(
  new URL(`../src/lib/${filename}`, import.meta.url),
  'utf8'
);
const javascript = transformSync(source, { loader: 'ts' }).code;
const compiled = compileModule(javascript, {
  filename,
  generate: 'client'
}).js.code;
const bundle = await build({
  stdin: {
    contents: compiled,
    resolveDir: fileURLToPath(new URL('..', import.meta.url)),
    sourcefile: filename
  },
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'browser'
});
const { GenerationPlaybackController } = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);

function setup() {
  const previousAudio = globalThis.Audio;
  const audios = [];
  globalThis.Audio = class {
    paused = false;
    constructor(src) {
      this.src = src;
      audios.push(this);
    }
    play() {
      this.paused = false;
      return Promise.resolve();
    }
    pause() {
      this.paused = true;
    }
  };
  const items = [{ id: 'first' }, { id: 'second' }];
  const controller = new GenerationPlaybackController({
    getItems: () => items,
    getNextCursor: () => null,
    loadMore: async () => {},
    getTake: (item) => ({ artifact_id: item.id }),
    onSelect: () => {},
    onError: () => {}
  });
  return {
    items,
    audios,
    controller,
    restore: () => {
      controller.stop();
      if (previousAudio === undefined) delete globalThis.Audio;
      else globalThis.Audio = previousAudio;
    }
  };
}

test('a canceled attempt cannot drop replacement playback handles', async (t) => {
  const { items, audios, controller, restore } = setup();
  t.after(restore);
  const first = controller.playOnly(items[0]);
  const second = controller.playOnly(items[1]);
  await first;
  assert.equal(audios[0].paused, true);
  assert.equal(controller.activePlayingId, 'second');
  controller.togglePause();
  assert.equal(audios[1].paused, true);
  controller.togglePause();
  assert.equal(audios[1].paused, false);
  controller.stop();
  assert.equal(audios[1].paused, true);
  await second;
  assert.equal(controller.active, false);
});

test('stopping a superseded playlist still stops its replacement', async (t) => {
  const { items, audios, controller, restore } = setup();
  t.after(restore);
  const playlist = controller.playFrom('first');
  const replacement = controller.playOnly(items[1]);
  await playlist;
  assert.equal(audios.length, 2);
  controller.stop();
  assert.equal(audios[1].paused, true);
  await replacement;
});

test('ordinary completion and media errors release playback', async (t) => {
  const { items, audios, controller, restore } = setup();
  t.after(restore);
  const first = controller.playOnly(items[0]);
  audios[0].onended();
  await first;
  assert.equal(controller.active, false);
  const second = controller.playOnly(items[1]);
  audios[1].onerror();
  await second;
  assert.equal(controller.active, false);
  assert.equal(controller.activePlayingId, '');
});
