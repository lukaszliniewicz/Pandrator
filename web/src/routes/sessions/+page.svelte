<script lang="ts">
  import { errorMessage } from '$lib/errors';
  import {
    ArchiveRestore,
    ChevronDown,
    CirclePlus,
    FileAudio,
    FileText,
    RefreshCw,
    Search,
    Trash2
  } from '@lucide/svelte';
  import type { ArtifactRecord, SessionRecord } from '$lib/api-models';
  import { artifactApi, sessionApi } from '$lib/domain-api';
  import { appState } from '$lib/app-state.svelte';
  import ArtifactPreview from '$lib/ArtifactPreview.svelte';
  import NewSessionWizard from '$lib/NewSessionWizard.svelte';
  import SessionDeleteDialog from '$lib/SessionDeleteDialog.svelte';
  import { apiJson } from '$lib/api';
  import { artifactRoleLabel } from '$lib/artifact-display';
  import { WORKFLOW_LABELS } from '$lib/library-display';
  import { languageLabel } from '$lib/language-registry';
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
  const visible = $derived(
    items.filter(
      (item) =>
        (!workflowKind || item.workflow_kind === workflowKind) &&
        (!projectStatus || item.status === projectStatus) &&
        `${item.name} ${item.translation_project?.name ?? ''} ${languageLabel(item.target_language || item.source_language)}`
          .toLowerCase()
          .includes(search.trim().toLowerCase())
    )
  );
  const groups = $derived.by(() => {
    const grouped = new Map<
      string,
      { project: SessionRecord['translation_project']; items: SessionRecord[] }
    >();
    const matched = new Set(visible.map((item) => item.id));
    for (const item of items) {
      const key = item.translation_project?.id || item.id;
      const group = grouped.get(key) ?? {
        project: item.translation_project,
        items: []
      };
      group.items.push(item);
      grouped.set(key, group);
    }
    return [...grouped.values()]
      .filter((group) => group.items.some((item) => matched.has(item.id)))
      .map((group) => ({
        ...group,
        items: group.items.sort(
          (a, b) =>
            Number(b.translation_project?.role === 'source') -
            Number(a.translation_project?.role === 'source')
        )
      }))
      .sort((a, b) =>
        sort === 'name'
          ? (a.project?.name ?? a.items[0].name).localeCompare(
              b.project?.name ?? b.items[0].name
            )
          : sort === 'created'
            ? Math.max(...b.items.map((i) => Date.parse(i.created_at))) -
              Math.max(...a.items.map((i) => Date.parse(i.created_at)))
            : Math.max(...b.items.map((i) => Date.parse(i.updated_at))) -
              Math.max(...a.items.map((i) => Date.parse(i.updated_at)))
      );
  });
  async function load() {
    const current = ++request;
    loading = true;
    error = '';
    try {
      const [sessions, policy] = await Promise.all([
        sessionApi.list(showTrash),
        apiJson<{ days: number | null }>('/session-trash-policy')
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
      const updated = await sessionApi.trash(item.id, item.revision);
      items = items.map((value) => (value.id === item.id ? updated : value));
      await appState.refresh();
    } catch (caught) {
      error = errorMessage(caught);
    }
  }
  async function restore(item: SessionRecord) {
    try {
      const updated = await sessionApi.restore(item.id, item.revision);
      items = items.map((value) => (value.id === item.id ? updated : value));
      await appState.refresh();
    } catch (caught) {
      error = errorMessage(caught);
    }
  }
  async function reindex(item: SessionRecord) {
    const result = await sessionApi.reindex(item.id);
    error = result.reports.length
      ? `${result.reports.length} artifact issue(s) found.`
      : 'Reindex complete; no artifact problems found.';
  }
  $effect(() => {
    void showTrash;
    void load();
  });
</script>

<div class="mx-auto max-w-7xl">
  <header class="flex flex-wrap items-end justify-between gap-5">
    <div>
      <h1 class="mt-2 text-4xl font-semibold">Projects</h1>
      <p class="muted mt-3">
        Recordings, audiobooks, subtitles and their language versions.
      </p>
    </div>
    <div class="flex flex-wrap items-center gap-3">
      <label class="flex items-center gap-2 text-sm font-semibold"
        ><input
          type="checkbox"
          bind:checked={showTrash}
          class="accent-[var(--accent)]"
        /> Show trash</label
      ><button
        onclick={() => (wizard = true)}
        class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white"
        ><CirclePlus size={17} /> Add project</button
      >
    </div>
  </header>
  {#if showTrash}<p class="muted mt-4 text-sm">
      {retentionEnabled
        ? 'Automatic deletion follows each saved date while Pandrator is running. Unfinished work and shared dependencies can block cleanup.'
        : 'Automatic deletion is off. Projects remain recoverable until permanently deleted.'}
      <a class="underline" href="/settings#session-trash">Trash policy</a>
    </p>{/if}
  {#if error}<div
      class="mt-5 rounded-xl border border-[var(--line)] bg-[var(--accent-soft)] p-3 text-sm"
    >
      {error}
    </div>{/if}
  <div
    class="mt-7 flex items-center gap-3 rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] px-4"
  >
    <Search class="muted" size={17} /><input
      bind:value={search}
      aria-label="Search projects"
      placeholder="Search projects"
      class="w-full bg-transparent py-3 outline-none"
    />
  </div>
  <div class="mt-3 flex flex-wrap items-end gap-3">
    <label class="text-xs font-semibold"
      >Workflow<select
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
        bind:value={projectStatus}
        class="mt-1 block rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-sm font-normal"
        ><option value="">All statuses</option
        >{#each [...new Set(items.map((item) => item.status))].sort() as status}<option
            value={status}>{status.replaceAll('_', ' ')}</option
          >{/each}</select
      ></label
    >
    <label class="text-xs font-semibold"
      >Sort<select
        bind:value={sort}
        class="mt-1 block rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-sm font-normal"
        ><option value="updated">Recently updated</option><option
          value="created">Recently created</option
        ><option value="name">Name</option></select
      ></label
    >
    <p role="status" class="muted ml-auto py-2 text-xs">
      {loading ? 'Loading projects…' : `${groups.length} projects`}
    </p>
  </div>
  <div class="surface mt-5 overflow-hidden rounded-2xl">
    {#each groups as group}
      {#if group.project}<header
          class="border-b border-[var(--line)] bg-[var(--accent-soft)] px-4 py-3"
        >
          <a
            class="text-sm font-semibold text-[var(--accent)]"
            href={`/sessions/${group.project.source_session_id}/languages`}
            >{group.project.name}</a
          >
          <p class="muted mt-1 text-xs">
            One corrected source · {group.items.filter(
              (item) => item.translation_project?.role === 'branch'
            ).length} language {group.items.filter(
              (item) => item.translation_project?.role === 'branch'
            ).length === 1
              ? 'version'
              : 'versions'}
          </p>
          <div class="mt-2 flex flex-wrap items-center gap-2">
            {#each group.items as version (version.id)}<a
                href={`/sessions/${version.id}`}
                class="rounded-lg border border-[var(--line)] bg-[var(--paper)] px-2 py-1 text-xs font-semibold"
                >{version.translation_project?.role === 'source'
                  ? 'Source'
                  : languageLabel(
                      version.target_language ||
                        version.translation_project?.target_language ||
                        ''
                    )}</a
              >{/each}
            <button
              class="ml-auto text-xs font-semibold text-[var(--accent)]"
              aria-expanded={Boolean(variantsExpanded[group.project.id])}
              onclick={() =>
                (variantsExpanded[group.project!.id] =
                  !variantsExpanded[group.project!.id])}
              >{variantsExpanded[group.project.id]
                ? 'Hide language details'
                : 'Show language details'}</button
            >
          </div>
        </header>{/if}
      {#each group.items as item}
        {#if item.translation_project?.role !== 'branch' || !group.project || variantsExpanded[group.project.id] || !group.items.some((version) => version.translation_project?.role === 'source')}
          <article class="border-b border-[var(--line)] last:border-0">
            <div class="flex flex-wrap items-center gap-3 p-4">
              <a href={`/sessions/${item.id}`} class="min-w-0 flex-1"
                ><div class="truncate font-semibold">{item.name}</div>
                <div class="muted mt-1 text-xs capitalize">
                  {WORKFLOW_LABELS[item.workflow_kind]}{item.target_language
                    ? ` · ${languageLabel(item.target_language)}`
                    : ''} · {item.status} · updated {new Date(
                    item.updated_at
                  ).toLocaleString()}
                </div>
                {#if item.trashed_at}<p class="muted mt-1 text-xs">
                    {item.status === 'purging'
                      ? 'Permanent deletion in progress or awaiting retry'
                      : retentionEnabled && item.purge_after
                        ? `Scheduled for deletion ${new Date(item.purge_after).toLocaleString()}`
                        : 'Kept in Trash'}
                  </p>{/if}</a
              >
              <button
                onclick={() => reindex(item)}
                title="Reindex artifacts"
                class="tool"><RefreshCw size={16} /></button
              >{#if item.status === 'trashed' || item.status === 'purging'}{#if item.status !== 'purging'}<button
                    onclick={() => restore(item)}
                    class="tool"><ArchiveRestore size={16} /> Restore</button
                  >{/if}<button
                  class="tool text-red-600"
                  onclick={() => (deleteTarget = item)}
                  ><Trash2 size={16} />{item.status === 'purging'
                    ? 'Retry deletion'
                    : 'Delete permanently'}</button
                >{:else}<button
                  onclick={() => trash(item)}
                  class="tool text-red-500"><Trash2 size={16} /> Trash</button
                >{/if}<button
                onclick={() => expand(item.id)}
                class="tool"
                aria-label={`${expanded === item.id ? 'Collapse' : 'Expand'} artifacts for ${item.name}`}
                aria-expanded={expanded === item.id}
                ><ChevronDown
                  class={expanded === item.id ? 'rotate-180' : ''}
                  size={17}
                /></button
              >
            </div>
            {#if expanded === item.id}
              <div class="border-t border-[var(--line)] bg-[var(--paper)] p-4">
                <div class="section-label mb-3">Artifacts</div>
                <div class="grid gap-2 md:grid-cols-2">
                  {#each artifacts[item.id] ?? [] as artifact}
                    <button
                      onclick={() => {
                        preview = artifact;
                      }}
                      class="flex items-center gap-3 rounded-xl border border-[var(--line)] p-3 text-left"
                      >{#if artifact.kind === 'audio'}<FileAudio
                          size={17}
                        />{:else}<FileText size={17} />{/if}
                      <div class="min-w-0">
                        <div class="truncate text-sm font-semibold">
                          {artifactRoleLabel(artifact.role)}
                        </div>
                        <div class="muted truncate text-xs">
                          {artifact.relative_path} · {artifact.state}
                        </div>
                      </div></button
                    >
                  {:else}<p class="muted text-sm">
                      No registered artifacts.
                    </p>{/each}
                </div>
              </div>
            {/if}
          </article>
        {/if}
      {/each}
    {:else}<div class="muted p-10 text-center">
        No matching projects.
      </div>{/each}
  </div>
</div>
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

<style>
  .tool {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    border: 1px solid var(--line);
    border-radius: 0.7rem;
    padding: 0.55rem 0.7rem;
    font-size: 0.75rem;
    font-weight: 650;
  }
</style>
