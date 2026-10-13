import type { JobRecord } from './api-models';

export function jobProgressDetail(job: JobRecord, fallback = ''): string {
  if (job.status === 'queued') {
    const blocker = job.waiting_for_job;
    if (blocker) {
      const kind = [
        'generation.run',
        'tts.generate',
        'dubbing.generate'
      ].includes(blocker.kind)
        ? 'audio generation'
        : blocker.kind.replaceAll('.', ' ').replaceAll('_', ' ');
      return `Queued — waiting for ${kind}${blocker.progress_detail ? `: ${blocker.progress_detail}` : '.'}`;
    }
    return job.progress_detail || 'Queued — waiting for an available worker.';
  }
  return job.progress_detail || fallback;
}
