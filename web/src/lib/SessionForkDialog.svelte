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
      const forked = await sessionApi.forkAtCheckpoint(session.id, {
        checkpoint_artifact_id: artifactId,
        name: name.trim()
      });
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
          <div class="eyebrow">Branch session</div>
          <h2 id="session-fork-title" class="mt-1 text-xl font-semibold">
            Fork after {stageLabel.toLowerCase()}
          </h2>
        </div>
        <button
          type="button"
          onclick={onclose}
          class="rounded-xl p-2"
          aria-label="Close session fork dialog"><X size={19} /></button
        >
      </div>

      <p id="session-fork-description" class="muted mt-4 text-sm leading-6">
        The new session keeps current sources, settings, and the selected text
        path through this {stageLabel.toLowerCase()}. Generation runs, audio
        takes, assemblies, and exports stay in the original session.
      </p>

      <label class="mt-5 block text-sm font-semibold" for="session-fork-name">
        New session name
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
