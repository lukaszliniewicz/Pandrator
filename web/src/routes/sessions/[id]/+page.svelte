<script lang="ts">
  import { page } from '$app/state';
  import { getContext } from 'svelte';
  import SessionWorkspace from '$lib/SessionWorkspace.svelte';
  import ProjectLanguageBoundary from '$lib/ProjectLanguageBoundary.svelte';
  import {
    PROJECT_WORKSPACE,
    type ProjectWorkspace,
    type WorkspaceScope
  } from '$lib/project-workspace.svelte';
  import {
    useSessionContext,
    useSourceSessionContext
  } from '$lib/session-context';
  const context = useSessionContext();
  const source = useSourceSessionContext();
  const workspace = getContext<ProjectWorkspace>(PROJECT_WORKSPACE);
  const multilingual = $derived(
    Boolean(workspace.membership || workspace.source.languages.value?.setup)
  );
  let workspaceMode = $state<'review' | 'automatic'>('review');
  let openedLanguages = $state<WorkspaceScope[]>([]);
  let openedSourceId = '';
  $effect(() => {
    const sourceId = workspace.sourceId;
    if (sourceId !== openedSourceId) {
      openedSourceId = sourceId;
      openedLanguages = [];
    }
    const selected = workspace.selected;
    if (
      multilingual &&
      selected.record.session &&
      selected.record.outcome &&
      !openedLanguages.some((scope) => scope.id === selected.id)
    )
      openedLanguages = [...openedLanguages, selected];
  });
</script>

{#if multilingual && source.session && source.outcome}
  <div class="space-y-4" data-testid="multilingual-project-overview">
    {#key source.session.id}
      <div
        data-testid="shared-source-workflow"
        data-source-session-id={source.session.id}
      >
        <SessionWorkspace
          session={source.session}
          outcome={source.outcome}
          workflowStore={source.workflow}
          scope="source"
          bind:workspaceMode
          onupdated={() => source.reload()}
        />
      </div>
      <ProjectLanguageBoundary />
    {/key}
    {#if context.loading && !context.session}<p
        class="muted rounded-2xl border border-[var(--line)] p-5"
        role="status"
      >
        Loading language controls…
      </p>{/if}
    {#if context.error}<p class="text-sm text-red-500" role="alert">
        {context.error}
      </p>{/if}
    {#each openedLanguages as language (language.id)}
      <div
        hidden={language.id !== workspace.selectedId}
        data-testid="language-workflow"
        data-language-session-id={language.id}
      >
        {#if language.context.session && language.context.outcome}
          <SessionWorkspace
            session={language.context.session}
            outcome={language.context.outcome}
            workflowStore={language.workflow}
            scope="language"
            bind:workspaceMode
            manageMode={false}
            active={language.id === workspace.selectedId}
            onupdated={() => language.context.reload()}
            initialSettingsStage={language.id === workspace.selectedId
              ? (page.url.searchParams.get('settings') ?? '')
              : ''}
          />
        {/if}
      </div>
    {/each}
  </div>
{:else if context.session && context.outcome}{#key context.session.id}<SessionWorkspace
      session={context.session}
      outcome={context.outcome}
      workflowStore={context.workflow}
      initialSettingsStage={page.url.searchParams.get('settings') ?? ''}
      onupdated={() => context.reload()}
    />{/key}{/if}
