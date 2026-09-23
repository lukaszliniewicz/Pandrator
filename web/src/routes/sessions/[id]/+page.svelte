<script lang="ts">
  import { page } from '$app/state';
  import SessionWorkspace from '$lib/SessionWorkspace.svelte';
  import { useSessionContext } from '$lib/session-context';
  const context = useSessionContext();
</script>

{#if context.session && context.outcome}{#key context.session.id}<SessionWorkspace
      session={context.session}
      outcome={context.outcome}
      workflowStore={context.workflow}
      initialSettingsStage={page.url.searchParams.get('settings') ?? ''}
      onupdated={() => context.reload()}
    />{/key}{/if}
