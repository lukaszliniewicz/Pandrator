<script lang="ts">
  import { page } from '$app/state';
  import { Languages, LoaderCircle, Plus, RefreshCw } from '@lucide/svelte';
  import { onMount } from 'svelte';
  import type {
    TranslationProject,
    SubtitleReviewCatalogItem
  } from '$lib/api-models';
  import { sessionApi, translationProjectApi } from '$lib/domain-api';
  import { errorMessage } from '$lib/errors';
  import { invalidationBus } from '$lib/invalidation';
  import { useSessionContext } from '$lib/session-context';
  import { LANGUAGE_OPTIONS } from '$lib/settings-fields';
  import {
    translationBranchStatus,
    translationLanguageName as languageName
  } from '$lib/translation-project-display';

  const context = useSessionContext();
  const sessionId = $derived(String(page.params.id));
  let project = $state<TranslationProject | null>(null);
  let loading = $state(true);
  let busy = $state(false);
  let error = $state('');
  let name = $state('');
  let languages = $state<string[]>([]);
  let checkpointOptions = $state<SubtitleReviewCatalogItem[]>([]);
  let chosenCheckpointId = $state('');
  let request = 0;
  let pending: { signature: string; key: string } | null = null;
  const correction = $derived(
    context.workflow.snapshot?.stages.find((stage) => stage.key === 'correct')
  );
  const checkpointId = $derived(
    chosenCheckpointId ||
      correction?.selected_artifact_id ||
      correction?.artifact?.id ||
      checkpointOptions.at(-1)?.artifact_id ||
      ''
  );
  const availableLanguages = $derived(
    LANGUAGE_OPTIONS.filter((option) => {
      const code = String(option.value).toLowerCase();
      return (
        code !== 'auto' &&
        code !== project?.source_language.toLowerCase() &&
        !project?.branches.some(
          (branch) => branch.target_language.toLowerCase() === code
        )
      );
    })
  );

  async function load(id = sessionId) {
    const current = ++request;
    loading = true;
    try {
      const result = await translationProjectApi.forSession(id);
      const catalog = !result.project
        ? await sessionApi.subtitleCatalog(id)
        : null;
      if (current === request) {
        project = result.project;
        checkpointOptions =
          catalog?.items.filter(
            (item) => item.stage === 'correction' && item.state === 'current'
          ) ?? [];
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
    languages = [];
    checkpointOptions = [];
    chosenCheckpointId = '';
    name = '';
    pending = null;
    void load(sessionId);
  });

  function requestKey(body: object) {
    const signature = JSON.stringify(body);
    if (pending?.signature !== signature)
      pending = { signature, key: crypto.randomUUID() };
    return pending.key;
  }

  async function createProject(event: SubmitEvent) {
    event.preventDefault();
    if (!context.session || !checkpointId || busy) return;
    busy = true;
    error = '';
    const id = sessionId;
    const body = {
      checkpoint_artifact_id: checkpointId,
      expected_revision: context.session.revision,
      name: name.trim() || context.session.name
    };
    try {
      const result = await translationProjectApi.create(
        id,
        body,
        requestKey(body)
      );
      if (id === sessionId) project = result.project;
      pending = null;
    } catch (caught) {
      if (id === sessionId) error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }

  async function addBranches(event: SubmitEvent) {
    event.preventDefault();
    if (!project || !languages.length || languages.length > 20 || busy) return;
    busy = true;
    error = '';
    const id = sessionId;
    const projectId = project.id;
    const body = {
      expected_revision: project.revision,
      targets: languages.map((target_language) => ({ target_language }))
    };
    try {
      const result = await translationProjectApi.addBranches(
        projectId,
        body,
        requestKey(body)
      );
      if (id === sessionId) {
        project = result.project;
        languages = [];
      }
      pending = null;
    } catch (caught) {
      if (id === sessionId) error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }

  onMount(() =>
    invalidationBus.subscribe((batch) => {
      const related = new Set([
        sessionId,
        project?.source_session_id,
        ...(project?.branches.map((branch) => branch.session_id) ?? [])
      ]);
      if (
        !busy &&
        batch.session_ids.some((id) => related.has(id)) &&
        batch.resources.some((resource) =>
          ['sessions', 'workflow', 'jobs', 'output'].includes(resource)
        )
      )
        void load();
    })
  );
</script>

<div class="space-y-6">
  <div class="flex flex-wrap items-start justify-between gap-4">
    <div>
      <h2 class="flex items-center gap-3 text-2xl font-semibold">
        <Languages size={24} />Languages
      </h2>
      <p class="muted mt-2 max-w-3xl text-sm leading-6">
        Share one corrected source and edited video. Keep each language’s
        translation, voice and exports independent.
      </p>
    </div>
    <button
      type="button"
      onclick={() => void load()}
      disabled={loading || busy}
      class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-4 py-2 text-sm font-semibold"
      ><RefreshCw size={16} />Refresh</button
    >
  </div>
  {#if error}<p
      role="alert"
      class="rounded-xl bg-red-500/10 p-4 text-sm text-red-500"
    >
      {error}
    </p>{/if}
  {#if loading && !project}
    <p role="status" class="muted flex items-center gap-2">
      <LoaderCircle class="animate-spin" size={18} />Loading language project…
    </p>
  {:else if project}
    <section class="surface rounded-2xl p-6">
      <h3 class="text-lg font-semibold">{project.name}</h3>
      <p class="muted mt-2 text-sm">
        Source: <a
          class="font-semibold text-[var(--accent)]"
          href={`/sessions/${project.source_session_id}`}
          >{project.source_session_name}</a
        >
        · {languageName(project.source_language)}
      </p>
      <p class="muted mt-2 text-sm leading-6">
        New languages start from the saved correction and timeline. Changes
        within one language do not change the others.
      </p>
    </section>
    <div class="grid gap-4 lg:grid-cols-2">
      {#each project.branches as branch (branch.id)}
        <section class="surface rounded-2xl p-5">
          <div class="flex items-start justify-between gap-4">
            <h3 class="text-lg font-semibold">
              {languageName(branch.target_language)}
            </h3>
            <span class="muted text-xs uppercase">{branch.target_language}</span
            >
          </div>
          <p class="muted mt-2 text-sm">{branch.name}</p>
          <p class="mt-3 text-sm" role="status">
            {translationBranchStatus(branch)}
          </p>
          {#if !branch.trashed_at}
            <div
              class="mt-4 flex flex-wrap gap-4 text-sm font-semibold text-[var(--accent)]"
            >
              <a href={`/sessions/${branch.session_id}`}>Open language</a>
              <a href={`/sessions/${branch.session_id}/text`}>Subtitles</a>
              {#if context.session?.workflow_kind !== 'subtitles'}<a
                  href={`/sessions/${branch.session_id}/voice`}>Voice & audio</a
                >{/if}
              <a href={`/sessions/${branch.session_id}/output`}>Output</a>
            </div>
          {/if}
        </section>
      {:else}<p
          class="muted rounded-2xl border border-dashed border-[var(--line)] p-6"
        >
          Add languages below to start their translations.
        </p>{/each}
    </div>
    <form onsubmit={addBranches} class="surface rounded-2xl p-6">
      <h3 class="text-lg font-semibold">Add languages</h3>
      <p class="muted mt-2 text-sm leading-6">
        Choose up to 20 languages. Each gets its own workspace, so translations
        can proceed in parallel.
      </p>
      <label
        for="project-target-languages"
        class="mt-4 block text-sm font-semibold">Target languages</label
      >
      <select
        id="project-target-languages"
        multiple
        size="6"
        bind:value={languages}
        disabled={busy}
        class="mt-2 w-full max-w-lg rounded-xl border border-[var(--line)] bg-[var(--paper)] p-3"
      >
        {#each availableLanguages as option}<option value={String(option.value)}
            >{option.label}</option
          >{/each}
      </select>
      <p class="muted mt-2 text-xs">
        Hold Ctrl or Command to choose multiple languages. {languages.length} selected.
      </p>
      {#if languages.length > 20}<p
          role="alert"
          class="mt-2 text-sm text-red-500"
        >
          Choose at most 20 languages at a time.
        </p>{/if}
      <button
        type="submit"
        disabled={busy || !languages.length || languages.length > 20}
        class="mt-5 flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-50"
      >
        {#if busy}<LoaderCircle class="animate-spin" size={16} />Creating
          language workspaces…{:else}<Plus size={16} />Add languages{/if}
      </button>
    </form>
  {:else}
    <form onsubmit={createProject} class="surface max-w-3xl rounded-2xl p-6">
      <h3 class="text-lg font-semibold">Create a multilingual project</h3>
      <p class="muted mt-2 text-sm leading-6">
        Save the current corrected source as the starting point for every
        language.
      </p>
      {#if checkpointId}
        {#if checkpointOptions.length > 1}
          <label
            for="project-correction"
            class="mt-5 block text-sm font-semibold">Corrected source</label
          >
          <select
            id="project-correction"
            value={checkpointId}
            onchange={(event) =>
              (chosenCheckpointId = event.currentTarget.value)}
            disabled={busy}
            class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3"
          >
            {#each checkpointOptions as item}<option value={item.artifact_id}
                >Correction v{item.version} · {item.language ??
                  'Source language'}</option
              >{/each}
          </select>
        {/if}
        <label
          for="translation-project-name"
          class="mt-5 block text-sm font-semibold">Project name</label
        >
        <input
          id="translation-project-name"
          bind:value={name}
          placeholder={context.session?.name ?? 'Project name'}
          maxlength="255"
          disabled={busy}
          class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3"
        />
        <button
          type="submit"
          disabled={busy || !context.session}
          class="mt-5 flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-50"
        >
          {#if busy}<LoaderCircle class="animate-spin" size={16} />Creating
            project…{:else}<Languages size={16} />Create multilingual project{/if}
        </button>
      {:else}<p class="mt-4 text-sm">
          Finish subtitle correction before creating language branches.
        </p>
        <a
          class="mt-3 inline-block text-sm font-semibold text-[var(--accent)]"
          href={`/sessions/${sessionId}/text`}>Open text & subtitles</a
        >{/if}
    </form>
  {/if}
</div>
