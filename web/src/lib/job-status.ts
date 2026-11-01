import type { JobRecord } from './api-models';

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

export function isStorageWait(job: Pick<JobRecord, 'status' | 'error_code'>) {
  return job.status === 'queued' && job.error_code === 'storage_pressure';
}
