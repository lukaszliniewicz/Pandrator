<script lang="ts">
  import type { WorkflowStage } from './api-models';
  import ParameterLabel from './ParameterLabel.svelte';
  import {
    apiTimestamp,
    estimatedRemaining,
    formatRunCost,
    formatRunDuration
  } from './run-metrics';
  let { stage }: { stage: WorkflowStage } = $props();
  let now = $state(Date.now());
  $effect(() => {
    if (
      stage.status !== 'running' ||
      !stage.run_metrics?.started_at ||
      stage.run_metrics.finished_at
    )
      return;
    now = Date.now();
    const timer = window.setInterval(() => (now = Date.now()), 1000);
    return () => window.clearInterval(timer);
  });
  const elapsed = $derived.by(() => {
    const metrics = stage.run_metrics;
    if (!metrics) return null;
    const start = apiTimestamp(metrics.started_at);
    if (
      stage.status === 'running' &&
      !metrics.finished_at &&
      Number.isFinite(start)
    )
      return Math.max(0, (now - start) / 1000);
    return metrics.duration_seconds;
  });
  const costAware = $derived(
    ['correct', 'translate', 'optimize_tts', 'generate_audio'].includes(
      stage.key
    )
  );
  // Historical segment coverage may include reused takes from older runs.
  // Project only job progress, whose elapsed time and numerator share a run.
  const remaining = $derived(
    estimatedRemaining(
      elapsed,
      stage.progress_basis === 'segments' ? null : stage.progress
    )
  );
</script>

{#if elapsed != null || stage.usage || stage.status === 'running'}
  <dl
    class="mt-3 flex flex-wrap gap-x-5 gap-y-2 text-xs"
    aria-label={`${stage.title} run metrics`}
  >
    {#if elapsed != null}<div class="flex items-center gap-1.5 tabular-nums">
        <dt class="muted">
          {stage.status === 'running' ? 'Elapsed' : 'Duration'}
        </dt>
        <dd class="font-semibold">{formatRunDuration(elapsed)}</dd>
      </div>{/if}
    {#if stage.status === 'running'}<div class="flex items-center gap-1.5">
        <dt class="muted">
          <ParameterLabel
            section="workflow"
            name="remaining"
            label="Estimated time left"
            description="Approximate time based on elapsed time and progress in this run. Model loading, different block lengths, retries and pauses can change it. Reused audio from older runs is not used to estimate speed."
          />
        </dt>
        <dd class="font-semibold tabular-nums">{remaining}</dd>
      </div>{/if}
    {#if stage.usage || (costAware && stage.run_metrics)}<div
        class="flex items-center gap-1.5"
      >
        <dt class="muted">{stage.usage?.estimated ? 'Est. cost' : 'Cost'}</dt>
        <dd class="font-semibold">
          {formatRunCost(stage.usage?.cost_usd ?? null)}{stage.usage
            ?.has_unpriced_usage
            ? ' + unpriced usage'
            : ''}
        </dd>
      </div>{/if}
  </dl>
{/if}
