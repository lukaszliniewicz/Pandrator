<script lang="ts">
  import { appState } from '$lib/app-state.svelte';
  import { workspaceApi } from '$lib/workspace-api';
  import { errorMessage } from '$lib/errors';
  let busy = $state(false);
  let error = $state('');
  let understood = $state(false);
  async function enable() {
    busy = true;
    error = '';
    try {
      await workspaceApi.enable(appState.installationRevision);
      await appState.refreshIdentity();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
</script>

<svelte:head><title>Collaboration — Pandrator</title></svelte:head>
<div class="mx-auto max-w-3xl">
  <a class="muted text-sm underline" href="/settings">Application settings</a>
  <h1 class="mt-4 text-3xl font-semibold">Collaboration</h1>
  {#if !appState.isAdministrator}
    <p class="surface mt-6 rounded-2xl p-6">
      An Administrator can enable collaboration for this installation.
    </p>
  {:else if appState.collaborationEnabled}
    <section class="surface mt-6 rounded-2xl p-6">
      <h2 class="text-xl font-semibold">Collaboration is enabled</h2>
      <p class="muted mt-3">
        Each person signs in with their own account and has a private workspace.
        Shared workspaces belong to the team.
      </p>
      <div class="mt-5 flex flex-wrap gap-3">
        <a class="btn btn-primary" href="/settings/users">Manage users</a><a
          class="btn btn-secondary"
          href="/settings/workspaces">Workspaces & invitations</a
        >
      </div>
      {#if !appState.principal?.scopes.includes('app.workspaces.enroll')}<p
          class="mt-4 text-sm"
        >
          Sign out and sign in again to approve creating and joining workspaces.
        </p>{/if}
    </section>
  {:else}
    <section class="surface mt-6 rounded-2xl p-6">
      <h2 class="text-xl font-semibold">Use Pandrator with a small team</h2>
      <div class="muted mt-4 space-y-3 text-sm leading-relaxed">
        <p>
          Solo is the default. Enable collaboration to add named sign-in and
          shared workspaces.
        </p>
        <p>
          Every member of a shared workspace can create, edit, run and export
          its projects, and organize its folders. Managers also manage
          membership and workspace settings.
        </p>
        <p>
          Your existing work stays in your private workspace. Invite people to a
          shared workspace when you want to collaborate. An Administrator
          manages accounts and model downloads; that role does not grant access
          to private projects.
        </p>
        <p>
          Set an Administrator password before enabling collaboration. Remote
          team members need an HTTPS address configured through your normal
          hosting setup.
        </p>
      </div>
      {#if !appState.collaborationAvailable}
        <p class="mt-5 rounded-xl border border-[var(--line)] p-3 text-sm">
          Collaboration is awaiting release qualification in this build.
        </p>
      {:else}
        {#if !appState.administratorPasswordConfigured}<p class="mt-5 text-sm">
            First, <a class="underline" href="/settings/users"
              >set an Administrator password in Manage users</a
            >, then sign in with it and return here.
          </p>{/if}
        <label class="mt-5 flex items-start gap-3 text-sm"
          ><input
            class="mt-1"
            type="checkbox"
            bind:checked={understood}
          />Enable named accounts and the shared workspace workflow on this
          installation.</label
        >
        <button
          class="btn btn-primary mt-5"
          disabled={busy ||
            !understood ||
            !appState.administratorPasswordConfigured}
          onclick={enable}>{busy ? 'Enabling…' : 'Enable collaboration'}</button
        >
      {/if}
    </section>
  {/if}
  {#if error}<p role="alert" class="mt-4 text-sm text-red-500">{error}</p>{/if}
</div>
