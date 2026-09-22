// Viewport windowing math for the generation segment table.
//
// Pure functions only (no DOM, no Svelte): the table component owns scroll
// listeners, ResizeObserver measurements, and rendering, while every index
// decision is computed here so it stays unit-testable under plain node.
//
// Model: the list has `count` slots. Slot `i` owns a segment row plus, for
// i > 0, the passage-boundary row that joins it to slot i - 1. The boundary
// height is folded into the following slot, so a window over slot indexes
// always renders each boundary together with its right-hand segment and the
// original neighbor indexes stay correct even when the left neighbor is
// virtualized away.
//
// Heights: `heights[i]` is the measured pixel height of slot i, or an
// estimate for unmeasured slots. `buildOffsets` returns prefix sums of
// length count + 1; offsets[i] is the content offset where slot i starts.

export interface VirtualSelection {
  /** Sorted unique slot indexes to render (window union pinned). */
  indexes: number[];
  /** Spacer height above the first rendered slot. */
  topGap: number;
  /** Gap heights keyed by rendered slot: spacer before that slot. */
  gaps: Map<number, number>;
  /** Spacer height below the last rendered slot. */
  bottomGap: number;
  /** Total content height of all slots. */
  totalHeight: number;
}

export function buildOffsets(heights: number[]): number[] {
  const offsets = new Array<number>(heights.length + 1);
  offsets[0] = 0;
  for (let i = 0; i < heights.length; i += 1) {
    const height =
      Number.isFinite(heights[i]) && heights[i] > 0 ? heights[i] : 0;
    offsets[i + 1] = offsets[i] + height;
  }
  return offsets;
}

/** First slot whose end edge sits below `scrollTop` (binary search). */
export function findStartIndex(offsets: number[], scrollTop: number): number {
  const count = offsets.length - 1;
  if (count <= 0 || scrollTop <= 0) return 0;
  let low = 0;
  let high = count - 1;
  let result = count - 1;
  while (low <= high) {
    const mid = (low + high) >> 1;
    if (offsets[mid + 1] > scrollTop) {
      result = mid;
      high = mid - 1;
    } else {
      low = mid + 1;
    }
  }
  return result;
}

/** First slot whose start edge reaches past `viewportBottom` (binary search). */
export function findEndIndex(
  offsets: number[],
  viewportBottom: number
): number {
  const count = offsets.length - 1;
  if (count <= 0) return -1;
  let low = 0;
  let high = count - 1;
  let result = count - 1;
  while (low <= high) {
    const mid = (low + high) >> 1;
    if (offsets[mid] >= viewportBottom) {
      result = Math.max(0, mid - 1);
      high = mid - 1;
    } else if (offsets[mid + 1] >= viewportBottom) {
      return mid;
    } else {
      low = mid + 1;
    }
  }
  return result;
}

export function selectVirtualWindow(
  count: number,
  offsets: number[],
  scrollTop: number,
  viewportHeight: number,
  overscan: number,
  pinned: number[] = []
): VirtualSelection {
  const empty: VirtualSelection = {
    indexes: [],
    topGap: 0,
    gaps: new Map(),
    bottomGap: 0,
    totalHeight: offsets[count] ?? 0
  };
  if (count <= 0) return empty;
  const viewport = viewportHeight > 0 ? viewportHeight : 0;
  const clampedTop = Math.max(0, scrollTop);
  const start = Math.max(
    0,
    findStartIndex(offsets, clampedTop) - Math.max(0, overscan)
  );
  const end = Math.min(
    count - 1,
    findEndIndex(offsets, clampedTop + viewport) + Math.max(0, overscan)
  );
  const selected = new Set<number>();
  for (let i = start; i <= end; i += 1) selected.add(i);
  for (const pin of pinned) {
    if (Number.isInteger(pin) && pin >= 0 && pin < count) selected.add(pin);
  }
  const indexes = [...selected].sort((a, b) => a - b);
  const gaps = new Map<number, number>();
  for (let pos = 1; pos < indexes.length; pos += 1) {
    const previous = indexes[pos - 1];
    const current = indexes[pos];
    if (current > previous + 1) {
      gaps.set(current, offsets[current] - offsets[previous + 1]);
    }
  }
  return {
    indexes,
    topGap: offsets[indexes[0]] - offsets[0],
    gaps,
    bottomGap: offsets[count] - offsets[indexes[indexes.length - 1] + 1],
    totalHeight: offsets[count]
  };
}

/** Slot heights for the table: estimate until a row is measured. */
export function slotHeights(
  count: number,
  heightAt: (index: number) => number | undefined,
  estimatedRowHeight: number,
  boundaryRowHeight: number
): number[] {
  const heights = new Array<number>(count);
  for (let i = 0; i < count; i += 1) {
    const measured = heightAt(i);
    const row =
      measured !== undefined && measured > 0 ? measured : estimatedRowHeight;
    heights[i] = row + (i > 0 ? boundaryRowHeight : 0);
  }
  return heights;
}
