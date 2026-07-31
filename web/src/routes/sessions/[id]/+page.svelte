<script lang="ts">
  import SessionWorkspace from '$lib/SessionWorkspace.svelte';
  import { useSessionContext } from '$lib/session-context';
  const context = useSessionContext();
</script>

{#if context.session && context.outcome}<SessionWorkspace
    session={context.session}
    outcome={context.outcome}
    workflowStore={context.workflow}
    onback={() => (location.href = '/sessions')}
    onupdated={() => context.reload()}
  />{/if}
