<script lang="ts">
  import { page } from '$app/state';
  import { appState } from '$lib/app-state.svelte';
  import JobActivityList from '$lib/JobActivityList.svelte';
  const jobs = $derived(appState.jobs.filter((job: any) => job.session_id === page.params.id || job.payload_json?.session_id === page.params.id));
</script>

<div><h2 class="text-2xl font-semibold">Session activity</h2><p class="muted mt-2">Jobs, complete logs, errors, progress, and costs for this workspace.</p><div class="mt-6"><JobActivityList {jobs}/></div></div>
