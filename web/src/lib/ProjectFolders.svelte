<script lang="ts">
  import {
    ChevronDown,
    Folder,
    FolderPlus,
    Folders,
    Inbox
  } from '@lucide/svelte';
  import { SvelteSet } from 'svelte/reactivity';
  import { untrack } from 'svelte';
  import type {
    ProjectFolderRecord,
    ProjectFolderSnapshot
  } from './project-library';
  import { folderPath } from './project-library';

  let {
    snapshot,
    selected,
    onselect,
    oncreate
  }: {
    snapshot: ProjectFolderSnapshot;
    selected: string;
    onselect: (id: string) => void;
    oncreate: () => void;
  } = $props();
  const expanded = new SvelteSet<string>();
  const folders = $derived(snapshot.items);
  const choices = $derived(
    [...folders].sort((left, right) =>
      folderPath(left, folders).localeCompare(folderPath(right, folders))
    )
  );
  const children = (id: string | null) =>
    folders
      .filter((folder) => folder.parent_id === id)
      .sort((a, b) => a.name.localeCompare(b.name));
  function count(id: string) {
    return snapshot.memberships.filter((item) => item.folder_id === id).length;
  }
  function opened(folder: ProjectFolderRecord) {
    return expanded.has(folder.id);
  }
  $effect(() => {
    const current = folders.find((item) => item.id === selected);
    const all = folders;
    untrack(() => {
      const seen = new Set<string>();
      let parent = all.find((item) => item.id === current?.parent_id);
      while (parent && !seen.has(parent.id)) {
        seen.add(parent.id);
        expanded.add(parent.id);
        parent = all.find((item) => item.id === parent?.parent_id);
      }
    });
  });
</script>

<nav aria-label="Project folders" class="surface min-w-0 rounded-2xl p-3">
  <div class="mb-3 flex items-center justify-between gap-2">
    <h2 class="text-sm font-semibold">Folders</h2>
    <button
      class="btn btn-icon btn-secondary"
      aria-label="New folder"
      onclick={oncreate}><FolderPlus size={16} /></button
    >
  </div>
  <label class="block text-xs font-semibold lg:hidden"
    >Folder
    <select
      aria-label="Folder"
      value={selected}
      onchange={(event) => onselect(event.currentTarget.value)}
      class="mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2.5 text-sm"
    >
      <option value="">All projects</option>
      <option value="unfiled">Unfiled</option>
      {#each choices as folder}<option value={folder.id}
          >{folderPath(folder, folders)}</option
        >{/each}
    </select>
  </label>
  <div class="hidden space-y-1 lg:block">
    <button
      class="folder-choice"
      class:selected={selected === ''}
      aria-current={selected === '' ? 'page' : undefined}
      onclick={() => onselect('')}><Folders size={16} />All projects</button
    >
    <button
      class="folder-choice"
      class:selected={selected === 'unfiled'}
      aria-current={selected === 'unfiled' ? 'page' : undefined}
      onclick={() => onselect('unfiled')}><Inbox size={16} />Unfiled</button
    >
    {@render tree(null)}
  </div>
</nav>

{#snippet tree(parentId: string | null)}
  <ul class={parentId ? 'ml-3 border-l border-[var(--line)] pl-2' : 'mt-2'}>
    {#each children(parentId) as folder (folder.id)}
      {@const nested = children(folder.id).length > 0}
      <li>
        <div class="flex items-center gap-1">
          {#if nested}<button
              class="shrink-0 rounded-lg p-1.5"
              aria-label={`${opened(folder) ? 'Collapse' : 'Expand'} folder ${folder.name}`}
              aria-expanded={opened(folder)}
              aria-controls={`folder-children-${folder.id}`}
              onclick={() =>
                expanded.has(folder.id)
                  ? expanded.delete(folder.id)
                  : expanded.add(folder.id)}
              ><ChevronDown
                size={14}
                class={opened(folder) ? '' : '-rotate-90'}
              /></button
            >{:else}<span class="w-7 shrink-0" aria-hidden="true"></span>{/if}
          <button
            class="folder-choice min-w-0"
            class:selected={selected === folder.id}
            aria-current={selected === folder.id ? 'page' : undefined}
            aria-label={`Open folder ${folder.name}`}
            onclick={() => onselect(folder.id)}
            ><Folder size={15} class="shrink-0" /><span
              class="min-w-0 break-words">{folder.name}</span
            ><span class="muted ml-auto text-xs tabular-nums"
              >{count(folder.id)}</span
            ></button
          >
        </div>
        {#if nested}<div
            id={`folder-children-${folder.id}`}
            hidden={!opened(folder)}
          >
            {@render tree(folder.id)}
          </div>{/if}
      </li>
    {/each}
  </ul>
{/snippet}

<style>
  .folder-choice {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    width: 100%;
    border-radius: 0.65rem;
    padding: 0.55rem 0.6rem;
    text-align: left;
    font-size: 0.8rem;
  }
  .folder-choice:hover {
    background: var(--paper-strong);
  }
  .folder-choice.selected {
    color: var(--accent);
    background: var(--accent-soft);
    font-weight: 600;
  }
</style>
