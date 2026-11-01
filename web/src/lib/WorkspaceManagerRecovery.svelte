<script lang="ts">
  import { onDestroy, onMount, tick } from 'svelte';
  import { RefreshCw } from '@lucide/svelte';
  import { ApiError } from './api';
  import { appState } from './app-state.svelte';
  import { errorMessage } from './errors';
  import {
    workspaceRecoveryApi,
    type RecoverableWorkspace
  } from './workspace-recovery-api';

  let items = $state<RecoverableWorkspace[]>([]);
  let selections = $state<Record<string, string>>({});
  let loading = $state(false);
  let busy = $state('');
  let error = $state('');
  let message = $state('');
  let outcome = $state<HTMLParagraphElement>();
  let hasMore = $state(false);
  let offset = 0;
  let active = true;
  let generation = 0;
  let controller: AbortController | undefined;

  onMount(() => void load());
  onDestroy(() => {
    active = false;
    generation += 1;
    controller?.abort();
  });

  async function load(more = false) {
    if (!appState.isAdministrator || !appState.collaborationEnabled) return;
    controller?.abort();
    controller = new AbortController();
    const current = ++generation;
    const identity = appState.identityKey;
    const nextOffset = more ? offset + 50 : 0;
    loading = true;
    error = '';
    try {
      const result = await workspaceRecoveryApi.list(
        nextOffset,
        controller.signal
      );
      if (
        !active ||
        current !== generation ||
        identity !== appState.identityKey
      )
        return;
      items = more ? [...items, ...result.items] : result.items;
      if (!more) selections = {};
      offset = nextOffset;
      hasMore = result.has_more;
    } catch (caught) {
      if (active && current === generation && !controller.signal.aborted)
        error = errorMessage(caught);
    } finally {
      if (active && current === generation) loading = false;
    }
  }

  async function recover(workspace: RecoverableWorkspace) {
    const userId = selections[workspace.id];
    if (!userId || busy || !appState.isAdministrator) return;
    const candidate = workspace.candidates.find(
      (item) => item.user_id === userId
    );
    if (!candidate) return;
    busy = workspace.id;
    error = '';
    message = '';
    try {
      await workspaceRecoveryApi.promote(workspace, userId);
      if (!active) return;
      message = `${candidate.display_name} is now a Manager of ${workspace.name}.`;
      await load();
      await tick();
      if (active) outcome?.focus();
    } catch (caught) {
      if (!active) return;
      if (caught instanceof ApiError && caught.status === 409) {
        await load();
        error =
          'The workspace changed. Review the current recovery list before trying again.';
      } else error = errorMessage(caught);
    } finally {
      if (active) busy = '';
    }
  }
</script>

<section
  id="workspace-manager-recovery"
  class="surface mt-4 rounded-2xl p-4 sm:p-6"
  aria-label="Workspace recovery"
  aria-busy={loading || Boolean(busy)}
>
  <div class="flex flex-wrap items-start justify-between gap-3">
    <div class="min-w-0 flex-1 basis-64">
      <h2 class="font-semibold">Workspaces without a Manager</h2>
      <p class="muted mt-2 max-w-2xl text-sm">
        If a shared workspace has no active Manager, promote one of its existing
        Members. Recovery records the change and keeps the workspace private.
      </p>
    </div>
    <button
      class="btn btn-secondary"
      onclick={() => load()}
      disabled={loading || Boolean(busy)}
      ><RefreshCw size={15} /> Refresh recovery list</button
    >
  </div>
  {#if message}<p
      class="mt-4 rounded-xl bg-[var(--accent-soft)] p-3 text-sm"
      role="status"
      tabindex="-1"
      bind:this={outcome}
    >
      {message}
    </p>{/if}
  {#if error}<p
      class="mt-4 text-sm text-red-700 dark:text-red-300"
      role="alert"
    >
      {error}
    </p>{/if}
  {#if loading && !items.length}<p class="muted mt-5 text-sm" role="status">
      Checking workspace management…
    </p>
  {:else if !items.length && !error}<p class="muted mt-5 text-sm">
      Every shared workspace has an active Manager.
    </p>
  {:else}
    <ul class="mt-5 space-y-3">
      {#each items as workspace (workspace.id)}
        <li class="rounded-xl border border-[var(--line)] p-4">
          <h3 class="break-words font-semibold">{workspace.name}</h3>
          {#if workspace.state === 'archived'}<p class="muted mt-1 text-xs">
              Archived workspace
            </p>{/if}
          {#if workspace.candidates.length}
            <form
              class="mt-3 flex flex-wrap items-end gap-3"
              onsubmit={(event) => {
                event.preventDefault();
                void recover(workspace);
              }}
            >
              <label class="min-w-0 flex-1 basis-48 text-sm font-semibold"
                >New Manager
                <select
                  class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2.5"
                  bind:value={selections[workspace.id]}
                  aria-label={`New Manager for ${workspace.name}`}
                  required
                  disabled={Boolean(busy) || loading}
                >
                  <option value="">Choose a Member</option>
                  {#each workspace.candidates as candidate (candidate.user_id)}<option
                      value={candidate.user_id}>{candidate.display_name}</option
                    >{/each}
                </select>
              </label>
              <button
                class="btn btn-primary"
                type="submit"
                disabled={!selections[workspace.id] || Boolean(busy) || loading}
                >{busy === workspace.id
                  ? 'Recovering…'
                  : 'Promote to Manager'}</button
              >
            </form>
          {:else}
            <p class="muted mt-3 text-sm">
              There are no active Members to promote. Re-enable an existing
              member account, then refresh this list.
            </p>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}
  {#if hasMore}<button
      class="btn btn-secondary mt-4"
      onclick={() => load(true)}
      disabled={loading || Boolean(busy)}
      >{loading ? 'Loading…' : 'Show more workspaces'}</button
    >{/if}
</section>
