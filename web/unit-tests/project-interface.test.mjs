import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath, URL } from 'node:url';
import { build, transformSync } from 'esbuild';
import { compileModule } from 'svelte/compiler';

const filename = 'project-interface.svelte.ts';
const source = readFileSync(
  new URL(`../src/lib/${filename}`, import.meta.url),
  'utf8'
);
const compiled = compileModule(transformSync(source, { loader: 'ts' }).code, {
  filename,
  generate: 'client'
}).js.code;
const bundle = await build({
  stdin: {
    contents: compiled,
    resolveDir: fileURLToPath(new URL('../src/lib', import.meta.url)),
    sourcefile: filename
  },
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'browser',
  plugins: [
    {
      name: 'project-interface-api',
      setup(builder) {
        builder.onResolve({ filter: /^\.\/(domain-api|errors)$/ }, (args) => ({
          path: args.path,
          namespace: 'interface-mock'
        }));
        builder.onLoad(
          { filter: /.*/, namespace: 'interface-mock' },
          (args) => ({
            contents: args.path.endsWith('errors')
              ? 'export const errorMessage = error => error.message;'
              : 'export const sessionApi = { update: (...args) => globalThis.interfaceFixture.update(...args) };',
            loader: 'js'
          })
        );
      }
    }
  ]
});
const { ProjectInterface } = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
};
function fixture(mode) {
  let record = {
    id: 'project',
    revision: 7,
    ...(mode ? { interface_mode: mode } : {})
  };
  const writes = [];
  let reloads = 0;
  globalThis.interfaceFixture = {
    update: async (id, revision, body) => {
      writes.push({ id, revision, body });
      record = { ...record, ...body, revision: revision + 1 };
    }
  };
  return {
    state: new ProjectInterface(
      () => record,
      async () => {
        reloads++;
      }
    ),
    writes,
    reloads: () => reloads,
    record: () => record,
    navigate: () => {
      record = { id: 'other', revision: 2, interface_mode: 'advanced' };
    }
  };
}

test('legacy preference stays Advanced and an unchanged selection performs no write', async () => {
  const f = fixture();
  assert.equal(f.state.mode, 'advanced');
  await f.state.change('advanced');
  assert.deepEqual(f.writes, []);
});

test('switch waits for queued wording, patches only the preference with its revision, and suppresses duplicate clicks', async () => {
  const f = fixture('advanced'),
    gate = deferred();
  f.state.guard(async () => {
    await gate.promise;
    return true;
  });
  const switching = f.state.change('simple');
  assert.equal(f.state.pending, true);
  assert.deepEqual(f.writes, []);
  await f.state.change('simple');
  gate.resolve();
  await switching;
  assert.deepEqual(f.writes, [
    { id: 'project', revision: 7, body: { interface_mode: 'simple' } }
  ]);
  assert.equal(f.reloads(), 1);
  assert.equal(f.state.mode, 'simple');
  assert.equal(f.state.pending, false);
});

test('an unfinished editor blocks simplification without changing the stored preference', async () => {
  const f = fixture('advanced');
  const release = f.state.guard(async () => false);
  await f.state.change('simple');
  assert.equal(f.state.mode, 'advanced');
  assert.deepEqual(f.writes, []);
  assert.match(f.state.error, /Finish or close/);
  release();
  await f.state.change('simple');
  assert.equal(f.state.mode, 'simple');
  assert.equal(f.state.error, '');
});

test('a failed preference save remains visible without discarding the current mode', async () => {
  const f = fixture('advanced');
  globalThis.interfaceFixture.update = async () => {
    throw new Error('Revision conflict');
  };
  await f.state.change('simple');
  assert.equal(f.state.mode, 'advanced');
  assert.equal(f.state.error, 'Revision conflict');
  assert.equal(f.state.pending, false);
  assert.equal(f.reloads(), 0);
});

test('navigation during an editor guard does not apply the old choice to the newly opened project', async () => {
  const f = fixture('advanced'),
    gate = deferred();
  f.state.guard(async () => {
    await gate.promise;
    return true;
  });
  const switching = f.state.change('simple');
  f.navigate();
  gate.resolve();
  await switching;
  assert.deepEqual(f.writes, []);
  assert.equal(f.record().id, 'other');
  assert.equal(f.state.mode, 'advanced');
});
