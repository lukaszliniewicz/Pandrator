<script lang="ts">
  import { onMount } from 'svelte';
  import { appState } from '$lib/app-state.svelte';
  import { ApiMutationAttempt } from '$lib/api';
  import { errorMessage } from '$lib/errors';
  import {
    workspaceApi,
    type WorkspaceRecord,
    type WorkspaceMember,
    type WorkspaceInvitation,
    type WorkspaceUser
  } from '$lib/workspace-api';
  let workspaces = $state<WorkspaceRecord[]>([]);
  let workspace = $state<WorkspaceRecord | null>(null);
  let members = $state<WorkspaceMember[]>([]);
  let invitations = $state<WorkspaceInvitation[]>([]);
  let inbox = $state<WorkspaceInvitation[]>([]);
  let loading = $state(true);
  let busy = $state(false);
  let error = $state('');
  let message = $state('');
  let name = $state('');
  let rename = $state('');
  let query = $state('');
  let users = $state<WorkspaceUser[]>([]);
  let selectedUser = $state('');
  let role = $state<'manager' | 'member'>('member');
  let searchBusy = $state(false);
  let searchRevision = 0;
  let loadRevision = 0;
  const creation = new ApiMutationAttempt();
  const invitation = new ApiMutationAttempt();
  const canManage = $derived(
    workspace?.kind === 'team' && workspace.allowed_actions.includes('manage')
  );
  const canEnroll = $derived(
    ['app.read', 'app.write', 'app.workspaces.enroll'].every((scope) =>
      appState.principal?.scopes.includes(scope)
    )
  );
  async function details(selected: WorkspaceRecord | null) {
    const revision = ++loadRevision;
    workspace = selected;
    rename = selected?.name ?? '';
    members = [];
    invitations = [];
    users = [];
    selectedUser = '';
    query = '';
    if (
      !selected ||
      selected.kind === 'personal' ||
      selected.state === 'archived'
    )
      return;
    const [roster, pending] = await Promise.all([
      workspaceApi.members(selected.id),
      selected.role === 'manager'
        ? workspaceApi.invitations(selected.id)
        : Promise.resolve({ items: [] })
    ]);
    if (revision !== loadRevision) return;
    members = roster.items;
    invitations = pending.items.filter((item) => item.state === 'pending');
  }
  async function load(
    selectedId = workspace?.id ?? appState.activeWorkspaceId
  ) {
    loading = true;
    error = '';
    try {
      const [all, received] = await Promise.all([
        workspaceApi.list(true),
        workspaceApi.inbox()
      ]);
      workspaces = all.items;
      inbox = received.items.filter((item) => item.state === 'pending');
      await details(
        workspaces.find((item) => item.id === selectedId) ??
          workspaces[0] ??
          null
      );
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      loading = false;
    }
  }
  async function run(
    action: () => Promise<unknown>,
    success: string,
    nextId?: () => string
  ) {
    busy = true;
    error = '';
    message = '';
    try {
      await action();
      await appState.refreshWorkspaces();
      const target = nextId?.();
      await load(target ?? workspace?.id);
      if (target) appState.selectWorkspace(target);
      message = success;
    } catch (caught) {
      error = errorMessage(caught);
      // Revision conflicts need a fresh view before another governance command.
      await load().catch(() => undefined);
      if (!error) error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
  async function create() {
    let created = '';
    await run(
      async () => {
        const result = await workspaceApi.create(name.trim(), creation);
        created = result.id;
        appState.selectWorkspace(created);
        name = '';
      },
      'Workspace created.',
      () => created
    );
  }
  async function search() {
    if (!workspace || query.trim().length < 2) return;
    const revision = ++searchRevision;
    const id = workspace.id;
    searchBusy = true;
    error = '';
    try {
      const result = await workspaceApi.searchUsers(id, query.trim());
      if (revision === searchRevision && workspace?.id === id) {
        users = result.items;
        selectedUser = '';
      }
    } catch (caught) {
      if (revision === searchRevision) error = errorMessage(caught);
    } finally {
      if (revision === searchRevision) searchBusy = false;
    }
  }
  function select(event: Event) {
    error = '';
    message = '';
    searchRevision += 1;
    const selected =
      workspaces.find(
        (item) => item.id === (event.currentTarget as HTMLSelectElement).value
      ) ?? null;
    void details(selected).catch((caught) => {
      error = errorMessage(caught);
    });
  }
  onMount(() => {
    if (appState.collaborationEnabled) void load();
    else loading = false;
    return () => {
      loadRevision += 1;
      searchRevision += 1;
    };
  });
</script>

<svelte:head><title>Workspaces & invitations — Pandrator</title></svelte:head>
<div class="mx-auto max-w-4xl space-y-6">
  <header>
    <a class="muted text-sm underline" href="/settings">Application settings</a>
    <h1 class="mt-4 text-3xl font-semibold">Workspaces & invitations</h1>
    <p class="muted mt-3">
      Collaborate in shared workspaces. Every member can work on all their
      projects.
    </p>
  </header>
  {#if !appState.collaborationEnabled}
    <section class="surface rounded-2xl p-6">
      <p>This installation uses Solo mode.</p>
      {#if appState.isAdministrator}<a
          href="/settings/collaboration"
          class="btn btn-primary mt-4">Set up collaboration</a
        >{/if}
    </section>
  {:else}
    {#if error}<p
        role="alert"
        class="rounded-xl border border-red-500/30 p-3 text-sm text-red-500"
      >
        {error}
      </p>{/if}
    {#if message}<p
        role="status"
        class="rounded-xl bg-[var(--accent-soft)] p-3 text-sm"
      >
        {message}
      </p>{/if}
    {#if loading}<p role="status" class="muted">Loading workspaces…</p>{/if}
    {#if inbox.length}
      <section class="surface rounded-2xl p-6">
        <h2 class="text-xl font-semibold">Invitations</h2>
        <ul class="mt-4 space-y-3">
          {#each inbox as item (item.id)}<li
              class="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-[var(--line)] p-4"
            >
              <div>
                <strong>{item.workspace_name}</strong>
                <p class="muted mt-1 text-sm">
                  {item.invited_by_display_name} invited you as {item.role ===
                  'manager'
                    ? 'a Manager'
                    : 'a Member'}. Expires {new Date(
                    item.expires_at
                  ).toLocaleDateString()}.
                </p>
              </div>
              <div class="flex gap-2">
                <button
                  class="btn btn-primary"
                  disabled={busy || !canEnroll}
                  onclick={() =>
                    run(
                      () => workspaceApi.respond(item.id, true),
                      'Invitation accepted.'
                    )}>Join workspace</button
                ><button
                  class="btn btn-secondary"
                  disabled={busy}
                  onclick={() =>
                    run(
                      () => workspaceApi.respond(item.id, false),
                      'Invitation declined.'
                    )}>Decline</button
                >
              </div>
            </li>{/each}
        </ul>
      </section>
    {/if}
    <section class="surface rounded-2xl p-6">
      <h2 class="text-xl font-semibold">Create a shared workspace</h2>
      <form
        class="mt-4 flex flex-wrap items-end gap-3"
        onsubmit={(event) => {
          event.preventDefault();
          void create();
        }}
      >
        <label class="min-w-48 flex-1 text-sm font-semibold"
          >Workspace name<input
            class="rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 mt-2 w-full"
            bind:value={name}
            maxlength="255"
            required
            disabled={busy}
          /></label
        ><button
          class="btn btn-primary"
          disabled={busy || !canEnroll || !name.trim()}>Create workspace</button
        >
      </form>
      {#if !canEnroll}<p class="muted mt-3 text-sm">
          Sign in again with an account allowed to create or join workspaces.
        </p>{/if}
    </section>
    <section class="surface rounded-2xl p-6">
      <label class="text-sm font-semibold" for="managed-workspace"
        >Manage workspace</label
      ><select
        id="managed-workspace"
        class="rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 mt-2 w-full"
        value={workspace?.id ?? ''}
        onchange={select}
        disabled={busy || loading}
        >{#each workspaces as item}<option value={item.id}
            >{item.kind === 'personal'
              ? 'My workspace (private)'
              : item.name}{item.state === 'archived'
              ? ' — archived'
              : ''}</option
          >{/each}</select
      >
      {#if workspace?.kind === 'personal'}<p class="muted mt-4 text-sm">
          Your personal workspace is private. Create a shared workspace to
          collaborate.
        </p>
      {:else if workspace}
        <div class="mt-5 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 class="text-xl font-semibold">{workspace.name}</h2>
            <p class="muted mt-1 text-sm">
              {workspace.role === 'manager' ? 'Manager' : 'Member'} · {workspace.state ===
              'archived'
                ? 'Archived'
                : 'Shared workspace'}
            </p>
          </div>
          {#if workspace.state === 'active'}<button
              class="btn btn-secondary"
              disabled={busy}
              onclick={() => {
                const current = workspace;
                if (
                  current &&
                  confirm(
                    'Leave this workspace? You will lose access to its projects. Your work stays with the workspace.'
                  )
                )
                  void run(
                    () => workspaceApi.lifecycle(current, 'leave'),
                    'You left the workspace.'
                  );
              }}>Leave workspace</button
            >{/if}
        </div>
        {#if workspace.state === 'archived'}
          <p class="muted mt-4 text-sm">
            Projects are retained while this workspace is archived.
          </p>
          {#if canManage}<button
              class="btn btn-primary mt-4"
              disabled={busy}
              onclick={() => {
                const current = workspace;
                if (current)
                  void run(
                    () => workspaceApi.lifecycle(current, 'restore'),
                    'Workspace restored.'
                  );
              }}>Restore workspace</button
            >{/if}
        {:else}
          <h3 class="mt-6 font-semibold">Members</h3>
          <ul class="mt-3 divide-y divide-[var(--line)]">
            {#each members as member (member.user_id)}<li
                class="flex flex-wrap items-center justify-between gap-3 py-3"
              >
                <div>
                  <strong class="text-sm">{member.display_name}</strong>
                  <p class="muted text-xs">
                    {member.login_name}{member.is_active
                      ? ''
                      : ' · account disabled'} · {member.role === 'manager'
                      ? 'Manager'
                      : 'Member'}
                  </p>
                </div>
                {#if canManage}<div class="flex flex-wrap gap-2">
                    <button
                      class="btn btn-secondary text-xs"
                      disabled={busy}
                      onclick={() => {
                        const current = workspace;
                        if (current)
                          void run(
                            () =>
                              workspaceApi.setRole(
                                current,
                                member.user_id,
                                member.role === 'manager' ? 'member' : 'manager'
                              ),
                            'Member role updated.'
                          );
                      }}
                      >{member.role === 'manager'
                        ? 'Make Member'
                        : 'Make Manager'}</button
                    >{#if member.user_id !== appState.principal?.user_id}<button
                        class="btn btn-secondary text-xs"
                        disabled={busy}
                        onclick={() => {
                          const current = workspace;
                          if (
                            current &&
                            confirm(
                              `Remove ${member.display_name} from this workspace? Their access will be revoked; their work stays with the workspace.`
                            )
                          )
                            void run(
                              () =>
                                workspaceApi.remove(current, member.user_id),
                              'Member removed.'
                            );
                        }}>Remove</button
                      >{/if}
                  </div>{/if}
              </li>{/each}
          </ul>
          {#if canManage}
            <div class="mt-6 border-t border-[var(--line)] pt-6">
              <h3 class="font-semibold">Invite a user</h3>
              <p class="muted mt-2 text-sm">
                An Administrator creates accounts in <a
                  href="/settings/users"
                  class="underline">Manage users</a
                >. Search for an existing user to invite them.
              </p>
              <form
                class="mt-3 flex flex-wrap items-end gap-3"
                onsubmit={(event) => {
                  event.preventDefault();
                  void search();
                }}
              >
                <label class="flex-1 text-sm"
                  >Name or login<input
                    class="rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 mt-2 w-full"
                    bind:value={query}
                    minlength="2"
                    maxlength="64"
                    required
                  /></label
                ><button
                  class="btn btn-secondary"
                  disabled={busy || searchBusy || query.trim().length < 2}
                  >{searchBusy ? 'Searching…' : 'Search users'}</button
                >
              </form>
              {#if users.length}<form
                  class="mt-3 flex flex-wrap items-end gap-3"
                  onsubmit={(event) => {
                    event.preventDefault();
                    const current = workspace;
                    if (current)
                      void run(
                        () =>
                          workspaceApi.invite(
                            current,
                            selectedUser,
                            role,
                            invitation
                          ),
                        'Invitation sent.'
                      );
                  }}
                >
                  <label class="flex-1 text-sm"
                    >User<select
                      class="rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 mt-2 w-full"
                      bind:value={selectedUser}
                      required
                      ><option value="">Choose a user</option
                      >{#each users as user}<option value={user.user_id}
                          >{user.display_name} ({user.login_name})</option
                        >{/each}</select
                    ></label
                  ><label class="text-sm"
                    >Role<select
                      class="rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 mt-2 w-full"
                      bind:value={role}
                      ><option value="member">Member</option><option
                        value="manager">Manager</option
                      ></select
                    ></label
                  ><button
                    class="btn btn-primary"
                    disabled={busy || !selectedUser}>Send invitation</button
                  >
                </form>{/if}
              {#if invitations.length}<h4 class="mt-5 text-sm font-semibold">
                  Pending invitations
                </h4>
                <ul class="mt-2 space-y-2">
                  {#each invitations as item}<li
                      class="flex flex-wrap items-center justify-between gap-3 text-sm"
                    >
                      <span
                        >{item.display_name} · {item.role === 'manager'
                          ? 'Manager'
                          : 'Member'}</span
                      ><button
                        class="btn btn-secondary text-xs"
                        disabled={busy}
                        onclick={() => {
                          const current = workspace;
                          if (current)
                            void run(
                              () => workspaceApi.revoke(current, item.id),
                              'Invitation revoked.'
                            );
                        }}>Revoke invitation</button
                      >
                    </li>{/each}
                </ul>{/if}
            </div>
            <form
              class="mt-6 flex flex-wrap items-end gap-3 border-t border-[var(--line)] pt-6"
              onsubmit={(event) => {
                event.preventDefault();
                const current = workspace;
                if (current)
                  void run(
                    () => workspaceApi.rename(current, rename.trim()),
                    'Workspace renamed.'
                  );
              }}
            >
              <label class="flex-1 text-sm"
                >Workspace name<input
                  class="rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 mt-2 w-full"
                  bind:value={rename}
                  maxlength="255"
                  required
                /></label
              ><button
                class="btn btn-secondary"
                disabled={busy ||
                  !rename.trim() ||
                  rename.trim() === workspace.name}>Save name</button
              >
            </form>
            <button
              class="btn btn-secondary mt-6"
              disabled={busy}
              onclick={() => {
                const current = workspace;
                if (
                  current &&
                  confirm(
                    'Archive this workspace? Its projects will be retained and unavailable until a Manager restores it.'
                  )
                )
                  void run(
                    () => workspaceApi.lifecycle(current, 'archive'),
                    'Workspace archived.'
                  );
              }}>Archive workspace</button
            >
          {/if}
        {/if}
      {/if}
    </section>
  {/if}
</div>
