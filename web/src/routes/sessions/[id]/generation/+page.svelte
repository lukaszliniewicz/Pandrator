<script lang="ts">
  import GenerationWorkspace from '$lib/GenerationWorkspace.svelte';
  import { useSessionContext } from '$lib/session-context';
  const context = useSessionContext();
</script>

{#if context.session?.workflow_kind === 'subtitles'}
  <p class="muted">
    This project uses subtitles. Open Text &amp; subtitles to work on its
    segments.
  </p>
{:else if context.session}
  <GenerationWorkspace
    sessionId={context.session.id}
    workflowKind={context.session.workflow_kind}
  />
{/if}
