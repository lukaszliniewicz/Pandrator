<script lang="ts">
  import {
    ArrowRight,
    Languages,
    Save,
    ShieldCheck,
    TriangleAlert,
    WandSparkles
  } from '@lucide/svelte';
  import type { GenerationSegment, SpeechPlan } from './api-models';
  import type { ComparisonDecisionRow } from './generation-view-models';
  import TextDiff from './TextDiff.svelte';
  import { untrack } from 'svelte';
  import { errorMessage } from './errors';
  import ReviewWorkspace from './ReviewWorkspace.svelte';
  import UnsavedChangesDialog from './UnsavedChangesDialog.svelte';
  import { guardEditorNavigation } from './editor-navigation';

  let {
    item,
    readOnly = false,
    plan,
    decisionRows,
    text,
    diff,
    regenerate,
    ontext,
    ontogglediff,
    ontoggleregenerate,
    onclose,
    onsave
  }: {
    item: GenerationSegment;
    readOnly?: boolean;
    plan: SpeechPlan;
    decisionRows: ComparisonDecisionRow[];
    text: string;
    diff: boolean;
    regenerate: boolean;
    ontext: (value: string) => void;
    ontogglediff: () => void;
    ontoggleregenerate: (value: boolean) => void;
    onclose: () => void;
    onsave: () => void | Promise<void>;
  } = $props();
  let toolsOpen = $state(false);
  let saving = $state(false);
  let error = $state('');
  let pendingLeave = $state<(() => void) | null>(null);
  const baseline = untrack(() => text);
  function requestLeave(action = onclose) {
    if (saving) return;
    if (!readOnly && text !== baseline) pendingLeave = action;
    else action();
  }
  async function save() {
    if (readOnly || saving || !text.trim()) return;
    saving = true;
    error = '';
    try {
      await onsave();
      const action = pendingLeave;
      pendingLeave = null;
      action?.();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      saving = false;
    }
  }
  guardEditorNavigation({
    dirty: () => !readOnly && text !== baseline,
    pending: () => saving,
    requestLeave
  });
</script>

<ReviewWorkspace
  closeLabel="Close speech delivery review"
  titleId="segment-optimization-title"
  onclose={() => requestLeave()}
  busy={saving}
  bind:toolsOpen
  toolsTitle="Plan details"
>
  {#snippet header()}
    <div class="min-w-0 flex-1">
      <div class="section-label">Generation segment {item.ordinal + 1}</div>
      <div class="mt-1 flex flex-wrap items-center gap-2">
        <h2 id="segment-optimization-title" class="text-lg font-semibold">
          Review speech delivery
        </h2>
        {#if plan.version}
          <span class={`plan-state ${plan.status}`}
            >{plan.status === 'safe_fallback'
              ? 'safe fallback'
              : plan.status}</span
          >
          <span class="plan-state neutral">{plan.mode_used}</span>
        {/if}
      </div>
      <p class="muted mt-1 text-xs">
        Display text remains unchanged. Saving this delivery marks existing
        audio takes stale.
      </p>
    </div>
    <button
      onclick={() => void save()}
      disabled={readOnly || saving || !text.trim()}
      class="btn btn-primary"
      ><Save size={14} /> {saving ? 'Saving…' : 'Save delivery'}</button
    >
  {/snippet}
  {#snippet tools()}<button
      onclick={ontogglediff}
      aria-pressed={diff}
      class="btn btn-secondary"
      >{diff ? 'Show side by side' : 'Show diff'}</button
    >
    {#if plan.version}
      <section
        class="mt-5 overflow-hidden rounded-2xl border border-[var(--line)]"
      >
        <header
          class="flex flex-wrap items-center justify-between gap-3 bg-[var(--accent-soft)] px-4 py-3"
        >
          <div>
            <h3 class="text-sm font-semibold">Structured decisions</h3>
            <p class="muted mt-0.5 text-xs">
              {plan.model || 'Unknown model'} · {plan.language || 'und'} display →
              {plan.voice_language || plan.language || 'und'} voice
            </p>
          </div>
          <a href="/pronunciations" class="action bg-[var(--paper)]"
            ><Languages size={14} /> Open pronunciation library</a
          >
        </header>
        <div class="grid gap-px bg-[var(--line)] md:grid-cols-2">
          <div class="bg-[var(--paper-strong)] p-4">
            <h4
              class="flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-[var(--muted)]"
            >
              <WandSparkles size={14} /> Planned changes
            </h4>
            {#if decisionRows.length}
              <div class="mt-3 space-y-2">
                {#each decisionRows as decision (decision.id)}
                  <div class="decision-row">
                    <div class="min-w-0">
                      <strong>{decision.written}</strong>
                      <div class="muted mt-0.5 truncate text-[.65rem]">
                        {(decision.signals ?? []).join(' · ') || decision.task}
                      </div>
                    </div>
                    <ArrowRight
                      class="shrink-0 text-[var(--muted)]"
                      size={14}
                    />
                    <div class="min-w-0 text-right">
                      <span
                        class="font-mono text-xs font-semibold text-[var(--accent)]"
                        >{decision.spoken || decision.written}</span
                      >
                      <div class="muted mt-0.5 text-[.62rem] uppercase">
                        {decision.action}{decision.confidence
                          ? ` · ${decision.confidence}`
                          : ''}
                      </div>
                    </div>
                  </div>
                {/each}
              </div>
            {:else}
              <p class="muted mt-3 text-xs">
                No unresolved spans were changed.
              </p>
            {/if}
          </div>
          <div class="bg-[var(--paper-strong)] p-4">
            <h4
              class="flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-[var(--muted)]"
            >
              <ShieldCheck size={14} /> Reused and proposed
            </h4>
            {#if plan.known_pronunciations?.length}
              <div class="mt-3 space-y-2">
                {#each plan.known_pronunciations as known}
                  <div class="decision-row">
                    <strong>{known.text}</strong>
                    <ArrowRight class="text-[var(--muted)]" size={14} />
                    <span class="font-mono text-xs text-[var(--accent)]"
                      >{known.spoken}</span
                    >
                  </div>
                {/each}
              </div>
            {:else}
              <p class="muted mt-3 text-xs">
                No reviewed library entries matched this segment.
              </p>
            {/if}
            {#if plan.proposals?.length}
              <div
                class="mt-4 rounded-xl border border-amber-400/30 bg-amber-500/10 p-3"
              >
                <div
                  class="flex items-center gap-2 text-xs font-semibold text-amber-700"
                >
                  <TriangleAlert size={14} />{plan.proposals.length} pronunciation
                  {plan.proposals.length === 1 ? 'needs' : 'need'} review
                </div>
                <div class="mt-2 flex flex-wrap gap-1.5">
                  {#each plan.proposals as proposal}
                    <span
                      class="rounded-full bg-[var(--paper)] px-2 py-1 font-mono text-[.65rem]"
                      >{proposal.source_form} → {proposal.phonetic}</span
                    >
                  {/each}
                </div>
              </div>
            {/if}
          </div>
        </div>
        {#if plan.validation?.errors?.length || plan.validation?.warnings?.length}
          <div class="border-t border-[var(--line)] px-4 py-3 text-xs">
            <strong>Validator report</strong>
            {#each plan.validation.errors ?? [] as message}<p
                class="mt-1 text-red-600"
              >
                {message}
              </p>{/each}
            {#each plan.validation.warnings ?? [] as message}<p
                class="mt-1 text-amber-700"
              >
                {message}
              </p>{/each}
          </div>
        {/if}
      </section>
    {/if}
  {/snippet}
  {#snippet notices()}
    {#if readOnly}<p role="status" class="muted px-4 py-2 text-sm">
        Historical run · read-only speech delivery.
      </p>{/if}{#if error}<p
        role="alert"
        class="px-4 py-2 text-sm text-red-600"
      >
        {error}
      </p>{/if}{/snippet}
  {#snippet footer()}<div class="border-t border-[var(--line)] px-4 py-3">
      <label class="mr-auto flex items-center gap-2 text-xs font-semibold">
        <input
          type="checkbox"
          disabled={readOnly || saving}
          checked={regenerate}
          onchange={(event) => ontoggleregenerate(event.currentTarget.checked)}
          class="accent-[var(--accent)]"
        />
        Regenerate this segment after saving
      </label>
    </div>{/snippet}
  <div class="min-h-0 flex-1 overflow-auto p-4 sm:p-6">
    {#if diff}
      <div class="grid gap-4">
        <TextDiff before={String(item.text ?? '')} after={text} />
        <section>
          <h3
            class="mb-2 text-xs font-bold uppercase tracking-wider text-[var(--muted)]"
          >
            Speech delivery · editable
          </h3>
          <textarea
            value={text}
            disabled={readOnly || saving}
            aria-label="Speech delivery text"
            oninput={(event) => ontext(event.currentTarget.value)}
            class="min-h-44 w-full resize-y rounded-2xl border border-[var(--line)] bg-[var(--paper)] p-4 text-sm leading-7"
          ></textarea>
        </section>
      </div>
    {:else}
      <div class="grid gap-4 md:grid-cols-2">
        <section>
          <h3
            class="mb-2 text-xs font-bold uppercase tracking-wider text-[var(--muted)]"
          >
            Display text
          </h3>
          <div
            class="h-full min-h-44 rounded-2xl border border-[var(--line)] bg-[var(--paper)] p-4 text-sm leading-7"
          >
            {item.text}
          </div>
        </section>
        <section>
          <h3
            class="mb-2 text-xs font-bold uppercase tracking-wider text-[var(--muted)]"
          >
            Speech delivery · editable
          </h3>
          <textarea
            value={text}
            disabled={readOnly || saving}
            aria-label="Speech delivery text"
            oninput={(event) => ontext(event.currentTarget.value)}
            class="h-full min-h-44 w-full resize-y rounded-2xl border border-[var(--line)] bg-[var(--paper)] p-4 text-sm leading-7"
          ></textarea>
        </section>
      </div>
    {/if}
  </div>
</ReviewWorkspace>
{#if pendingLeave}<UnsavedChangesDialog
    onsave={() => void save()}
    ondiscard={() => {
      const action = pendingLeave;
      pendingLeave = null;
      action?.();
    }}
    oncancel={() => (pendingLeave = null)}
    busy={saving}
    {error}
  />{/if}

<style>
  .plan-state {
    border-radius: 999px;
    background: color-mix(in srgb, var(--success) 15%, transparent);
    padding: 0.22rem 0.5rem;
    color: var(--success);
    font-size: 0.58rem;
    font-weight: 800;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
  .plan-state.safe_fallback {
    background: rgba(245, 158, 11, 0.14);
    color: #a16207;
  }
  .plan-state.neutral {
    background: var(--accent-soft);
    color: var(--accent);
  }
  .decision-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
    align-items: center;
    gap: 0.6rem;
    border: 1px solid var(--line);
    border-radius: 0.7rem;
    background: var(--paper);
    padding: 0.6rem 0.7rem;
    font-size: 0.75rem;
  }
  .action {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    border: 1px solid var(--line);
    border-radius: 0.55rem;
    padding: 0.4rem 0.6rem;
    font-size: 0.7rem;
    font-weight: 700;
  }
  .action.primary {
    background: var(--action-bg);
    color: white;
  }
  .action.primary:hover {
    background: var(--action-hover);
  }
  .action:disabled {
    opacity: 0.35;
  }
</style>
