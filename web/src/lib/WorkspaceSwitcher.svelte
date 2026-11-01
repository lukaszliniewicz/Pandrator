<script lang="ts">
  import { goto } from '$app/navigation';
  import { UsersRound } from '@lucide/svelte';
  import { appState } from './app-state.svelte';
  let {
    compact = false,
    onnavigate = () => {}
  }: { compact?: boolean; onnavigate?: () => void } = $props();
  async function select(event: Event) {
    const control = event.currentTarget as HTMLSelectElement;
    const id = control.value;
    try {
      // Router guards must accept leaving the editor before its scope changes.
      await goto(`/sessions?workspace=${encodeURIComponent(id)}`);
      onnavigate();
    } catch {
      // A dirty editor may cancel navigation; keep the existing scope selected.
    } finally {
      control.value = appState.activeWorkspaceId;
    }
  }
</script>

<div class="mb-3 border-b border-[var(--line)] pb-3">
  {#if compact}
    <a
      href="/settings/workspaces"
      onclick={onnavigate}
      class="nav-item grid size-11 place-items-center rounded-xl"
      aria-label="Workspaces"
      title={appState.activeWorkspace?.name ?? 'Workspaces'}
      ><UsersRound size={19} /></a
    >
  {:else}
    <label
      class="section-label block px-1"
      style:opacity="var(--workspace-details-opacity, 1)"
      for="active-workspace">Workspace</label
    >
    <div class="relative mt-2">
      <select
        id="active-workspace"
        class="min-h-11 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-sm"
        style:font-size="var(--workspace-select-font-size, 0.875rem)"
        style:appearance="var(--workspace-select-appearance, auto)"
        title={appState.activeWorkspace?.kind === 'personal'
          ? 'My workspace'
          : (appState.activeWorkspace?.name ?? 'Workspace')}
        value={appState.activeWorkspaceId}
        onchange={select}
        disabled={!appState.workspacesReady}
      >
        {#each appState.workspaces as workspace}<option
            class="text-sm"
            value={workspace.id}
            >{workspace.kind === 'personal'
              ? 'My workspace'
              : workspace.name}</option
          >{/each}
      </select>
      <span
        aria-hidden="true"
        class="pointer-events-none absolute inset-0 place-items-center"
        style:display="var(--workspace-select-icon-display, none)"
        ><UsersRound size={19} /></span
      >
    </div>
    <a
      class="muted mt-2 flex min-h-11 items-center px-1 text-xs underline"
      style:gap="var(--workspace-link-gap, 0.5rem)"
      style:justify-content="var(--workspace-link-justify, start)"
      href="/settings/workspaces"
      aria-label="Workspaces & invitations"
      title="Workspaces & invitations"
      onclick={onnavigate}
      ><UsersRound class="shrink-0" size={16} /><span
        class="overflow-hidden whitespace-nowrap"
        style:max-width="var(--workspace-link-label-max, 100%)"
        style:opacity="var(--workspace-details-opacity, 1)"
        >Workspaces & invitations</span
      ></a
    >
  {/if}
</div>
