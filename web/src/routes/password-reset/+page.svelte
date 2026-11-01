<script lang="ts">
  import { onMount } from 'svelte';
  import { afterNavigate, goto } from '$app/navigation';
  import { KeyRound } from '@lucide/svelte';
  import { accountApi } from '$lib/account-api';
  import { errorMessage } from '$lib/errors';

  let token = $state('');
  let ready = $state(false);
  let password = $state('');
  let confirmation = $state('');
  let saving = $state(false);
  let complete = $state(false);
  let error = $state('');
  let linkRevision = 0;
  let active = true;
  let lastFragment = '';

  function consumeResetLink() {
    const fragment = location.hash;
    if (!fragment) {
      lastFragment = '';
      ready = true;
      return;
    }
    if (fragment === lastFragment) return;
    lastFragment = fragment;
    linkRevision += 1;
    token = new URLSearchParams(fragment.slice(1)).get('token') ?? '';
    password = '';
    confirmation = '';
    error = '';
    saving = false;
    complete = false;
    ready = true;
    void goto(location.pathname + location.search, {
      replaceState: true,
      noScroll: true,
      keepFocus: true
    }).catch((caught) => {
      if (active) error = errorMessage(caught);
    });
  }

  afterNavigate(consumeResetLink);

  onMount(() => {
    ready = true;
    window.addEventListener('hashchange', consumeResetLink);
    return () => {
      active = false;
      window.removeEventListener('hashchange', consumeResetLink);
      token = '';
      password = '';
      confirmation = '';
    };
  });

  async function save() {
    if (saving) return;
    error = '';
    if (password !== confirmation) {
      error = 'The passwords do not match.';
      return;
    }
    saving = true;
    const revision = linkRevision;
    try {
      await accountApi.redeemReset(token, password);
      if (!active || revision !== linkRevision) return;
      token = '';
      password = '';
      confirmation = '';
      complete = true;
    } catch (caught) {
      if (active && revision === linkRevision) error = errorMessage(caught);
    } finally {
      if (active && revision === linkRevision) saving = false;
    }
  }
</script>

<svelte:head><title>Set your password — Pandrator</title></svelte:head>

<main class="grid min-h-screen place-items-center p-5">
  <section
    class="surface w-full max-w-md rounded-3xl p-7 sm:p-9"
    aria-labelledby="password-title"
  >
    <KeyRound class="text-[var(--accent)]" size={28} />
    <h1 id="password-title" class="mt-5 text-2xl font-semibold">
      Set your Pandrator password
    </h1>
    {#if complete}
      <p class="muted mt-4" role="status">
        Your password is saved. Existing sessions for this account have been
        signed out.
      </p>
      <a class="btn btn-primary mt-6" href="/" data-sveltekit-reload
        >Return to Pandrator</a
      >
    {:else if ready && !token}
      <p class="muted mt-4" role="alert">
        This link has no password reset token. Ask an Administrator for a new
        link.
      </p>
    {:else if ready}
      <p class="muted mt-3 text-sm">
        Use at least 15 characters. A few unrelated words work well.
      </p>
      <form
        class="mt-6 space-y-5"
        onsubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        <label class="block text-sm font-semibold"
          >New password
          <input
            class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2.5"
            type="password"
            bind:value={password}
            minlength="15"
            maxlength="1024"
            autocomplete="new-password"
            required
            disabled={saving}
          />
        </label>
        <label class="block text-sm font-semibold"
          >Confirm new password
          <input
            class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2.5"
            type="password"
            bind:value={confirmation}
            minlength="15"
            maxlength="1024"
            autocomplete="new-password"
            required
            disabled={saving}
          />
        </label>
        {#if error}<p
            class="text-sm text-red-700 dark:text-red-300"
            role="alert"
          >
            {error}
          </p>{/if}
        <button class="btn btn-primary w-full" type="submit" disabled={saving}
          >{saving ? 'Saving…' : 'Save password'}</button
        >
      </form>
    {/if}
  </section>
</main>
