import assert from 'node:assert/strict';
import { test } from 'node:test';
import { settledRefresh } from '../src/lib/settled-refresh.ts';

function setup(t) {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const calls = { run: 0, wait: 0 };
  const refresh = settledRefresh(() => calls.run++, {
    settleMs: 750,
    onwait: () => calls.wait++
  });
  return { calls, refresh };
}

test('a not-ready first change does nothing', (t) => {
  const { calls, refresh } = setup(t);
  refresh.change(false);
  t.mock.timers.tick(2000);
  assert.deepEqual(calls, { run: 0, wait: 0 });
});

test('the first ready change runs at once', (t) => {
  const { calls, refresh } = setup(t);
  refresh.change(true);
  assert.equal(calls.run, 1);
  assert.equal(calls.wait, 0);
});

test('a burst of changes runs once after the last one settles', (t) => {
  const { calls, refresh } = setup(t);
  refresh.change(true);
  refresh.change(true);
  t.mock.timers.tick(500);
  refresh.change(true);
  t.mock.timers.tick(500);
  refresh.change(true);
  assert.equal(calls.run, 1);
  assert.equal(calls.wait, 3);
  t.mock.timers.tick(749);
  assert.equal(calls.run, 1);
  t.mock.timers.tick(1);
  assert.equal(calls.run, 2);
});

test('closing the panel drops a pending run and reopening runs at once', (t) => {
  const { calls, refresh } = setup(t);
  refresh.change(true);
  refresh.change(true);
  refresh.change(false);
  t.mock.timers.tick(2000);
  assert.equal(calls.run, 1);
  refresh.change(true);
  assert.equal(calls.run, 2);
});

test('cancel drops a pending run', (t) => {
  const { calls, refresh } = setup(t);
  refresh.change(true);
  refresh.change(true);
  refresh.cancel();
  t.mock.timers.tick(2000);
  assert.equal(calls.run, 1);
});

test('now runs once and drops a pending run', (t) => {
  const { calls, refresh } = setup(t);
  refresh.change(true);
  refresh.change(true);
  refresh.now();
  assert.equal(calls.run, 2);
  t.mock.timers.tick(2000);
  assert.equal(calls.run, 2);
  refresh.now();
  assert.deepEqual(calls, { run: 3, wait: 1 });
});

test('closing the panel drops a pending settle', (t) => {
  const { calls, refresh } = setup(t);
  refresh.change(true);
  refresh.settle(250);
  refresh.change(false);
  t.mock.timers.tick(2000);
  assert.equal(calls.run, 1);
});

test('settle restarts on repeat and uses its own interval', (t) => {
  const { calls, refresh } = setup(t);
  refresh.settle(250);
  t.mock.timers.tick(200);
  refresh.settle(250);
  t.mock.timers.tick(200);
  assert.equal(calls.run, 0);
  t.mock.timers.tick(50);
  assert.equal(calls.run, 1);
  assert.equal(calls.wait, 2);
});
