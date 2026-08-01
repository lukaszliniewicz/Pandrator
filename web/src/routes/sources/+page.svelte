<script lang="ts">
  import { errorMessage } from '$lib/errors';
  import {
    ArchiveRestore,
    File,
    FileAudio,
    FileText,
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
  import type { SourceAsset } from '$lib/api-models';
  import type { PreviewableArtifact } from '$lib/artifact-display';
  import ArtifactPreview from '$lib/ArtifactPreview.svelte';
  let sources = $state<SourceAsset[]>([]);
  let search = $state('');
  let showTrash = $state(false);
  let uploading = $state(false);
  let progress = $state(0);
  let error = $state('');
  let message = $state('');
  let preview = $state<PreviewableArtifact | null>(null);
  let editingId = $state('');
  let editName = $state('');
  const visible = $derived(
    sources.filter(
      (item) =>
        item.display_name.toLowerCase().includes(search.toLowerCase()) ||
        item.kind.includes(search.toLowerCase())
    )
  );
  async function load() {
    sources = (await sourceApi.list(showTrash)).items;
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
    void load().catch((caught) => (error = errorMessage(caught)));
  });
</script>

<div class="mx-auto max-w-7xl">
  <header class="flex flex-wrap items-end justify-between gap-5">
    <div>
      <div class="eyebrow">Source library</div>
      <h1 class="mt-2 text-4xl font-semibold">Reusable material</h1>
      <p class="muted mt-3">
        One managed source can serve several sessions. Reference counts prevent
        accidental removal while a session still uses it.
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
      placeholder="Search by name or file type"
      class="w-full bg-transparent py-3 outline-none"
    />
  </div>
  <div class="mt-5 grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
    {#each visible as item}
      <article
        class:trashed={item.state === 'trashed'}
        class="surface rounded-2xl p-5"
      >
        <div class="flex items-start gap-3">
          <div
            class="grid size-10 place-items-center rounded-xl bg-[var(--accent-soft)] text-[var(--accent)]"
          >
            {#if item.kind.match(/wav|mp3|flac|m4a/)}<FileAudio
                size={18}
              />{:else if item.external_path}<Link2 size={18} />{:else}<FileText
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
              {item.size_bytes
                ? `${(item.size_bytes / 1048576).toFixed(1)} MB`
                : 'External'}
            </dd>
          </div>
          <div>
            <dt>Record</dt>
            <dd>Revision {item.revision}</dd>
          </div>
          <div>
            <dt>References</dt>
            <dd>{item.reference_count}</dd>
          </div>
        </dl>
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
                ? `Detach ${item.reference_count} session attachment(s) first`
                : 'Move to recoverable trash'}
              class="action danger"><Trash2 size={15} /></button
            >{/if}
        </div>
      </article>
    {:else}<div
        class="muted col-span-full rounded-2xl border border-dashed border-[var(--line)] p-12 text-center"
      >
        <File class="mx-auto mb-3" size={24} />No reusable sources yet.
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
