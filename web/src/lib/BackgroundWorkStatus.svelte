<script lang="ts">
  import { managerApi } from './admin-api';
  import { appState } from './app-state.svelte';
  import type { JobRecord } from './api-models';

  let { jobs }: { jobs: JobRecord[] } = $props();
  type WorkerService = {
    id: string;
    process?: { pid?: number } | null;
    health?: { state?: string } | null;
  };
  let worker = $state<WorkerService | null>(null);
  const running = $derived(
    jobs.filter((job) => ['running', 'cancel_requested'].includes(job.status))
  );
  const queued = $derived(jobs.filter((job) => job.status === 'queued'));
  const runningKinds = $derived(
    Array.from(new Set(running.map((job) => job.kind))).map((kind) =>
      kind === 'project.copy.current'
        ? 'Project copy'
        : ['dubbing.transcribe', 'transcription.quick'].includes(kind)
          ? 'Transcription'
          : kind.replaceAll('.', ' ').replaceAll('_', ' ')
    )
  );
  const workerState = $derived(
    !worker
      ? 'unknown'
      : worker.process === null
        ? 'stopped'
        : worker.process?.pid && worker.health?.state === 'healthy'
          ? 'healthy'
          : worker.process?.pid && worker.health?.state === 'unhealthy'
            ? 'unhealthy'
            : 'unknown'
  );

  $effect(() => {
    const identity = appState.identityKey;
    worker = null;
    if (!appState.isAdministrator) return;
    let active = true;
    let refreshing = false;
    const current = () =>
      active && appState.isAdministrator && appState.identityKey === identity;
    async function refreshWorker() {
      if (refreshing || !current()) return;
      refreshing = true;
      try {
        const result = await managerApi.services<{ items?: WorkerService[] }>();
        if (current())
          worker =
            result.items?.find((item) => item.id === 'pandrator.worker') ??
            null;
      } catch {
        if (current()) worker = null;
      } finally {
        refreshing = false;
      }
    }
    void refreshWorker();
    const timer = setInterval(() => void refreshWorker(), 30_000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  });
</script>

<div aria-label="Background work status" class="space-y-1 text-sm">
  <p>{running.length} running · {queued.length} queued</p>
  {#if runningKinds.length}<p class="muted text-xs">
      Running: {runningKinds.join(', ')}. Queued jobs may wait for this work.
    </p>{/if}
  {#if appState.isAdministrator}<p class="muted text-xs">
      {#if workerState === 'healthy'}Worker is running. Job counts do not
        indicate free capacity.
      {:else if workerState === 'stopped'}Worker is stopped. Queued jobs need a
        running worker.
      {:else if workerState === 'unhealthy'}Worker is not healthy. Check setup
        and activity logs.
      {:else}Worker health is unknown. Check setup if queued work does not
        start.{/if}
    </p>{/if}
  <a
    href="/activity"
    class="inline-block text-xs font-semibold text-[var(--accent)]"
    >Inspect activity and logs</a
  >
</div>
