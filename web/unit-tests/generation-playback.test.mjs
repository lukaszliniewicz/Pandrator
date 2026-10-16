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

function setup(sourceItems = [{ id: 'first' }, { id: 'second' }]) {
  const previousAudio = globalThis.Audio;
  const audios = [];
  globalThis.Audio = class {
    paused = false;
    currentTime = 0;
    plays = 0;
    constructor(src) {
      this.src = src;
      audios.push(this);
    }
    play() {
      this.paused = false;
      this.plays += 1;
      return Promise.resolve();
    }
    pause() {
      this.paused = true;
    }
  };
  const items = sourceItems;
  const controller = new GenerationPlaybackController({
    getItems: () => items,
    getNextCursor: () => null,
    loadMore: async () => {},
    getTake: (item) =>
      item.missing
        ? undefined
        : { artifact_id: item.id, assembly_silences: item.assembly_silences },
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

test('prepending cached segments does not replay the current recording', async (t) => {
  const { items, audios, controller, restore } = setup();
  t.after(restore);
  const playing = controller.playFrom('first');
  items.unshift({ id: 'earlier' });
  audios[0].onended();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(audios.length, 2);
  assert.ok(audios[1].src.includes('/second/'));
  controller.stop();
  await playing;
});

function fakeClock(t) {
  const oldWindow = globalThis.window;
  const oldPerformance = Object.getOwnPropertyDescriptor(
    globalThis,
    'performance'
  );
  let now = 0;
  let next = 0;
  const timers = new Map();
  globalThis.window = {
    setTimeout(callback, delay) {
      const id = ++next;
      timers.set(id, { callback, due: now + delay });
      return id;
    },
    clearTimeout(id) {
      timers.delete(id);
    }
  };
  Object.defineProperty(globalThis, 'performance', {
    configurable: true,
    value: { now: () => now }
  });
  t.after(() => {
    if (oldWindow === undefined) delete globalThis.window;
    else globalThis.window = oldWindow;
    if (oldPerformance)
      Object.defineProperty(globalThis, 'performance', oldPerformance);
  });
  return {
    timers,
    async tick(milliseconds) {
      now += milliseconds;
      for (const [id, timer] of [...timers]) {
        if (timer.due <= now) {
          timers.delete(id);
          timer.callback();
        }
      }
      await new Promise((resolve) => setImmediate(resolve));
    }
  };
}

test('playlist waits only between playable recordings and uses the assembly pause', async (t) => {
  const clock = fakeClock(t);
  const { audios, controller, restore } = setup([
    { id: 'first', silence_after_ms: 700, assembly_silence_after_ms: 250 },
    { id: 'missing', missing: true },
    { id: 'removed', removed: true },
    { id: 'last', silence_after_ms: 700 }
  ]);
  t.after(restore);
  const playing = controller.playFrom('first');
  await clock.tick(0);
  audios[0].onended();
  await clock.tick(0);
  assert.equal(audios.length, 1);
  await clock.tick(249);
  assert.equal(audios.length, 1);
  await clock.tick(1);
  assert.equal(audios.length, 2);
  assert.match(audios[1].src, /\/last\//);
  audios[1].onended();
  await playing;
  assert.equal(controller.active, false);
  assert.equal(clock.timers.size, 0);
});

test('one-recording audition adds no internal or trailing assembly silence', async (t) => {
  const clock = fakeClock(t);
  const { items, audios, controller, restore } = setup([
    {
      id: 'first',
      silence_after_ms: 700,
      assembly_silences: [{ at_ms: 100, duration_ms: 100 }]
    }
  ]);
  t.after(restore);
  const playing = controller.playOnly(items[0]);
  await clock.tick(0);
  audios[0].currentTime = 0.1;
  audios[0].ontimeupdate();
  assert.equal(audios[0].paused, false);
  audios[0].onended();
  await playing;
  assert.equal(clock.timers.size, 0);
});

test('playlist internal voice gap respects pause and stop', async (t) => {
  const clock = fakeClock(t);
  const { audios, controller, restore } = setup([
    { id: 'first', assembly_silences: [{ at_ms: 100, duration_ms: 100 }] }
  ]);
  t.after(restore);
  const playing = controller.playFrom('first');
  await clock.tick(0);
  audios[0].currentTime = 0.1;
  audios[0].ontimeupdate();
  assert.equal(audios[0].paused, true);
  controller.togglePause();
  await clock.tick(100);
  assert.equal(audios[0].plays, 1);
  controller.togglePause();
  assert.equal(audios[0].plays, 1);
  await clock.tick(100);
  assert.equal(audios[0].plays, 2);
  controller.stop();
  await playing;
  await clock.tick(1000);
  assert.equal(audios[0].paused, true);
  assert.equal(audios[0].plays, 2);
});

test('timed workflow zero overrides stored audiobook pause', async (t) => {
  const clock = fakeClock(t);
  const { audios, controller, restore } = setup([
    { id: 'first', silence_after_ms: 700, assembly_silence_after_ms: 0 },
    { id: 'second' }
  ]);
  t.after(restore);
  const playing = controller.playFrom('first');
  await clock.tick(0);
  audios[0].onended();
  await clock.tick(0);
  assert.equal(audios.length, 2);
  controller.stop();
  await playing;
});
