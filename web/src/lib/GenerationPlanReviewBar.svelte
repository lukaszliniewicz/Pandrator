<script lang="ts">
  import { Check, LoaderCircle } from '@lucide/svelte';
  import { onDestroy, untrack } from 'svelte';
  import { errorMessage } from './errors';
  import {
    sessionFlowAction,
    speechPlanState,
    type SpeechPlanState
  } from './session-flow';

  let {
    sessionId,
    revisionId,
    current = true,
    blocked = false,
    refreshKey,
    beforeReview,
    onrefresh
  }: {
    sessionId: string;
    revisionId: string;
    current?: boolean;
    blocked?: boolean;
    refreshKey?: unknown;
    beforeReview: () => Promise<void>;
    onrefresh: () => Promise<unknown>;
  } = $props();

  let reviewState = $state<SpeechPlanState | null>(null);
  let pending = $state(false);
  let saving = $state(false);
  let error = $state('');
  let needsInspection = $state(false);
  let notice = $state('');
  let alive = true;
  let readTicket = 0;
  onDestroy(() => {
    alive = false;
    readTicket++;
  });

  const selected = $derived(
    reviewState?.items.find((item) => item.id === revisionId)
  );
  const matches = $derived(
    current &&
      reviewState?.session_id === sessionId &&
      reviewState.selected_revision_id === revisionId &&
      Boolean(reviewState.content_signature)
  );
  const canReview = $derived(
    matches &&
      Boolean(selected) &&
      !selected?.reviewed &&
      !blocked &&
      !pending &&
      !saving &&
      !needsInspection &&
      !reviewState?.blocked_reason
  );

  async function loadReview(targetSession: string, targetRevision: string) {
    const ticket = ++readTicket;
    pending = true;
    try {
      const next = await speechPlanState(targetSession, { summary: true });
      if (
        !alive ||
        ticket !== readTicket ||
        sessionId !== targetSession ||
        revisionId !== targetRevision ||
        !current
      )
        return;
      if (
        next.session_id !== targetSession ||
        next.selected_revision_id !== targetRevision
      ) {
        reviewState = null;
        needsInspection = true;
        error =
          'The selected plan changed. Reload the plan and inspect it before marking it reviewed.';
        return;
      }
      reviewState = next;
      if (!needsInspection) error = '';
    } catch (caught) {
      if (!alive || ticket !== readTicket) return;
      reviewState = null;
      error = errorMessage(caught);
    } finally {
      if (alive && ticket === readTicket) pending = false;
    }
  }

  $effect(() => {
    void refreshKey;
    const targetSession = sessionId;
    const targetRevision = revisionId;
    if (!current) {
      readTicket++;
      reviewState = null;
      pending = false;
      return;
    }
    // Review cannot finish during generation or an edit. Do not repeatedly
    // inspect the entire plan just because a running job refreshed its rows.
    if (blocked) {
      readTicket++;
      pending = false;
      return;
    }
    // Data refreshes do not change the view or mark anything reviewed.
    untrack(() => {
      void loadReview(targetSession, targetRevision);
    });
  });

  async function reloadForInspection() {
    if (saving || blocked) return;
    const targetSession = sessionId;
    const targetRevision = revisionId;
    error = '';
    pending = true;
    try {
      await onrefresh();
      if (
        !alive ||
        sessionId !== targetSession ||
        revisionId !== targetRevision ||
        !current
      )
        return;
      needsInspection = false;
      notice =
        'Plan reloaded. Inspect the wording and boundaries before marking it reviewed.';
      await loadReview(targetSession, targetRevision);
    } catch (caught) {
      if (alive) error = errorMessage(caught);
    } finally {
      if (alive) pending = false;
    }
  }

  async function markReviewed() {
    if (!canReview || !reviewState?.content_signature) return;
    // Capture the inspected signature BEFORE waiting for outstanding edits.
    // Never fetch a newer signature during confirmation or silently retry it.
    const targetSession = sessionId;
    const targetRevision = revisionId;
    const signature = reviewState.content_signature;
    const inspected = reviewState;
    saving = true;
    error = '';
    notice = '';
    try {
      await beforeReview();
      if (
        !alive ||
        !current ||
        sessionId !== targetSession ||
        revisionId !== targetRevision
      )
        return;
      if (blocked)
        throw new Error(
          'Wait for pending edits to finish, then review the updated plan.'
        );
      await sessionFlowAction(targetSession, 'generation-plan/review', {
        revision_id: targetRevision,
        content_signature: signature
      });
      if (
        !alive ||
        !current ||
        sessionId !== targetSession ||
        revisionId !== targetRevision
      )
        return;
      readTicket++;
      pending = false;
      reviewState = {
        ...inspected,
        items: inspected.items.map((item) =>
          item.id === targetRevision ? { ...item, reviewed: true } : item
        )
      };
      notice = 'Plan marked reviewed. No audio was generated.';
    } catch (caught) {
      if (
        !alive ||
        sessionId !== targetSession ||
        revisionId !== targetRevision
      )
        return;
      needsInspection = true;
      error = errorMessage(caught);
    } finally {
      if (alive) saving = false;
    }
  }
</script>

<section
  class="review-bar border-b border-[var(--line)] bg-[var(--paper-strong)] px-4 py-2 text-xs"
  aria-label="Speech plan review"
  data-testid="drawer-plan-review"
  aria-busy={pending || saving}
>
  {#if !current}
    <p class="muted">
      Run history is read-only here. Switch to Active mix (the current audio) to
      review the selected plan.
    </p>
  {:else}
    <div class="flex flex-wrap items-center justify-between gap-2">
      <p class="min-w-0">
        {#if selected && matches}
          <strong>Plan v{selected.revision_number}</strong> · {selected.active_segment_count}
          included blocks ·
          {#if selected.reviewed}<span
              class="inline-flex items-center gap-1 font-semibold text-[var(--success)]"
              ><Check size={13} /> Reviewed</span
            >
          {:else}<span>Needs review</span>{/if}
        {:else if pending}<span class="muted">Loading review status…</span>
        {:else if blocked}<span class="muted"
            >Review is available after pending work finishes.</span
          >
        {:else}<span class="muted">Review status unavailable</span>{/if}
      </p>
      {#if selected && !selected.reviewed}
        <button
          type="button"
          class="review-button inline-flex items-center gap-1.5 rounded-lg border border-[var(--line)] px-3 py-1.5 font-semibold disabled:opacity-40"
          disabled={!canReview}
          onclick={() => void markReviewed()}
          data-testid="drawer-mark-reviewed"
        >
          {#if saving}<LoaderCircle
              size={14}
              class="animate-spin"
            />{:else}<Check size={14} />{/if}
          {saving ? 'Saving review…' : 'Mark reviewed'}
        </button>
      {/if}
    </div>
    {#if selected && !selected.reviewed}
      <p class="muted mt-1">
        Marks this whole plan reviewed, not just visible or filtered blocks. It
        does not generate audio.
      </p>
    {/if}
    {#if reviewState?.warning}<p
        class="mt-1 text-[var(--warning)]"
        role="status"
      >
        {reviewState.warning}
      </p>{/if}
    {#if reviewState?.blocked_reason}<p class="mt-1" role="status">
        {reviewState.blocked_reason}
      </p>{/if}
    {#if error}
      <div class="mt-1 flex flex-wrap items-center gap-2">
        <p role="alert" class="text-red-600">{error}</p>
        <button
          type="button"
          class="underline underline-offset-2"
          disabled={blocked || pending || saving}
          onclick={() => void reloadForInspection()}
          >Reload plan for review</button
        >
      </div>
    {/if}
    {#if notice}<p class="muted mt-1" role="status">{notice}</p>{/if}
  {/if}
</section>
