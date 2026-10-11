<script lang="ts">
  import {
    ListChecks,
    Eye,
    Check,
    RefreshCw,
    LoaderCircle,
    Settings2
  } from '@lucide/svelte';
  import type { CastDraftController } from './generation-controls';
  import type { Snippet } from 'svelte';
  import { page } from '$app/state';
  import SpeechPlanPicker from './SpeechPlanPicker.svelte';
  import NarrationPlanSettings from './NarrationPlanSettings.svelte';
  import PerformancePanel from './PerformancePanel.svelte';
  import AudioReuseNotice from './AudioReuseNotice.svelte';
  import { openSpeechPlanEditor, type SpeechPlanState } from './session-flow';
  let {
    inputControls,
    sessionId,
    workflowKind = 'audiobook',
    multiVoice = false,
    plan,
    busy = false,
    showCasting = true,
    externalCastPanel,
    onprepare,
    onselect,
    onreview,
    onsettings,
    onperformancechange
  }: {
    inputControls?: Snippet;
    sessionId: string;
    workflowKind?: string;
    multiVoice?: boolean;
    plan: SpeechPlanState | null;
    busy?: boolean;
    showCasting?: boolean;
    externalCastPanel?: CastDraftController;
    onprepare: () => Promise<unknown>;
    onselect: (id: string) => Promise<unknown>;
    onreview: () => Promise<unknown>;
    onsettings: () => void;
    onperformancechange?: () => void;
  } = $props();
  const selected = $derived(
    plan?.items.find((item) => item.id === plan.selected_revision_id)
  );
</script>

<section
  id="speech-preparation"
  class="surface rounded-2xl border border-[var(--line)] p-4 sm:rounded-3xl sm:p-6"
  aria-label="Speech plan"
>
  <header class="flex items-start gap-4">
    <span
      class="grid size-11 shrink-0 place-items-center rounded-2xl bg-[var(--accent-soft)] text-[var(--accent)]"
      ><ListChecks size={21} /></span
    >
    <div class="min-w-0 flex-1">
      <h2 class="text-lg font-semibold">Speech plan</h2>
      <p class="muted mt-1 text-sm">
        Divide your chosen text into blocks, then open Generation to adjust the
        spoken words and delivery{multiVoice ? ', and assign voices' : ''}.
        Audio is generated from the selected plan.
      </p>
    </div>
    {#if selected?.reviewed}<span
        class="rounded-lg bg-[var(--accent-soft)] px-3 py-1 text-xs font-semibold"
        >Reviewed</span
      >{/if}
  </header>
  <div class="mt-4 space-y-3 sm:ml-[3.75rem]">
    {#if inputControls}{@render inputControls()}{:else if plan?.current_input}<p
        class="text-sm"
      >
        Selected input: <strong
          >{plan.current_input.label}{plan.current_input.version
            ? ` v${plan.current_input.version}`
            : ''}</strong
        >
      </p>{:else}<p class="muted text-sm">
        Complete or select the intended input text before preparing a plan.
      </p>{/if}
    {#if plan?.items.length}
      <SpeechPlanPicker
        {plan}
        disabled={busy || Boolean(plan.blocked_reason)}
        label="Selected speech-plan version"
        {onselect}
      />
      {#if selected}
        <p class="text-sm" data-testid="speech-plan-summary">
          <strong
            >{selected.active_segment_count ?? selected.segment_count} included blocks</strong
          >
          <span class="muted">
            · {selected.reviewed ? 'Reviewed' : 'Not reviewed'}</span
          >
        </p>
        <p class="muted text-sm" data-testid="speech-plan-next-action">
          {#if selected.reviewed && plan?.can_generate}
            Use Generate audio to choose whether to continue, refresh, or record
            everything.
          {:else if selected.reviewed}
            Plan reviewed. Check the generation step below for any remaining
            requirements.
          {:else}
            Open Generation to check the spoken wording and delivery, then mark
            the plan reviewed from Speech plans.
          {/if}
        </p>
      {/if}
      <AudioReuseNotice
        settingsStale={selected?.audio_settings_stale_segment_count ??
          undefined}
        identityUnknown={selected?.audio_identity_unknown_segment_count ??
          undefined}
      />
    {/if}
    {#if workflowKind === 'audiobook'}<NarrationPlanSettings
        {sessionId}
        {busy}
        onchanged={onperformancechange}
      />{/if}
    {#if plan?.warning}<p class="text-sm text-amber-700" role="status">
        {plan.warning}
      </p>{/if}
    {#if plan?.blocked_reason}<p class="muted text-sm">
        {plan.blocked_reason}
      </p>{/if}
    <div class="flex flex-wrap gap-2">
      {#if showCasting}<a
          class="btn btn-secondary"
          href={`/sessions/${sessionId}/voice#characters-cast`}
          >Characters &amp; cast</a
        >{/if}
      <button
        class="btn btn-secondary"
        onclick={onsettings}
        disabled={busy || !plan}
        ><Settings2 size={16} />{workflowKind === 'audiobook'
          ? 'Narration settings'
          : 'Timing & block settings'}</button
      >
      <button
        class={selected ? 'btn btn-secondary' : 'btn btn-primary'}
        disabled={busy || !plan?.can_prepare || Boolean(plan?.blocked_reason)}
        onclick={() => void onprepare()}
        >{#if busy}<LoaderCircle
            class="animate-spin"
            size={16}
          />{:else}<RefreshCw size={16} />{/if}{plan?.selected_revision_id
          ? 'Prepare a new plan'
          : 'Prepare speech plan'}</button
      >
      {#if selected}
        <button
          class="btn btn-primary"
          disabled={busy}
          onclick={() => openSpeechPlanEditor(sessionId)}
          ><Eye size={16} /> Open Generation</button
        >
        {#if !selected.reviewed}
          <button
            class="btn btn-secondary"
            disabled={busy || Boolean(plan?.blocked_reason)}
            onclick={() => void onreview()}
            ><Check size={16} /> Mark reviewed</button
          >
        {/if}
      {/if}
    </div>
    <details class="text-sm" data-testid="speech-plan-details">
      <summary class="cursor-pointer font-semibold"
        >Plan details &amp; help</summary
      >
      <div class="muted mt-2 space-y-2 text-xs leading-relaxed">
        {#if selected}
          <p>{selected.summary}</p>
          {#if selected.audio_reuse_checked !== false && typeof selected.reusable_segment_count === 'number' && typeof selected.stale_segment_count === 'number'}
            <p>
              {selected.reusable_segment_count} recordings match the current settings
              · {selected.stale_segment_count} blocks have missing, changed or unverified
              audio.
            </p>
          {:else}
            <p>
              Recording compatibility has not been checked yet. Generate audio
              previews exactly what will be kept or replaced before you confirm.
            </p>
          {/if}
        {/if}
        <p>
          A speech plan divides the chosen text into blocks. Preparing a new
          version keeps older versions in history and does not record audio.
        </p>
        <p>
          Editing works on this selected plan. A block action affects the whole
          block; selecting words targets a phrase; placing the cursor offers
          vocal cues. XML is available from each block’s actions.
        </p>
        <p>
          Speech text includes optimized wording when available. Display and
          subtitle wording stay separate. Model-support feedback explains which
          directions can affect new audio.
        </p>
      </div>
    </details>
    {#if selected}
      {#key `${sessionId}:${selected.id}`}
        <PerformancePanel
          showCasting={multiVoice}
          {externalCastPanel}
          {sessionId}
          revisionId={selected.id}
          initialOpen={page.url.hash === '#speech-preparation'}
          {busy}
          onchanged={onperformancechange}
        />
      {/key}
    {/if}
  </div>
</section>
