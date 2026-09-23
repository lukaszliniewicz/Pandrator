import { apiJson, ApiError } from './api';

/**
 * Configurable source logical-passage settings (frontend domain module).
 *
 * Backend contract: tmp/source-passage-backend-contract.md (backend-owned).
 * Settings section `source_passages` with five integer keys. Validation mirrors
 * the backend validator: strict integers, per-key ranges, and
 * min_chars <= preferred_chars. Unknown keys are never sent.
 *
 * Semantics (human-precise, per contract):
 * - min_chars applies to fallback clause selection only; short complete
 *   sentences stay independent.
 * - preferred_chars and sentence_lookahead_chars are soft; text is never cut
 *   to fit them.
 * - cue_join_gap_ms is guarded joining only (verified unfinished same-speaker
 *   phrases may bridge gaps at or below it); never an automatic split
 *   threshold.
 * - diagnostic_span_ms only raises a diagnostic flag
 *   (span_preference_exceeded); never a hard cap.
 *
 * Editing these settings never rewrites pinned passage packets, accepted
 * correction/translation artifacts or ledgers, generation plans, or takes.
 * Only an explicit rebuild creates a NEW source branch; the original source
 * and all downstream work are preserved and the new branch is not
 * auto-selected.
 */

export const SOURCE_PASSAGE_SECTION = 'source_passages';

export type SourcePassageKey =
  | 'min_chars'
  | 'preferred_chars'
  | 'sentence_lookahead_chars'
  | 'cue_join_gap_ms'
  | 'diagnostic_span_ms';

export type SourcePassageValues = Record<SourcePassageKey, number>;

export const SOURCE_PASSAGE_DEFAULTS: SourcePassageValues = {
  min_chars: 60,
  preferred_chars: 160,
  sentence_lookahead_chars: 20,
  cue_join_gap_ms: 650,
  diagnostic_span_ms: 8000
};

export type SourcePassageControl = {
  key: SourcePassageKey;
  label: string;
  min: number;
  max: number;
  step: number;
  unit: string;
  help: string;
};

export const SOURCE_PASSAGE_CONTROLS: SourcePassageControl[] = [
  {
    key: 'min_chars',
    label: 'Minimum substantial-clause size',
    min: 1,
    max: 500,
    step: 1,
    unit: 'characters',
    help: 'Fallback size for clause selection only. Short complete sentences stay independent; this never forces a cut.'
  },
  {
    key: 'preferred_chars',
    label: 'Preferred passage size (soft)',
    min: 1,
    max: 1000,
    step: 1,
    unit: 'characters',
    help: 'Soft length preference. Passages may run longer to reach a natural boundary; text is never cut to fit.'
  },
  {
    key: 'sentence_lookahead_chars',
    label: 'Sentence fit window (soft)',
    min: 0,
    max: 200,
    step: 1,
    unit: 'characters',
    help: 'How far past the preferred size to look for a fitting sentence boundary. Soft; no forced text cuts.'
  },
  {
    key: 'cue_join_gap_ms',
    label: 'Cue join gap (guarded joining)',
    min: 0,
    max: 3000,
    step: 50,
    unit: 'milliseconds',
    help: 'Verified unfinished same-speaker phrases may bridge gaps at or below this size. Guarded joining only, never an automatic split threshold.'
  },
  {
    key: 'diagnostic_span_ms',
    label: 'Diagnostic span (not a cap)',
    min: 1000,
    max: 60000,
    step: 500,
    unit: 'milliseconds',
    help: 'Flags long spans for diagnostics only. Never a hard cap; long unpunctuated phrases are kept.'
  }
];

/** Fill missing/invalid entries from built-in defaults without mutating input. */
export function coerceSourcePassageValues(
  saved: Record<string, unknown> | null | undefined
): SourcePassageValues {
  const result = { ...SOURCE_PASSAGE_DEFAULTS };
  if (!saved) return result;
  for (const control of SOURCE_PASSAGE_CONTROLS) {
    const raw = saved[control.key];
    if (typeof raw === 'boolean') continue;
    const value = Number(raw);
    if (Number.isInteger(value)) result[control.key] = value;
  }
  return result;
}

/** validation_error-compatible messages keyed by field; empty means valid. */
export function validateSourcePassageValues(
  values: Record<string, unknown>
): Partial<Record<SourcePassageKey, string>> {
  const errors: Partial<Record<SourcePassageKey, string>> = {};
  for (const control of SOURCE_PASSAGE_CONTROLS) {
    const raw = values[control.key];
    if (typeof raw === 'boolean' || raw === '' || raw == null) {
      errors[control.key] = 'Enter a whole number.';
      continue;
    }
    const value = Number(raw);
    if (!Number.isInteger(value)) {
      errors[control.key] = 'Enter a whole number.';
      continue;
    }
    if (value < control.min || value > control.max) {
      errors[control.key] =
        `Enter ${control.min} to ${control.max} ${control.unit}.`;
    }
  }
  if (
    errors.min_chars === undefined &&
    errors.preferred_chars === undefined &&
    Number(values.min_chars) > Number(values.preferred_chars)
  ) {
    errors.preferred_chars =
      'Preferred size must be at least the minimum clause size.';
  }
  return errors;
}

/** Exact five-key payload; unknown keys are never sent (backend rejects them). */
export function sourcePassagePayload(
  values: Record<string, unknown>
): SourcePassageValues {
  return {
    min_chars: Number(values.min_chars),
    preferred_chars: Number(values.preferred_chars),
    sentence_lookahead_chars: Number(values.sentence_lookahead_chars),
    cue_join_gap_ms: Number(values.cue_join_gap_ms),
    diagnostic_span_ms: Number(values.diagnostic_span_ms)
  };
}

export type SourcePassageStatus = {
  artifact_id: string;
  revision_id: string;
  content_hash: string;
  pinned: boolean;
  policy_version?: string | null;
  source_passage_settings?: SourcePassageValues | null;
  source_passage_settings_hash?: string | null;
  source_passage_settings_revision?: number | null;
  passage_count?: number | null;
  display_content_hash?: string | null;
};

type SourcePassagePreview = {
  passage_count: number;
  items: unknown[];
  policy_version: string;
  effective_settings: SourcePassageValues;
  settings_hash: string;
};

export type SourcePassagePreviewResponse = SourcePassagePreview & {
  artifact_id: string;
  revision_id: string;
  content_hash: string;
  settings_revision: number;
  pinned: boolean;
  truncated: boolean;
  warnings: string[];
};

export type SourcePassageRebuildRequest = {
  expected_source_revision_id: string;
  expected_source_content_hash: string;
  expected_settings_revision: number;
  /** Required: preview.settings_hash from a fresh preview. Detects global-default changes that a session revision alone would miss. */
  expected_settings_hash: string;
  source_passages?: Partial<SourcePassageValues> | null;
  idempotency_key?: string | null;
};

export type SourcePassageRebuildResponse = {
  branch_artifact_id: string;
  branch_revision_id: string;
  branch_document_id: string;
  passage_count: number;
  policy_version: string;
  effective_settings: SourcePassageValues;
  settings_hash: string;
  preserved: {
    original_artifact_id: string;
    downstream_untouched: boolean;
    selection_unchanged: boolean;
  };
};

export function isSourcePassageConflict(caught: unknown): boolean {
  return (
    caught instanceof ApiError &&
    (caught.status === 409 ||
      ['revision_conflict', 'source_changed'].includes(caught.code))
  );
}

function encodeId(value: string) {
  return encodeURIComponent(value);
}

/** Read-only pinned-passage status for one raw source artifact. */
export function sourcePassageStatus(
  sessionId: string,
  artifactId: string,
  signal?: AbortSignal
): Promise<SourcePassageStatus> {
  return apiJson<SourcePassageStatus>(
    `/sessions/${encodeId(sessionId)}/sources/${encodeId(artifactId)}/passages`,
    { signal }
  );
}

/** Build a bounded preview without writing anything. */
export function previewSourcePassages(
  sessionId: string,
  artifactId: string,
  overrides?: Partial<SourcePassageValues> | null
): Promise<SourcePassagePreviewResponse> {
  return apiJson<SourcePassagePreviewResponse>(
    `/sessions/${encodeId(sessionId)}/sources/${encodeId(artifactId)}/passages/preview`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(
        overrides ? { source_passages: { ...overrides } } : {}
      )
    }
  );
}

/**
 * Explicit rebuild into a NEW source branch. Guards are compared before
 * anything is built (stale -> 409, no mutation): source revision + content
 * hash, session settings revision, AND the preview settings hash (which also
 * catches global-default changes). Idempotency travels in the HTTP
 * Idempotency-Key header; the JSON body carries no idempotency field.
 * Callers must refresh status/settings and ask the user to preview again on
 * 409 instead of silently retrying. The original artifact, downstream work,
 * and stage selections are preserved; the branch is not auto-selected.
 */
export function rebuildSourcePassages(
  sessionId: string,
  artifactId: string,
  request: SourcePassageRebuildRequest
): Promise<SourcePassageRebuildResponse> {
  return apiJson<SourcePassageRebuildResponse>(
    `/sessions/${encodeId(sessionId)}/sources/${encodeId(artifactId)}/passages/rebuild`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(request.idempotency_key
          ? { 'Idempotency-Key': request.idempotency_key }
          : {})
      },
      body: JSON.stringify({
        expected_source_revision_id: request.expected_source_revision_id,
        expected_source_content_hash: request.expected_source_content_hash,
        expected_settings_revision: request.expected_settings_revision,
        expected_settings_hash: request.expected_settings_hash,
        ...(request.source_passages
          ? { source_passages: { ...request.source_passages } }
          : {})
      })
    }
  );
}
