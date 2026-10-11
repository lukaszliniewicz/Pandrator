import { apiJson, ApiMutationAttempt } from './api';

export type SoundAnchor = {
  kind:
    | 'before_segment'
    | 'after_segment'
    | 'source_time'
    | 'after_word'
    | 'next_pause';
  segment_id?: string;
  time_seconds?: number;
  quote?: string;
  occurrence?: number;
  start?: number;
  end?: number;
};
export type SoundPlayback = {
  mode: 'insert' | 'overlay';
  offset_ms: number;
  gain_db: number;
  trim_start_seconds: number;
  trim_end_seconds?: number;
  fade_in_ms: number;
  fade_out_ms: number;
};
export type ProjectSoundCue = {
  logical_id: string;
  variant_id: string;
  anchor: SoundAnchor & {
    resolution?: { status?: string; reason?: string; shift_ms?: number };
  };
  playback: SoundPlayback;
  included: boolean;
  placement_status: 'ready' | 'needs_placement';
  sound: {
    name: string;
    artifact_id: string | null;
    duration_seconds: number | null;
    status: string;
  };
};
export type ProjectSoundPlan = {
  session_id: string;
  revision: number;
  plan_id: string | null;
  speech_plan_revision_id: string | null;
  cues: { items: ProjectSoundCue[]; next_cursor: string | null };
};
export function publicSoundAnchor(anchor: SoundAnchor): SoundAnchor {
  if (anchor.kind === 'source_time')
    return { kind: anchor.kind, time_seconds: anchor.time_seconds ?? 0 };
  if (anchor.kind === 'before_segment' || anchor.kind === 'after_segment')
    return { kind: anchor.kind, segment_id: anchor.segment_id };
  return {
    kind: anchor.kind,
    segment_id: anchor.segment_id,
    quote: anchor.quote,
    occurrence: anchor.occurrence,
    ...(anchor.start !== undefined
      ? { start: anchor.start, end: anchor.end }
      : {})
  };
}
const base = (id: string) => `/sessions/${encodeURIComponent(id)}/sound-plan`;
export function projectSounds(
  id: string,
  cursor?: string,
  segmentIds?: string[]
) {
  const query = new URLSearchParams({ limit: segmentIds ? '100' : '40' });
  if (cursor) query.set('cursor', cursor);
  segmentIds?.forEach((segmentId) => query.append('segment_id', segmentId));
  return apiJson<ProjectSoundPlan>(`${base(id)}?${query}`);
}
export function changeProjectSound(
  id: string,
  revision: number,
  cue: {
    variant_id: string;
    anchor: SoundAnchor;
    playback: SoundPlayback;
    included: boolean;
  },
  logicalId?: string,
  attempt = new ApiMutationAttempt()
) {
  const path = `${base(id)}/cues${logicalId ? `/${encodeURIComponent(logicalId)}` : ''}`;
  const body = { expected_revision: revision, ...cue };
  return apiJson<{ revision: number; cue: ProjectSoundCue }>(path, {
    method: logicalId ? 'PATCH' : 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...attempt.headersFor(path, body)
    },
    body: JSON.stringify(body)
  });
}
export function removeProjectSound(
  id: string,
  revision: number,
  logicalId: string
) {
  const path = `${base(id)}/cues/${encodeURIComponent(logicalId)}`;
  return apiJson(path, {
    method: 'DELETE',
    headers: {
      ...new ApiMutationAttempt().headersFor(path, { revision }),
      'If-Match': String(revision)
    }
  });
}
export function restoreProjectSounds(
  id: string,
  revision: number,
  sourcePlanId: string
) {
  const path = `${base(id)}/restore`;
  const body = { expected_revision: revision, source_plan_id: sourcePlanId };
  return apiJson(path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...new ApiMutationAttempt().headersFor(path, body)
    },
    body: JSON.stringify(body)
  });
}
export const soundPlacementLabel = (anchor: SoundAnchor) => {
  if (anchor.kind === 'source_time')
    return `At ${(anchor.time_seconds ?? 0).toFixed(2)} s`;
  if (anchor.kind === 'after_word') return `After “${anchor.quote}”`;
  if (anchor.kind === 'next_pause') return `Next pause after “${anchor.quote}”`;
  return anchor.kind === 'before_segment' ? 'Before segment' : 'After segment';
};
export type SoundMixStatus = {
  base_artifact_id?: string | null;
  artifact_id: string | null;
  job_id: string | null;
  status: string;
  sound_plan_revision: number | null;
};
export const projectSoundMix = (id: string, baseArtifactId?: string) =>
  apiJson<SoundMixStatus>(
    `${base(id)}/mix${baseArtifactId ? `?${new URLSearchParams({ base_artifact_id: baseArtifactId })}` : ''}`
  );
export function runProjectSounds(
  id: string,
  revision: number,
  action: 'render' | 'resolve',
  baseArtifactId?: string,
  attempt = new ApiMutationAttempt()
) {
  const path = `${base(id)}/${action}`;
  const body = {
    expected_revision: revision,
    ...(action === 'render' ? { base_artifact_id: baseArtifactId } : {})
  };
  return apiJson<{
    job_id: string | null;
    status: string;
    artifact_id?: string | null;
  }>(path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...attempt.headersFor(path, body)
    },
    body: JSON.stringify(body)
  });
}
