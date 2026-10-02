<script lang="ts">
  import { onMount } from 'svelte';
  import type {
    ProjectOperation,
    ProjectExportManifest,
    ProjectExportBundle,
    TranslationProject
  } from './api-models';
  import { translationProjectApi, sessionApi } from './domain-api';
  import { ApiError } from './api';
  import { errorMessage } from './errors';
  import { translationLanguageName } from './translation-project-display';

  let {
    project,
    selected,
    onchanged
  }: {
    project: TranslationProject;
    selected: string[];
    onchanged: () => void;
  } = $props();
  let operation = $state<ProjectOperation | null>(null);
  let exportKind = $state<ProjectOperation['export_kind']>('subtitles');
  let accepted = $state<string[]>([]);
  let busy = $state(false);
  let error = $state('');
  let manifest = $state<ProjectExportManifest | null>(null);
  let bundle = $state<ProjectExportBundle | null>(null);
  let bundleKey = '';
  let bundleTimer: ReturnType<typeof setTimeout> | undefined;
  let manifestBusy = $state(false);
  let actionKey = '';
  let timer: ReturnType<typeof setTimeout> | undefined;
  let alive = false;
  const confirmations = $derived([
    ...new Set(
      operation?.children
        .filter((child) => child.eligible)
        .flatMap((child) => child.preview?.required_confirmations ?? []) ?? []
    )
  ]);
  const canExecute = $derived(
    operation?.status === 'preview' &&
      !operation.expired &&
      operation.eligible_count > 0 &&
      confirmations.every((item) => accepted.includes(item))
  );
  const storageKey = $derived(`pandrator:project-operation:${project.id}`);
  const active = $derived(
    operation && ['running', 'canceling'].includes(operation.status)
  );

  function remember(value: ProjectOperation) {
    if (operation?.id !== value.id) {
      manifest = null;
      bundle = null;
      bundleKey = '';
      clearTimeout(bundleTimer);
    }
    operation = value;
    sessionStorage.setItem(storageKey, value.id);
    schedulePoll();
  }
  async function collect() {
    if (!operation || manifestBusy) return;
    const id = operation.id;
    manifestBusy = true;
    error = '';
    try {
      const result = await translationProjectApi.exportManifest(id);
      if (alive && operation?.id === id) {
        if (manifest?.manifest_digest !== result.manifest_digest) {
          bundle = null;
          bundleKey = '';
          clearTimeout(bundleTimer);
        }
        manifest = result;
      }
    } catch (caught) {
      if (alive) error = errorMessage(caught);
    } finally {
      manifestBusy = false;
    }
  }
  async function makeBundle() {
    if (!operation || !manifest?.manifest.complete || manifestBusy) return;
    const id = operation.id;
    const digest = manifest.manifest_digest;
    manifestBusy = true;
    error = '';
    try {
      const result = await translationProjectApi.exportBundle(
        id,
        digest,
        bundleKey || (bundleKey = crypto.randomUUID())
      );
      if (
        alive &&
        operation?.id === id &&
        manifest?.manifest_digest === digest
      ) {
        bundle = result;
        if (
          ['queued', 'running', 'retrying', 'cancel_requested'].includes(
            result.status
          )
        ) {
          clearTimeout(bundleTimer);
          bundleTimer = setTimeout(() => void makeBundle(), 2500);
        } else if (['failed', 'canceled'].includes(result.status)) {
          bundleKey = '';
          error = `ZIP preparation ${result.status}. Refresh the manifest and retry.`;
        }
      }
    } catch (caught) {
      if (alive) {
        if (
          caught instanceof ApiError &&
          [
            'bundle_job_terminal',
            'bundle_cache_invalid',
            'bundle_job_unavailable'
          ].includes(caught.code)
        )
          bundleKey = '';
        error = errorMessage(caught);
      }
    } finally {
      manifestBusy = false;
    }
  }
  async function cancelBundle() {
    if (!bundle || manifestBusy) return;
    manifestBusy = true;
    try {
      await sessionApi.cancelJob(bundle.job_id);
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      manifestBusy = false;
    }
  }
  function downloadManifest() {
    if (!manifest) return;
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(manifest.manifest, null, 2) + '\n'], {
        type: 'application/json'
      })
    );
    const link = document.createElement('a');
    link.href = url;
    link.download = `project-${project.id}-r${manifest.manifest.project_revision}-manifest.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function schedulePoll() {
    clearTimeout(timer);
    if (
      alive &&
      operation &&
      ['running', 'canceling'].includes(operation.status)
    )
      timer = setTimeout(() => void refresh(), 2500);
  }
  async function refresh() {
    if (!operation) return;
    const id = operation.id;
    try {
      const result = await translationProjectApi.operation(id);
      if (alive && operation?.id === id) {
        const finished =
          ['running', 'canceling'].includes(operation.status) &&
          !['running', 'canceling'].includes(result.status);
        remember(result);
        if (finished) onchanged();
      }
    } catch (caught) {
      if (alive) {
        error = errorMessage(caught);
        schedulePoll();
      }
    }
  }
  async function preview(action: ProjectOperation['action']) {
    if (busy || !selected.length) return;
    busy = true;
    error = '';
    accepted = [];
    try {
      remember(
        await translationProjectApi.previewOperation(
          project.id,
          {
            selected_branch_ids: selected,
            expected_project_revision: project.revision,
            action,
            export_kind: exportKind
          },
          crypto.randomUUID()
        )
      );
      actionKey = crypto.randomUUID();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
  async function execute() {
    if (!operation || !canExecute || busy) return;
    busy = true;
    error = '';
    try {
      remember(
        await translationProjectApi.executeOperation(
          operation,
          accepted,
          actionKey || (actionKey = crypto.randomUUID())
        )
      );
      onchanged();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
  async function cancel() {
    if (!operation || busy) return;
    busy = true;
    error = '';
    try {
      remember(
        await translationProjectApi.cancelOperation(
          operation.id,
          crypto.randomUUID()
        )
      );
      onchanged();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
  async function retry() {
    if (!operation || busy) return;
    busy = true;
    error = '';
    accepted = [];
    try {
      remember(
        await translationProjectApi.retryOperation(
          operation.id,
          project.revision,
          crypto.randomUUID()
        )
      );
      actionKey = crypto.randomUUID();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      busy = false;
    }
  }
  function confirmationLabel(value: string) {
    return (
      (
        {
          external_provider:
            'Allow the listed external providers to receive these inputs',
          estimated_cost_unknown:
            'Accept that the provider cost estimate is unavailable',
          destructive_rerun: 'Accept the listed replacement of current results'
        } as Record<string, string>
      )[value] ?? value.replaceAll('_', ' ')
    );
  }
  onMount(() => {
    alive = true;
    const id = sessionStorage.getItem(storageKey);
    if (id)
      void translationProjectApi
        .operation(id)
        .then((value) => {
          if (alive && value.project_id === project.id) remember(value);
        })
        .catch(() => sessionStorage.removeItem(storageKey));
    return () => {
      alive = false;
      clearTimeout(timer);
      clearTimeout(bundleTimer);
    };
  });
</script>

<section class="surface rounded-2xl p-5">
  <h3 class="text-lg font-semibold">Selected languages</h3>
  <p class="muted mt-2 text-sm">
    {selected.length} selected. Preview each action before submitting it. Translation
    remains available for review; generation requires a reviewed, current speech plan.
  </p>
  <div class="mt-4 flex flex-wrap items-center gap-3">
    <button
      type="button"
      class="rounded-lg border border-[var(--line)] px-3 py-2 text-sm font-semibold disabled:opacity-50"
      disabled={busy || !selected.length}
      onclick={() => preview('translate')}>Translate selected</button
    >
    <button
      type="button"
      class="rounded-lg border border-[var(--line)] px-3 py-2 text-sm font-semibold disabled:opacity-50"
      disabled={busy || !selected.length}
      onclick={() => preview('generate')}>Generate selected</button
    >
    <label class="text-sm"
      >Export <select
        class="rounded-lg border border-[var(--line)] bg-[var(--paper)] p-2"
        bind:value={exportKind}
        disabled={busy}
        ><option value="subtitles">Translated SRT subtitles</option><option
          value="configured">Each language's output settings</option
        ></select
      ></label
    >
    <button
      type="button"
      class="rounded-lg border border-[var(--line)] px-3 py-2 text-sm font-semibold disabled:opacity-50"
      disabled={busy || !selected.length}
      onclick={() => preview('export')}>Export selected</button
    >
  </div>
  {#if error}<p class="mt-3 text-sm text-red-500" role="alert">{error}</p>{/if}
  {#if operation}
    <div class="mt-5 border-t border-[var(--line)] pt-4">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <h4 class="font-semibold capitalize">
          {operation.action} · {operation.status}
        </h4>
        <button
          type="button"
          class="text-sm font-semibold text-[var(--accent)]"
          disabled={busy}
          onclick={refresh}>Refresh progress</button
        >
      </div>
      {#if operation.status === 'preview'}<p class="muted mt-2 text-sm">
          {operation.child_job_count} eligible jobs. Preview expires {new Date(
            operation.expires_at
          ).toLocaleTimeString()}. {operation.expired
            ? 'Create a fresh preview to continue.'
            : ''}
        </p>{/if}
      <ul class="mt-3 space-y-3">
        {#each operation.children as child (child.branch_id)}
          <li class="rounded-xl border border-[var(--line)] p-3 text-sm">
            <div class="flex flex-wrap justify-between gap-2">
              {#if child.session_id && !child.session_deleted}<a
                  class="font-semibold text-[var(--accent)]"
                  href={`/sessions/${child.session_id}`}
                  >{translationLanguageName(child.target_language)}</a
                >{:else}<span class="font-semibold"
                  >{translationLanguageName(child.target_language)}</span
                >{/if}<span>{child.state.replaceAll('_', ' ')}</span>
            </div>
            {#if child.reason || child.error?.message}<p
                class="mt-1 text-red-500"
              >
                {child.reason || child.error?.message}
              </p>{/if}
            {#if child.manual_resume_required}<p class="muted mt-1">
                Continue the existing translation work in this language's
                workspace. Completed batches are preserved.
              </p>{/if}
            {#if typeof child.progress === 'number' && ['running', 'queued', 'canceling'].includes(child.state)}<progress
                class="mt-2 w-full"
                aria-label={`${translationLanguageName(child.target_language)} progress`}
                max="1"
                value={child.progress}
              ></progress>{/if}
            {#if child.preview}<details class="mt-2">
                <summary class="cursor-pointer font-semibold"
                  >Captured inputs, settings and provider requirements</summary
                >
                <pre
                  class="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-[var(--paper)] p-3 text-xs">{JSON.stringify(
                    child.preview,
                    null,
                    2
                  )}</pre>
              </details>{/if}
            {#if child.result?.download_url}<a
                class="mt-2 inline-block font-semibold text-[var(--accent)]"
                href={child.result.download_url}
                download>Download completed output</a
              >{:else if child.retained?.result?.download_url}<a
                class="mt-2 inline-block font-semibold text-[var(--accent)]"
                href={child.retained.result.download_url}
                download>Download preserved output</a
              >{/if}
          </li>
        {/each}
      </ul>
      {#if operation.status === 'preview'}
        {#each confirmations as confirmation}<label
            class="mt-3 flex items-start gap-2 text-sm"
            ><input
              type="checkbox"
              bind:group={accepted}
              value={confirmation}
              disabled={busy}
            /><span>{confirmationLabel(confirmation)}</span></label
          >{/each}
        <button
          type="button"
          class="mt-4 rounded-xl bg-[var(--accent)] px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
          disabled={busy || !canExecute}
          onclick={execute}
          >Submit {operation.eligible_count} eligible {operation.action} jobs</button
        >
      {:else if active}<button
          type="button"
          class="mt-4 rounded-lg border border-[var(--line)] px-3 py-2 text-sm font-semibold"
          disabled={busy || operation.status === 'canceling'}
          onclick={cancel}>Cancel unfinished work</button
        >
        <p class="muted mt-2 text-xs">
          Completed results are retained. Accepted agent translation batches may
          need to be stopped in their existing workflow.
        </p>
      {:else if ['partial', 'failed', 'canceled'].includes(operation.status)}<button
          type="button"
          class="mt-4 rounded-lg border border-[var(--line)] px-3 py-2 text-sm font-semibold"
          disabled={busy}
          onclick={retry}>Preview retry of unfinished languages</button
        >{/if}
      {#if operation.action === 'export' && operation.status !== 'preview'}
        <div class="mt-5 border-t border-[var(--line)] pt-4">
          <h4 class="font-semibold">Collect outputs</h4>
          <p class="muted mt-1 text-sm">
            Verify completed exports, download their manifest, or collect every
            selected language into one ZIP.
          </p>
          <button
            type="button"
            class="mt-3 rounded-lg border border-[var(--line)] px-3 py-2 text-sm font-semibold"
            disabled={manifestBusy}
            onclick={collect}>Refresh export manifest</button
          >
          {#if manifest}
            <p class="mt-2 text-sm" aria-live="polite">
              {manifest.manifest.complete
                ? 'Every selected export is verified.'
                : 'Some selected exports are incomplete. Complete or retry them before creating a ZIP.'}
            </p>
            <ul class="mt-3 space-y-2 text-sm">
              {#each manifest.manifest.languages as language (language.branch_id)}<li
                >
                  <strong>{translationLanguageName(language.language)}</strong
                  >{#if language.reason}<p class="text-red-500">
                      {language.reason}
                    </p>{/if}
                  {#each language.artifacts as file (file.artifact_id)}<a
                      class="mt-1 block break-all text-[var(--accent)]"
                      href={`/api/v1/artifacts/${file.artifact_id}/content`}
                      download={file.filename}>{file.filename}</a
                    >{/each}
                </li>{/each}
            </ul>
            <div class="mt-3 flex flex-wrap gap-3">
              <button
                type="button"
                class="rounded-lg border border-[var(--line)] px-3 py-2 text-sm font-semibold"
                onclick={downloadManifest}>Download manifest</button
              >
              <button
                type="button"
                class="rounded-lg border border-[var(--line)] px-3 py-2 text-sm font-semibold"
                disabled={manifestBusy ||
                  !manifest.manifest.complete ||
                  (!!bundle &&
                    [
                      'queued',
                      'running',
                      'retrying',
                      'cancel_requested'
                    ].includes(bundle.status))}
                onclick={makeBundle}>Prepare ZIP</button
              >
              {#if bundle?.status === 'ready' && bundle.content_url}<a
                  class="rounded-lg bg-[var(--accent)] px-3 py-2 text-sm font-semibold text-white"
                  href={bundle.content_url}
                  download>Download ZIP</a
                >{:else if bundle && ['queued', 'running', 'retrying', 'cancel_requested'].includes(bundle.status)}<span
                  class="muted self-center text-sm"
                  aria-live="polite"
                  >ZIP {bundle.status.replaceAll('_', ' ')}</span
                ><button
                  type="button"
                  class="text-sm font-semibold text-[var(--accent)]"
                  disabled={manifestBusy ||
                    bundle.status === 'cancel_requested'}
                  onclick={cancelBundle}>Cancel ZIP preparation</button
                >{/if}
            </div>
          {/if}
        </div>
      {/if}
    </div>
  {/if}
</section>
