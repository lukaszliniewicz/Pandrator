<script lang="ts">
  import ParameterLabel from './ParameterLabel.svelte';
  import {
    SOURCE_PASSAGE_CONTROLS,
    SOURCE_PASSAGE_SECTION
  } from './source-passages';
  import type { SourcePassageSettingsState } from './source-passage-settings.svelte';
  let { state }: { state: SourcePassageSettingsState } = $props();
</script>

<fieldset class="rounded-xl border border-[var(--line)] p-4">
  <legend class="px-1 text-sm font-semibold"> Source logical passages </legend>
  <p class="muted text-xs leading-relaxed">
    Control how source cues become logical passages for dubbing. Saving changes
    these settings only. Pinned passages, accepted corrections and translations,
    speech plans, and audio takes stay unchanged until you explicitly rebuild as
    a new branch.
  </p>
  {#if !state.available}<p
      role="status"
      class="mt-2 rounded-lg bg-amber-500/10 p-2 text-xs text-amber-700"
    >
      The source-passage settings section is not available on this backend yet.
      Built-in defaults are shown; saving them will be retried once the backend
      update lands.
    </p>{/if}
  <div class="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
    {#each SOURCE_PASSAGE_CONTROLS as control}
      <div class="text-xs font-semibold">
        <ParameterLabel
          section={SOURCE_PASSAGE_SECTION}
          name={control.key}
          label={control.label}
          controlId={`sp-${control.key}`}
          compact
        /><input
          id={`sp-${control.key}`}
          type="number"
          disabled={state.rebuildLoading}
          min={control.min}
          max={control.max}
          step={control.step}
          value={state.values[control.key]}
          oninput={(event) =>
            state.setValue(control.key, event.currentTarget.value)}
          aria-invalid={Boolean(state.errors[control.key])}
          aria-describedby={`sp-help-${control.key}`}
          class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
        /><span
          id={`sp-help-${control.key}`}
          class="muted mt-1 block font-normal">{control.help}</span
        >
        {#if state.errors[control.key]}<span
            role="alert"
            class="mt-1 block font-normal text-red-600"
            >{state.errors[control.key]}</span
          >{/if}
      </div>
    {/each}
  </div>
  <details class="mt-3 rounded-xl border border-[var(--line)] p-4">
    <summary class="cursor-pointer text-sm font-semibold">
      Preview &amp; rebuild (optional)
    </summary>
    <p class="muted mt-2 text-xs leading-relaxed">
      Preview builds a bounded, read-only passage count from these values
      without writing anything. Rebuild creates a new source branch guarded by
      the live source revision, content hash, and the fresh preview’s settings
      hash. The original source, downstream work, and your current selection are
      preserved; the new branch is not auto-selected.
    </p>
    {#if !state.artifactId}<p role="status" class="muted mt-2 text-xs">
        Select or produce a transcribed source first; preview and rebuild act on
        the current transcription artifact.
      </p>{:else}
      <div class="mt-3 flex flex-wrap items-center gap-2">
        <button
          type="button"
          onclick={() => state.loadStatus()}
          disabled={state.statusLoading}
          class="rounded-lg border border-[var(--line)] px-3 py-2 text-xs font-semibold disabled:opacity-40"
          >{state.statusLoading ? 'Loading status…' : 'Reload status'}</button
        >
        <button
          type="button"
          onclick={() => state.runPreview()}
          disabled={state.previewLoading ||
            state.rebuildLoading ||
            !state.valid}
          class="rounded-lg border border-[var(--line)] px-3 py-2 text-xs font-semibold disabled:opacity-40"
          >{state.previewLoading ? 'Previewing…' : 'Preview passages'}</button
        >
        <button
          type="button"
          onclick={() => state.runRebuild()}
          disabled={state.rebuildLoading || !state.preview || !state.status}
          title={state.preview
            ? 'Rebuild as a new branch'
            : 'Run a fresh preview first'}
          class="rounded-lg bg-[var(--accent)] px-3 py-2 text-xs font-semibold text-white disabled:opacity-40"
          >{state.rebuildLoading
            ? 'Rebuilding…'
            : 'Rebuild as new branch'}</button
        >
      </div>
      {#if state.statusError}<p role="alert" class="mt-2 text-xs text-red-600">
          {state.statusError}
        </p>{/if}
      {#if state.status}<p role="status" class="muted mt-2 text-xs">
          {state.status.pinned
            ? `Pinned passages: ${state.status.passage_count ?? 'unknown'} · policy ${state.status.policy_version ?? 'unknown'}`
            : 'No pinned passages for this source yet.'}
        </p>{/if}
      {#if state.previewError}<p role="alert" class="mt-2 text-xs text-red-600">
          {state.previewError}
        </p>{/if}
      {#if state.preview}<div
          role="status"
          class="mt-2 rounded-lg bg-[var(--accent-soft)] p-2 text-xs"
        >
          <p>
            Preview: {state.preview.passage_count} passages {#if state.status?.passage_count != null}
              {@const delta =
                state.preview.passage_count - (state.status.passage_count ?? 0)}
              · pinned: {state.status.passage_count} · difference
              {delta >= 0 ? `+${delta}` : delta}{/if}
          </p>
          {#if state.preview.warnings.length}<ul class="mt-1 list-disc pl-4">
              {#each state.preview.warnings as warning}<li>
                  {warning}
                </li>{/each}
            </ul>{/if}
        </div>{/if}
      {#if state.rebuildError}<p role="alert" class="mt-2 text-xs text-red-600">
          {state.rebuildError}
        </p>{/if}
      {#if state.rebuildResult}<div
          role="status"
          class="mt-2 rounded-lg bg-[var(--accent-soft)] p-2 text-xs"
        >
          <p>
            New branch: {state.rebuildResult.passage_count} passages · {state
              .rebuildResult.branch_artifact_id}
          </p>
          <p class="muted mt-1">
            The original source and downstream work are unchanged, and your
            current selection is unchanged. Find the new branch in the existing
            source picker.
          </p>
        </div>{/if}
    {/if}
  </details>
</fieldset>
