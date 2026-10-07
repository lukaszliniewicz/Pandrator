<script lang="ts">
  import { goto } from '$app/navigation';
  import { GitFork, LoaderCircle, X } from '@lucide/svelte';
  import type { SessionRecord } from './api-models';
  import { sessionApi } from './domain-api';
  import { errorMessage } from './errors';
  import { modalFocus } from './modal-focus';

  let {
    session,
    stage,
    artifactId,
    onclose
  }: {
    session: SessionRecord;
    stage: 'correction' | 'translation';
    artifactId: string;
    onclose: () => void;
  } = $props();

  const stageLabel = $derived(
    stage === 'translation' ? 'Translation' : 'Correction'
  );
  const defaultName = $derived(`${session.name} — ${stageLabel} fork`);
  let name = $state('');
  let initialized = false;
  let submitting = $state(false);
  let carryMediaAssets = $state(true);
  let requestKey = '';
  let requestSignature = '';
  let error = $state('');

  $effect(() => {
    if (!initialized) {
      name = defaultName;
      initialized = true;
    }
  });

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    if (!name.trim() || submitting) return;
    submitting = true;
    error = '';
    try {
      const body = {
        checkpoint_artifact_id: artifactId,
        name: name.trim(),
        expected_revision: session.revision,
        carry_media_assets: carryMediaAssets
      };
      const signature = JSON.stringify(body);
      if (signature !== requestSignature) {
        requestSignature = signature;
        requestKey = crypto.randomUUID();
      }
      const forked = await sessionApi.forkAtCheckpoint(
        session.id,
        body,
        requestKey
      );
      onclose();
      await goto(`/sessions/${forked.id}`);
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      submitting = false;
    }
  }
</script>

<div
  class="fixed inset-0 z-[80] grid place-items-center bg-black/50 p-4 backdrop-blur-sm"
  role="presentation"
  onclick={(event) => event.target === event.currentTarget && onclose()}
>
  <div
    use:modalFocus={{ onclose, initialFocus: '#session-fork-name' }}
    class="surface w-full max-w-lg rounded-3xl p-6 sm:p-7"
    role="dialog"
    aria-modal="true"
    aria-labelledby="session-fork-title"
    aria-describedby="session-fork-description"
  >
    <form onsubmit={submit}>
      <div class="flex items-start gap-4">
        <span
          class="grid size-11 shrink-0 place-items-center rounded-2xl bg-[var(--accent-soft)] text-[var(--accent)]"
          ><GitFork size={20} /></span
        >
        <div class="min-w-0 flex-1">
          <h2 id="session-fork-title" class="mt-1 text-xl font-semibold">
            Fork after {stageLabel.toLowerCase()}
          </h2>
        </div>
        <button
          type="button"
          onclick={onclose}
          class="rounded-xl p-2"
          aria-label="Close project fork dialog"><X size={19} /></button
        >
      </div>

      <p id="session-fork-description" class="muted mt-4 text-sm leading-6">
        The new project keeps your sources, settings, and selected subtitles
        through this {stageLabel.toLowerCase()}. Its translations, voices and
        exports can then develop independently.
      </p>

      <label class="mt-5 block text-sm font-semibold" for="session-fork-name">
        New project name
      </label>
      <input
        id="session-fork-name"
        bind:value={name}
        maxlength="255"
        required
        class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3"
      />
      <p class="muted mt-2 text-xs">
        If that name already exists, Pandrator adds a number.
      </p>

      <label class="mt-5 flex items-start gap-3 text-sm">
        <input type="checkbox" bind:checked={carryMediaAssets} class="mt-1" />
        <span>
          <span class="block font-semibold">Keep edited video and timeline</span
          >
          <span class="muted mt-1 block"
            >Use the same cuts and base video in the new project.</span
          >
        </span>
      </label>

      {#if error}<p
          role="alert"
          class="mt-4 rounded-xl bg-red-500/10 px-4 py-3 text-sm text-red-500"
        >
          {error}
        </p>{/if}

      <div class="mt-7 flex justify-end gap-3">
        <button
          type="button"
          onclick={onclose}
          class="rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm font-semibold"
          >Cancel</button
        >
        <button
          type="submit"
          disabled={submitting || !name.trim()}
          class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-50"
        >
          {#if submitting}<LoaderCircle
              class="animate-spin"
              size={16}
            />{:else}<GitFork size={16} />{/if}
          {submitting ? 'Creating fork…' : 'Create fork'}
        </button>
      </div>
    </form>
  </div>
</div>
