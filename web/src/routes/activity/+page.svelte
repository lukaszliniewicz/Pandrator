<script lang="ts">
  import { RefreshCw } from '@lucide/svelte';
  import { appState } from '$lib/app-state.svelte';
  import JobActivityList from '$lib/JobActivityList.svelte';
</script>

<div class="mx-auto max-w-7xl">
  <header class="flex flex-wrap items-end justify-between gap-4"><div><div class="eyebrow">Activity & logs</div><h1 class="mt-2 text-4xl font-semibold">Background work</h1><p class="muted mt-3">Inspect durable job logs, errors, retries, and replayable progress.</p></div><button onclick={() => appState.refresh()} class="btn btn-secondary"><RefreshCw size={16}/> Refresh</button></header>
  <div class="mt-7"><JobActivityList jobs={appState.jobs}/></div>
</div>
