<script lang="ts">
  import ProjectLanguageBoundary from './ProjectLanguageBoundary.svelte';
  let { plannedOnly = false }: { sessionId: string; plannedOnly?: boolean } =
    $props();
</script>

<ProjectLanguageBoundary {plannedOnly} />
