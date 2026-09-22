// Race/error coverage for web/src/lib/inflight-dedupe.ts.
// Run: node unit-tests/inflight-dedupe.test.mjs (from web/).
// No ports, no server, no build. Fails on any unhandled rejection.
/*global process, console, setTimeout */
import assert from 'node:assert/strict';
import { dedupeInflight } from '../src/lib/inflight-dedupe.ts';

const unhandled = [];
process.on('unhandledRejection', (reason) => {
  unhandled.push(reason);
});

const tick = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function testConcurrentSharers() {
  const slots = new Map();
  let starts = 0;
  const start = async () => {
    starts += 1;
    await tick(20);
    return 'shared-value';
  };
  const [a, b, c] = await Promise.all([
    dedupeInflight(slots, 'k', start),
    dedupeInflight(slots, 'k', start),
    dedupeInflight(slots, 'k', start)
  ]);
  assert.equal(starts, 1);
  assert.deepEqual([a, b, c], ['shared-value', 'shared-value', 'shared-value']);
  assert.equal(slots.size, 0);
}

async function testSeparateKeys() {
  const slots = new Map();
  let starts = 0;
  const start = async () => {
    starts += 1;
    return starts;
  };
  const [a, b] = await Promise.all([
    dedupeInflight(slots, 'k1', start),
    dedupeInflight(slots, 'k2', start)
  ]);
  assert.equal(starts, 2);
  assert.deepEqual([a, b].sort(), [1, 2]);
}

async function testSequentialRefetch() {
  const slots = new Map();
  let starts = 0;
  const start = async () => {
    starts += 1;
    return `v${starts}`;
  };
  assert.equal(await dedupeInflight(slots, 'k', start), 'v1');
  assert.equal(await dedupeInflight(slots, 'k', start), 'v2');
  assert.equal(starts, 2);
}

async function testForceBypassesInflight() {
  const slots = new Map();
  const events = [];
  const slow = async () => {
    events.push('slow-start');
    await tick(60);
    events.push('slow-end');
    return 'slow';
  };
  const fast = async () => {
    events.push('fast-start');
    await tick(5);
    events.push('fast-end');
    return 'fast';
  };
  const slowPromise = dedupeInflight(slots, 'k', slow);
  const fastPromise = dedupeInflight(slots, 'k', fast, true);
  // A non-forced caller arriving now shares the forced request.
  const sharerPromise = dedupeInflight(slots, 'k', slow);
  const [slowValue, fastValue, sharerValue] = await Promise.all([
    slowPromise,
    fastPromise,
    sharerPromise
  ]);
  assert.equal(slowValue, 'slow');
  assert.equal(fastValue, 'fast');
  assert.equal(sharerValue, 'fast');
  // Late slow settler must not break the slot: the next call refetches.
  let restarts = 0;
  const restart = async () => {
    restarts += 1;
    return 'fresh';
  };
  assert.equal(await dedupeInflight(slots, 'k', restart), 'fresh');
  assert.equal(restarts, 1);
  assert.equal(slots.size, 0);
}

async function testRejectionSharedAndSlotCleared() {
  const slots = new Map();
  let starts = 0;
  const fail = async () => {
    starts += 1;
    await tick(10);
    throw new Error(`boom-${starts}`);
  };
  const results = await Promise.allSettled([
    dedupeInflight(slots, 'k', fail),
    dedupeInflight(slots, 'k', fail)
  ]);
  assert.equal(starts, 1);
  assert.ok(results.every((r) => r.status === 'rejected'));
  assert.match(results[0].reason.message, /boom-1/);
  assert.equal(slots.size, 0);
  // Failure clears the slot: the next caller retries.
  let retries = 0;
  const retry = async () => {
    retries += 1;
    return 'recovered';
  };
  assert.equal(await dedupeInflight(slots, 'k', retry), 'recovered');
  assert.equal(retries, 1);
}

async function testForcedRejectionDoesNotClobberNewerSlot() {
  const slots = new Map();
  const slowFail = async () => {
    await tick(50);
    throw new Error('slow-fail');
  };
  const fastOk = async () => {
    await tick(5);
    return 'fast-ok';
  };
  const slowPromise = dedupeInflight(slots, 'k', slowFail);
  const fastPromise = dedupeInflight(slots, 'k', fastOk, true);
  assert.equal(await fastPromise, 'fast-ok');
  await assert.rejects(slowPromise, /slow-fail/);
  let restarts = 0;
  assert.equal(
    await dedupeInflight(slots, 'k', async () => {
      restarts += 1;
      return 'fresh';
    }),
    'fresh'
  );
  assert.equal(restarts, 1);
}

await testConcurrentSharers();
await testSeparateKeys();
await testSequentialRefetch();
await testForceBypassesInflight();
await testRejectionSharedAndSlotCleared();
await testForcedRejectionDoesNotClobberNewerSlot();
await tick(20);
assert.deepEqual(unhandled, []);
console.log('inflight-dedupe: 6 race/error cases passed, no unhandled rejections');
