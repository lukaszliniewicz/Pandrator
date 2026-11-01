<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import {
    Copy,
    KeyRound,
    Pencil,
    Plus,
    RefreshCw,
    ShieldCheck,
    UsersRound,
    X
  } from '@lucide/svelte';
  import {
    accountApi,
    type AccountRecord,
    type AccountRole,
    type PasswordResetLink
  } from './account-api';
  import { ApiError, ApiMutationAttempt } from './api';
  import { appState } from './app-state.svelte';
  import { errorMessage } from './errors';
  import { modalFocus } from './modal-focus';
  import type WorkspaceManagerRecovery from './WorkspaceManagerRecovery.svelte';

  let accounts = $state<AccountRecord[]>([]);
  let initialUserId = $state('');
  let namedUserAccess = $state(false);
  let loading = $state(true);
  let message = $state('');
  let loadError = $state('');
  let creating = $state(false);
  let editing = $state<AccountRecord | null>(null);
  let loginName = $state('');
  let displayName = $state('');
  let role = $state<AccountRole>('user');
  let enabled = $state(true);
  let saving = $state(false);
  let formError = $state('');
  let resetBusy = $state<string | null>(null);
  let resetLink = $state<PasswordResetLink | null>(null);
  let resetName = $state('');
  let resetUrl = $state('');
  let copyMessage = $state('');
  let linkInput = $state<HTMLInputElement>();
  let active = true;
  let mutation = new ApiMutationAttempt();
  let RecoveryPanel = $state<typeof WorkspaceManagerRecovery>();
  let recoveryOpen = $state(false);
  let recoveryLoading = $state(false);
  let recoveryError = $state('');
  const editorOpen = $derived(creating || editing !== null);
  const initialProtected = $derived(
    Boolean(editing && editing.user_id === initialUserId && !namedUserAccess)
  );
  const fieldClass =
    'mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2.5 disabled:opacity-60';

  onMount(() => {
    if (appState.isAdministrator) void load();
  });
  onDestroy(() => {
    active = false;
    resetLink = null;
    resetUrl = '';
  });

  async function load() {
    if (!appState.isAdministrator) return;
    loading = true;
    loadError = '';
    try {
      const result = await accountApi.list();
      if (!active) return;
      accounts = result.items;
      initialUserId = result.initial_user_id;
      namedUserAccess = Boolean(result.named_user_access);
    } catch (caught) {
      if (active) loadError = errorMessage(caught);
    } finally {
      if (active) loading = false;
    }
  }

  async function toggleRecovery() {
    if (
      !appState.isAdministrator ||
      !appState.collaborationEnabled ||
      recoveryLoading
    )
      return;
    if (recoveryOpen) {
      recoveryOpen = false;
      return;
    }
    recoveryLoading = true;
    recoveryError = '';
    try {
      const module = await import('./WorkspaceManagerRecovery.svelte');
      if (!active) return;
      RecoveryPanel = module.default;
      recoveryOpen = true;
    } catch (caught) {
      if (active) recoveryError = errorMessage(caught);
    } finally {
      if (active) recoveryLoading = false;
    }
  }

  function openEditor(account: AccountRecord | null = null) {
    if (!appState.isAdministrator) return;
    editing = account;
    creating = account === null;
    loginName = account?.login_name ?? '';
    displayName = account?.display_name ?? '';
    role = account?.role ?? 'user';
    enabled = account?.is_active ?? true;
    formError = '';
    mutation = new ApiMutationAttempt();
  }

  function closeEditor() {
    if (saving) return;
    creating = false;
    editing = null;
    formError = '';
  }

  async function saveAccount() {
    if (saving || !appState.isAdministrator) return;
    saving = true;
    formError = '';
    try {
      if (creating) {
        await accountApi.create(
          { login_name: loginName, display_name: displayName, role },
          mutation
        );
        message =
          'Account created. Create a password setup link for this person.';
      } else if (editing) {
        await accountApi.update(
          editing.user_id,
          {
            expected_auth_revision: editing.auth_revision,
            display_name: displayName,
            role,
            is_active: enabled
          },
          mutation
        );
        message = 'Account updated.';
      }
      creating = false;
      editing = null;
      await load();
      await appState.refreshIdentity();
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === 'stale_account') {
        creating = false;
        editing = null;
        await load();
        message =
          'This account changed. Select Edit again to review its current settings.';
      } else {
        formError = errorMessage(caught);
      }
    } finally {
      saving = false;
    }
  }

  async function issueReset(account: AccountRecord) {
    if (resetBusy || !appState.isAdministrator) return;
    resetBusy = account.user_id;
    message = '';
    loadError = '';
    try {
      const result = await accountApi.issueResetLink(
        account.user_id,
        account.auth_revision
      );
      if (!active) return;
      if (
        !/^\/password-reset#token=[A-Za-z0-9_-]{43,256}$/.test(
          result.reset_path
        )
      )
        throw new Error('The server returned an invalid password setup link.');
      resetLink = result;
      resetName = account.display_name;
      resetUrl = new URL(result.reset_path, window.location.origin).href;
      copyMessage = '';
    } catch (caught) {
      if (active) {
        loadError = errorMessage(caught);
        if (caught instanceof ApiError && caught.code === 'stale_account')
          await load();
      }
    } finally {
      if (active) resetBusy = null;
    }
  }

  function closeReset() {
    resetLink = null;
    resetUrl = '';
    copyMessage = '';
  }

  async function copyLink() {
    try {
      await navigator.clipboard.writeText(resetUrl);
      copyMessage = 'Link copied.';
    } catch {
      linkInput?.focus();
      linkInput?.select();
      copyMessage =
        'The link is selected. Copy it with your usual keyboard shortcut.';
    }
  }
</script>

<header class="mt-5 flex flex-wrap items-end justify-between gap-4">
  <div>
    <h1 class="flex items-center gap-3 text-3xl font-semibold">
      <UsersRound size={28} /> Users
    </h1>
    <p class="muted mt-3 max-w-2xl text-sm">
      Manage accounts and Administrator access. Workspace membership controls
      collaboration; personal work stays private.
    </p>
  </div>
  {#if appState.isAdministrator}
    <button class="btn btn-primary" onclick={() => openEditor()}
      ><Plus size={16} /> Add user</button
    >
  {/if}
</header>

{#if !appState.isAdministrator}
  <section class="surface mt-6 rounded-2xl p-6" role="alert">
    An Administrator account is required to manage users.
  </section>
{:else}
  <section
    class="surface mt-6 rounded-2xl p-4 sm:p-6"
    aria-label="User accounts"
    aria-busy={loading}
  >
    <div class="mb-5 flex flex-wrap items-center justify-between gap-3">
      <h2 class="font-semibold">
        Accounts{loading ? '' : ` (${accounts.length})`}
      </h2>
      <button
        class="btn btn-secondary"
        onclick={() => load()}
        disabled={loading}><RefreshCw size={15} /> Refresh</button
      >
    </div>
    {#if message}<p
        class="mb-4 rounded-xl bg-[var(--accent-soft)] p-3 text-sm"
        role="status"
      >
        {message}
      </p>{/if}
    {#if loadError}<p
        class="mb-4 text-sm text-red-700 dark:text-red-300"
        role="alert"
      >
        {loadError}
      </p>{/if}
    {#if !namedUserAccess && accounts.length > 1}
      <p class="muted mb-4 rounded-xl border border-[var(--line)] p-3 text-sm">
        Solo mode is active. Enable collaboration to let additional accounts
        sign in and work in shared workspaces.
      </p>
    {/if}
    {#if loading && !accounts.length}
      <p class="muted py-6 text-sm" role="status">Loading accounts…</p>
    {:else if !accounts.length && !loadError}
      <p class="muted py-6 text-sm">No accounts found.</p>
    {:else}
      <ul class="space-y-3">
        {#each accounts as account (account.user_id)}
          <li
            class="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-[var(--line)] p-4"
          >
            <div class="min-w-0 flex-1 basis-48">
              <h3 class="break-words font-semibold">
                {account.display_name}{#if account.user_id === appState.principal?.user_id}<span
                    class="muted ml-2 text-xs font-normal">You</span
                  >{/if}
              </h3>
              <p class="muted mt-1 break-all text-sm">{account.login_name}</p>
              <div
                class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs"
              >
                <span class="inline-flex items-center gap-1.5"
                  >{#if account.role === 'administrator'}<ShieldCheck
                      size={13}
                    /> Administrator{:else}User{/if}</span
                >
                <span class="muted"
                  >{account.is_active ? 'Enabled' : 'Disabled'}</span
                >
                {#if !account.password_configured}<span class="muted"
                    >Password not set</span
                  >{/if}
              </div>
            </div>
            <div class="flex flex-wrap gap-2">
              <button
                class="btn btn-secondary"
                aria-label={`Edit ${account.display_name}`}
                onclick={() => openEditor(account)}
                ><Pencil size={14} /> Edit</button
              >
              <button
                class="btn btn-secondary"
                aria-label={`Create password link for ${account.display_name}`}
                disabled={!account.is_active || resetBusy !== null}
                onclick={() => issueReset(account)}
                ><KeyRound size={14} />{resetBusy === account.user_id
                  ? 'Creating…'
                  : account.password_configured
                    ? 'Reset password'
                    : 'Set up password'}</button
              >
            </div>
          </li>
        {/each}
      </ul>
    {/if}
  </section>
  {#if appState.collaborationEnabled}
    <div class="mt-5">
      <button
        class="btn btn-secondary"
        onclick={toggleRecovery}
        aria-expanded={recoveryOpen}
        aria-controls="workspace-manager-recovery"
        disabled={recoveryLoading}
      >
        {recoveryLoading
          ? 'Opening recovery…'
          : recoveryOpen
            ? 'Hide workspace recovery'
            : 'Recover workspace management'}
      </button>
      {#if recoveryError}<p
          class="mt-3 text-sm text-red-700 dark:text-red-300"
          role="alert"
        >
          {recoveryError}
        </p>{/if}
    </div>
    {#if recoveryOpen && RecoveryPanel}<RecoveryPanel />{/if}
  {/if}
{/if}

{#if editorOpen && appState.isAdministrator}
  <div
    class="fixed inset-0 z-[70] grid place-items-center bg-black/45 p-4 backdrop-blur-sm"
    role="presentation"
  >
    <div
      use:modalFocus={{ onclose: closeEditor }}
      class="surface max-h-[92vh] w-full max-w-lg overflow-auto rounded-3xl p-5 sm:p-7"
      role="dialog"
      aria-modal="true"
      aria-labelledby="account-editor-title"
    >
      <header class="flex items-center justify-between gap-4">
        <h2 id="account-editor-title" class="text-xl font-semibold">
          {creating ? 'Add user' : 'Edit account'}
        </h2>
        <button
          class="btn btn-icon btn-secondary"
          aria-label="Close account editor"
          onclick={closeEditor}
          disabled={saving}><X size={18} /></button
        >
      </header>
      <form
        class="mt-6 space-y-5"
        onsubmit={(event) => {
          event.preventDefault();
          void saveAccount();
        }}
      >
        <label class="block text-sm font-semibold"
          >Display name<input
            class={fieldClass}
            bind:value={displayName}
            maxlength="255"
            required
            disabled={saving}
            autocomplete="off"
          /></label
        >
        <label class="block text-sm font-semibold"
          >Login name<input
            class={fieldClass}
            bind:value={loginName}
            aria-label="Login name"
            aria-describedby={creating ? 'account-login-help' : undefined}
            maxlength="64"
            pattern="[A-Za-z0-9][A-Za-z0-9._\-]*"
            required
            disabled={saving || !creating}
            autocomplete="off"
          />
          {#if creating}<span
              id="account-login-help"
              class="muted mt-1.5 block text-xs font-normal"
              >Letters, numbers, dots, underscores, and hyphens. Login names are
              saved in lowercase.</span
            >{/if}
        </label>
        <label class="block text-sm font-semibold"
          >Role<select
            class={fieldClass}
            bind:value={role}
            aria-label="Role"
            aria-describedby="account-role-help"
            disabled={saving || initialProtected}
            ><option value="user">User</option><option value="administrator"
              >Administrator</option
            ></select
          >
          <span
            id="account-role-help"
            class="muted mt-1.5 block text-xs font-normal"
            >Administrators manage users, model downloads, and installation
            settings.</span
          >
        </label>
        {#if !creating}
          <label class="flex items-start gap-3 text-sm"
            ><input
              class="mt-1 accent-[var(--accent)]"
              type="checkbox"
              bind:checked={enabled}
              disabled={saving || initialProtected}
            /><span
              ><strong class="block">Account enabled</strong><span
                class="muted text-xs"
                >Disabling signs this person out and blocks further sign-in.</span
              ></span
            ></label
          >
        {/if}
        {#if initialProtected}<p
            class="muted rounded-xl border border-[var(--line)] p-3 text-xs"
          >
            The initial Administrator's access is protected while single-user
            mode is active.
          </p>{/if}
        {#if formError}<p
            class="text-sm text-red-700 dark:text-red-300"
            role="alert"
          >
            {formError}
          </p>{/if}
        <div class="flex flex-wrap justify-end gap-3 pt-1">
          <button
            class="btn btn-secondary"
            type="button"
            onclick={closeEditor}
            disabled={saving}>Cancel</button
          ><button class="btn btn-primary" type="submit" disabled={saving}
            >{saving
              ? 'Saving…'
              : creating
                ? 'Create account'
                : 'Save account'}</button
          >
        </div>
      </form>
    </div>
  </div>
{/if}

{#if resetLink && appState.isAdministrator}
  <div
    class="fixed inset-0 z-[70] grid place-items-center bg-black/45 p-4 backdrop-blur-sm"
    role="presentation"
  >
    <div
      use:modalFocus={{ onclose: closeReset }}
      class="surface w-full max-w-lg rounded-3xl p-5 sm:p-7"
      role="dialog"
      aria-modal="true"
      aria-labelledby="reset-link-title"
    >
      <header class="flex items-center justify-between gap-4">
        <h2 id="reset-link-title" class="text-xl font-semibold">
          Password link for {resetName}
        </h2>
        <button
          class="btn btn-icon btn-secondary"
          aria-label="Close password link"
          onclick={closeReset}><X size={18} /></button
        >
      </header>
      <p class="muted mt-4 text-sm">
        Share this link privately with {resetName}. It can be used once and
        expires {new Date(resetLink.expires_at).toLocaleString()}.
      </p>
      <label class="mt-5 block text-sm font-semibold"
        >Password setup link<input
          bind:this={linkInput}
          class={fieldClass}
          value={resetUrl}
          readonly
          spellcheck="false"
          autocomplete="off"
        /></label
      >
      <p class="muted mt-3 text-xs">
        Creating another link replaces this one. The current password remains
        valid until the new password is saved.
      </p>
      {#if copyMessage}<p class="mt-3 text-sm" role="status">
          {copyMessage}
        </p>{/if}
      <div class="mt-6 flex flex-wrap justify-end gap-3">
        <button class="btn btn-secondary" onclick={closeReset}>Close</button
        ><button class="btn btn-primary" onclick={copyLink}
          ><Copy size={15} /> Copy link</button
        >
      </div>
    </div>
  </div>
{/if}
