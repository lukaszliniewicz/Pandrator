<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { ArrowRight, Eye, Search } from '@lucide/svelte';
  import { appState } from '$lib/app-state.svelte';
  import { errorMessage } from '$lib/errors';
  import { languageLabel } from '$lib/language-registry';
  import {
    projectSharingApi,
    type SharedProject
  } from '$lib/project-sharing-api';

  let items = $state<SharedProject[]>([]);
  let query = $state('');
  let appliedQuery = $state('');
  let offset = $state(0);
  let loading = $state(true);
  let error = $state('');
  let request = 0;
  const controller = new AbortController();
  async function load(nextOffset = 0, term = appliedQuery) {
    const current = ++request;
    loading = true;
    error = '';
    items = [];
    try {
      const value = await projectSharingApi.shared(
        term,
        nextOffset,
        controller.signal
      );
      if (request !== current) return;
      items = value.items;
      offset = nextOffset;
      appliedQuery = term;
    } catch (caught) {
      if (request === current) error = errorMessage(caught);
    } finally {
      if (request === current) loading = false;
    }
  }
  onMount(() => {
    if (appState.collaborationEnabled) void load();
    else loading = false;
  });
  onDestroy(() => {
    request++;
    controller.abort();
  });
</script>

<svelte:head><title>Shared with me · Pandrator</title></svelte:head>
<div class="mx-auto max-w-5xl">
  <p class="section-label flex items-center gap-2"><Eye size={17} />Can view</p>
  <h1 class="mt-2 text-3xl font-semibold tracking-tight">Shared with me</h1>
  <p class="muted mt-3 max-w-2xl text-sm">
    Read projects shared with your account and download their existing outputs.
    Projects you can edit are in your workspaces.
  </p>
  {#if !appState.collaborationEnabled}<p
      class="surface mt-6 rounded-2xl p-5 text-sm"
    >
      Project sharing is available when collaboration is enabled.
    </p>
  {:else}
    <form
      class="mt-6 flex max-w-xl gap-2"
      onsubmit={(event) => {
        event.preventDefault();
        void load(0, query.trim());
      }}
    >
      <label for="shared-project-search" class="sr-only"
        >Find a shared project</label
      ><input
        id="shared-project-search"
        class="min-w-0 flex-1 rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2"
        bind:value={query}
        maxlength="255"
        placeholder="Find a shared project"
      /><button class="btn btn-secondary" disabled={loading}
        ><Search size={16} />Find</button
      >
    </form>
    {#if error}<div class="mt-5 text-sm" role="alert">
        <p class="text-red-500">{error}</p>
        <button class="btn btn-secondary mt-3" onclick={() => void load(offset)}
          >Retry</button
        >
      </div>
    {:else if loading}<p class="muted mt-6 text-sm" role="status">
        Loading shared projects…
      </p>
    {:else if !items.length}<p class="surface mt-6 rounded-2xl p-5 text-sm">
        {offset
          ? 'No more shared projects.'
          : appliedQuery
            ? 'No shared projects match this search.'
            : 'No projects have been shared with you yet.'}
      </p>
    {:else}<ul class="mt-6 grid gap-3 sm:grid-cols-2">
        {#each items as project (project.project_id)}<li>
            <a
              class="surface group flex h-full items-start justify-between gap-4 rounded-2xl p-5 hover:border-[var(--accent)]"
              href={`/shared/${encodeURIComponent(project.source_session_id)}`}
              ><div class="min-w-0">
                <h2 class="break-words font-semibold">{project.name}</h2>
                <p class="muted mt-2 text-xs">
                  {languageLabel(project.source_language ?? '')} · {project.workflow_kind.replace(
                    '_',
                    ' '
                  )} · Can view
                </p>
                <p class="muted mt-2 text-xs">
                  Updated {new Date(project.updated_at).toLocaleDateString()}
                </p>
              </div>
              <ArrowRight
                size={18}
                class="mt-1 shrink-0 text-[var(--accent)]"
              /></a
            >
          </li>{/each}
      </ul>{/if}
    {#if !loading && (offset > 0 || items.length === 50)}<nav
        aria-label="Shared project pages"
        class="mt-5 flex gap-3"
      >
        <button
          class="btn btn-secondary"
          disabled={offset === 0}
          onclick={() => void load(Math.max(0, offset - 50))}>Previous</button
        ><button
          class="btn btn-secondary"
          disabled={items.length < 50}
          onclick={() => void load(offset + 50)}>Next</button
        >
      </nav>{/if}
  {/if}
</div>
