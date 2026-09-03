<script lang="ts">
  import { FolderPlus, Save, Trash2 } from '@lucide/svelte';
  import { managerApi } from './admin-api';
  import { errorMessage } from './errors';

  type SourceRoot = { name: string; path: string };
  type LocalPaths = { source_roots: SourceRoot[]; output_root: string | null };

  let roots = $state<SourceRoot[]>([]);
  let outputRoot = $state('');
  let loading = $state(true);
  let saving = $state(false);
  let message = $state('');
  let failed = $state(false);

  async function load() {
    loading = true;
    failed = false;
    try {
      const payload = await managerApi.mcpPaths<LocalPaths>();
      roots = (payload.source_roots ?? []).map((item) => ({ ...item }));
      outputRoot = payload.output_root ?? '';
    } catch (caught) {
      failed = true;
      message = errorMessage(caught);
    } finally {
      loading = false;
    }
  }

  function addRoot() {
    roots = [...roots, { name: '', path: '' }];
  }

  function removeRoot(index: number) {
    roots = roots.filter((_item, candidate) => candidate !== index);
  }

  async function save() {
    saving = true;
    message = '';
    try {
      const payload = await managerApi.saveMcpPaths<LocalPaths>({
        source_roots: roots.map((item) => ({
          name: item.name.trim(),
          path: item.path.trim()
        })),
        output_root: outputRoot.trim() || null
      });
      roots = (payload.source_roots ?? []).map((item) => ({ ...item }));
      outputRoot = payload.output_root ?? '';
      message =
        'Local MCP paths saved. Running MCP tools will pick them up automatically.';
      failed = false;
    } catch (caught) {
      failed = true;
      message = errorMessage(caught);
    } finally {
      saving = false;
    }
  }

  load();
</script>

<section class="surface mt-6 rounded-2xl p-6">
  <div class="flex flex-wrap items-start justify-between gap-4">
    <div>
      <div class="eyebrow">Automation</div>
      <h2 class="mt-2 text-xl font-semibold">Local MCP filesystem access</h2>
      <p class="muted mt-2 max-w-3xl text-sm leading-relaxed">
        These directories are the only local files an MCP client may browse and
        import. New managed installations expose the current user's home
        directory as <code>home</code>
        by default. Paths must be absolute. The output directory is where requested
        artifacts can be materialized.
      </p>
    </div>
    <button class="btn btn-primary" onclick={save} disabled={loading || saving}>
      <Save size={15} />{saving ? 'Saving…' : 'Save MCP paths'}
    </button>
  </div>

  {#if message}
    <p
      class="mt-4 rounded-xl p-3 text-sm"
      class:bg-[var(--danger-soft)]={failed}
      class:bg-[var(--accent-soft)]={!failed}
    >
      {message}
    </p>
  {/if}

  {#if loading}
    <p class="muted mt-5 text-sm">Loading approved paths…</p>
  {:else}
    <div class="mt-5 space-y-3">
      <div class="flex items-center justify-between gap-3">
        <h3 class="text-sm font-semibold">Source roots</h3>
        <button class="btn btn-secondary" onclick={addRoot}
          ><FolderPlus size={15} /> Add root</button
        >
      </div>
      {#if roots.length === 0}
        <p
          class="muted rounded-xl border border-dashed border-[var(--line)] p-4 text-sm"
        >
          No local source directory is currently exposed to MCP clients.
        </p>
      {/if}
      {#each roots as root, index}
        <div
          class="grid gap-2 rounded-xl border border-[var(--line)] p-3 sm:grid-cols-[10rem_1fr_auto]"
        >
          <label class="text-xs font-semibold">
            Name
            <input
              class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-sm"
              bind:value={root.name}
              placeholder="home"
            />
          </label>
          <label class="text-xs font-semibold">
            Absolute path
            <input
              class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-sm"
              bind:value={root.path}
              placeholder="/home/me or C:\\Users\\me"
            />
          </label>
          <button
            class="btn btn-secondary self-end"
            aria-label={`Remove ${root.name || 'source root'}`}
            onclick={() => removeRoot(index)}><Trash2 size={15} /></button
          >
        </div>
      {/each}
    </div>

    <label class="mt-6 block text-sm font-semibold">
      Artifact output directory
      <input
        class="mt-2 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-sm"
        bind:value={outputRoot}
        placeholder="/path/to/Pandrator/exports or C:\\Users\\me\\Pandrator\\exports"
      />
      <small class="muted mt-2 block font-normal">
        Leave empty to disable local artifact downloads. Changing these paths
        does not expose their absolute values to the language model: MCP
        browsing uses the configured root names and relative paths.
      </small>
    </label>
  {/if}
</section>
