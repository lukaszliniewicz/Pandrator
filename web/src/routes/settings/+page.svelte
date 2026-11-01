<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { errorMessage } from '$lib/errors';
  import { RefreshCw, Save, ShieldCheck } from '@lucide/svelte';
  import { settingApi } from '$lib/admin-api';
  import type { GpuDevice } from '$lib/api-models';
  import { appState } from '$lib/app-state.svelte';
  import GlobalSettingsPanel from '$lib/GlobalSettingsPanel.svelte';
  import SessionTrashPolicy from '$lib/SessionTrashPolicy.svelte';
  import McpLocalPathsPanel from '$lib/McpLocalPathsPanel.svelte';
  import type PublicSharingSettings from '$lib/PublicSharingSettings.svelte';

  let wizardVisible = $state(false);
  let retention = $state(30);
  let revision = $state(0);
  let saving = $state(false);
  let message = $state('');
  let active = true;
  let openSections = $state<string[]>([]);
  let sharingOpen = $state(false);
  let SharingSettingsComponent = $state<typeof PublicSharingSettings | null>(
    null
  );
  let sharingLoadError = $state('');
  async function loadSharingSettings() {
    try {
      SharingSettingsComponent = (
        await import('$lib/PublicSharingSettings.svelte')
      ).default;
    } catch (caught) {
      sharingLoadError = errorMessage(caught);
    }
  }
  const workspaceId = $derived(
    appState.collaborationEnabled
      ? appState.activeWorkspaceId || undefined
      : undefined
  );
  const workspaceName = $derived(
    appState.collaborationEnabled
      ? (appState.activeWorkspace?.name ?? 'this workspace')
      : 'your projects'
  );
  const defaultsKey = $derived(
    `${appState.contentKey}:${appState.activeWorkspace?.allowed_actions.join(',') ?? ''}`
  );
  type WebPreferences = {
    show_startup_wizard?: boolean;
    retention_days?: number;
  };
  const sections = [
    'text',
    'stt',
    'subtitles',
    'correction',
    'translation',
    'tts',
    'audio',
    'rvc',
    'source_cleaning',
    'output'
  ];
  const acronyms: Record<string, string> = {
    tts: 'TTS',
    stt: 'STT',
    rvc: 'RVC'
  };
  const sectionLabel = (section: string) =>
    acronyms[section] ??
    section
      .replaceAll('_', ' ')
      .replace(/\b\w/g, (letter) => letter.toUpperCase());
  const gpuDevices = $derived(
    Array.isArray(appState.capabilities?.gpu?.devices)
      ? appState.capabilities.gpu.devices
      : []
  );
  const gpuMemory = (device: GpuDevice) =>
    Number(device.vram_mb) > 0
      ? `${(Number(device.vram_mb) / 1024).toFixed(Number(device.vram_mb) >= 10240 ? 0 : 1)} GiB`
      : 'VRAM unknown';

  async function load() {
    try {
      const result = await settingApi.get<WebPreferences>('web.preferences');
      if (!active) return;
      revision = result.revision;
      wizardVisible = Boolean(result.value?.show_startup_wizard);
      retention = Number(result.value?.retention_days ?? 30);
    } catch {
      if (active) revision = 0;
    }
  }

  async function save() {
    if (!appState.isAdministrator) return;
    saving = true;
    message = '';
    try {
      const result = await settingApi.put<WebPreferences>(
        'web.preferences',
        revision,
        { show_startup_wizard: wizardVisible, retention_days: retention }
      );
      if (!active) return;
      revision = result.revision;
      message = 'Application preferences saved.';
    } catch (caught) {
      if (active) message = errorMessage(caught);
    } finally {
      if (active) saving = false;
    }
  }

  onMount(() => {
    if (appState.isAdministrator) void load();
  });
  onDestroy(() => {
    active = false;
  });
</script>

<div class="mx-auto max-w-5xl">
  <header class="flex items-end justify-between gap-4">
    <div>
      <h1 class="mt-2 text-4xl font-semibold">Application settings</h1>
      <p class="muted mt-3">
        Workspace defaults and installation administration.
      </p>
    </div>
    {#if appState.isAdministrator}<button
        onclick={save}
        disabled={saving}
        class="btn btn-primary"
        ><Save size={16} />{saving
          ? 'Saving…'
          : 'Save installation settings'}</button
      >{/if}
  </header>
  {#if message}<p class="mt-4 rounded-xl bg-[var(--accent-soft)] p-3 text-sm">
      {message}
    </p>{/if}
  <section
    class="surface mt-6 rounded-2xl p-6"
    aria-labelledby="workspace-defaults-title"
  >
    <h2 id="workspace-defaults-title" class="mt-2 text-xl font-semibold">
      Workspace defaults
    </h2>
    <p class="muted mt-2 text-sm">
      Defaults for {workspaceName}. Project overrides take precedence. Provider
      and endpoint connections live under Providers &amp; services.
    </p>
    {#if appState.collaborationEnabled && !appState.workspacesReady}<p
        class="muted mt-4"
        role="status"
      >
        Loading workspace…
      </p>
    {:else}{#key defaultsKey}<div class="mt-5 space-y-2">
          {#each sections as section}<details
              open={openSections.includes(section)}
              class="rounded-xl border border-[var(--line)] p-4"
              ontoggle={(event) => {
                openSections = event.currentTarget.open
                  ? [...new Set([...openSections, section])]
                  : openSections.filter((item) => item !== section);
              }}
            >
              <summary class="cursor-pointer font-semibold"
                >{sectionLabel(section)}</summary
              >
              {#if openSections.includes(section)}<GlobalSettingsPanel
                  {section}
                  {workspaceId}
                  {workspaceName}
                />{/if}
            </details>{/each}
        </div>{/key}{/if}
  </section>
  {#if appState.isAdministrator}
    <details
      class="mt-6 rounded-2xl border border-[var(--line)] p-4"
      ontoggle={(event) => {
        sharingOpen = event.currentTarget.open;
        if (sharingOpen && !SharingSettingsComponent)
          void loadSharingSettings();
      }}
    >
      <summary class="cursor-pointer font-semibold"
        >Web hosting and public sharing (optional)</summary
      >
      {#if sharingLoadError}<p role="alert" class="mt-3">
          {sharingLoadError}
        </p>{/if}
      {#if sharingOpen && SharingSettingsComponent}<SharingSettingsComponent
        />{/if}
    </details>
    <div id="session-trash" class="mt-6 scroll-mt-6">
      <SessionTrashPolicy />
    </div>
    <div class="mt-8 grid gap-5 md:grid-cols-2">
      <section class="surface rounded-2xl p-6">
        <h2 class="mt-2 text-xl font-semibold">Setup and tours</h2>
        <label class="mt-5 flex items-start gap-3"
          ><input
            type="checkbox"
            bind:checked={wizardVisible}
            class="mt-1 accent-[var(--accent)]"
          /><span
            ><strong class="block text-sm">Show guided creation on Home</strong
            ><small class="muted"
              >The launcher can always be opened manually from Home.</small
            ></span
          ></label
        >
      </section>
      <section class="surface rounded-2xl p-6">
        <h2 class="mt-2 text-xl font-semibold">Capability snapshot</h2>
        <div class="muted mt-4 space-y-2 text-sm">
          <div>
            FFmpeg: {appState.capabilities?.ffmpeg?.available
              ? 'Ready'
              : 'Missing'}
          </div>
          <div>
            CrispASR: {appState.capabilities?.stt?.crispasr
              ? 'Ready'
              : 'Missing'}
          </div>
          <div>
            GPU: {appState.capabilities?.gpu?.available
              ? `${gpuDevices.length} detected`
              : 'Not detected'}
          </div>
          {#each gpuDevices as device}<div
              class="rounded-lg border border-[var(--line)] px-3 py-2"
            >
              <strong class="text-[var(--ink)]">{device.name}</strong><span
                class="mt-0.5 block text-xs"
                >{device.vendor} · {gpuMemory(device)}{device.apis?.length
                  ? ` · ${device.apis.join(', ')}`
                  : ''}</span
              >
            </div>{/each}{#if appState.capabilities?.gpu?.guidance}<p
              class="text-xs leading-relaxed"
            >
              {appState.capabilities.gpu.guidance}
            </p>{/if}
        </div>
        <button
          onclick={() => appState.refreshCapabilities()}
          class="btn btn-secondary mt-5"
          ><RefreshCw size={15} /> Probe again</button
        >
      </section>
      <section class="surface rounded-2xl p-6">
        <h2 class="mt-2 text-xl font-semibold">Temporary data</h2>
        <label class="mt-5 block text-sm font-semibold"
          >Keep compactable logs and temporary history for<input
            type="number"
            min="1"
            max="3650"
            bind:value={retention}
            class="mx-2 w-20 rounded-lg border border-[var(--line)] bg-[var(--paper)] px-2 py-1"
          />days</label
        >
        <p class="muted mt-3 text-xs">
          User-created sources and artifacts are never deleted by retention
          policy.
        </p>
      </section>
      <section class="surface rounded-2xl p-6">
        <div class="flex items-center gap-3">
          <ShieldCheck class="text-[var(--accent)]" />
          <div>
            <h2 class="mt-1 text-xl font-semibold">Accounts and security</h2>
          </div>
        </div>
        <p class="muted mt-4 text-sm leading-relaxed">
          Credentials are resolved from secret references, environment
          variables, or the OS keyring. Run snapshots never store plaintext
          secrets.
        </p>
        {#if appState.isAdministrator}
          {#if appState.collaborationAvailable}<a
              class="btn btn-secondary mt-5 mr-2"
              href="/settings/collaboration"
              >{appState.collaborationEnabled
                ? 'Collaboration settings'
                : 'Enable collaboration'}</a
            >{/if}
          <a class="btn btn-secondary mt-5" href="/settings/users"
            >Manage users</a
          >
          <p class="muted mt-3 text-xs">
            Create accounts, manage Administrator access, and issue password
            setup links.
          </p>
        {/if}
        {#if appState.collaborationEnabled}<a
            class="btn btn-secondary mt-4"
            href="/settings/workspaces">Workspaces & invitations</a
          >{/if}
      </section>
    </div>
    <McpLocalPathsPanel />
  {:else}<p class="muted mt-6 text-sm">
      An Administrator manages installation settings and user accounts.
    </p>{/if}
</div>
