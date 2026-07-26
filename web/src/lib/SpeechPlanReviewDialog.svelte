<script lang="ts">
  import {
    ArrowRight,
    Languages,
    Save,
    ShieldCheck,
    TriangleAlert,
    WandSparkles,
    X
  } from '@lucide/svelte';
  import type { GenerationSegment, SpeechPlan } from './api-models';
  import type { ComparisonDecisionRow } from './generation-view-models';
  import TextDiff from './TextDiff.svelte';

  let {
    item,
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
</script>

<div
  class="fixed inset-0 z-[95] grid place-items-center bg-black/55 p-3 backdrop-blur-sm"
  role="presentation"
  onclick={(event) => event.target === event.currentTarget && onclose()}
>
  <div class="comparison-modal flex max-h-[92vh] w-full max-w-5xl flex-col overflow-hidden rounded-3xl" role="dialog" aria-modal="true" aria-labelledby="segment-optimization-title">
    <header class="flex items-start gap-4 border-b border-[var(--line)] px-5 py-4">
      <div class="min-w-0 flex-1">
        <div class="eyebrow">Generation segment {item.ordinal + 1}</div>
        <div class="mt-1 flex flex-wrap items-center gap-2">
          <h2 id="segment-optimization-title" class="text-xl font-semibold">Review speech plan</h2>
          {#if plan.version}
            <span class={`plan-state ${plan.status}`}>{plan.status === 'safe_fallback' ? 'safe fallback' : plan.status}</span>
            <span class="plan-state neutral">{plan.mode_used}</span>
          {/if}
        </div>
        <p class="muted mt-1 text-xs">Display text remains unchanged. Saving this delivery marks existing audio takes stale.</p>
      </div>
      <button onclick={ontogglediff} class:active={diff} class="action">{diff ? 'Side by side' : 'Diff'}</button>
      <button onclick={onclose} class="rounded-xl p-2" aria-label="Close"><X size={20}/></button>
    </header>

    <div class="min-h-0 flex-1 overflow-auto p-5">
      {#if diff}
        <div class="grid gap-4">
          <TextDiff before={String(item.text ?? '')} after={text}/>
          <section>
            <h3 class="mb-2 text-xs font-bold uppercase tracking-wider text-[var(--muted)]">Speech delivery · editable</h3>
            <textarea value={text} oninput={(event) => ontext(event.currentTarget.value)} class="min-h-44 w-full resize-y rounded-2xl border border-[var(--line)] bg-[var(--paper)] p-4 text-sm leading-7"></textarea>
          </section>
        </div>
      {:else}
        <div class="grid gap-4 md:grid-cols-2">
          <section>
            <h3 class="mb-2 text-xs font-bold uppercase tracking-wider text-[var(--muted)]">Display text</h3>
            <div class="h-full min-h-44 rounded-2xl border border-[var(--line)] bg-[var(--paper)] p-4 text-sm leading-7">{item.text}</div>
          </section>
          <section>
            <h3 class="mb-2 text-xs font-bold uppercase tracking-wider text-[var(--muted)]">Speech delivery · editable</h3>
            <textarea value={text} oninput={(event) => ontext(event.currentTarget.value)} class="h-full min-h-44 w-full resize-y rounded-2xl border border-[var(--line)] bg-[var(--paper)] p-4 text-sm leading-7"></textarea>
          </section>
        </div>
      {/if}

      {#if plan.version}
        <section class="mt-5 overflow-hidden rounded-2xl border border-[var(--line)]">
          <header class="flex flex-wrap items-center justify-between gap-3 bg-[var(--accent-soft)] px-4 py-3">
            <div>
              <h3 class="text-sm font-semibold">Structured decisions</h3>
              <p class="muted mt-0.5 text-xs">{plan.model || 'Unknown model'} · {plan.language || 'und'} display → {plan.voice_language || plan.language || 'und'} voice</p>
            </div>
            <a href="/pronunciations" class="action bg-[var(--paper)]"><Languages size={14}/> Open pronunciation library</a>
          </header>
          <div class="grid gap-px bg-[var(--line)] md:grid-cols-2">
            <div class="bg-[var(--paper-strong)] p-4">
              <h4 class="flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-[var(--muted)]"><WandSparkles size={14}/> Planned changes</h4>
              {#if decisionRows.length}
                <div class="mt-3 space-y-2">
                  {#each decisionRows as decision (decision.id)}
                    <div class="decision-row">
                      <div class="min-w-0">
                        <strong>{decision.written}</strong>
                        <div class="muted mt-0.5 truncate text-[.65rem]">{(decision.signals ?? []).join(' · ') || decision.task}</div>
                      </div>
                      <ArrowRight class="shrink-0 text-[var(--muted)]" size={14}/>
                      <div class="min-w-0 text-right">
                        <span class="font-mono text-xs font-semibold text-[var(--accent)]">{decision.spoken || decision.written}</span>
                        <div class="muted mt-0.5 text-[.62rem] uppercase">{decision.action}{decision.confidence ? ` · ${decision.confidence}` : ''}</div>
                      </div>
                    </div>
                  {/each}
                </div>
              {:else}
                <p class="muted mt-3 text-xs">No unresolved spans were changed.</p>
              {/if}
            </div>
            <div class="bg-[var(--paper-strong)] p-4">
              <h4 class="flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-[var(--muted)]"><ShieldCheck size={14}/> Reused and proposed</h4>
              {#if plan.known_pronunciations?.length}
                <div class="mt-3 space-y-2">
                  {#each plan.known_pronunciations as known}
                    <div class="decision-row">
                      <strong>{known.text}</strong>
                      <ArrowRight class="text-[var(--muted)]" size={14}/>
                      <span class="font-mono text-xs text-[var(--accent)]">{known.spoken}</span>
                    </div>
                  {/each}
                </div>
              {:else}
                <p class="muted mt-3 text-xs">No reviewed library entries matched this segment.</p>
              {/if}
              {#if plan.proposals?.length}
                <div class="mt-4 rounded-xl border border-amber-400/30 bg-amber-500/10 p-3">
                  <div class="flex items-center gap-2 text-xs font-semibold text-amber-700">
                    <TriangleAlert size={14}/>{plan.proposals.length} pronunciation {plan.proposals.length === 1 ? 'needs' : 'need'} review
                  </div>
                  <div class="mt-2 flex flex-wrap gap-1.5">
                    {#each plan.proposals as proposal}
                      <span class="rounded-full bg-[var(--paper)] px-2 py-1 font-mono text-[.65rem]">{proposal.source_form} → {proposal.phonetic}</span>
                    {/each}
                  </div>
                </div>
              {/if}
            </div>
          </div>
          {#if plan.validation?.errors?.length || plan.validation?.warnings?.length}
            <div class="border-t border-[var(--line)] px-4 py-3 text-xs">
              <strong>Validator report</strong>
              {#each plan.validation.errors ?? [] as message}<p class="mt-1 text-red-600">{message}</p>{/each}
              {#each plan.validation.warnings ?? [] as message}<p class="mt-1 text-amber-700">{message}</p>{/each}
            </div>
          {/if}
        </section>
      {/if}
    </div>

    <footer class="flex flex-wrap items-center justify-end gap-3 border-t border-[var(--line)] px-5 py-4">
      <label class="mr-auto flex items-center gap-2 text-xs font-semibold">
        <input type="checkbox" checked={regenerate} onchange={(event) => ontoggleregenerate(event.currentTarget.checked)} class="accent-[var(--accent)]"/>
        Regenerate this segment after saving
      </label>
      <button onclick={onclose} class="action">Cancel</button>
      <button onclick={onsave} disabled={!text.trim()} class="action primary"><Save size={14}/> Save review</button>
    </footer>
  </div>
</div>

<style>
  .comparison-modal { border: 1px solid var(--line); background: var(--paper-strong); box-shadow: 0 22px 70px rgba(0, 0, 0, .25); }
  .plan-state { border-radius: 999px; background: color-mix(in srgb, var(--success) 15%, transparent); padding: .22rem .5rem; color: var(--success); font-size: .58rem; font-weight: 800; letter-spacing: .06em; text-transform: uppercase; }
  .plan-state.safe_fallback { background: rgba(245, 158, 11, .14); color: #a16207; }
  .plan-state.neutral { background: var(--accent-soft); color: var(--accent); }
  .decision-row { display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr); align-items: center; gap: .6rem; border: 1px solid var(--line); border-radius: .7rem; background: var(--paper); padding: .6rem .7rem; font-size: .75rem; }
  .action { display: flex; align-items: center; gap: .35rem; border: 1px solid var(--line); border-radius: .55rem; padding: .4rem .6rem; font-size: .7rem; font-weight: 700; }
  .action.primary { background: var(--action-bg); color: white; }
  .action.primary:hover { background: var(--action-hover); }
  .action:disabled { opacity: .35; }
</style>
