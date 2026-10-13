<script lang="ts">
  import { page } from '$app/state';
  import { goto } from '$app/navigation';
  import { untrack } from 'svelte';
  import {
    ArchiveRestore,
    ArrowDown,
    ArrowUp,
    ChevronDown,
    CirclePlus,
    FileAudio,
    FileCheck2,
    FileText,
    Folder,
    MoreHorizontal,
    RefreshCw,
    Search,
    Trash2
  } from '@lucide/svelte';
  import type { ArtifactRecord, SessionRecord } from '$lib/api-models';
  import { artifactApi, sessionApi } from '$lib/domain-api';
  import { appState } from '$lib/app-state.svelte';
  import { apiJson } from '$lib/api';
  import { errorMessage } from '$lib/errors';
  import { artifactRoleLabel } from '$lib/artifact-display';
  import { WORKFLOW_LABELS } from '$lib/library-display';
  import { languageLabel } from '$lib/language-registry';
  import {
    folderPath,
    libraryProjects,
    projectActivity,
    projectActivityStatus,
    projectHref,
    versionLanguageLabel,
    type LibraryProject,
    type ProjectFolderRecord
  } from '$lib/project-library';
  import { ProjectFoldersStore } from '$lib/project-folders-store.svelte';
  import ArtifactPreview from '$lib/ArtifactPreview.svelte';
  import NewSessionWizard from '$lib/NewSessionWizard.svelte';
  import SessionDeleteDialog from '$lib/SessionDeleteDialog.svelte';
  import ProjectFolders from '$lib/ProjectFolders.svelte';
  import ProjectFolderDialog from '$lib/ProjectFolderDialog.svelte';
  import ProjectFolderAssignment from '$lib/ProjectFolderAssignment.svelte';

  const folders = new ProjectFoldersStore();
  let items = $state<SessionRecord[]>([]);
  let search = $state('');
  let workflowKind = $state('');
  let projectStatus = $state('');
  let sort = $state('updated');
  let variantsExpanded = $state<Record<string, boolean>>({});
  let loading = $state(true);
  let request = 0;
  let showTrash = $state(false);
  let expanded = $state('');
  let artifacts = $state<Record<string, ArtifactRecord[]>>({});
  let error = $state('');
  let preview = $state<ArtifactRecord | null>(null);
  let wizard = $state(false);
  let deleteTarget = $state<SessionRecord | null>(null);
  let retentionEnabled = $state(false);
  let folderDialog = $state(false);
  let editFolder = $state<ProjectFolderRecord | null>(null);
  let assignTarget = $state<LibraryProject | null>(null);
  let ordering = $state(false);
  let lastFolder = $state<string | null>(null);
  const selectedFolder = $derived(page.url.searchParams.get('folder') ?? '');
  const currentFolder = $derived(
    folders.value.items.find((folder) => folder.id === selectedFolder)
  );
  const allProjects = $derived(libraryProjects(items));
  const filtersActive = $derived(
    Boolean(search.trim() || workflowKind || projectStatus)
  );
  function matches(item: SessionRecord, name: string) {
    return (
      (!workflowKind || item.workflow_kind === workflowKind) &&
      (!projectStatus ||
        projectActivityStatus(item, appState.jobs) === projectStatus) &&
      `${name} ${item.name} ${versionLanguageLabel(item)} ${languageLabel(item.source_language)}`
        .toLowerCase()
        .includes(search.trim().toLowerCase())
    );
  }
  const groups = $derived.by(() => {
    const memberships = folders.value.memberships;
    const positions = new Map(
      memberships
        .filter((member) => member.folder_id === selectedFolder)
        .map((member) => [member.project_id, member.position])
    );
    const filed = new Set(memberships.map((member) => member.project_id));
    return allProjects
      .filter(
        (group) =>
          (selectedFolder === '' ||
            (selectedFolder === 'unfiled'
              ? !filed.has(group.id)
              : positions.has(group.id))) &&
          group.versions.some((item) => matches(item, group.name))
      )
      .sort((a, b) => {
        if (sort === 'manual' && currentFolder)
          return (positions.get(a.id) ?? 0) - (positions.get(b.id) ?? 0);
        if (sort === 'name') return a.name.localeCompare(b.name);
        const field = sort === 'created' ? 'created_at' : 'updated_at';
        return (
          Math.max(...b.versions.map((item) => Date.parse(item[field]))) -
          Math.max(...a.versions.map((item) => Date.parse(item[field])))
        );
      });
  });
  async function selectFolder(id: string) {
    const params = new URLSearchParams(page.url.search);
    if (id) params.set('folder', id);
    else params.delete('folder');
    sort = id && id !== 'unfiled' ? 'manual' : 'updated';
    await goto(`/sessions${params.size ? `?${params}` : ''}`, {
      noScroll: true,
      keepFocus: true
    });
  }
  async function load(includeTrashed = showTrash) {
    const current = ++request;
    loading = true;
    error = '';
    try {
      const [sessions, policy] = await Promise.all([
        sessionApi.list(includeTrashed),
        apiJson<{ days: number | null }>('/session-trash-policy'),
        folders.load(true)
      ]);
      if (current === request) {
        items = sessions.items;
        retentionEnabled = policy.days !== null;
      }
    } catch (caught) {
      if (current === request) error = errorMessage(caught);
    } finally {
      if (current === request) loading = false;
    }
  }
  async function expand(id: string) {
    expanded = expanded === id ? '' : id;
    if (expanded && !artifacts[id])
      artifacts[id] = (
        await artifactApi.list({ sessionId: id, limit: 200 })
      ).items;
  }
  async function trash(item: SessionRecord) {
    try {
      await sessionApi.trash(item.id, item.revision);
      await Promise.all([load(), appState.refreshSessions()]);
    } catch (caught) {
      error = errorMessage(caught);
    }
  }
  async function restore(item: SessionRecord) {
    try {
      await sessionApi.restore(item.id, item.revision);
      await Promise.all([load(), appState.refreshSessions()]);
    } catch (caught) {
      error = errorMessage(caught);
    }
  }
  async function reindex(item: SessionRecord) {
    try {
      const result = await sessionApi.reindex(item.id);
      error = result.reports.length
        ? `${result.reports.length} artifact issue(s) found.`
        : 'Reindex complete; no artifact problems found.';
    } catch (caught) {
      error = errorMessage(caught);
    }
  }
  async function move(group: LibraryProject, direction: number) {
    if (!currentFolder || ordering) return;
    const visibleIndex = groups.findIndex((item) => item.id === group.id);
    const neighbor = groups[visibleIndex + direction];
    if (!neighbor) return;
    const ids = folders.value.memberships
      .filter((member) => member.folder_id === currentFolder.id)
      .sort((a, b) => a.position - b.position)
      .map((member) => member.project_id);
    const here = ids.indexOf(group.id),
      there = ids.indexOf(neighbor.id);
    if (here < 0 || there < 0) return;
    [ids[here], ids[there]] = [ids[there], ids[here]];
    ordering = true;
    try {
      await folders.reorder(currentFolder, ids);
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      ordering = false;
    }
  }
  function newFolder() {
    editFolder = null;
    folderDialog = true;
  }
  $effect(() => {
    if (selectedFolder !== lastFolder) {
      lastFolder = selectedFolder;
      sort =
        selectedFolder && selectedFolder !== 'unfiled' ? 'manual' : 'updated';
    }
  });
  $effect(() => {
    const includeTrashed = showTrash;
    void untrack(() => load(includeTrashed));
  });
</script>

<div class="mx-auto max-w-7xl">
  <header class="flex flex-wrap items-end justify-between gap-5">
    <div>
      <h1 class="mt-2 text-4xl font-semibold">Projects</h1>
      <p class="muted mt-3">
        Shared sources, language versions and course folders.
      </p>
    </div>
    <div class="flex flex-wrap items-center gap-3">
      <label class="flex items-center gap-2 text-sm font-semibold"
        ><input
          type="checkbox"
          bind:checked={showTrash}
          class="accent-[var(--accent)]"
        />Show trash</label
      ><button onclick={() => (wizard = true)} class="btn btn-primary"
        ><CirclePlus size={17} />Add project</button
      >
    </div>
  </header>
  {#if showTrash}<p class="muted mt-4 text-sm">
      {retentionEnabled
        ? 'Automatic deletion follows each saved date while Pandrator is running. Unfinished work and shared dependencies can block cleanup.'
        : 'Automatic deletion is off. Projects remain recoverable until permanently deleted.'}
      <a class="underline" href="/settings#session-trash">Trash policy</a>
    </p>{/if}
  {#if error || folders.error}<div
      class="mt-5 rounded-xl border border-[var(--line)] bg-[var(--accent-soft)] p-3 text-sm"
      role="status"
    >
      {error || folders.error}
    </div>{/if}
  <div class="mt-7 grid min-w-0 gap-5 lg:grid-cols-[15rem_minmax(0,1fr)]">
    <aside class="min-w-0">
      <ProjectFolders
        snapshot={folders.value}
        selected={selectedFolder}
        onselect={(id) => void selectFolder(id)}
        oncreate={newFolder}
      />
    </aside>
    <div class="min-w-0">
      {#if currentFolder}<div
          class="mb-4 flex flex-wrap items-center justify-between gap-3"
        >
          <div class="min-w-0">
            <p class="muted break-words text-xs">
              {folderPath(currentFolder, folders.value.items)}
            </p>
            <h2
              class="mt-1 flex items-center gap-2 break-words text-lg font-semibold"
            >
              <Folder size={19} />{currentFolder.name}
            </h2>
          </div>
          <button
            class="btn btn-secondary"
            onclick={() => {
              editFolder = currentFolder!;
              folderDialog = true;
            }}>Edit folder</button
          >
        </div>{:else if selectedFolder === 'unfiled'}<h2
          class="mb-4 text-lg font-semibold"
        >
          Unfiled projects
        </h2>{:else if selectedFolder && !loading}<p
          class="mb-4 text-sm"
          role="alert"
        >
          This folder is unavailable. <button
            class="underline"
            onclick={() => selectFolder('')}>Show all projects</button
          >
        </p>{/if}
      <div
        class="flex items-center gap-3 rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] px-4"
      >
        <Search class="muted shrink-0" size={17} /><input
          bind:value={search}
          aria-label="Search projects or languages"
          placeholder="Search projects or languages"
          class="w-full min-w-0 bg-transparent py-3 outline-none"
        />
      </div>
      <div class="mt-3 flex flex-wrap items-end gap-3">
        <label class="text-xs font-semibold"
          >Workflow<select
            aria-label="Workflow"
            bind:value={workflowKind}
            class="mt-1 block rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-sm font-normal"
            ><option value="">All workflows</option
            >{#each Object.entries(WORKFLOW_LABELS) as [value, label]}<option
                {value}>{label}</option
              >{/each}</select
          ></label
        >
        <label class="text-xs font-semibold"
          >Status<select
            aria-label="Status"
            bind:value={projectStatus}
            class="mt-1 block rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-sm font-normal"
            ><option value="">All statuses</option
            >{#each [...new Set(items.map( (item) => projectActivityStatus(item, appState.jobs) ))].sort() as status}<option
                value={status}>{status.replaceAll('_', ' ')}</option
              >{/each}</select
          ></label
        >
        <label class="text-xs font-semibold"
          >Sort<select
            aria-label="Sort"
            bind:value={sort}
            class="mt-1 block rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-sm font-normal"
            >{#if currentFolder}<option value="manual">Course order</option
              >{/if}<option value="updated">Recently updated</option><option
              value="created">Recently created</option
            ><option value="name">Name</option></select
          ></label
        >
        <p role="status" class="muted ml-auto py-2 text-xs">
          {loading
            ? 'Loading projects…'
            : `${groups.length} project${groups.length === 1 ? '' : 's'}`}
        </p>
      </div>
      <div class="mt-5 space-y-3">
        {#each groups as group, index (group.id)}
          {@const nested = Boolean(group.membership)}
          {@const open =
            nested && (Boolean(variantsExpanded[group.id]) || filtersActive)}
          <article
            class="surface rounded-2xl border border-[var(--line)]"
            data-project-id={group.id}
          >
            <div class="flex items-start gap-3 p-4">
              {#if nested}<button
                  class="mt-0.5 shrink-0 rounded-lg p-1"
                  aria-label={`${open ? 'Collapse' : 'Expand'} languages for ${group.name}`}
                  aria-expanded={open}
                  aria-controls={`project-languages-${group.id}`}
                  onclick={() =>
                    (variantsExpanded[group.id] = !variantsExpanded[group.id])}
                  ><ChevronDown
                    size={18}
                    class={open ? '' : '-rotate-90'}
                  /></button
                >{/if}
              <div class="min-w-0 flex-1">
                <a
                  class="block break-words font-semibold"
                  href={projectHref(
                    group.sourceId,
                    selectedFolder === 'unfiled' ? '' : selectedFolder
                  )}>{group.name}</a
                >
                <p class="muted mt-1 text-xs">
                  {nested
                    ? `${languageLabel(group.source?.source_language || group.versions[0].source_language)} source · ${group.versions.length} language version${group.versions.length === 1 ? '' : 's'}`
                    : `${WORKFLOW_LABELS[group.versions[0].workflow_kind]} · ${projectActivity(group.versions[0], appState.jobs)} · Updated ${new Date(group.versions[0].updated_at).toLocaleString()}`}
                </p>
                {#if nested}<p class="muted mt-2 text-xs">
                    {group.versions.map(versionLanguageLabel).join(' · ')}
                  </p>{/if}
              </div>
              <div
                class="flex shrink-0 flex-wrap items-center justify-end gap-1"
              >
                {#if currentFolder && sort === 'manual'}<button
                    class="tool"
                    aria-label={`Move ${group.name} up`}
                    disabled={ordering || index === 0}
                    onclick={() => move(group, -1)}
                    ><ArrowUp size={15} /></button
                  ><button
                    class="tool"
                    aria-label={`Move ${group.name} down`}
                    disabled={ordering || index === groups.length - 1}
                    onclick={() => move(group, 1)}
                    ><ArrowDown size={15} /></button
                  >{/if}
                <button
                  class="tool"
                  aria-label={`Folders for ${group.name}`}
                  title="Project folders"
                  onclick={() => (assignTarget = group)}
                  ><Folder size={16} /></button
                >
                {#if !nested}{@render itemActions(
                    group.versions[0],
                    false
                  )}{:else if group.source}<details class="relative">
                    <summary
                      class="tool list-none"
                      aria-label={`Project actions for ${group.name}`}
                      ><MoreHorizontal size={16} /></summary
                    >
                    <div
                      class="absolute right-0 z-20 mt-2 min-w-56 rounded-xl border border-[var(--line)] bg-[var(--paper)] p-3 shadow-lg"
                    >
                      <button
                        class="flex w-full items-center gap-2 rounded-lg p-2 text-left text-xs"
                        onclick={() => reindex(group.source!)}
                        ><RefreshCw size={15} />Reindex shared files</button
                      >{#if group.source.trashed_at}<button
                          class="flex w-full items-center gap-2 rounded-lg p-2 text-left text-xs"
                          onclick={() => restore(group.source!)}
                          ><ArchiveRestore size={15} />Restore shared source</button
                        >{:else}<button
                          class="flex w-full items-center gap-2 rounded-lg p-2 text-left text-xs text-red-500"
                          onclick={() => trash(group.source!)}
                          ><Trash2 size={15} />Trash shared source</button
                        >
                        <p class="muted px-2 pt-1 text-xs">
                          Adding languages requires an available shared source.
                        </p>{/if}
                    </div>
                  </details>{/if}
              </div>
            </div>
            {#if nested}<div
                id={`project-languages-${group.id}`}
                hidden={!open}
                class="mx-4 mb-4 ml-7 border-l border-[var(--line)] pl-4"
              >
                <div
                  class="flex flex-wrap items-center gap-3 border-t border-[var(--line)] py-3"
                >
                  <FileCheck2 size={16} class="muted shrink-0" />
                  <div class="min-w-0 flex-1">
                    <p class="text-sm font-semibold">
                      Shared source · {languageLabel(
                        group.source?.source_language ||
                          group.versions[0].source_language
                      )}
                    </p>
                    <p class="muted mt-1 text-xs">
                      Source preparation, transcription and correction
                    </p>
                    {#if group.source?.trashed_at}<p
                        class="mt-1 text-xs text-red-500"
                      >
                        Shared source is in Trash.
                      </p>{/if}
                  </div>
                  <a
                    class="text-xs font-semibold text-[var(--accent)]"
                    href={projectHref(
                      group.sourceId,
                      selectedFolder === 'unfiled' ? '' : selectedFolder,
                      '/sources'
                    )}>Review source</a
                  >
                </div>
                {#each group.versions as item (item.id)}<div
                    class="border-t border-[var(--line)] py-3"
                  >
                    <div class="flex flex-wrap items-center gap-3">
                      <a
                        href={projectHref(
                          item.id,
                          selectedFolder === 'unfiled' ? '' : selectedFolder
                        )}
                        class="min-w-0 flex-1 rounded-lg"
                        class:language-match={filtersActive &&
                          matches(item, group.name)}
                        ><p class="text-sm font-semibold">
                          {versionLanguageLabel(
                            item
                          )}{#if !item.target_language}<span
                              class="muted font-normal"
                            >
                              · Original language</span
                            >{/if}
                        </p>
                        <p class="muted mt-1 text-xs">
                          {WORKFLOW_LABELS[item.workflow_kind]} · {projectActivity(
                            item,
                            appState.jobs
                          )}
                        </p>
                        {#if item.trashed_at}<p class="muted mt-1 text-xs">
                            {item.status === 'purging'
                              ? 'Permanent deletion in progress or awaiting retry'
                              : retentionEnabled && item.purge_after
                                ? `Scheduled for deletion ${new Date(item.purge_after).toLocaleString()}`
                                : 'Kept in Trash'}
                          </p>{/if}</a
                      ><a
                        class="text-xs font-semibold text-[var(--accent)]"
                        href={projectHref(
                          item.id,
                          selectedFolder === 'unfiled' ? '' : selectedFolder
                        )}>Open language</a
                      >{@render itemActions(item, item.id === group.sourceId)}
                    </div>
                    {#if expanded === item.id}{@render artifactList(item)}{/if}
                  </div>{/each}
              </div>{:else if expanded === group.versions[0].id}{@render artifactList(
                group.versions[0]
              )}{/if}
          </article>
        {:else}<div class="surface muted rounded-2xl p-8 text-center">
            {loading
              ? 'Loading projects…'
              : selectedFolder && !filtersActive
                ? 'This folder has no visible projects. Use a project’s folder button to add it here.'
                : 'No matching projects.'}
          </div>{/each}
      </div>
    </div>
  </div>
</div>

{#snippet itemActions(item: SessionRecord, sharedOwner: boolean)}
  <button
    onclick={() => reindex(item)}
    title="Reindex artifacts"
    aria-label={`Reindex files for ${item.name}`}
    class="tool"><RefreshCw size={16} /></button
  >
  {#if !sharedOwner}
    {#if item.status === 'trashed' || item.status === 'purging'}{#if item.status !== 'purging'}<button
          onclick={() => restore(item)}
          class="tool"><ArchiveRestore size={16} />Restore</button
        >{/if}<button
        class="tool text-red-600"
        onclick={() => (deleteTarget = item)}
        ><Trash2 size={16} />{item.status === 'purging'
          ? 'Retry deletion'
          : 'Delete permanently'}</button
      >{:else}<button onclick={() => trash(item)} class="tool text-red-500"
        ><Trash2 size={16} />Trash</button
      >{/if}
  {/if}
  <button
    onclick={() => expand(item.id)}
    class="tool"
    aria-label={`${expanded === item.id ? 'Collapse' : 'Expand'} artifacts for ${item.name}`}
    aria-expanded={expanded === item.id}
    ><ChevronDown
      class={expanded === item.id ? 'rotate-180' : ''}
      size={17}
    /></button
  >
{/snippet}

{#snippet artifactList(item: SessionRecord)}
  <div class="mt-3 border-t border-[var(--line)] bg-[var(--paper)] p-3">
    <div class="section-label mb-3">Artifacts</div>
    <div class="grid gap-2 md:grid-cols-2">
      {#each artifacts[item.id] ?? [] as artifact}<button
          onclick={() => (preview = artifact)}
          class="flex min-w-0 items-center gap-3 rounded-xl border border-[var(--line)] p-3 text-left"
          >{#if artifact.kind === 'audio'}<FileAudio
              size={17}
              class="shrink-0"
            />{:else}<FileText size={17} class="shrink-0" />{/if}
          <div class="min-w-0">
            <div class="truncate text-sm font-semibold">
              {artifactRoleLabel(artifact.role)}
            </div>
            <div class="muted truncate text-xs">
              {artifact.relative_path} · {artifact.state}
            </div>
          </div></button
        >{:else}<p class="muted text-sm">No registered artifacts.</p>{/each}
    </div>
  </div>
{/snippet}

{#if preview}<ArtifactPreview
    artifact={preview}
    onclose={() => (preview = null)}
  />{/if}
{#if wizard}<NewSessionWizard onclose={() => (wizard = false)} />{/if}
{#if deleteTarget}<SessionDeleteDialog
    session={deleteTarget}
    onclose={() => (deleteTarget = null)}
    ondeleted={async () => {
      await load();
      await appState.refresh();
    }}
  />{/if}
{#if folderDialog}<ProjectFolderDialog
    store={folders}
    folder={editFolder}
    parentId={currentFolder?.id ?? null}
    onclose={() => (folderDialog = false)}
    onsaved={(id) => void selectFolder(id ?? '')}
  />{/if}
{#if assignTarget}<ProjectFolderAssignment
    store={folders}
    projectId={assignTarget.id}
    projectName={assignTarget.name}
    onclose={() => (assignTarget = null)}
  />{/if}

<style>
  .tool {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    border: 1px solid var(--line);
    border-radius: 0.7rem;
    padding: 0.55rem 0.6rem;
    font-size: 0.75rem;
    font-weight: 600;
  }
  .language-match {
    background: var(--accent-soft);
    padding: 0.4rem 0.5rem;
  }
</style>
