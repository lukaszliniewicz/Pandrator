// Delayed-response coverage for web/src/lib/speech-plan-history-detail.ts.
// Run: node unit-tests/speech-plan-history-detail.test.mjs (from web/).
// No ports, no server, no build. Fails on any unhandled rejection.
/*global process, console, setTimeout */
import assert from 'node:assert/strict';
import {
  createRevisionDetailLoader,
  historyEntryKey,
  mergeRevisionDetail
} from '../src/lib/speech-plan-history-detail.ts';

const unhandled = [];
process.on('unhandledRejection', (reason) => {
  unhandled.push(reason);
});

const tick = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function summaryRow(id, entryId, batch) {
  return {
    id,
    entry_id: entryId,
    revision_number: 3,
    summary: 'Automatic timing repair · 1 accepted / 2 attempted',
    origin: 'automatic',
    segment_count: 4,
    active_segment_count: 4,
    reusable_segment_count: null,
    stale_segment_count: null,
    audio_reuse_checked: false,
    repair_batch: batch ?? null
  };
}

function store(items) {
  const state = {
    items,
    loading: false,
    error: '',
    activeRevisionId: 'active-0'
  };
  const sink = {
    getItems: () => state.items,
    setItems: (next) => {
      state.items = next;
    },
    setLoading: (value) => {
      state.loading = value;
    },
    setError: (message) => {
      state.error = message;
    },
    setActiveRevisionId: (id) => {
      state.activeRevisionId = id;
    }
  };
  return { state, sink };
}

const fullAudio = {
  segment_count: 4,
  active_segment_count: 4,
  reusable_segment_count: 1,
  stale_segment_count: 3,
  audio_settings_stale_segment_count: 2,
  audio_identity_unknown_segment_count: 1
};

async function testSlowRowInvalidatedByCachedRow() {
  // Pending slow rowA, then select already-checked rowB: B's early return
  // must still bump the ticket so A's late response is dropped entirely.
  const rowA = summaryRow('rev-a', 'entry-a');
  const rowB = {
    ...summaryRow('rev-b', 'entry-b'),
    reusable_segment_count: 2,
    stale_segment_count: 2,
    audio_reuse_checked: true
  };
  const { state, sink } = store([rowA, rowB]);
  const gateA = deferred();
  const loader = createRevisionDetailLoader(
    sink,
    {
      fetchRevision: () => gateA.promise,
      fetchBatch: () => {
        throw new Error('must not fetch a batch here');
      }
    },
    (caught) => String(caught && caught.message ? caught.message : caught)
  );
  const pending = loader.load({
    entryKey: 'entry-a',
    revisionId: 'rev-a',
    needsAudio: true,
    needsBatch: false,
    batchId: null
  });
  assert.equal(state.loading, true);
  // Select the cached row while A is still in flight.
  await loader.load({
    entryKey: 'entry-b',
    revisionId: 'rev-b',
    needsAudio: false,
    needsBatch: false,
    batchId: null
  });
  assert.equal(state.loading, false);
  assert.equal(state.error, '');
  // A's late success must not touch items, spinner, error, or active revision.
  gateA.resolve({ revision: fullAudio, active_revision_id: 'active-9' });
  await pending;
  await tick(0);
  assert.deepEqual(state.items, [rowA, rowB]);
  assert.equal(state.loading, false);
  assert.equal(state.error, '');
  assert.equal(state.activeRevisionId, 'active-0');
}

async function testSlowErrorInvalidatedByCachedRow() {
  const rowA = summaryRow('rev-a', 'entry-a');
  const rowB = {
    ...summaryRow('rev-b', 'entry-b'),
    reusable_segment_count: 0,
    stale_segment_count: 4,
    audio_reuse_checked: true
  };
  const { state, sink } = store([rowA, rowB]);
  const gateA = deferred();
  const loader = createRevisionDetailLoader(
    sink,
    {
      fetchRevision: () => gateA.promise,
      fetchBatch: () => {
        throw new Error('must not fetch a batch here');
      }
    },
    (caught) => `detail failed: ${caught.message}`
  );
  const pending = loader.load({
    entryKey: 'entry-a',
    revisionId: 'rev-a',
    needsAudio: true,
    needsBatch: false,
    batchId: null
  });
  await loader.load({
    entryKey: 'entry-b',
    revisionId: 'rev-b',
    needsAudio: false,
    needsBatch: false,
    batchId: null
  });
  gateA.reject(new Error('slow boom'));
  await pending;
  await tick(0);
  assert.equal(state.error, '');
  assert.equal(state.loading, false);
}

async function testBatchMergesOnlyIntoMatchingEntry() {
  // Two grouped entries share one revision id but belong to different repair
  // batches: counts merge into both rows, the authoritative batch only into
  // the selected entry.
  const sharedA = summaryRow('rev-shared', 'batch-entry-a', {
    id: 'batch-a',
    undo_checked: false,
    can_undo: null
  });
  const sharedB = summaryRow('rev-shared', 'batch-entry-b', {
    id: 'batch-b',
    undo_checked: true,
    can_undo: false
  });
  const { state, sink } = store([sharedA, sharedB]);
  const loader = createRevisionDetailLoader(
    sink,
    {
      fetchRevision: async () => ({
        revision: fullAudio,
        active_revision_id: 'active-1'
      }),
      fetchBatch: async (batchId) => {
        assert.equal(batchId, 'batch-a');
        await tick(10);
        return {
          repair_batch: { id: 'batch-a', undo_checked: true, can_undo: true }
        };
      }
    },
    (caught) => String(caught)
  );
  await loader.load({
    entryKey: 'batch-entry-a',
    revisionId: 'rev-shared',
    needsAudio: true,
    needsBatch: true,
    batchId: 'batch-a'
  });
  const [a, b] = state.items;
  // Counts merged into both rows sharing the revision id.
  for (const row of [a, b]) {
    assert.equal(row.reusable_segment_count, 1);
    assert.equal(row.stale_segment_count, 3);
    assert.equal(row.audio_reuse_checked, true);
    // Grouping decorations untouched.
    assert.equal(row.summary, sharedA.summary);
  }
  assert.equal(a.entry_id, 'batch-entry-a');
  assert.deepEqual(a.repair_batch, {
    id: 'batch-a',
    undo_checked: true,
    can_undo: true
  });
  // Sibling entry keeps its own (different) batch object untouched.
  assert.deepEqual(b.repair_batch, {
    id: 'batch-b',
    undo_checked: true,
    can_undo: false
  });
  assert.equal(b.entry_id, 'batch-entry-b');
  assert.equal(state.loading, false);
  assert.equal(state.error, '');
  assert.equal(state.activeRevisionId, 'active-1');
}

async function testAudioOnlyMergePreservesBatchAndDecorations() {
  const row = summaryRow('rev-x', 'entry-x', {
    id: 'batch-x',
    undo_checked: true,
    can_undo: false
  });
  const merged = mergeRevisionDetail(
    [row],
    'entry-x',
    'rev-x',
    fullAudio,
    null
  );
  assert.equal(merged.length, 1);
  const [next] = merged;
  assert.equal(next.reusable_segment_count, 1);
  assert.equal(next.audio_reuse_checked, true);
  assert.equal(next.entry_id, 'entry-x');
  assert.equal(next.summary, row.summary);
  assert.equal(next.revision_number, row.revision_number);
  // No batch payload: the existing eligibility object is preserved by identity.
  assert.equal(next.repair_batch, row.repair_batch);
}

async function testUnrelatedRevisionIdsUntouched() {
  const rows = [summaryRow('rev-1', 'entry-1'), summaryRow('rev-2', 'entry-2')];
  const merged = mergeRevisionDetail(rows, 'entry-1', 'rev-1', fullAudio, {
    id: 'batch-1'
  });
  assert.equal(merged[0].audio_reuse_checked, true);
  assert.deepEqual(merged[0].repair_batch, { id: 'batch-1' });
  assert.deepEqual(merged[1], rows[1]);
}

async function testEntryKeyFallsBackToId() {
  assert.equal(historyEntryKey({ id: 'r1' }), 'r1');
  assert.equal(historyEntryKey({ id: 'r1', entry_id: 'e1' }), 'e1');
}

await testSlowRowInvalidatedByCachedRow();
await testSlowErrorInvalidatedByCachedRow();
await testBatchMergesOnlyIntoMatchingEntry();
await testAudioOnlyMergePreservesBatchAndDecorations();
await testUnrelatedRevisionIdsUntouched();
await testEntryKeyFallsBackToId();
await tick(20);
assert.deepEqual(unhandled, []);
console.log(
  'speech-plan-history-detail: 6 delayed-response/merge cases passed, no unhandled rejections'
);
