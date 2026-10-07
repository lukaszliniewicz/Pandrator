<script lang="ts">
  import { page } from '$app/state';
  import { Languages, LoaderCircle, Plus, RefreshCw } from '@lucide/svelte';
  import { onMount } from 'svelte';
  import type {
    TranslationProject,
    MultilingualSetup,
    TranslationProjectPayload,
    SubtitleReviewCatalogItem,
    ProjectReadiness
  } from '$lib/api-models';
  import { sessionApi, translationProjectApi } from '$lib/domain-api';
  import { errorMessage } from '$lib/errors';
  import { invalidationBus } from '$lib/invalidation';
  import { useSessionContext } from '$lib/session-context';
  import LanguagePicker from '$lib/LanguagePicker.svelte';
  import ProjectOperationPanel from '$lib/ProjectOperationPanel.svelte';
  import {
    translationBranchStatus,
    translationLanguageName as languageName
  } from '$lib/translation-project-display';

  const context = useSessionContext();
  const sessionId = $derived(String(page.params.id));
  let project = $state<TranslationProject | null>(null);
  let selectedBranches = $state<string[]>([]);
  function statusName(state: ProjectReadiness | undefined) {
    return (state?.status ?? 'unavailable').replaceAll('_', ' ');
  }
  function settingsOrigin(origin: string) {
    return (
      (
        {
          automatic_language_defaults: 'Automatic language defaults',
          copied_source: 'Copied source settings',
          custom_override: 'Custom override',
          unknown_historical: 'Historical settings; origin unrecorded'
        } as Record<string, string>
      )[origin] ?? origin
    );
  }
  function toggleBranch(id: string, checked: boolean) {
    selectedBranches = checked
      ? [...new Set([...selectedBranches, id])]
      : selectedBranches.filter((value) => value !== id);
  }
  let setup = $state<MultilingualSetup | null>(null);
  let setupState = $state<TranslationProjectPayload['setup_state']>('none');
  let readyCheckpointId = $state('');
  let blockedReason = $state('');
  let editingSetup = $state(false);
  let plannedLanguages = $state<string[]>([]);
  let plannedVoiceover = $state(false);
  let plannedSourceSubtitles = $state(true);
  let plannedCarrySubtitleSettings = $state(false);
  let carrySourceSubtitleSettings = $state(false);
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
  const transcription = $derived(
    context.workflow.snapshot?.stages.find(
      (stage) => stage.key === 'transcribe'
    )
  );
  const selectedSourceId = $derived(
    correction?.selected_artifact_id ||
      correction?.artifact?.id ||
      transcription?.selected_artifact_id ||
      transcription?.artifact?.id ||
      ''
  );
  const checkpointId = $derived(
    chosenCheckpointId ||
      (checkpointOptions.some((item) => item.artifact_id === selectedSourceId)
        ? selectedSourceId
        : '') ||
      readyCheckpointId ||
      checkpointOptions.findLast((item) => item.stage === 'correction')
        ?.artifact_id ||
      checkpointOptions.findLast((item) => item.stage === 'transcription')
        ?.artifact_id ||
      ''
  );
  const effectiveSourceLanguage = $derived(
    [
      project?.source_language,
      checkpointOptions.find((item) => item.artifact_id === checkpointId)
        ?.language,
      context.session?.source_language
    ].find((code) => code && code !== 'auto') ?? 'auto'
  );
  const excludedLanguages = $derived([
    effectiveSourceLanguage,
    ...(project?.branches.map((branch) => branch.target_language) ?? [])
  ]);
  const planValid = $derived(
    plannedLanguages.length > 0 &&
      plannedLanguages.length <= 20 &&
      !plannedLanguages.includes(effectiveSourceLanguage.toLowerCase())
  );
  const branchesValid = $derived(
    languages.length > 0 &&
      languages.length <= 20 &&
      !languages.some((code) =>
        excludedLanguages.some(
          (excluded) => excluded.toLowerCase() === code.toLowerCase()
        )
      )
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
        setup = result.setup;
        setupState = result.setup_state;
        blockedReason = result.setup_blocked_reason ?? '';
        readyCheckpointId =
          result.source_checkpoint_artifact_id ??
          result.correction_checkpoint_artifact_id ??
          '';
        if (!editingSetup) {
          plannedLanguages = [...(setup?.target_languages ?? [])];
          plannedVoiceover = setup?.generate_voiceover ?? false;
          plannedSourceSubtitles = setup?.keep_source_subtitles ?? true;
          plannedCarrySubtitleSettings =
            setup?.carry_source_subtitle_settings ?? false;
        }
        checkpointOptions =
          catalog?.items.filter(
            (item) =>
              ['correction', 'transcription'].includes(item.stage) &&
              item.state === 'current'
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
    selectedBranches = [];
    setup = null;
    editingSetup = false;
    readyCheckpointId = '';
    languages = [];
    carrySourceSubtitleSettings = false;
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
    if (
      !context.session ||
      !checkpointId ||
      busy ||
      editingSetup ||
      (setup && setupState !== 'ready')
    )
      return;
    busy = true;
    error = '';
    const id = sessionId;
    const body = {
      checkpoint_artifact_id: checkpointId,
      expected_revision: context.session.revision,
      name: name.trim() || context.session.name,
      create_planned_branches: Boolean(setup)
    };
    try {
      const result = await translationProjectApi.create(
        id,
        body,
        requestKey(body)
      );
      if (id === sessionId) {
        project = result.project;
        setupState = result.setup_state;
      }
      pending = null;
    } catch (caught) {
      if (id === sessionId) error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }

  async function addBranches(event: SubmitEvent) {
    event.preventDefault();
    if (!project || !branchesValid || busy) return;
    busy = true;
    error = '';
    const id = sessionId;
    const projectId = project.id;
    const body = {
      expected_revision: project.revision,
      targets: languages.map((target_language) => ({
        target_language,
        carry_source_subtitle_settings: carrySourceSubtitleSettings
      }))
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

  async function saveSetup(event: SubmitEvent) {
    event.preventDefault();
    if (!context.session || !planValid || busy) return;
    busy = true;
    error = '';
    const id = sessionId;
    const body = {
      multilingual_setup: {
        target_languages: plannedLanguages,
        generate_voiceover: plannedVoiceover,
        keep_source_subtitles: plannedSourceSubtitles,
        carry_source_subtitle_settings: plannedCarrySubtitleSettings
      }
    };
    try {
      await sessionApi.update(
        id,
        context.session.revision,
        body,
        requestKey({ expected_revision: context.session.revision, ...body })
      );
      pending = null;
      if (id === sessionId) {
        editingSetup = false;
        await context.reload();
        await load(id);
      }
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
        Share one source and edited video, with optional correction. Keep each
        language’s translation, voice and exports independent.
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
        New languages start from the pinned source and timeline. Changes within
        one language do not change the others.
      </p>
      {#if project.source_status}<p class="muted mt-2 break-all text-xs">
          Pinned source {project.source_status.pinned_checkpoint.revision_id ||
            project.checkpoint_artifact_id} · {new Date(
            project.source_status.pinned_checkpoint.revision_created_at ||
              project.source_status.pinned_checkpoint.created_at ||
              project.created_at
          ).toLocaleString()} · source {project.source_content_hash.slice(
            0,
            12
          )}
        </p>
        {#if project.source_status.source_changed}<div
            class="mt-3 rounded-xl border border-amber-500/40 bg-amber-500/10 p-3 text-sm"
            role="status"
          >
            <p class="font-semibold">A newer source is available.</p>
            <p class="mt-1">
              Existing languages keep their pinned source and timeline. Use the
              <a
                class="font-semibold underline"
                href={`/sessions/${project.source_session_id}`}>overview</a
              >
              to update the source for newly added languages.
            </p>
            {#each project.source_status.reasons as reason}<p
                class="muted mt-1 text-xs"
              >
                {reason}
              </p>{/each}
          </div>{/if}
      {/if}
    </section>
    <div class="flex flex-wrap items-center gap-4 text-sm">
      <button
        type="button"
        class="font-semibold text-[var(--accent)]"
        onclick={() =>
          (selectedBranches = project!.branches
            .filter((branch) => !branch.trashed_at)
            .slice(0, 20)
            .map((branch) => branch.id))}>Select available languages</button
      ><button
        type="button"
        class="font-semibold text-[var(--accent)]"
        onclick={() => (selectedBranches = [])}>Clear selection</button
      >
    </div>
    <div class="grid gap-4 lg:grid-cols-2">
      {#each project.branches as branch (branch.id)}
        <section class="surface rounded-2xl p-5">
          <div class="flex items-start justify-between gap-4">
            <label class="flex min-w-0 items-start gap-2"
              ><input
                type="checkbox"
                aria-label={`Select ${languageName(branch.target_language)}`}
                checked={selectedBranches.includes(branch.id)}
                disabled={Boolean(branch.trashed_at) ||
                  (!selectedBranches.includes(branch.id) &&
                    selectedBranches.length >= 20)}
                onchange={(event) =>
                  toggleBranch(branch.id, event.currentTarget.checked)}
              />
              <h3 class="text-lg font-semibold">
                {languageName(branch.target_language)}
              </h3>
            </label>
            <span class="muted text-xs uppercase">{branch.target_language}</span
            >
          </div>
          <p class="muted mt-2 text-sm">{branch.name}</p>
          <p class="mt-3 text-sm" role="status">
            {translationBranchStatus(branch)}
          </p>
          {#if branch.target_language_matches_settings === false}<p
              class="mt-2 text-sm text-red-500"
              role="alert"
            >
              Translation settings use {languageName(
                branch.effective_target_language || ''
              )}. Restore this branch's target language before submitting
              project actions.
            </p>{/if}
          {#if branch.readiness}<dl class="mt-3 grid grid-cols-2 gap-2 text-xs">
              {#each [{ label: 'Translation', state: branch.readiness.translation }, { label: 'Review', state: branch.readiness.review }, { label: 'Speech plan', state: branch.readiness.speech_plan }, { label: 'Voice', state: branch.readiness.voice }, { label: 'Generation', state: branch.readiness.generation }, { label: 'Configured output', state: branch.readiness.exports.configured }, { label: 'SRT subtitles', state: branch.readiness.exports.subtitles }] as item}
                <div class="rounded-lg border border-[var(--line)] p-2">
                  <dt class="muted">{item.label}</dt>
                  <dd class="mt-1 font-semibold">{statusName(item.state)}</dd>
                  {#if item.state.coverage_unverified}<p class="muted mt-1">
                      Language coverage unverified
                    </p>{/if}{#each item.state.reasons ?? [] as reason}<p
                      class="mt-1 break-words text-red-500"
                    >
                      {reason}
                    </p>{/each}{#if item.state.failure?.message}<p
                      class="mt-1 break-words text-red-500"
                    >
                      {item.state.failure.message}
                    </p>{/if}{#if item.state.active_job_id && item.state.progress_detail}<p
                      class="muted mt-1"
                    >
                      {item.state.progress_detail}
                    </p>{/if}
                </div>
              {/each}
            </dl>{/if}
          {#if branch.settings}<details class="mt-3 text-xs">
              <summary class="cursor-pointer font-semibold"
                >Effective voice and subtitle settings</summary
              >{#each [['Voice', branch.settings.tts], ['Subtitles', branch.settings.subtitles]] as entry}<div
                  class="mt-2 rounded-lg border border-[var(--line)] p-2"
                >
                  <p class="font-semibold">{entry[0] as string}</p>
                  <p class="muted mt-1">
                    {settingsOrigin(
                      (entry[1] as typeof branch.settings.tts).origin
                    )}
                  </p>
                  <dl class="mt-2 space-y-1">
                    {#each Object.entries((entry[1] as typeof branch.settings.tts).effective) as [key, value]}<div
                        class="flex flex-wrap justify-between gap-x-3"
                      >
                        <dt class="muted">{key.replaceAll('_', ' ')}</dt>
                        <dd class="break-all">
                          {typeof value === 'object'
                            ? JSON.stringify(value)
                            : String(value)}
                        </dd>
                      </div>{/each}
                  </dl>
                </div>{/each}
            </details>{/if}
          {#if !branch.trashed_at}
            <div
              class="mt-4 flex flex-wrap gap-4 text-sm font-semibold text-[var(--accent)]"
            >
              <a href={`/sessions/${branch.session_id}`}>Open language</a>
              <a href={`/sessions/${branch.session_id}/text`}>Subtitles</a>
              {#if branch.workflow_kind === 'voiceover'}<a
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
    <ProjectOperationPanel
      {project}
      selected={selectedBranches}
      onchanged={() => void load()}
    />
    <form onsubmit={addBranches} class="surface rounded-2xl p-6">
      <h3 class="text-lg font-semibold">Add languages</h3>
      <p class="muted mt-2 text-sm leading-6">
        Choose up to 20 languages. Each gets its own workspace, so translations
        can proceed in parallel.
      </p>
      <div class="mt-4 max-w-xl">
        <LanguagePicker
          bind:value={languages}
          excluded={excludedLanguages}
          disabled={busy}
        />
      </div>
      <label class="mt-4 flex items-start gap-2 text-sm">
        <input
          type="checkbox"
          bind:checked={carrySourceSubtitleSettings}
          disabled={busy}
        />
        <span
          >Use source subtitle settings in new languages<span
            class="muted mt-1 block text-xs"
            >Otherwise, each new language starts with automatic line-length and
            reading-speed limits for its own language.</span
          ></span
        >
      </label>
      <button
        type="submit"
        disabled={busy || !branchesValid}
        class="mt-5 flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-50"
      >
        {#if busy}<LoaderCircle class="animate-spin" size={16} />Creating
          language workspaces…{:else}<Plus size={16} />Add languages{/if}
      </button>
    </form>
  {:else}
    {#if setup}
      <section class="surface max-w-3xl rounded-2xl p-6">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <h3 class="text-lg font-semibold">Your language plan</h3>
          <button
            type="button"
            disabled={busy}
            onclick={() => {
              editingSetup = !editingSetup;
              plannedLanguages = [...setup!.target_languages];
              plannedVoiceover = setup!.generate_voiceover;
              plannedSourceSubtitles = setup!.keep_source_subtitles;
              plannedCarrySubtitleSettings =
                setup!.carry_source_subtitle_settings ?? false;
            }}
            class="text-sm font-semibold text-[var(--accent)]"
            >{editingSetup ? 'Cancel changes' : 'Edit language plan'}</button
          >
        </div>
        {#if editingSetup}
          <form onsubmit={saveSetup} class="mt-4 space-y-4">
            <LanguagePicker
              bind:value={plannedLanguages}
              excluded={[effectiveSourceLanguage]}
              disabled={busy}
            />
            <label class="flex items-start gap-2 text-sm"
              ><input
                type="checkbox"
                bind:checked={plannedVoiceover}
                disabled={busy}
              /><span>Include voiceovers in each language</span></label
            >
            <label class="flex items-start gap-2 text-sm"
              ><input
                type="checkbox"
                bind:checked={plannedSourceSubtitles}
                disabled={busy}
              /><span>Keep source subtitles alongside translations</span></label
            >
            <label class="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                bind:checked={plannedCarrySubtitleSettings}
                disabled={busy}
              />
              <span
                >Use source subtitle settings in new languages<span
                  class="muted mt-1 block text-xs"
                  >Otherwise, automatic limits follow each new language.</span
                ></span
              >
            </label>
            <button
              type="submit"
              disabled={busy || !planValid}
              class="rounded-xl bg-[var(--accent)] px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
              >{busy ? 'Saving…' : 'Save language plan'}</button
            >
          </form>
        {:else}
          <p class="mt-3 text-sm font-semibold">
            {setup.target_languages.map(languageName).join(', ')}
          </p>
          <p class="muted mt-2 text-sm">
            {setup.generate_voiceover
              ? 'Subtitles and voiceovers'
              : 'Subtitles only'} · {setup.keep_source_subtitles
              ? 'Source subtitles included'
              : 'Translated subtitles only'}
          </p>
          <p class="muted mt-2 text-sm">
            {setup.carry_source_subtitle_settings
              ? 'New languages use source subtitle settings.'
              : 'New languages use automatic subtitle limits for their own language.'}
          </p>
          <p class="mt-3 text-sm leading-6" role="status">
            {setupState === 'ready'
              ? 'The source is ready. Review it before creating your language versions.'
              : setupState === 'blocked'
                ? blockedReason
                : 'Create and review the source subtitles first. Your selected languages are saved.'}
          </p>
        {/if}
        <a
          href={`/sessions/${sessionId}/text${checkpointId ? `?review=${encodeURIComponent(checkpointId)}` : ''}`}
          class="mt-3 inline-block text-sm font-semibold text-[var(--accent)]"
          >Review source subtitles</a
        >
      </section>
    {/if}
    <form onsubmit={createProject} class="surface max-w-3xl rounded-2xl p-6">
      <h3 class="text-lg font-semibold">Create a multilingual project</h3>
      <p class="muted mt-2 text-sm leading-6">
        Save the current source checkpoint as the starting point for every
        language. {#if setup}Create all selected workspaces together;
          translation and voice generation start separately inside each one.{/if}
      </p>
      {#if checkpointId}
        {#if checkpointOptions.length > 1}
          <label
            for="project-correction"
            class="mt-5 block text-sm font-semibold">Source checkpoint</label
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
                >{item.stage === 'correction' ? 'Correction' : 'Transcription'} v{item.version}
                · {item.language ?? 'Source language'}</option
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
          disabled={busy ||
            !context.session ||
            editingSetup ||
            (Boolean(setup) && setupState !== 'ready')}
          class="mt-5 flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-50"
        >
          {#if busy}<LoaderCircle class="animate-spin" size={16} />Creating
            project…{:else}<Languages size={16} />{setup
              ? 'Create language workspaces'
              : 'Create multilingual project'}{/if}
        </button>
      {:else}<p class="mt-4 text-sm">
          Create and review source subtitles before adding languages.
        </p>
        <a
          class="mt-3 inline-block text-sm font-semibold text-[var(--accent)]"
          href={`/sessions/${sessionId}/text`}>Open text & subtitles</a
        >{/if}
    </form>
  {/if}
</div>
