<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/state';
  import { goto } from '$app/navigation';
  import { publicLinkApi } from '$lib/public-link-api';
  import {
    clearProjectLink,
    selectProjectLink
  } from '$lib/project-link-context';
  import { appState } from '$lib/app-state.svelte';
  import { errorMessage } from '$lib/errors';
  let token = $state('');
  let error = $state('');
  let busy = $state(false);
  onMount(() => {
    appState.suspendIdentity();
    clearProjectLink();
    token = new URLSearchParams(location.hash.slice(1)).get('token') ?? '';
    history.replaceState(history.state, '', location.pathname);
    if (!token)
      error =
        'Open the complete link supplied by the project owner. It may have expired or been revoked.';
  });
  async function open() {
    busy = true;
    error = '';
    try {
      const result = await publicLinkApi.redeem(page.params.id!, token);
      token = '';
      selectProjectLink({
        id: result.link.id,
        csrf: result.csrf_token,
        mode: result.link.mode,
        projectId: result.link.project_id,
        rootSessionId: result.link.root_session_id,
        label: result.link.label
      });
      if (result.link.mode === 'review_translation')
        await goto(`/share/${result.link.id}/review`, { replaceState: true });
      else {
        await appState.initialize();
        await goto(`/sessions/${result.link.root_session_id}`, {
          replaceState: true
        });
      }
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
</script>

<main class="grid min-h-screen place-items-center p-5">
  <section
    class="surface w-full max-w-lg rounded-2xl p-7"
    aria-labelledby="shared-link-title"
  >
    <h1 id="shared-link-title" class="text-2xl font-semibold">
      Open shared Pandrator work
    </h1>
    <p class="muted mt-3">
      The link grants access to one project or one translation review. Your
      visitor access expires and the owner can revoke it.
    </p>
    {#if error}<p role="alert" class="mt-4 text-red-700 dark:text-red-300">
        {error}
      </p>{/if}
    <button
      class="btn btn-primary mt-5"
      disabled={busy || !token}
      onclick={open}>{busy ? 'Opening…' : 'Open shared work'}</button
    >
  </section>
</main>
