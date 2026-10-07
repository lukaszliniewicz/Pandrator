<script lang="ts">
  import { getContext, onMount, untrack } from 'svelte';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { Languages, LoaderCircle, Plus, X } from '@lucide/svelte';
  import {
    PROJECT_WORKSPACE,
    type ProjectWorkspace
  } from './project-workspace.svelte';
  import { translationProjectApi } from './domain-api';
  import { errorMessage } from './errors';
  import { languageLabel } from './language-registry';
  import LanguagePicker from './LanguagePicker.svelte';

  let { plannedOnly = false }: { plannedOnly?: boolean } = $props();
  const workspace = getContext<ProjectWorkspace>(PROJECT_WORKSPACE);
  const store = $derived(workspace.source.languages);
  const source = $derived(workspace.source.record.session);
  const payload = $derived(store.value);
  const project = $derived(payload?.project);
  const branches = $derived(
    project?.branches.filter((branch) => !branch.trashed_at) ?? []
  );
  const chosen = $derived(
    branches.find((branch) => branch.session_id === workspace.selectedId)
  );
  const setup = $derived(payload?.setup);
  let adding = $state(false);
  let targets = $state<string[]>([]);
  let busy = $state(false);
  let error = $state('');
  let sourceUpdate: { signature: string; key: string } | null = null;
  let pending: {
    signature: string;
    createKey: string;
    addKey: string;
    sourceRevision: number;
    sourceName: string;
    carrySubtitleSettings: boolean;
    projectId?: string;
    projectRevision?: number;
  } | null = null;
  const checkpointId = $derived(
    workspace.source.workflow.snapshot?.stages.find(
      (stage) => stage.key === 'correct'
    )?.selected_artifact_id ||
      workspace.source.workflow.snapshot?.stages.find(
        (stage) => stage.key === 'correct'
      )?.artifact?.id ||
      workspace.source.workflow.snapshot?.stages.find(
        (stage) => stage.key === 'transcribe'
      )?.selected_artifact_id ||
      workspace.source.workflow.snapshot?.stages.find(
        (stage) => stage.key === 'transcribe'
      )?.artifact?.id ||
      payload?.source_checkpoint_artifact_id ||
      payload?.correction_checkpoint_artifact_id ||
      ''
  );
  const excluded = $derived([
    project?.source_language ?? source?.source_language ?? '',
    ...(project?.branches.map((branch) => branch.target_language) ?? [])
  ]);

  onMount(() => store.connect());
  $effect(() => {
    const current = store;
    void untrack(() => current.load()).catch(() => undefined);
  });

  function openAdd() {
    targets = [
      ...(setup?.target_languages.filter(
        (language) => !excluded.includes(language)
      ) ?? [])
    ];
    error = '';
    adding = true;
  }

  function select(id: string) {
    if (id !== workspace.selectedId)
      void goto(`/sessions/${id}${page.url.search}`);
  }

  async function useNewerSource() {
    const checkpoint = project?.source_status?.current_checkpoint?.artifact_id;
    if (!source || !project || !checkpoint || busy) return;
    const body = {
      expected_revision: project.revision,
      expected_source_revision: source.revision,
      checkpoint_artifact_id: checkpoint
    };
    const signature = JSON.stringify([project.id, body]);
    if (sourceUpdate?.signature !== signature)
      sourceUpdate = { signature, key: crypto.randomUUID() };
    busy = true;
    error = '';
    try {
      await translationProjectApi.updateSource(
        project.id,
        body,
        sourceUpdate.key
      );
      await store.load(true);
      sourceUpdate = null;
    } catch (caught) {
      error = errorMessage(caught);
      await store.load(true).catch(() => undefined);
    } finally {
      busy = false;
    }
  }

  async function addLanguages() {
    if (!source || !checkpointId || !targets.length || busy) return;
    const sourceId = source.id;
    const selectedTargets = [...targets];
    const signature = JSON.stringify([
      sourceId,
      checkpointId,
      selectedTargets,
      setup?.carry_source_subtitle_settings ?? false
    ]);
    if (pending?.signature !== signature)
      pending = {
        signature,
        createKey: crypto.randomUUID(),
        addKey: crypto.randomUUID(),
        sourceRevision: source.revision,
        sourceName: source.name,
        carrySubtitleSettings: setup?.carry_source_subtitle_settings ?? false
      };
    const keys = pending;
    busy = true;
    error = '';
    try {
      if (!keys.projectId) {
        let current = project;
        if (!current) {
          const created = await translationProjectApi.create(
            sourceId,
            {
              expected_revision: keys.sourceRevision,
              checkpoint_artifact_id: checkpointId,
              name: keys.sourceName
            },
            keys.createKey
          );
          current = created.project;
        }
        if (!current)
          throw new Error('The language project could not be created.');
        keys.projectId = current.id;
        keys.projectRevision = current.revision;
      }
      const result = await translationProjectApi.addBranches(
        keys.projectId,
        {
          expected_revision: keys.projectRevision!,
          targets: selectedTargets.map((target_language) => ({
            target_language,
            carry_source_subtitle_settings: keys.carrySubtitleSettings
          }))
        },
        keys.addKey
      );
      await Promise.all([store.load(true), workspace.source.record.load(true)]);
      adding = false;
      pending = null;
      const next = result.project?.branches.find((branch) =>
        selectedTargets.includes(branch.target_language)
      );
      if (next) select(next.session_id);
    } catch (caught) {
      error = errorMessage(caught);
      // A created empty project remains recoverable after a branch-creation failure.
      await store.load(true).catch(() => undefined);
    } finally {
      busy = false;
    }
  }
</script>

{#if !plannedOnly || setup || project || store.error}
  <section
    class="surface rounded-2xl border border-[var(--line)] p-4 sm:p-5"
    aria-label="Translation languages"
    data-testid="project-language-boundary"
  >
    <div
      class="flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-end sm:justify-between"
    >
      <div class="min-w-0 flex-1">
        <div class="mb-2 flex items-center gap-2 text-sm font-semibold">
          <Languages size={18} class="text-[var(--accent)]" />Translate
        </div>
        {#if branches.length && source}
          <label class="block max-w-sm text-xs font-semibold"
            >Target language
            <select
              aria-label="Target language"
              class="mt-1.5 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2.5 text-sm"
              value={workspace.selectedId}
              disabled={busy}
              onchange={(event) => select(event.currentTarget.value)}
            >
              <option value={source.id}
                >Original · {languageLabel(
                  project?.source_language || source.source_language
                )}</option
              >
              {#each branches as branch (branch.id)}
                <option value={branch.session_id}
                  >{languageLabel(branch.target_language)}</option
                >
              {/each}
            </select>
          </label>
          <p class="muted mt-2 text-xs">
            {chosen
              ? `${languageLabel(chosen.target_language)} translation, voices, generation and output are shown below.`
              : 'Original-language controls are shown below.'} Each language keeps
            its settings and recordings.
          </p>
        {:else if store.loading}
          <p class="muted text-sm" role="status">Loading languages…</p>
        {:else}
          <p class="muted text-sm">
            {setup
              ? setup.target_languages.map(languageLabel).join(', ')
              : 'Translate the selected source into one or more languages.'}
          </p>
        {/if}
      </div>
      <div class="flex flex-wrap gap-2">
        <button
          class="btn btn-sm btn-secondary"
          onclick={openAdd}
          disabled={busy || store.loading}
          ><Plus size={15} />Add language</button
        >
        {#if source && project}<a
            class="btn btn-sm btn-secondary"
            href={`/sessions/${source.id}/languages`}>Manage languages</a
          >{/if}
      </div>
    </div>
    {#if project?.source_status?.source_changed}
      <div
        class="mt-3 rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-3 text-sm"
        role="status"
      >
        <p>
          Newer source available. Existing languages keep the source version
          used for their translation. <a
            class="font-semibold underline"
            href={`/sessions/${project.source_session_id}/text`}
            >Review source</a
          >
        </p>
        {#if project.source_status.current_checkpoint?.artifact_id}
          <button
            class="btn btn-sm btn-secondary mt-2"
            disabled={busy}
            onclick={useNewerSource}
            >Use newer source for added languages</button
          >
        {/if}
      </div>
    {/if}
    {#if chosen && project && (chosen.source_content_hash !== project.source_content_hash || (chosen.source_checkpoint_role && chosen.source_checkpoint_role !== project.checkpoint_role))}
      <p class="muted mt-2 text-xs">
        This language uses an earlier source version.
      </p>
    {/if}
    {#if store.error || error}<p class="mt-3 text-sm text-red-500" role="alert">
        {error || store.error}
      </p>{/if}
    {#if adding}
      <div class="mt-4 border-t border-[var(--line)] pt-4">
        <div class="mb-3 flex items-center justify-between gap-2">
          <strong class="text-sm">Add target languages</strong><button
            class="btn btn-icon btn-secondary"
            aria-label="Close language selection"
            disabled={busy}
            onclick={() => (adding = false)}><X size={16} /></button
          >
        </div>
        <LanguagePicker bind:value={targets} {excluded} disabled={busy} />
        <p class="muted mt-3 text-xs">
          Uses the selected corrected source, or source subtitles when no
          correction is selected. Adding languages starts no provider work.
        </p>
        {#if !checkpointId}<p class="mt-2 text-sm" role="status">
            Prepare source subtitles before adding languages.
          </p>{/if}
        <button
          class="btn btn-primary mt-3"
          disabled={busy || !targets.length || !checkpointId}
          onclick={addLanguages}
          >{#if busy}<LoaderCircle size={16} class="animate-spin" />{/if}Add {targets.length ===
          1
            ? 'language'
            : 'languages'}</button
        >
      </div>
    {/if}
  </section>
{/if}
