<script lang="ts">
  import { projectLinkUrl } from '$lib/project-link-context';
  import { page } from '$app/state';
  import { goto } from '$app/navigation';
  import { onDestroy, onMount } from 'svelte';
  import { Download, Eye, RefreshCw } from '@lucide/svelte';
  import { appState } from '$lib/app-state.svelte';
  import { sessionApi, translationProjectApi } from '$lib/domain-api';
  import { artifactRoleLabel, formatBytes } from '$lib/artifact-display';
  import type {
    SessionRecord,
    TranslationProjectPayload
  } from '$lib/api-models';
  import ArtifactPreview from '$lib/ArtifactPreview.svelte';
  import { errorMessage } from '$lib/errors';
  import { ApiError } from '$lib/api';
  import { languageLabel } from '$lib/language-registry';
  import {
    projectSharingApi,
    type SharedProjectFile
  } from '$lib/project-sharing-api';

  let source = $state<SessionRecord | null>(null);
  let group = $state<TranslationProjectPayload['project']>(null);
  let files = $state<SharedProjectFile[]>([]);
  let selectedId = $state('');
  let preview = $state<SharedProjectFile | null>(null);
  let offset = $state(0);
  let hasMore = $state(false);
  let loading = $state(true);
  let error = $state('');
  let showHistory = $state(false);
  let mounted = $state(false);
  let request = 0;
  let lastRefresh = 0;
  let controller: AbortController | undefined;
  const languages = $derived(
    source
      ? [
          {
            id: group?.source_session_id ?? source.id,
            label: `${languageLabel(group?.source_language ?? source.source_language)} · Original`
          },
          ...(group?.branches ?? []).map((branch) => ({
            id: branch.session_id,
            label: languageLabel(branch.target_language)
          }))
        ]
      : []
  );
  const visibleFiles = $derived(
    files.filter((file) => showHistory || file.state === 'current')
  );
  const previewMime: Record<string, string> = {
    txt: 'text/plain',
    srt: 'text/plain',
    vtt: 'text/vtt',
    ass: 'text/plain',
    json: 'application/json',
    pdf: 'application/pdf',
    mp3: 'audio/mpeg',
    wav: 'audio/wav',
    mp4: 'video/mp4',
    webm: 'video/webm',
    flac: 'audio/flac',
    png: 'image/png',
    jpg: 'image/jpeg'
  };

  async function load(sourceId: string, requestedLanguage: string) {
    const current = ++request;
    controller?.abort();
    controller = new AbortController();
    loading = true;
    source = null;
    group = null;
    files = [];
    preview = null;
    offset = 0;
    hasMore = false;
    error = '';
    lastRefresh = Date.now();
    try {
      const [record, payload] = await Promise.all([
        sessionApi.get(sourceId),
        translationProjectApi.forSession(sourceId, 'compact')
      ]);
      if (request !== current) return;
      const ids = new Set([
        payload.project?.source_session_id ?? record.id,
        ...(payload.project?.branches ?? []).map((branch) => branch.session_id)
      ]);
      const languageId = ids.has(requestedLanguage)
        ? requestedLanguage
        : (payload.project?.source_session_id ?? record.id);
      const value = await projectSharingApi.files(
        languageId,
        0,
        controller.signal
      );
      if (request !== current) return;
      source = record;
      group = payload.project;
      selectedId = languageId;
      files = value.items;
      hasMore = value.items.length === 50;
    } catch (caught) {
      if (request === current) {
        source = null;
        group = null;
        files = [];
        error =
          caught instanceof ApiError && caught.status === 404
            ? 'This project is unavailable or your viewing access has changed.'
            : errorMessage(caught);
      }
    } finally {
      if (request === current) loading = false;
    }
  }
  async function more() {
    if (loading || !selectedId || !hasMore) return;
    const current = request;
    loading = true;
    try {
      const value = await projectSharingApi.files(
        selectedId,
        offset + 50,
        controller?.signal
      );
      if (request !== current) return;
      offset += 50;
      files = [...files, ...value.items];
      hasMore = value.items.length === 50;
    } catch (caught) {
      if (request === current) error = errorMessage(caught);
    } finally {
      if (request === current) loading = false;
    }
  }
  function refresh() {
    if (
      !mounted ||
      loading ||
      Date.now() - lastRefresh < 1000 ||
      !appState.collaborationEnabled
    )
      return;
    void load(
      page.params.id ?? '',
      page.url.searchParams.get('language') ?? ''
    );
  }
  $effect(() => {
    const id = page.params.id ?? '';
    const language = page.url.searchParams.get('language') ?? '';
    if (mounted && appState.collaborationEnabled) void load(id, language);
  });
  onMount(() => {
    mounted = true;
    if (!appState.collaborationEnabled) loading = false;
  });
  onDestroy(() => {
    mounted = false;
    request++;
    controller?.abort();
  });
</script>

<svelte:window onfocus={refresh} />
<svelte:document
  onvisibilitychange={() => {
    if (document.visibilityState === 'visible') refresh();
  }}
/>
<svelte:head
  ><title>{group?.name ?? source?.name ?? 'Shared project'} · Pandrator</title
  ></svelte:head
>
<div class="mx-auto max-w-6xl">
  <a href="/shared" class="text-sm font-semibold text-[var(--accent)]"
    >← Shared with me</a
  >
  <p class="section-label mt-6 flex items-center gap-2">
    <Eye size={16} />Can view
  </p>
  <h1 class="mt-2 break-words text-3xl font-semibold tracking-tight">
    {group?.name ?? source?.name ?? 'Shared project'}
  </h1>
  <p class="muted mt-3 text-sm">
    Saved work and existing outputs. Editing and processing are available to
    workspace members.
  </p>
  {#if !appState.collaborationEnabled}<p
      class="surface mt-6 rounded-2xl p-5 text-sm"
    >
      Project sharing is available when collaboration is enabled.
    </p>
  {:else if error}<div class="mt-6" role="alert">
      <p class="text-sm text-red-500">{error}</p>
      <button
        class="btn btn-secondary mt-3"
        onclick={() =>
          void load(
            page.params.id ?? '',
            page.url.searchParams.get('language') ?? ''
          )}>Try again</button
      >
    </div>
  {:else if loading && !source}<p role="status" class="muted mt-6 text-sm">
      Loading shared project…
    </p>
  {:else if source}
    <div class="mt-6 flex flex-wrap items-end justify-between gap-4">
      <label class="text-sm font-semibold" for="shared-project-language"
        >Project language<select
          id="shared-project-language"
          class="mt-2 block max-w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
          value={selectedId}
          onchange={(event) =>
            void goto(
              `/shared/${encodeURIComponent(source!.id)}?language=${encodeURIComponent(event.currentTarget.value)}`
            )}
          >{#each languages as language (language.id)}<option
              value={language.id}>{language.label}</option
            >{/each}</select
        ></label
      >
      <button
        class="btn btn-secondary"
        disabled={loading}
        onclick={() => void load(page.params.id ?? '', selectedId)}
        ><RefreshCw size={16} />Refresh</button
      >
    </div>
    <div class="mt-6 flex flex-wrap items-center justify-between gap-3">
      <h2 class="text-lg font-semibold">Saved files</h2>
      <label class="flex items-center gap-2 text-sm"
        ><input type="checkbox" bind:checked={showHistory} />Include earlier
        versions</label
      >
    </div>
    {#if !visibleFiles.length}<p class="surface mt-4 rounded-2xl p-5 text-sm">
        {files.length
          ? 'No current files in this page. Include earlier versions or load more files.'
          : 'No saved files for this language yet.'}
      </p>{/if}
    <ul class="mt-4 grid gap-3">
      {#each visibleFiles as file (file.id)}<li
          class="surface flex flex-wrap items-center justify-between gap-4 rounded-2xl p-4"
        >
          <div class="min-w-0">
            <h3 class="break-words font-semibold">
              {artifactRoleLabel(file.role)}
              {#if previewMime[file.kind]}<span
                  class="muted ml-2 text-xs font-normal uppercase"
                  >{file.kind}</span
                >{/if}
            </h3>
            <p class="muted mt-1 text-xs">
              {new Date(file.created_at).toLocaleString()} · {formatBytes(
                file.size_bytes
              )}{file.state !== 'current' ? ' · Earlier version' : ''}
            </p>
          </div>
          <div class="flex gap-2">
            <button
              class="btn btn-secondary"
              onclick={() => {
                preview = {
                  ...file,
                  relative_path: `saved.${file.kind}`,
                  mime_type: file.mime_type || previewMime[file.kind],
                  metadata_json: {
                    ...file.metadata_json,
                    original_filename: `${artifactRoleLabel(file.role)}.${file.kind}`
                  }
                };
              }}
              >View<span class="sr-only">
                {artifactRoleLabel(file.role)}</span
              ></button
            ><a
              class="btn btn-secondary"
              href={projectLinkUrl(
                `/api/v1/artifacts/${encodeURIComponent(file.id)}/content?download=1`
              )}
              download
              ><Download size={16} />Download<span class="sr-only">
                {artifactRoleLabel(file.role)}</span
              ></a
            >
          </div>
        </li>{/each}
    </ul>
    {#if hasMore}<button
        class="btn btn-secondary mt-4"
        disabled={loading}
        onclick={() => void more()}
        >{loading ? 'Loading…' : 'Load more files'}</button
      >{/if}
  {/if}
</div>
{#if preview}<ArtifactPreview
    artifact={preview}
    onclose={() => (preview = null)}
  />{/if}
