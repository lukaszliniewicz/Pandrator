<script lang="ts">
  import {
    BookOpen,
    Check,
    LoaderCircle,
    Settings2,
    Users
  } from '@lucide/svelte';
  import { onMount } from 'svelte';
  import { apiJson } from './api';
  import { errorMessage } from './errors';
  import { invalidationBus, invalidates } from './invalidation';
  import GenerationCastPanel from './GenerationCastPanel.svelte';
  import type { CastDraftController } from './generation-controls';
  import type { SpeechPlanState } from './session-flow';
  import { notifySessionFlowChange } from './session-flow';

  type Setup = {
    mode: 'single_voice' | 'multi_voice';
    configuration_revision: string;
    configured: boolean;
    annotation_mode: string;
    tts: { service: string; model: string; voice: string };
    segmentation?: { mode: string; target_chars: number; reason?: string };
  };
  let {
    sessionId,
    plan,
    busy = false,
    navigationManaged = false,
    castPanel = $bindable<CastDraftController | undefined>(),
    onchanged,
    onsettings
  }: {
    sessionId: string;
    plan: SpeechPlanState | null;
    busy?: boolean;
    navigationManaged?: boolean;
    castPanel?: CastDraftController;
    onchanged: () => void | Promise<void>;
    onsettings: () => void;
  } = $props();
  let setup = $state<Setup | null>(null);
  let pending = $state(false);
  let error = $state('');
  let helpOpen = $state(false);
  const draft = $derived(castPanel?.draftState());
  const selected = $derived(
    plan?.items.find((item) => item.id === plan.selected_revision_id)
  );
  const path = $derived(
    `/sessions/${encodeURIComponent(sessionId)}/audiobook-setup`
  );
  let serial = 0;
  async function load() {
    const request = ++serial;
    try {
      const value = await apiJson<Setup>(path);
      if (request === serial) {
        setup = value;
        error = '';
      }
    } catch (caught) {
      if (request === serial) error = errorMessage(caught);
    }
  }
  async function setMode(mode: Setup['mode']) {
    if (!setup || pending || draft?.dirty) return;
    pending = true;
    error = '';
    try {
      setup = await apiJson<Setup>(path, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          mode,
          expected_revision: setup.configuration_revision
        })
      });
      notifySessionFlowChange(sessionId);
      await onchanged();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      pending = false;
    }
  }
  onMount(() => {
    void load();
    const disconnect = invalidationBus.subscribe((batch) => {
      if (
        !pending &&
        (invalidates(batch, 'sessions', sessionId) ||
          invalidates(batch, 'workflow', sessionId))
      )
        void load();
    });
    return () => {
      serial++;
      disconnect();
    };
  });
</script>

<section
  class="surface rounded-2xl border border-[var(--line)] p-4 sm:rounded-3xl sm:p-6"
  aria-label="Audiobook voices"
  id="audiobook-casting"
>
  <header class="flex items-start gap-3 sm:gap-4">
    <span
      class="grid size-11 shrink-0 place-items-center rounded-2xl bg-[var(--accent-soft)] text-[var(--accent)]"
      ><Users size={21} /></span
    >
    <div class="min-w-0 flex-1">
      <h2 class="text-lg font-semibold">Audiobook voices</h2>
      <p class="muted mt-1 text-sm">
        Choose a narrator, or give the characters their own voices.
      </p>
    </div>
    {#if pending}<LoaderCircle
        class="animate-spin shrink-0"
        size={19}
        aria-label="Saving voice mode"
      />{/if}
  </header>
  {#if error}<p class="mt-4 text-sm text-red-700" role="alert">
      {error} <button class="underline" onclick={load}>Refresh setup</button>
    </p>{/if}
  {#if setup}
    <fieldset
      disabled={busy || pending || Boolean(draft?.dirty || draft?.blocked)}
      class="mt-5 grid gap-3 sm:grid-cols-2"
    >
      <legend class="sr-only">Audiobook voice mode</legend>
      {#each [{ value: 'single_voice' as const, title: 'One narrator', detail: 'Read the book with the session voice.' }, { value: 'multi_voice' as const, title: 'Multiple voices', detail: 'Identify dialogue, then cast narrator and characters.' }] as choice}
        <label class="mode-option" class:selected={setup.mode === choice.value}>
          <input
            type="radio"
            name={`audiobook-mode-${sessionId}`}
            checked={setup.mode === choice.value}
            onchange={() => setMode(choice.value)}
          />
          {#if choice.value === 'single_voice'}<BookOpen
              size={19}
            />{:else}<Users size={19} />{/if}
          <span
            ><strong>{choice.title}</strong><span
              class="muted block text-xs leading-relaxed mt-1"
              >{choice.detail}</span
            ></span
          >
        </label>
      {/each}
    </fieldset>
    {#if draft?.dirty}<p class="muted mt-2 text-xs">
        Save or discard the cast changes before switching voice mode.
      </p>{/if}
    {#if setup.mode === 'multi_voice'}
      <details
        data-testid="cast-help"
        bind:open={helpOpen}
        class="muted mt-5 rounded-xl border border-[var(--line)] p-3 text-sm"
      >
        <summary
          data-testid="cast-help-summary"
          class="cursor-pointer font-semibold text-[var(--ink)]"
          >How multiple voices work</summary
        >
        <ol class="steps mt-3" aria-label="Multiple-voice audiobook steps">
          <li>
            <span>1</span>
            <div>
              <strong>Prepare narration</strong>
              <p>Group sentences for the selected model.</p>
            </div>
          </li>
          <li>
            <span>2</span>
            <div>
              <strong>Identify speakers</strong>
              <p>Keep reporting clauses in the narrator’s voice.</p>
            </div>
          </li>
          <li>
            <span>3</span>
            <div>
              <strong>Cast &amp; review</strong>
              <p>Assign voices, check the plan, then generate.</p>
            </div>
          </li>
        </ol>
        <p class="mt-3 text-xs leading-relaxed">
          The spoken words stay unchanged. Extra delivery notes are optional and
          depend on the chosen model.
        </p>
      </details>
      {#if !setup.configured}<div
          class="mt-4 rounded-xl border border-amber-500/30 bg-amber-500/10 p-3 text-sm"
        >
          <p>Speaker annotation is not fully enabled for this workflow.</p>
          <button
            class="btn mt-2"
            disabled={busy || pending || Boolean(draft?.dirty)}
            onclick={() => setMode('multi_voice')}
            >Enable speaker preparation</button
          >
        </div>{/if}
      <p class="muted my-4 text-sm">
        Cast the narrator and characters below, then generate.
      </p>
      <GenerationCastPanel
        bind:this={castPanel}
        {sessionId}
        service={setup.tts.service}
        model={setup.tts.model}
        sessionVoice={setup.tts.voice}
        standalone={true}
        {navigationManaged}
        busy={busy || pending}
        onchanged={() => void onchanged()}
      />
      {#if selected}<p class="muted mt-4 flex items-center gap-2 text-xs">
          {#if selected.reviewed}<Check
              size={15}
            />{/if}{selected.segment_count} speech blocks · {selected.reviewed
            ? 'Plan reviewed'
            : 'Plan needs review'}. Inspect voices and delivery in the
          generation drawer.
        </p>{/if}
    {:else}<p class="muted mt-4 text-sm">
        {setup.tts.voice
          ? `Narrator: ${setup.tts.voice}`
          : 'Choose a narrator in Voice & audio.'}
        <a
          class="underline underline-offset-2"
          href={`/sessions/${encodeURIComponent(sessionId)}/voice`}
          >Choose voice</a
        >
      </p>{/if}
    <div
      class="mt-5 flex flex-wrap items-center justify-between gap-3 border-t border-[var(--line)] pt-4"
    >
      <p class="muted min-w-0 flex-1 text-xs leading-relaxed">
        {#if setup.segmentation}Narration groups complete sentences, up to
          <strong
            >{setup.segmentation.target_chars.toLocaleString()} characters</strong
          >.{:else}Narration groups complete sentences within the model’s
          budget.{/if} Paragraphs and chapters keep natural breaks. Reviewed plans
        stay unchanged.
      </p>
      <button
        class="btn btn-secondary"
        disabled={busy || pending}
        onclick={onsettings}><Settings2 size={15} /> Narration settings</button
      >
    </div>
  {:else if !error}<p class="muted mt-4 text-sm" role="status">
      Loading audiobook setup…
    </p>{/if}
</section>

<style>
  .mode-option {
    display: flex;
    gap: 0.7rem;
    align-items: start;
    border: 1px solid var(--line);
    border-radius: 0.8rem;
    padding: 1rem;
    cursor: pointer;
  }
  .mode-option input {
    margin-top: 0.2rem;
    accent-color: var(--accent);
  }
  .mode-option.selected {
    border-color: var(--accent);
    background: var(--accent-soft);
  }
  .mode-option strong {
    font-size: 0.85rem;
  }
  .steps {
    display: grid;
    gap: 0.9rem;
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
  .steps li {
    display: flex;
    gap: 0.6rem;
    font-size: 0.8rem;
  }
  .steps li > span {
    display: grid;
    place-items: center;
    width: 1.6rem;
    height: 1.6rem;
    flex-shrink: 0;
    border: 1px solid var(--line);
    border-radius: 50%;
    color: var(--accent);
    font-size: 0.7rem;
    font-weight: 700;
  }
  .steps p {
    color: var(--muted);
    font-size: 0.72rem;
    line-height: 1.5;
    margin-top: 0.25rem;
  }
  @media (max-width: 620px) {
    .steps {
      grid-template-columns: 1fr;
    }
  }
</style>
