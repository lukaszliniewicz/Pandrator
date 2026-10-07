<script lang="ts">
  import { errorMessage } from '$lib/errors';
  import {
    ArchiveRestore,
    File,
    FileAudio,
    FileVideo,
    FileText,
    BookOpen,
    Captions,
    Link2,
    Pencil,
    Plus,
    Save,
    Search,
    Trash2,
    X
  } from '@lucide/svelte';
  import { uploadManagedFile } from '$lib/api';
  import { sourceApi } from '$lib/domain-api';
  import type { SourceAsset, SourceReferencesPage } from '$lib/api-models';
  import {
    sourceCategory,
    SOURCE_CATEGORIES,
    WORKFLOW_LABELS,
    formatFileSize
  } from '$lib/library-display';
  import type { PreviewableArtifact } from '$lib/artifact-display';
  import ArtifactPreview from '$lib/ArtifactPreview.svelte';
  let sources = $state<SourceAsset[]>([]);
  let search = $state('');
  let category = $state('');
  let usage = $state('');
  let sort = $state('updated');
  let loading = $state(true);
  let referencesOpen = $state('');
  let references = $state<Record<string, SourceReferencesPage>>({});
  let referencesLoading = $state('');
  let referencesError = $state('');
  let request = 0;
  let showTrash = $state(false);
  let uploading = $state(false);
  let progress = $state(0);
  let error = $state('');
  let message = $state('');
  let preview = $state<PreviewableArtifact | null>(null);
  let editingId = $state('');
  let editName = $state('');
  const visible = $derived(
    sources
      .filter(
        (item) =>
          (!category || sourceCategory(item) === category) &&
          (!usage ||
            (usage === 'used'
              ? item.reference_count > 0
              : item.reference_count === 0)) &&
          `${item.display_name} ${item.kind}`
            .toLowerCase()
            .includes(search.trim().toLowerCase())
      )
      .sort(
        (a, b) =>
          (sort === 'name'
            ? a.display_name.localeCompare(b.display_name)
            : sort === 'size'
              ? b.size_bytes - a.size_bytes
              : sort === 'references'
                ? b.reference_count - a.reference_count
                : b.updated_at.localeCompare(a.updated_at)) ||
          a.id.localeCompare(b.id)
      )
  );
  async function load() {
    const current = ++request;
    loading = true;
    error = '';
    try {
      const result = await sourceApi.list(showTrash, true);
      if (current === request) {
        sources = result.items;
        references = {};
      }
    } catch (caught) {
      if (current === request) error = errorMessage(caught);
    } finally {
      if (current === request) loading = false;
    }
  }
  async function showReferences(item: SourceAsset, more = false) {
    if (!more) referencesOpen = referencesOpen === item.id ? '' : item.id;
    if (referencesOpen !== item.id || (references[item.id] && !more)) return;
    referencesLoading = item.id;
    referencesError = '';
    try {
      const result = await sourceApi.references(
        item.id,
        more ? (references[item.id]?.next_offset ?? 0) : 0
      );
      references[item.id] =
        more && references[item.id]
          ? {
              ...result,
              items: [...references[item.id].items, ...result.items]
            }
          : result;
    } catch (caught) {
      if (referencesOpen === item.id) referencesError = errorMessage(caught);
    } finally {
      if (referencesLoading === item.id) referencesLoading = '';
    }
  }
  async function upload(event: Event) {
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    uploading = true;
    error = '';
    try {
      await uploadManagedFile(file, undefined, (value) => (progress = value));
      message = 'Reusable source added.';
      await load();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      uploading = false;
      input.value = '';
    }
  }
  async function rename(item: SourceAsset) {
    if (!editName.trim()) return;
    error = '';
    try {
      await sourceApi.rename(item, editName.trim());
      editingId = '';
      message = 'Source renamed.';
      await load();
    } catch (caught) {
      error = errorMessage(caught);
    }
  }
  async function trash(item: SourceAsset) {
    if (item.reference_count) return;
    error = '';
    try {
      await sourceApi.trash(item);
      message =
        'Source moved to recoverable trash; its managed file was preserved.';
      await load();
    } catch (caught) {
      error = errorMessage(caught);
    }
  }
  async function restore(item: SourceAsset) {
    error = '';
    try {
      await sourceApi.restore(item);
      message = 'Source restored.';
      await load();
    } catch (caught) {
      error = errorMessage(caught);
    }
  }
  $effect(() => {
    void showTrash;
    void load();
  });
</script>

<div class="mx-auto max-w-7xl">
  <header class="flex flex-wrap items-end justify-between gap-5">
    <div>
      <h1 class="mt-2 text-4xl font-semibold">Source library</h1>
      <p class="muted mt-3">
        Reuse recordings and documents across projects. Open references to see
        where a source is used.
      </p>
    </div>
    <div class="flex items-center gap-3">
      <label
        class="flex cursor-pointer items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-3 text-sm font-semibold text-white"
        ><Plus size={17} />{uploading
          ? `Uploading ${Math.round(progress * 100)}%`
          : 'Add source'}<input
          type="file"
          class="sr-only"
          onchange={upload}
        /></label
      ><button
        type="button"
        role="switch"
        aria-checked={showTrash}
        onclick={() => (showTrash = !showTrash)}
        class:active={showTrash}
        class="trash-toggle"
        title={showTrash ? 'Hide trashed sources' : 'Show trashed sources'}
        ><Trash2 size={14} /><span>Trash</span><span class="toggle-track"
          ><span></span></span
        ></button
      >
    </div>
  </header>
  {#if error}<p class="mt-4 rounded-xl bg-red-500/10 p-3 text-sm text-red-500">
      {error}
    </p>{/if}{#if message}<p
      class="mt-4 rounded-xl bg-[var(--accent-soft)] p-3 text-sm"
    >
      {message}
    </p>{/if}
  <div
    class="mt-7 flex items-center gap-3 rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] px-4"
  >
    <Search class="muted" size={17} /><input
      bind:value={search}
      aria-label="Search sources"
      placeholder="Search by name or file type"
      class="w-full bg-transparent py-3 outline-none"
    />
  </div>
  <div class="mt-3 flex flex-wrap items-end gap-3">
    <label class="text-xs font-semibold"
      >Type<select
        bind:value={category}
        class="mt-1 block rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-sm font-normal"
        ><option value="">All types</option
        >{#each SOURCE_CATEGORIES as option}<option value={option.value}
            >{option.label}</option
          >{/each}</select
      ></label
    >
    <label class="text-xs font-semibold"
      >Usage<select
        bind:value={usage}
        class="mt-1 block rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-sm font-normal"
        ><option value="">Any usage</option><option value="used"
          >Attached to projects</option
        ><option value="unused">Unattached</option></select
      ></label
    >
    <label class="text-xs font-semibold"
      >Sort<select
        bind:value={sort}
        class="mt-1 block rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-sm font-normal"
        ><option value="updated">Recently updated</option><option value="name"
          >Name</option
        ><option value="size">Largest first</option><option value="references"
          >Most references</option
        ></select
      ></label
    >
    <p class="muted ml-auto py-2 text-xs" role="status">
      {loading
        ? 'Loading sources…'
        : `${visible.length} of ${sources.length} sources`}
    </p>
  </div>
  <div class="mt-5 grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
    {#each visible as item (item.id)}
      <article
        class:trashed={item.state === 'trashed'}
        class="surface rounded-2xl p-5"
      >
        <div class="flex items-start gap-3">
          <div
            class="grid size-10 place-items-center rounded-xl bg-[var(--accent-soft)] text-[var(--accent)]"
            role="img"
            aria-label={`${SOURCE_CATEGORIES.find((category) => category.value === sourceCategory(item))?.label ?? 'Other'} source`}
          >
            {#if sourceCategory(item) === 'audio'}<FileAudio
                size={18}
              />{:else if sourceCategory(item) === 'video'}<FileVideo
                size={18}
              />{:else if sourceCategory(item) === 'epub'}<BookOpen
                size={18}
              />{:else if sourceCategory(item) === 'subtitles'}<Captions
                size={18}
              />{:else if ['pdf', 'text'].includes(sourceCategory(item))}<FileText
                size={18}
              />{:else if item.external_path}<Link2 size={18} />{:else}<File
                size={18}
              />{/if}
          </div>
          <div class="min-w-0 flex-1">
            {#if editingId === item.id}<div class="flex gap-1">
                <input
                  bind:value={editName}
                  class="min-w-0 flex-1 rounded-lg border border-[var(--line)] bg-[var(--paper)] px-2 py-1 text-sm"
                /><button
                  onclick={() => rename(item)}
                  aria-label="Save source name"><Save size={15} /></button
                ><button
                  onclick={() => (editingId = '')}
                  aria-label="Cancel rename"><X size={15} /></button
                >
              </div>{:else}<div class="flex items-center gap-2">
                <h2 class="min-w-0 flex-1 truncate font-semibold">
                  {item.display_name}
                </h2>
                <button
                  onclick={() => {
                    editingId = item.id;
                    editName = item.display_name;
                  }}
                  aria-label="Rename source"><Pencil size={14} /></button
                >
              </div>{/if}
            <div class="muted mt-1 text-xs uppercase">
              {item.kind} · {item.state}
            </div>
          </div>
        </div>
        <dl class="muted mt-5 grid grid-cols-3 gap-3 text-xs">
          <div>
            <dt>Size</dt>
            <dd>
              {formatFileSize(item.size_bytes)}
            </dd>
          </div>
          <div>
            <dt>Record</dt>
            <dd>Revision {item.revision}</dd>
          </div>
          <div>
            <dt>References</dt>
            <dd>
              <button
                type="button"
                class="text-[var(--accent)] underline decoration-dotted underline-offset-4"
                aria-label={`Show projects using ${item.display_name}`}
                aria-expanded={referencesOpen === item.id}
                onclick={() => showReferences(item)}
                >{item.reference_count}
                {item.reference_count === 1
                  ? 'reference'
                  : 'references'}</button
              >
            </dd>
          </div>
        </dl>
        {#if referencesOpen === item.id}
          <div class="mt-4 rounded-xl border border-[var(--line)] p-3">
            <h3 class="text-xs font-semibold">Used in projects</h3>
            {#if referencesError}<p
                role="alert"
                class="mt-2 text-xs text-red-500"
              >
                {referencesError}<button
                  class="ml-2 underline"
                  onclick={() => showReferences(item, true)}>Retry</button
                >
              </p>{/if}
            <ul class="mt-2 space-y-3">
              {#each references[item.id]?.items ?? [] as reference (reference.attachment_id)}
                <li class="text-xs">
                  <a
                    href={`/sessions/${reference.session_id}/sources`}
                    class="font-semibold text-[var(--accent)] hover:underline"
                    >{reference.session_name}</a
                  >
                  <p class="muted mt-1">
                    {WORKFLOW_LABELS[reference.workflow_kind]} · {reference.target_language ||
                      reference.source_language} · {reference.status ===
                      'trashed' || reference.status === 'purging'
                      ? 'In trash'
                      : reference.is_current
                        ? 'Current attachment'
                        : 'Earlier attachment'} · {reference.role}
                  </p>
                </li>
              {:else}{#if !referencesLoading && !referencesError}<li
                    class="muted text-xs"
                  >
                    No project attachments.
                  </li>{/if}{/each}
            </ul>
            {#if referencesLoading === item.id}<p
                class="muted mt-2 text-xs"
                role="status"
              >
                Loading references…
              </p>{:else if references[item.id]?.next_offset != null}<button
                class="mt-3 text-xs font-semibold text-[var(--accent)]"
                onclick={() => showReferences(item, true)}
                >More references</button
              >{/if}
          </div>
        {/if}
        <div class="mt-4 flex gap-2">
          {#if item.artifact_id}<button
              onclick={() => {
                preview = {
                  id: item.artifact_id,
                  role: 'source',
                  kind: item.kind,
                  mime_type: item.mime_type,
                  size_bytes: item.size_bytes,
                  state: item.state,
                  relative_path: item.display_name
                };
              }}
              class="action flex-1">Preview</button
            >{/if}{#if item.state === 'trashed'}<button
              onclick={() => restore(item)}
              class="action"><ArchiveRestore size={15} /> Restore</button
            >{:else}<button
              onclick={() => trash(item)}
              disabled={item.reference_count > 0}
              title={item.reference_count
                ? `Detach ${item.reference_count} project attachment(s) first`
                : 'Move to recoverable trash'}
              class="action danger"><Trash2 size={15} /></button
            >{/if}
        </div>
      </article>
    {:else}<div
        class="muted col-span-full rounded-2xl border border-dashed border-[var(--line)] p-12 text-center"
      >
        <File class="mx-auto mb-3" size={24} />{loading
          ? 'Loading sources…'
          : sources.length
            ? 'No sources match these filters.'
            : 'No reusable sources yet.'}
      </div>{/each}
  </div>
</div>
{#if preview}<ArtifactPreview
    artifact={preview}
    onclose={() => (preview = null)}
  />{/if}

<style>
  .trashed {
    opacity: 0.65;
  }
  .action {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.35rem;
    border: 1px solid var(--line);
    border-radius: 0.65rem;
    padding: 0.5rem 0.7rem;
    font-size: 0.72rem;
    font-weight: 650;
  }
  .action.danger {
    color: #dc4b4b;
  }
  .action:disabled {
    cursor: not-allowed;
    opacity: 0.35;
  }
  .trash-toggle {
    display: inline-flex;
    min-height: 2.75rem;
    align-items: center;
    gap: 0.45rem;
    border: 1px solid var(--line);
    border-radius: 0.75rem;
    padding: 0.55rem 0.7rem;
    color: var(--muted);
    font-size: 0.75rem;
    font-weight: 700;
  }
  .trash-toggle.active {
    color: var(--ink);
    background: var(--accent-soft);
  }
  .toggle-track {
    width: 1.75rem;
    border-radius: 999px;
    background: var(--line);
    padding: 0.15rem;
    transition: background 0.15s ease;
  }
  .toggle-track > span {
    display: block;
    width: 0.65rem;
    height: 0.65rem;
    border-radius: 999px;
    background: var(--paper-strong);
    transition: transform 0.15s ease;
  }
  .trash-toggle.active .toggle-track {
    background: var(--accent);
  }
  .trash-toggle.active .toggle-track > span {
    transform: translateX(0.8rem);
  }
  dd {
    margin-top: 0.2rem;
    font-weight: 650;
    color: var(--ink);
  }
</style>
