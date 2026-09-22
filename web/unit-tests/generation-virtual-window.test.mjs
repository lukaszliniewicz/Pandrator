// Unit coverage for web/src/lib/generation-virtual-window.ts.
// Run: node unit-tests/generation-virtual-window.test.mjs (from web/).
// No ports, no server, no build.
/*global process, console */
import assert from 'node:assert/strict';
import {
  buildOffsets,
  findEndIndex,
  findStartIndex,
  selectVirtualWindow,
  slotHeights
} from '../src/lib/generation-virtual-window.ts';

function uniformOffsets(count, height) {
  return buildOffsets(new Array(count).fill(height));
}

function testOffsetsPrefixSums() {
  assert.deepEqual(buildOffsets([]), [0]);
  assert.deepEqual(buildOffsets([10, 20, 30]), [0, 10, 30, 60]);
  // Non-finite entries degrade to zero instead of poisoning the sums.
  assert.deepEqual(buildOffsets([50, Number.NaN, -4]), [0, 50, 50, 50]);
}

function testStartSearchMatchesLinear() {
  const offsets = buildOffsets([100, 200, 150, 300, 120]);
  const linear = (scrollTop) => {
    for (let i = 0; i < 5; i += 1) {
      if (offsets[i + 1] > scrollTop) return i;
    }
    return 4;
  };
  for (let scrollTop = -50; scrollTop < 800; scrollTop += 7) {
    assert.equal(findStartIndex(offsets, scrollTop), linear(scrollTop));
  }
  assert.equal(findStartIndex([0], 999), 0);
}

function testEndSearchMatchesLinear() {
  const offsets = buildOffsets([100, 200, 150, 300, 120]);
  const linear = (bottom) => {
    for (let i = 0; i < 5; i += 1) {
      if (offsets[i + 1] >= bottom) return i;
    }
    return 4;
  };
  for (let bottom = 0; bottom < 900; bottom += 11) {
    assert.equal(findEndIndex(offsets, bottom), linear(bottom));
  }
  assert.equal(findEndIndex([0], 999), -1);
}

function testTopWindowWithOverscan() {
  const offsets = uniformOffsets(600, 250);
  const selection = selectVirtualWindow(600, offsets, 0, 600, 8);
  // 600px viewport shows ~3 rows + 8 overscan each side.
  assert.deepEqual(selection.indexes.slice(0, 3), [0, 1, 2]);
  assert.ok(
    selection.indexes.length <= 20,
    `window ${selection.indexes.length}`
  );
  assert.equal(selection.topGap, 0);
  assert.equal(selection.gaps.size, 0);
  assert.equal(selection.bottomGap, 600 * 250 - selection.indexes.length * 250);
  assert.equal(selection.totalHeight, 600 * 250);
}

function testScrolledWindowStaysBounded() {
  const offsets = uniformOffsets(600, 250);
  const middle = selectVirtualWindow(600, offsets, 75000, 600, 8);
  assert.ok(middle.indexes[0] >= 280 && middle.indexes[0] <= 300);
  assert.ok(middle.indexes.length <= 22, `window ${middle.indexes.length}`);
  // Gaps plus rendered heights always reconcile to the total height.
  const rendered = middle.indexes.length * 250;
  const gaps = [...middle.gaps.values()].reduce((sum, gap) => sum + gap, 0);
  assert.equal(middle.topGap + rendered + gaps + middle.bottomGap, 600 * 250);
}

function testScrollLastRow() {
  const offsets = uniformOffsets(600, 250);
  const total = 600 * 250;
  const selection = selectVirtualWindow(600, offsets, total - 600, 600, 8);
  assert.ok(selection.indexes.includes(599), 'last row rendered');
  assert.ok(!selection.indexes.includes(0), 'first row virtualized away');
  assert.equal(selection.bottomGap, 0);
}

function testMixedHeightsAndResize() {
  // Tall annotated rows mixed with compact ones; a resize re-measure only
  // shifts offsets without breaking window reconciliation.
  const before = buildOffsets([120, 480, 140, 520, 130, 460]);
  const selection = selectVirtualWindow(6, before, 500, 300, 1);
  assert.ok(selection.indexes.includes(1), 'tall row at offset 500 visible');
  const after = buildOffsets([120, 300, 140, 520, 130, 460]);
  const reselected = selectVirtualWindow(6, after, 500, 300, 1);
  const rendered = reselected.indexes.reduce(
    (sum, index) => sum + after[index + 1] - after[index],
    0
  );
  const gaps = [...reselected.gaps.values()].reduce((sum, gap) => sum + gap, 0);
  assert.equal(
    reselected.topGap + rendered + gaps + reselected.bottomGap,
    after[6]
  );
}

function testPinnedRowsPreservedWithoutRangeBlowup() {
  const offsets = uniformOffsets(600, 250);
  // Editing row 2, focused row 3, playing row 5 while scrolled to row ~400.
  const selection = selectVirtualWindow(
    600,
    offsets,
    100000,
    600,
    8,
    [2, 3, 5]
  );
  for (const pin of [2, 3, 5]) {
    assert.ok(selection.indexes.includes(pin), `pinned ${pin} rendered`);
  }
  // Pinned rows ride along as isolated slots, not as a 400-row range.
  assert.ok(
    selection.indexes.length <= 30,
    `window ${selection.indexes.length}`
  );
  assert.ok(selection.gaps.has(selection.indexes.find((i) => i > 5)));
  // No duplicates even when a pin already sits inside the window.
  const nearTop = selectVirtualWindow(600, offsets, 0, 600, 8, [1, 2]);
  assert.equal(new Set(nearTop.indexes).size, nearTop.indexes.length);
}

function testPinnedIndexesClamped() {
  const offsets = uniformOffsets(10, 100);
  const selection = selectVirtualWindow(10, offsets, 0, 500, 2, [-1, 99, 2.5]);
  assert.ok(!selection.indexes.includes(-1));
  assert.ok(selection.indexes.every((index) => index >= 0 && index < 10));
}

function testEmptyList() {
  const selection = selectVirtualWindow(0, [0], 0, 600, 8, [0]);
  assert.deepEqual(selection.indexes, []);
  assert.equal(selection.totalHeight, 0);
}

function testLoadMoreAppendsWithoutDuplicates() {
  const first = uniformOffsets(100, 250);
  const before = selectVirtualWindow(100, first, 0, 600, 8);
  const extended = uniformOffsets(200, 250);
  const after = selectVirtualWindow(200, extended, 0, 600, 8);
  assert.deepEqual(after.indexes, before.indexes);
  assert.equal(new Set(after.indexes).size, after.indexes.length);
  // Scrolled to the old end, the window reaches into the appended page.
  const tail = selectVirtualWindow(200, extended, 100 * 250 - 600, 600, 8);
  assert.ok(tail.indexes.includes(99));
  assert.ok(tail.indexes.includes(100), 'appended rows reachable');
}

function testSlotHeightsFoldBoundary() {
  const heights = slotHeights(3, () => undefined, 250, 13);
  assert.deepEqual(heights, [250, 263, 263]);
  const measured = slotHeights(3, (i) => (i === 1 ? 400 : undefined), 250, 13);
  assert.deepEqual(measured, [250, 413, 263]);
}

await Promise.all(
  [
    testOffsetsPrefixSums(),
    testStartSearchMatchesLinear(),
    testEndSearchMatchesLinear(),
    testTopWindowWithOverscan(),
    testScrolledWindowStaysBounded(),
    testScrollLastRow(),
    testMixedHeightsAndResize(),
    testPinnedRowsPreservedWithoutRangeBlowup(),
    testPinnedIndexesClamped(),
    testEmptyList(),
    testLoadMoreAppendsWithoutDuplicates(),
    testSlotHeightsFoldBoundary()
  ].map((result) => Promise.resolve(result))
);
console.log('generation-virtual-window: 12 window/math cases passed');
