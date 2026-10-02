import type { SubtitleReviewPayload, SubtitleSegment } from './api-models';

type Draft = {
  payload: SubtitleReviewPayload;
  baseline: string;
  editableArtifactId: string;
};
const drafts = new Map<string, Draft>();
const MAX_DRAFTS = 10;

export function reviewDraftKey(sessionId: string, artifactId: string) {
  return `${sessionId}:${artifactId}`;
}

export function reviewContent(payload: SubtitleReviewPayload | null) {
  return JSON.stringify(
    payload?.columns.map((column) => ({
      artifact: column.artifact_id,
      segments: column.segments.map((item) => ({
        ...item,
        speaker: item.speaker ?? '',
        starts_new_turn: Boolean(item.starts_new_turn),
        review_state: item.review_state ?? 'clear',
        review_note: item.review_note ?? ''
      })),
      passages: column.logical_passages?.map((item) => ({
        ...item,
        speaker: item.speaker ?? '',
        starts_new_turn: Boolean(item.starts_new_turn),
        deleted: Boolean(item.deleted),
        review_state: item.review_state ?? 'clear',
        review_note: item.review_note ?? ''
      }))
    })) ?? []
  );
}

function segmentKey(segment: SubtitleSegment) {
  return segment.id || segment.draft_id;
}

export function cloneReview(
  payload: SubtitleReviewPayload
): SubtitleReviewPayload {
  const copy: SubtitleReviewPayload = JSON.parse(JSON.stringify(payload));
  for (const column of copy.columns) {
    const records = new Map(
      column.segments.map((segment) => [segmentKey(segment), segment])
    );
    for (const row of copy.rows) {
      row.cells[column.artifact_id] = (row.cells[column.artifact_id] ?? []).map(
        (segment) => records.get(segmentKey(segment)) ?? segment
      );
    }
  }
  return copy;
}

export function rememberReviewDraft(
  key: string,
  payload: SubtitleReviewPayload,
  baseline: string,
  editableArtifactId: string
) {
  drafts.delete(key);
  drafts.set(key, {
    payload: cloneReview(payload),
    baseline,
    editableArtifactId
  });
  while (drafts.size > MAX_DRAFTS) drafts.delete(drafts.keys().next().value!);
}

export function readReviewDraft(key: string) {
  const draft = drafts.get(key);
  return draft ? { ...draft, payload: cloneReview(draft.payload) } : undefined;
}

export function forgetReviewDraft(key: string) {
  drafts.delete(key);
}

export function sameReviewSource(
  saved: SubtitleReviewPayload,
  current: SubtitleReviewPayload
) {
  return saved.columns.every((column) => {
    const fresh = current.columns.find(
      (item) => item.artifact_id === column.artifact_id
    );
    return (
      fresh &&
      fresh.revision_id === column.revision_id &&
      fresh.source_content_hash === column.source_content_hash &&
      (saved.edit_mode !== 'passages' ||
        fresh.composition_hash === column.composition_hash)
    );
  });
}
