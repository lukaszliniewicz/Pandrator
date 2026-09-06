<script lang="ts">
  import { errorMessage } from './errors';
  import { Activity, LoaderCircle } from '@lucide/svelte';
  import { artifactApi } from './domain-api';
  import { onDestroy } from 'svelte';

  let { artifactId }: { artifactId: string } = $props();
  let loading = $state(false);
  let points = $state<number[]>([]);
  let error = $state('');
  let controller: AbortController | undefined;
  let loadedArtifactId = '';
  const bars = $derived(
    points.length
      ? points.filter(
          (_item, index) =>
            index % Math.max(1, Math.ceil(points.length / 120)) === 0
        )
      : []
  );

  function waitForRetry(signal: AbortSignal) {
    return new Promise<void>((resolve, reject) => {
      if (signal.aborted) {
        reject(new DOMException('Aborted', 'AbortError'));
        return;
      }
      const timer = window.setTimeout(() => {
        signal.removeEventListener('abort', abort);
        resolve();
      }, 700);
      const abort = () => {
        window.clearTimeout(timer);
        reject(new DOMException('Aborted', 'AbortError'));
      };
      signal.addEventListener('abort', abort, { once: true });
    });
  }

  async function load() {
    controller?.abort();
    const request = new AbortController();
    controller = request;
    loading = true;
    error = '';
    try {
      for (let attempt = 0; attempt < 30; attempt += 1) {
        const result = await artifactApi.waveform(
          artifactId,
          1600,
          request.signal
        );
        if (Array.isArray(result.points)) {
          points = result.points;
          return;
        }
        await waitForRetry(request.signal);
      }
      error = 'Waveform is still being prepared.';
    } catch (caught) {
      if (!request.signal.aborted) error = errorMessage(caught);
    } finally {
      if (controller === request) {
        controller = undefined;
        loading = false;
      }
    }
  }

  $effect(() => {
    if (artifactId === loadedArtifactId) return;
    loadedArtifactId = artifactId;
    controller?.abort();
    controller = undefined;
    loading = false;
    points = [];
    error = '';
  });
  onDestroy(() => controller?.abort());
</script>

{#if points.length}
  <svg
    viewBox={`0 0 ${bars.length} 20`}
    preserveAspectRatio="none"
    class="mt-1 h-7 w-44"
    aria-label="Audio waveform"
  >
    {#each bars as value, index}<line
        x1={index}
        x2={index}
        y1={10 - value * 9}
        y2={10 + value * 9}
        stroke="currentColor"
        stroke-width=".7"
      />{/each}
  </svg>
{:else}
  <button
    onclick={load}
    disabled={loading}
    class="muted mt-1 flex items-center gap-1 text-[.65rem]"
    >{#if loading}<LoaderCircle
        class="animate-spin"
        size={11}
      />{:else}<Activity size={11} />{/if}
    {loading ? 'Preparing…' : 'Show waveform'}</button
  >
  {#if error}<span class="text-[.6rem] text-red-500">{error}</span>{/if}
{/if}
