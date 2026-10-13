<script lang="ts">
  import { onMount } from 'svelte';
  import { FolderPlus, LoaderCircle, Trash2, X } from '@lucide/svelte';
  import { modalFocus } from './modal-focus';
  import { errorMessage } from './errors';
  import {
    folderDescendants,
    folderPath,
    type ProjectFolderRecord
  } from './project-library';
  import type { ProjectFoldersStore } from './project-folders-store.svelte';

  let {
    store,
    folder = null,
    parentId = null,
    onclose,
    onsaved
  }: {
    store: ProjectFoldersStore;
    folder?: ProjectFolderRecord | null;
    parentId?: string | null;
    onclose: () => void;
    onsaved: (folderId: string | null) => void;
  } = $props();
  let name = $state('');
  let parent = $state('');
  let busy = $state(false);
  let error = $state('');
  let deleting = $state(false);
  const excluded = $derived(
    folder ? folderDescendants(folder.id, store.value.items) : new Set<string>()
  );
  const choices = $derived(
    [...store.value.items]
      .filter((item) => !excluded.has(item.id))
      .sort((a, b) =>
        folderPath(a, store.value.items).localeCompare(
          folderPath(b, store.value.items)
        )
      )
  );
  onMount(() => {
    name = folder?.name ?? '';
    parent = folder ? (folder.parent_id ?? '') : (parentId ?? '');
  });
  function close() {
    if (!busy) onclose();
  }
  async function save() {
    if (busy || !name.trim()) return;
    busy = true;
    error = '';
    try {
      if (folder) {
        const current =
          store.value.items.find((item) => item.id === folder!.id) ?? folder;
        if (deleting) {
          await store.remove(current);
          onsaved(null);
        } else {
          await store.update(current, name.trim(), parent || null);
          onsaved(folder.id);
        }
      } else {
        const result = await store.create(name.trim(), parent || null);
        onsaved(result.folder.id);
      }
      onclose();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
</script>

<div
  class="fixed inset-0 z-[80] grid place-items-center bg-black/50 p-4 backdrop-blur-sm"
  role="presentation"
  onclick={(event) => event.target === event.currentTarget && close()}
>
  <div
    class="surface scrollbar-on-demand max-h-[85dvh] w-full max-w-lg overflow-y-auto rounded-3xl p-6"
    use:modalFocus={{ onclose: close, initialFocus: '#project-folder-name' }}
    role="dialog"
    aria-modal="true"
    aria-labelledby="project-folder-dialog-title"
  >
    <form
      onsubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <div class="flex items-center justify-between gap-3">
        <h2
          id="project-folder-dialog-title"
          class="flex items-center gap-2 text-xl font-semibold"
        >
          <FolderPlus size={20} />{deleting
            ? 'Delete folder'
            : folder
              ? 'Edit folder'
              : 'New folder'}
        </h2>
        <button
          type="button"
          class="btn btn-icon btn-secondary"
          aria-label="Close folder dialog"
          disabled={busy}
          onclick={close}><X size={18} /></button
        >
      </div>
      {#if deleting}
        <p class="mt-5 text-sm">
          Delete “{folder?.name}”? Its projects remain available in All
          projects. Nested folders move to its parent.
        </p>
      {:else}
        <label
          class="mt-5 block text-sm font-semibold"
          for="project-folder-name">Folder name</label
        >
        <input
          id="project-folder-name"
          bind:value={name}
          maxlength="128"
          required
          disabled={busy}
          class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2.5"
        />
        <label
          for="project-folder-parent"
          class="mt-4 block text-sm font-semibold">Parent folder</label
        ><select
          id="project-folder-parent"
          bind:value={parent}
          disabled={busy}
          class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2.5 text-sm"
          ><option value="">Top level</option>{#each choices as item}<option
              value={item.id}>{folderPath(item, store.value.items)}</option
            >{/each}</select
        >
        <p class="muted mt-3 text-xs">
          Folders organize whole projects and all their languages.
        </p>
      {/if}
      {#if error}<p class="mt-4 text-sm text-red-500" role="alert">
          {error}
        </p>{/if}
      <div class="mt-6 flex flex-wrap items-center justify-end gap-2">
        {#if folder && !deleting}<button
            type="button"
            class="btn btn-secondary mr-auto text-red-500"
            disabled={busy}
            onclick={() => (deleting = true)}
            ><Trash2 size={16} />Delete folder</button
          >{/if}
        <button
          type="button"
          class="btn btn-secondary"
          disabled={busy}
          onclick={deleting ? () => (deleting = false) : close}>Cancel</button
        >
        <button
          type="submit"
          class="btn btn-primary"
          disabled={busy || !name.trim()}
          >{#if busy}<LoaderCircle
              size={16}
              class="animate-spin"
            />{/if}{deleting
            ? 'Delete folder'
            : folder
              ? 'Save folder'
              : 'Create folder'}</button
        >
      </div>
    </form>
  </div>
</div>
