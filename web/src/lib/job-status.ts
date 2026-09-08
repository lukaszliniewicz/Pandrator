const JOB_STATUSES = [
  'queued',
  'running',
  'cancel_requested',
  'succeeded',
  'failed',
  'canceled'
] as const;

export type JobStatus = (typeof JOB_STATUSES)[number];

export function isJobStatus(value: unknown): value is JobStatus {
  return JOB_STATUSES.some((status) => status === value);
}
