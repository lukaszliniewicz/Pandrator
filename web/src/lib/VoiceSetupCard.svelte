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
    workflow_kind: 'audiobook' | 'voiceover';
    legacy_voice_overrides: boolean;
    tts: { service: string; model: string; voice: string };
    segmentation?: { mode: string; target_chars: number; reason?: string };
  };
  let {
    sessionId,
    plan = null,
    busy = false,
    navigationManaged = false,
    initialCastOpen = false,
    castPanel = $bindable<CastDraftController | undefined>(),
    onchanged,
    onsettings
  }: {
    sessionId: string;
    plan?: SpeechPlanState | null;
    busy?: boolean;
    navigationManaged?: boolean;
    initialCastOpen?: boolean;
    castPanel?: CastDraftController;
    onchanged: () => void | Promise<void>;
    onsettings?: () => void;
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
    `/sessions/${encodeURIComponent(sessionId)}/voice-setup`
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
  aria-label="Voices"
  id="voice-setup"
>
  <header class="flex items-start gap-3 sm:gap-4">
    <span
      class="grid size-11 shrink-0 place-items-center rounded-2xl bg-[var(--accent-soft)] text-[var(--accent)]"
      ><Users size={21} /></span
    >
    <div class="min-w-0 flex-1">
      <h2 class="text-lg font-semibold">Voices</h2>
      <p class="muted mt-1 text-sm">
        Choose one voice for everything, or assign voices to different speakers.
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
      <legend class="sr-only">Voice mode</legend>
      {#each [{ value: 'single_voice' as const, title: setup.workflow_kind === 'audiobook' ? 'One narrator' : 'One voice', detail: 'Use the session voice throughout.' }, { value: 'multi_voice' as const, title: 'Multiple voices', detail: 'Use source speakers, identify dialogue, or assign voices manually.' }] as choice}
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
        <ol class="steps mt-3" aria-label="Multiple-voice steps">
          <li>
            <span>1</span>
            <div>
              <strong>Prepare speech text</strong>
              <p>Finish any wording changes before identifying speakers.</p>
            </div>
          </li>
          <li>
            <span>2</span>
            <div>
              <strong>Identify speakers</strong>
              <p>
                Use existing labels, manual assignments, or speaker analysis.
              </p>
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
      <p class="muted my-4 text-sm">
        Review speakers and assign their voices below. Optional speaker analysis
        is available in the prepared speech plan.
      </p>
      <div id="characters-cast">
        <GenerationCastPanel
          bind:this={castPanel}
          {sessionId}
          service={setup.tts.service}
          model={setup.tts.model}
          sessionVoice={setup.tts.voice}
          standalone={true}
          initialOpen={initialCastOpen}
          {navigationManaged}
          busy={busy || pending}
          onchanged={() => void onchanged()}
        />
      </div>
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
          ? `Session voice: ${setup.tts.voice}`
          : 'Choose the session voice in Voice & audio.'}
        <a
          class="underline underline-offset-2"
          href={`/sessions/${encodeURIComponent(sessionId)}/voice`}
          >Choose voice</a
        >
      </p>{/if}
    {#if setup.legacy_voice_overrides && setup.mode === 'single_voice'}
      <p class="muted mt-4 text-sm">
        This older session may have individual voice overrides.
      </p>
      <button
        class="btn mt-2"
        disabled={busy || pending || Boolean(draft?.dirty)}
        onclick={() => setMode('single_voice')}>Use one voice throughout</button
      >
    {/if}
    {#if setup.workflow_kind === 'audiobook' && onsettings}<div
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
          onclick={onsettings}
          ><Settings2 size={15} /> Narration settings</button
        >
      </div>{:else if setup.workflow_kind === 'voiceover'}<p
        class="muted mt-4 text-xs"
      >
        Speaker turns keep their original timing windows. Speech optimization
        and delivery directions are optional.
      </p>{/if}
  {:else if !error}<p class="muted mt-4 text-sm" role="status">
      Loading voice setup…
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
