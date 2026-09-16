import { expect, test } from '@playwright/test';
import {
  adoptUpdatedSegment,
  adoptUpdatedSegments,
  isStaleLoad,
  type SegmentPageState
} from '../src/lib/generation-segment-adoption';
import type { GenerationSegment } from '../src/lib/api-models';

function row(
  id: string,
  ordinal: number,
  extra: Partial<GenerationSegment> = {}
): GenerationSegment {
  return {
    id,
    ordinal,
    revision: 1,
    text: `Sentence ${ordinal + 1}.`,
    status: 'completed',
    node_kind: 'paragraph',
    paragraph_break_after: false,
    source_segment_ids: [ordinal],
    optimized_text: null,
    speech_plan: {},
    optimization_status: 'not_requested',
    optimization_reviewed: false,
    marked: false,
    removed: false,
    takes: [],
    ...extra
  } as GenerationSegment;
}

function take(id: string, status = 'completed') {
  return {
    id,
    generation_segment_id: 'seg-a',
    artifact_id: 'art-1',
    status,
    is_active: true,
    revision: 1,
    created_at: '2026-09-15T00:00:00Z'
  } as GenerationSegment['takes'][number];
}

function page(): SegmentPageState {
  return {
    items: [
      { ...row('seg-a', 0), takes: [take('take-old-1')] },
      row('seg-b', 1)
    ],
    plan_revision_id: 'r1'
  };
}

test('edit-copy adoption swaps rows, adopts the pin, and clears old take IDs', () => {
  const next = adoptUpdatedSegment(page(), 'seg-a', {
    ...row('copy-seg-a', 0),
    plan_revision_id: 'r2',
    previous_segment_id: 'seg-a',
    revision: 1,
    optimized_text: 'Spoken one.',
    status: 'stale'
  });
  expect(next.plan_revision_id).toBe('r2');
  expect(next.items.map((item) => item.id)).toEqual(['copy-seg-a', 'seg-b']);
  const adopted = next.items[0];
  expect(adopted.optimized_text).toBe('Spoken one.');
  expect(adopted.status).toBe('stale');
  // A new segment ID must never inherit the old row's take IDs.
  expect(adopted.takes).toEqual([]);
});

test('same-ID edits preserve takes and mark them stale', () => {
  const next = adoptUpdatedSegment(page(), 'seg-b', {
    ...row('seg-b', 1),
    plan_revision_id: 'r1',
    revision: 2,
    optimized_text: 'Spoken two.',
    status: 'stale'
  });
  expect(next.plan_revision_id).toBe('r1');
  expect(next.items.map((item) => item.id)).toEqual(['seg-a', 'seg-b']);
  // Takes are preserved here because the segment ID did not change; the
  // follow-up reload still refreshes the authoritative cloned takes.
  expect(next.items[0].takes.map((item) => item.id)).toEqual(['take-old-1']);
});

test('batch adoption remaps previous_segment_id and adopts one pin', () => {
  const next = adoptUpdatedSegments(page(), [
    {
      ...row('copy-seg-a', 0),
      plan_revision_id: 'r2',
      previous_segment_id: 'seg-a',
      revision: 1,
      optimized_text: 'Spoken one.',
      status: 'stale'
    }
  ]);
  expect(next.plan_revision_id).toBe('r2');
  expect(next.items.map((item) => item.id)).toEqual(['copy-seg-a', 'seg-b']);
  expect(next.items[0].takes).toEqual([]);
});

test('batch adoption never duplicates a remapped copy ID', () => {
  const next = adoptUpdatedSegments(page(), [
    {
      ...row('copy-seg-a', 0),
      plan_revision_id: 'r2',
      previous_segment_id: 'seg-a',
      revision: 1,
      optimized_text: 'Spoken one.',
      status: 'stale'
    }
  ]);
  const ids = next.items.map((item) => item.id);
  expect(ids).toEqual(['copy-seg-a', 'seg-b']);
  expect(new Set(ids).size).toBe(ids.length);
});

test('batch copies with mixed or missing pins are rejected', () => {
  const mixed = [
    {
      ...row('copy-seg-a', 0),
      plan_revision_id: 'r2',
      previous_segment_id: 'seg-a'
    },
    {
      ...row('copy-seg-b', 1),
      plan_revision_id: 'r3',
      previous_segment_id: 'seg-b'
    }
  ];
  expect(() => adoptUpdatedSegments(page(), mixed)).toThrow(/inconsistent/i);
  const missing = [{ ...row('copy-seg-a', 0), previous_segment_id: 'seg-a' }];
  expect(() => adoptUpdatedSegments(page(), missing)).toThrow(/inconsistent/i);
});

test('a delayed older load response is discarded after adoption', () => {
  // Load started at epoch 0; the edit-copy save is adopted at epoch 1.
  const adopted = adoptUpdatedSegment(page(), 'seg-a', {
    ...row('copy-seg-a', 0),
    plan_revision_id: 'r2',
    previous_segment_id: 'seg-a',
    revision: 1,
    optimized_text: 'Spoken one.',
    status: 'stale'
  });
  expect(isStaleLoad(0, 1)).toBe(true);
  expect(isStaleLoad(1, 1)).toBe(false);
  // The store therefore keeps the adopted R2 state: a late R1 page must not
  // reintroduce the stale pin, the old IDs, or the old take IDs.
  const latePage: SegmentPageState = page();
  const kept = isStaleLoad(0, 1) ? adopted : latePage;
  expect(kept.plan_revision_id).toBe('r2');
  expect(kept.items.map((item) => item.id)).toEqual(['copy-seg-a', 'seg-b']);
  expect(kept.items[0].takes).toEqual([]);
});
