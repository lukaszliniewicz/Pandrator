<script lang="ts">
  import { onMount } from 'svelte';
  import { FolderPlus, LoaderCircle, X } from '@lucide/svelte';
  import { modalFocus } from './modal-focus';
  import { errorMessage } from './errors';
  import { folderPath, type ProjectFolderSnapshot } from './project-library';
  import type { ProjectFoldersStore } from './project-folders-store.svelte';

  let {
    store,
    projectId,
    projectName,
    onclose,
    onsaved = () => {}
  }: {
    store: ProjectFoldersStore;
    projectId: string;
    projectName: string;
    onclose: () => void;
    onsaved?: () => void;
  } = $props();
  let selected = $state<string[]>([]);
  let baseline = $state<ProjectFolderSnapshot | null>(null);
  let ready = $state(false);
  let busy = $state(false);
  let error = $state('');
  let newName = $state('');
  let newParent = $state('');
  const choices = $derived(
    [...store.value.items].sort((a, b) =>
      folderPath(a, store.value.items).localeCompare(
        folderPath(b, store.value.items)
      )
    )
  );
  function readSelection() {
    baseline = {
      items: [...store.value.items],
      memberships: [...store.value.memberships]
    };
    selected = store.value.memberships
      .filter((item) => item.project_id === projectId)
      .map((item) => item.folder_id);
  }
  onMount(() => {
    let live = true;
    void store
      .load(true)
      .then(() => {
        if (live) {
          readSelection();
          ready = true;
        }
      })
      .catch((caught) => {
        if (live) error = errorMessage(caught);
      });
    return () => {
      live = false;
    };
  });
  function close() {
    if (!busy) onclose();
  }
  async function reloadSelection() {
    if (busy) return;
    busy = true;
    error = '';
    try {
      await store.load(true);
      readSelection();
      ready = true;
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
  async function save() {
    if (busy || !ready || !baseline) return;
    busy = true;
    error = '';
    try {
      await store.assign(projectId, selected, baseline);
      onsaved();
      onclose();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
  async function create() {
    if (busy || !newName.trim()) return;
    busy = true;
    error = '';
    try {
      const result = await store.create(newName.trim(), newParent || null);
      if (baseline)
        baseline = { ...baseline, items: [...baseline.items, result.folder] };
      selected = [...selected, result.folder.id];
      newName = '';
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
    use:modalFocus={{ onclose: close }}
    role="dialog"
    aria-modal="true"
    aria-labelledby="project-folder-assignment-title"
  >
    <div class="flex items-center justify-between gap-3">
      <h2 id="project-folder-assignment-title" class="text-xl font-semibold">
        Project folders
      </h2>
      <button
        class="btn btn-icon btn-secondary"
        aria-label="Close project folders"
        disabled={busy}
        onclick={close}><X size={18} /></button
      >
    </div>
    <p class="muted mt-3 break-words text-sm">{projectName}</p>
    {#if !ready && !error}<p
        class="mt-4 flex items-center gap-2 text-sm"
        role="status"
      >
        <LoaderCircle size={16} class="animate-spin" />Loading folders…
      </p>{/if}
    {#if ready}
      <fieldset class="mt-4 space-y-2" disabled={busy}>
        <legend class="mb-2 text-sm">Show this project in:</legend
        >{#each choices as folder}<label
            class="flex items-start gap-3 rounded-xl border border-[var(--line)] px-3 py-2.5 text-sm"
            ><input
              type="checkbox"
              value={folder.id}
              bind:group={selected}
              class="mt-1"
            /><span class="min-w-0 break-words"
              >{folderPath(folder, store.value.items)}</span
            ></label
          >{:else}<p class="muted text-sm">
            Create a folder to organize this project.
          </p>{/each}
      </fieldset>
      <details class="mt-5 border-t border-[var(--line)] pt-4">
        <summary class="text-sm font-semibold">Create a folder</summary><label
          class="mt-3 block text-xs font-semibold"
          >New folder name<input
            bind:value={newName}
            maxlength="128"
            disabled={busy}
            class="mt-1.5 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2.5 text-sm"
          /></label
        ><label
          for="project-assignment-folder-parent"
          class="mt-3 block text-xs font-semibold">Parent folder</label
        ><select
          id="project-assignment-folder-parent"
          bind:value={newParent}
          disabled={busy}
          class="mt-1.5 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2.5 text-sm"
          ><option value="">Top level</option>{#each choices as folder}<option
              value={folder.id}>{folderPath(folder, store.value.items)}</option
            >{/each}</select
        ><button
          class="btn btn-secondary mt-3"
          disabled={busy || !newName.trim()}
          onclick={create}
          ><FolderPlus size={16} />Create and select folder</button
        >
      </details>
      <p class="muted mt-4 text-xs">
        Each folder contains this whole project, including all languages.
        Unchecking every folder keeps it in All projects and Unfiled.
      </p>
    {/if}
    {#if error}<div class="mt-4 text-sm" role="alert">
        <p class="text-red-500">{error}</p>
        <button class="mt-2 underline" disabled={busy} onclick={reloadSelection}
          >Reload saved memberships</button
        >
      </div>{/if}
    <div class="mt-6 flex justify-end gap-2">
      <button class="btn btn-secondary" disabled={busy} onclick={close}
        >Cancel</button
      ><button class="btn btn-primary" disabled={busy || !ready} onclick={save}
        >{#if busy}<LoaderCircle size={16} class="animate-spin" />{/if}Save
        folders</button
      >
    </div>
  </div>
</div>
