<script lang="ts">
  import { onMount } from 'svelte';
  import { appState } from './app-state.svelte';
  import { publicLinkApi, type PublicSharingSettings } from './public-link-api';
  import { errorMessage } from './errors';
  let settings = $state<PublicSharingSettings>();
  let enabled = $state(false);
  let origin = $state('');
  let busy = $state(false);
  let error = $state('');
  let notice = $state('');
  async function load() {
    try {
      settings = await publicLinkApi.settings();
      enabled = settings.enabled;
      origin =
        settings.public_origin ?? settings.hosting?.canonical_origin ?? '';
    } catch (caught) {
      error = errorMessage(caught);
    }
  }
  async function save() {
    if (!settings) return;
    busy = true;
    error = '';
    notice = '';
    try {
      settings = await publicLinkApi.configure({
        enabled,
        public_origin: origin.trim() || null,
        revision: settings.revision
      });
      await appState.refreshIdentity();
      notice = enabled
        ? 'Public link sharing enabled.'
        : 'Sharing disabled. Existing links and visitor sessions have been revoked.';
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
  onMount(() => {
    void load();
  });
</script>

<section
  class="surface mt-6 rounded-2xl p-6"
  aria-labelledby="public-sharing-title"
>
  <h2 id="public-sharing-title" class="text-xl font-semibold">
    Public link sharing
  </h2>
  <p class="muted mt-2 text-sm">
    Optional access for people outside your team. Keep this off for a normal
    local installation.
  </p>
  {#if error}<p
      role="alert"
      class="mt-3 text-sm text-red-700 dark:text-red-300"
    >
      {error}
    </p>{/if}
  {#if notice}<p role="status" class="mt-3 text-sm">{notice}</p>{/if}
  {#if !settings && !error}<p class="muted mt-3" role="status">
      Loading sharing settings…
    </p>{/if}
  {#if settings}
    <label class="mt-5 flex items-start gap-3"
      ><input
        type="checkbox"
        bind:checked={enabled}
        disabled={busy}
        class="mt-1"
      /><span
        ><strong class="block"
          >Enable links to projects and translation reviews</strong
        ><small class="muted"
          >Anyone with a link can exercise its stated access. Project-management
          links can use configured paid services.</small
        ></span
      ></label
    >
    <label class="mt-4 block text-sm font-semibold"
      >Application origin<input
        class="input mt-1 w-full"
        type="url"
        bind:value={origin}
        disabled={busy}
        placeholder="https://pandrator.example.com"
      /></label
    >
    <p class="muted mt-2 text-sm">
      Configure your HTTPS reverse proxy first, then enter that same origin
      here. HTTP is supported only on loopback for local testing.
    </p>
    <p class="mt-3 text-sm">
      Turning sharing off revokes all existing links and visitor sessions.
      Re-enabling requires new links. Submitted corrections remain available to
      project editors.
    </p>
    <button class="btn btn-primary mt-4" onclick={save} disabled={busy}
      >{busy ? 'Saving…' : 'Save sharing settings'}</button
    >
    {#if settings.hosting}<details
        class="mt-5 border-t border-[var(--line)] pt-4"
      >
        <summary class="cursor-pointer font-semibold"
          >Hosting and protection setup</summary
        >
        <dl class="mt-3 grid gap-2 text-sm sm:grid-cols-2">
          <dt>Configured application origin</dt>
          <dd class="break-all">{settings.hosting.canonical_origin}</dd>
          <dt>Request transport</dt>
          <dd>{settings.hosting.secure_transport ? 'HTTPS' : 'HTTP'}</dd>
          <dt>Secure cookies</dt>
          <dd>{settings.hosting.secure_cookies ? 'Enabled' : 'Disabled'}</dd>
          <dt>Trusted proxy hops</dt>
          <dd>{settings.hosting.proxy_hops}</dd>
          <dt>Trusted hosts</dt>
          <dd class="break-all">{settings.hosting.trusted_hosts.join(', ')}</dd>
        </dl>
        <p class="muted mt-3 text-sm">
          Keep the backend listener and permanent Manager/MCP endpoints behind
          your network boundary. Forwarded client addresses are trustworthy only
          through the configured proxy chain.
        </p>
        <a
          class="btn btn-secondary mt-3"
          href="https://github.com/lukaszliniewicz/Pandrator/blob/main/docs/operations/remote-and-headless.md"
          target="_blank"
          rel="noreferrer">Hosting and security integration guide</a
        >
      </details>{/if}
  {/if}
</section>
