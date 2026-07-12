<script lang="ts">
  import { Activity, LoaderCircle } from '@lucide/svelte';
  import { api } from './api';

  let { artifactId }: { artifactId: string } = $props();
  let loading = $state(false);
  let points = $state<number[]>([]);
  let error = $state('');
  const bars = $derived(points.length ? points.filter((_item, index) => index % Math.max(1, Math.ceil(points.length / 120)) === 0) : []);

  async function load() {
    loading = true; error = '';
    try {
      for (let attempt = 0; attempt < 30; attempt += 1) {
        const result = await api<any>(`/artifacts/${artifactId}/waveform?points=1600`);
        if (Array.isArray(result.points)) { points = result.points; return; }
        await new Promise((resolve) => setTimeout(resolve, 700));
      }
      error = 'Waveform is still being prepared.';
    } catch (caught) { error = caught instanceof Error ? caught.message : String(caught); }
    finally { loading = false; }
  }
</script>

{#if points.length}
  <svg viewBox={`0 0 ${bars.length} 20`} preserveAspectRatio="none" class="mt-1 h-7 w-44" aria-label="Audio waveform">
    {#each bars as value, index}<line x1={index} x2={index} y1={10-value*9} y2={10+value*9} stroke="currentColor" stroke-width=".7"/>{/each}
  </svg>
{:else}
  <button onclick={load} disabled={loading} class="muted mt-1 flex items-center gap-1 text-[.65rem]">{#if loading}<LoaderCircle class="animate-spin" size={11}/>{:else}<Activity size={11}/>{/if} {loading?'Preparing…':'Show waveform'}</button>
  {#if error}<span class="text-[.6rem] text-red-500">{error}</span>{/if}
{/if}
