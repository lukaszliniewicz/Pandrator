<script lang="ts">
  import {
    ListChecks,
    Eye,
    Check,
    RefreshCw,
    LoaderCircle
  } from '@lucide/svelte';
  import SpeechPlanPicker from './SpeechPlanPicker.svelte';
  import { openSpeechPlanEditor, type SpeechPlanState } from './session-flow';
  let {
    sessionId,
    plan,
    busy = false,
    onprepare,
    onselect,
    onreview
  }: {
    sessionId: string;
    plan: SpeechPlanState | null;
    busy?: boolean;
    onprepare: () => Promise<unknown>;
    onselect: (id: string) => Promise<unknown>;
    onreview: () => Promise<unknown>;
  } = $props();
  const selected = $derived(
    plan?.items.find((item) => item.id === plan.selected_revision_id)
  );
</script>

<section
  class="surface rounded-3xl border border-[var(--line)] p-5 sm:p-6"
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
        Prepare the speech blocks, review their wording and boundaries, then
        generate audio separately.
      </p>
    </div>
    {#if selected?.reviewed}<span
        class="rounded-lg bg-[var(--accent-soft)] px-3 py-1 text-xs font-semibold"
        >Reviewed</span
      >{/if}
  </header>
  <div class="mt-4 space-y-3 sm:ml-[3.75rem]">
    {#if plan?.current_input}<p class="text-sm">
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
      {#if selected}<p class="muted text-sm">
          {selected.reusable_segment_count} reusable blocks · {selected.stale_segment_count}
          missing or stale. {selected.summary}.
        </p>{/if}
    {/if}
    {#if plan?.warning}<p class="text-sm text-amber-700" role="status">
        {plan.warning}
      </p>{/if}
    {#if plan?.blocked_reason}<p class="muted text-sm">
        {plan.blocked_reason}
      </p>{/if}
    <div class="flex flex-wrap gap-2">
      <button
        class="btn"
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
          ><Eye size={16} /> Review plan</button
        >
        <button
          class="btn"
          disabled={busy || selected.reviewed || Boolean(plan?.blocked_reason)}
          onclick={() => void onreview()}
          ><Check size={16} />
          {selected.reviewed ? 'Reviewed' : 'Mark reviewed'}</button
        >
      {/if}
    </div>
    <p class="muted text-xs">
      Preparation does not start speech synthesis. Any LLM rewriting belongs in
      the speech-text stage before this review; generation uses the selected
      plan’s wording.
    </p>
  </div>
</section>
