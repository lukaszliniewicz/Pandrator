<script lang="ts">
  import { onMount } from 'svelte';
  import type { TranslationProject } from './api-models';
  import { translationProjectApi } from './domain-api';
  import { errorMessage } from './errors';
  import { invalidationBus } from './invalidation';
  import {
    translationBranchStatus,
    translationLanguageName
  } from './translation-project-display';

  let { sessionId }: { sessionId: string } = $props();
  let project = $state<TranslationProject | null>(null);
  let loading = $state(true);
  let error = $state('');
  let request = 0;

  async function load(id: string) {
    const current = ++request;
    loading = true;
    try {
      const result = await translationProjectApi.forSession(id);
      if (current === request) {
        project = result.project;
        error = '';
      }
    } catch (caught) {
      if (current === request) error = errorMessage(caught);
    } finally {
      if (current === request) loading = false;
    }
  }

  $effect(() => {
    project = null;
    void load(sessionId);
  });

  onMount(() =>
    invalidationBus.subscribe((batch) => {
      const related = new Set([
        sessionId,
        project?.source_session_id,
        ...(project?.branches.map((branch) => branch.session_id) ?? [])
      ]);
      if (
        batch.session_ids.some((id) => related.has(id)) &&
        batch.resources.some((resource) =>
          ['sessions', 'workflow', 'jobs', 'output'].includes(resource)
        )
      ) {
        void load(sessionId);
      }
    })
  );
</script>

<section
  class="mt-3 max-w-3xl rounded-xl border border-[var(--line)] px-3.5 py-3"
  aria-label="Language versions"
>
  <div class="flex flex-wrap items-center justify-between gap-2 text-xs">
    <strong>Language versions</strong>
    <a
      class="font-semibold text-[var(--accent)]"
      href={`/sessions/${sessionId}/languages`}
    >
      {project ? 'Manage languages' : 'Create multilingual project'}
    </a>
  </div>
  {#if error}
    <p role="alert" class="mt-2 text-xs text-red-500">{error}</p>
  {:else if loading && !project}
    <p class="muted mt-2 text-xs" role="status">Loading language versions…</p>
  {:else if project?.branches.length}
    <ul class="mt-3 flex flex-wrap gap-2">
      {#each project.branches as branch (branch.id)}
        <li class="rounded-lg bg-[var(--paper-strong)] px-3 py-2 text-xs">
          {#if branch.trashed_at}
            <span class="muted"
              >{translationLanguageName(branch.target_language)} · In trash</span
            >
          {:else}
            <a
              class="font-semibold text-[var(--accent)]"
              href={`/sessions/${branch.session_id}/text`}
            >
              {translationLanguageName(branch.target_language)}
            </a>
            {#if branch.session_id === sessionId}<span class="muted">
                · This session</span
              >{/if}
            <span class="muted mt-1 block"
              >{translationBranchStatus(branch)}</span
            >
          {/if}
        </li>
      {/each}
    </ul>
  {:else}
    <p class="muted mt-2 text-xs">
      Start independent translations from one corrected source.
    </p>
  {/if}
</section>
