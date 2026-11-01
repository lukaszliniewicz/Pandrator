<script lang="ts">
  import { onDestroy, onMount, tick } from 'svelte';
  import { Check, Search, Users, X } from '@lucide/svelte';
  import { ApiError } from './api';
  import { errorMessage } from './errors';
  import { modalFocus } from './modal-focus';
  import {
    projectSharingApi,
    type ProjectViewers,
    type ProjectViewingCandidate
  } from './project-sharing-api';

  let {
    projectId,
    sourceSessionId,
    projectName,
    onclose
  }: {
    projectId: string;
    sourceSessionId: string;
    projectName: string;
    onclose: () => void;
  } = $props();
  let access = $state<ProjectViewers | null>(null);
  let query = $state('');
  let results = $state<ProjectViewingCandidate[]>([]);
  let searched = $state(false);
  let searching = $state(false);
  let loading = $state(true);
  let busy = $state('');
  let error = $state('');
  let notice = $state('');
  let copied = $state(false);
  let searchInput = $state<HTMLInputElement>();
  let alive = true;
  let searchRequest = 0;
  const controller = new AbortController();
  const link = $derived(
    typeof location === 'undefined'
      ? ''
      : `${location.origin}/shared/${encodeURIComponent(sourceSessionId)}`
  );

  async function load() {
    loading = true;
    try {
      const value = await projectSharingApi.viewers(
        projectId,
        controller.signal
      );
      if (alive) access = value;
    } catch (caught) {
      if (alive) error = errorMessage(caught);
    } finally {
      if (alive) loading = false;
    }
  }
  async function restoreSearchFocus() {
    await tick();
    if (alive && document.activeElement === document.body)
      searchInput?.focus({ preventScroll: true });
  }
  async function search() {
    const request = ++searchRequest;
    results = [];
    if (query.trim().length < 2) {
      searched = false;
      return;
    }
    searching = true;
    error = '';
    try {
      const value = await projectSharingApi.search(
        projectId,
        query.trim(),
        controller.signal
      );
      if (alive && request === searchRequest) {
        results = value.items;
        searched = true;
      }
    } catch (caught) {
      if (alive && request === searchRequest) error = errorMessage(caught);
    } finally {
      if (alive && request === searchRequest) {
        searching = false;
        await restoreSearchFocus();
      }
    }
  }
  async function change(userId: string, revoke = false) {
    if (!access || busy) return;
    busy = userId;
    error = notice = '';
    try {
      const value = await projectSharingApi.set(
        projectId,
        userId,
        access.revision,
        revoke
      );
      if (!alive) return;
      access = value;
      results = results.filter((user) => user.user_id !== userId);
      if (!revoke) {
        query = '';
        searched = false;
      }
      notice = revoke ? 'Viewing access removed.' : 'Can view access added.';
    } catch (caught) {
      if (!alive) return;
      error = errorMessage(caught);
      if (caught instanceof ApiError && caught.status === 409) await load();
    } finally {
      if (alive) {
        busy = '';
        await restoreSearchFocus();
      }
    }
  }
  async function copyLink() {
    try {
      await navigator.clipboard.writeText(link);
      if (alive) copied = true;
    } catch {
      if (alive)
        error = 'The link could not be copied. Select and copy it below.';
    }
  }
  onMount(() => {
    void load();
  });
  onDestroy(() => {
    alive = false;
    searchRequest++;
    controller.abort();
  });
</script>

<div
  class="fixed inset-0 z-50 grid place-items-center bg-black/45 p-3 backdrop-blur-sm sm:p-6"
>
  <div
    class="max-h-[90dvh] w-full max-w-xl overflow-y-auto rounded-3xl border border-[var(--line)] bg-[var(--paper-strong)] p-5 shadow-2xl sm:p-7"
    role="dialog"
    aria-modal="true"
    aria-labelledby="project-sharing-title"
    use:modalFocus={{ onclose }}
  >
    <header class="flex items-start justify-between gap-4">
      <div>
        <p class="section-label flex items-center gap-2">
          <Users size={16} />Project sharing
        </p>
        <h2
          id="project-sharing-title"
          class="mt-2 break-words text-xl font-semibold"
        >
          {projectName}
        </h2>
      </div>
      <button
        class="btn btn-icon btn-secondary shrink-0"
        aria-label="Close project sharing"
        onclick={onclose}><X size={18} /></button
      >
    </header>
    <p class="muted mt-4 text-sm">
      Can view lets a person read this project’s saved work and download
      existing outputs, across all languages. Invite them to a shared workspace
      to edit or process projects together.
    </p>
    {#if error}<p role="alert" class="mt-4 text-sm text-red-500">
        {error}
      </p>{/if}
    {#if notice}<p role="status" class="mt-4 text-sm">{notice}</p>{/if}
    {#if loading}<p role="status" class="muted mt-5 text-sm">
        Loading viewing access…
      </p>
    {:else if access}
      <h3 class="mt-6 text-sm font-semibold">People with Can view access</h3>
      {#if !access.items.length}<p class="muted mt-2 text-sm">
          No viewing grants. Workspace members already have access.
        </p>{/if}
      <ul class="mt-2 divide-y divide-[var(--line)]">
        {#each access.items as user (user.user_id)}
          <li class="flex flex-wrap items-center justify-between gap-3 py-3">
            <div class="min-w-0">
              <p class="break-words text-sm font-semibold">
                {user.display_name}{!user.is_active ? ' · Inactive' : ''}
              </p>
              <p class="muted text-xs">{user.login_name} · Can view</p>
            </div>
            <button
              class="btn btn-secondary text-xs"
              disabled={Boolean(busy)}
              onclick={() => void change(user.user_id, true)}
              >Remove access<span class="sr-only">
                for {user.display_name}</span
              ></button
            >
          </li>
        {/each}
      </ul>
      <form
        class="mt-6"
        onsubmit={(event) => {
          event.preventDefault();
          void search();
        }}
      >
        <label class="text-sm font-semibold" for="project-viewer-search"
          >Add a person</label
        >
        <div class="mt-2 flex gap-2">
          <input
            id="project-viewer-search"
            bind:this={searchInput}
            class="min-w-0 flex-1 rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2"
            bind:value={query}
            minlength="2"
            maxlength="64"
            placeholder="Name or login"
            required
          /><button
            class="btn btn-secondary"
            disabled={searching || Boolean(busy) || query.trim().length < 2}
            ><Search size={16} /><span>{searching ? 'Searching…' : 'Find'}</span
            ></button
          >
        </div>
      </form>
      {#if searched && !results.length}<p class="muted mt-3 text-sm">
          No matching accounts available to add. An Administrator can create a
          new account in Manage users.
        </p>{/if}
      <ul class="mt-2 divide-y divide-[var(--line)]">
        {#each results as user (user.user_id)}<li
            class="flex items-center justify-between gap-3 py-3"
          >
            <div class="min-w-0">
              <p class="break-words text-sm font-semibold">
                {user.display_name}
              </p>
              <p class="muted text-xs">{user.login_name}</p>
            </div>
            <button
              class="btn btn-primary shrink-0 text-xs"
              disabled={Boolean(busy)}
              onclick={() => void change(user.user_id)}
              >Share with {user.display_name}</button
            >
          </li>{/each}
      </ul>
      <div class="mt-6 border-t border-[var(--line)] pt-4">
        <p class="muted text-xs">
          Only people with project or workspace access can open this link.
          Trashing the source hides it from project viewers until it is
          restored.
        </p>
        <label class="sr-only" for="project-viewing-link"
          >Project viewing link</label
        ><input
          id="project-viewing-link"
          class="mt-3 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-xs"
          readonly
          value={link}
          onclick={(event) => event.currentTarget.select()}
        /><button class="btn btn-secondary mt-2" onclick={() => void copyLink()}
          >{#if copied}<Check size={16} />{/if}{copied
            ? 'Copied'
            : 'Copy viewing link'}</button
        >
      </div>
    {/if}
  </div>
</div>
