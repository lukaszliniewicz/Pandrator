<script lang="ts">
  import { GitMerge, TriangleAlert } from '@lucide/svelte';
  import type { GenerationSegment, SpeechBlockDecision } from './api-models';

  let {
    left,
    right,
    disabled = false,
    compact = false,
    onmerge
  }: {
    left: GenerationSegment;
    right: GenerationSegment;
    disabled?: boolean;
    compact?: boolean;
    onmerge: (left: GenerationSegment, right: GenerationSegment) => unknown;
  } = $props();

  const boundary = $derived<SpeechBlockDecision>(
    right.speech_block_provenance?.boundary_before ?? {}
  );
  const risks = $derived(right.speech_block_provenance?.risk_flags ?? []);
  const formationEvents = $derived(
    right.speech_block_provenance?.formation_events ?? []
  );
  const sourceCues = $derived(right.speech_block_provenance?.source_cues ?? []);
  const sourceReferences = $derived(
    boundary.source_references?.length
      ? boundary.source_references
      : sourceCues.map((cue) => cue.reference)
  );
  const summary = $derived(
    String(
      boundary.summary ??
        boundary.reason_code ??
        'Speech-block boundary retained by the planner.'
    )
  );
  const measurements = $derived(
    Object.entries(boundary.measurements ?? {}).filter(
      ([, value]) => value !== null && value !== ''
    )
  );

  function labelFor(key: string) {
    return key.replaceAll('_', ' ');
  }

  function formatTime(value: number | null | undefined) {
    if (value == null) return '';
    const minutes = Math.floor(value / 60_000);
    const seconds = Math.floor((value % 60_000) / 1_000);
    const milliseconds = value % 1_000;
    return `${minutes}:${String(seconds).padStart(2, '0')}.${String(milliseconds).padStart(3, '0')}`;
  }

  async function merge() {
    await onmerge(left, right);
  }
</script>

<span class:compact class:risky={risks.length > 0} class="boundary-marker">
  <button
    type="button"
    class="boundary-trigger"
    aria-label={`Boundary before segment ${right.ordinal + 1}: ${summary}`}
    onclick={(event) => {
      event.stopPropagation();
    }}
  >
    <span class="boundary-line"></span>
    {#if risks.length}<TriangleAlert size={compact ? 10 : 11} />{/if}
  </button>
  <span
    class="boundary-popover"
    role="group"
    aria-label="Speech block boundary details"
  >
    <strong>{summary}</strong>
    {#if boundary.reason_code}
      <span class="reason-code">{boundary.reason_code}</span>
    {/if}
    {#if measurements.length}
      <span class="measurements">
        {#each measurements as [key, value]}
          <span><b>{labelFor(key)}</b> {String(value)}</span>
        {/each}
      </span>
    {/if}
    {#if sourceReferences.length}
      <span class="source-evidence">
        <b>Source</b>
        {sourceReferences.map(String).join(', ')}
        {#if sourceCues.some((cue) => cue.start_ms != null)}
          · {formatTime(
            sourceCues.find((cue) => cue.start_ms != null)?.start_ms
          )}–{formatTime(
            [...sourceCues].reverse().find((cue) => cue.end_ms != null)?.end_ms
          )}
        {/if}
      </span>
    {/if}
    {#if formationEvents.length}
      <span class="formation-events">
        {#each formationEvents as event}
          <span
            ><b>{event.action ?? 'formed'}</b>
            {event.summary ?? event.reason_code ?? ''}</span
          >
        {/each}
      </span>
    {/if}
    {#if risks.length}
      <span class="risks">Review: {risks.join(' · ')}</span>
    {/if}
    <button type="button" class="merge-action" {disabled} onclick={merge}>
      <GitMerge size={12} /> Join blocks {left.ordinal + 1}–{right.ordinal + 1}
    </button>
  </span>
</span>

<style>
  .boundary-marker {
    position: relative;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.1rem;
    height: 1.6rem;
    margin: 0 0.12rem;
    vertical-align: middle;
    font-family: ui-sans-serif, system-ui, sans-serif;
    user-select: none;
  }
  .boundary-marker.compact {
    width: 100%;
    height: 0.8rem;
    margin: 0;
  }
  .boundary-trigger {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.12rem;
    width: 100%;
    height: 100%;
    color: var(--muted);
  }
  .boundary-line {
    width: 2px;
    height: 1rem;
    border-radius: 999px;
    background: color-mix(in srgb, var(--muted) 52%, transparent);
    transition:
      height 0.12s ease,
      background 0.12s ease;
  }
  .compact .boundary-line {
    width: min(7rem, 18vw);
    height: 2px;
  }
  .risky .boundary-line {
    background: #d97706;
  }
  .boundary-trigger:hover .boundary-line,
  .boundary-trigger:focus-visible .boundary-line {
    height: 1.35rem;
    background: var(--accent);
  }
  .compact .boundary-trigger:hover .boundary-line,
  .compact .boundary-trigger:focus-visible .boundary-line {
    width: min(10rem, 24vw);
    height: 2px;
  }
  .boundary-trigger:focus-visible {
    border-radius: 0.35rem;
    outline: 2px solid color-mix(in srgb, var(--accent) 45%, transparent);
    outline-offset: 1px;
  }
  .boundary-popover {
    position: absolute;
    z-index: 35;
    bottom: calc(100% + 0.35rem);
    left: 50%;
    display: none;
    width: min(21rem, 78vw);
    transform: translateX(-50%);
    border: 1px solid var(--line);
    border-radius: 0.8rem;
    background: var(--paper-strong);
    padding: 0.65rem;
    color: var(--ink);
    box-shadow: 0 14px 36px rgba(0, 0, 0, 0.2);
    font-size: 0.68rem;
    font-style: normal;
    font-weight: 500;
    line-height: 1.35;
    text-align: left;
  }
  .boundary-marker:hover .boundary-popover,
  .boundary-marker:focus-within .boundary-popover {
    display: grid;
    gap: 0.42rem;
  }
  .reason-code {
    color: var(--muted);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.6rem;
  }
  .measurements {
    display: flex;
    flex-wrap: wrap;
    gap: 0.25rem 0.55rem;
    color: var(--muted);
  }
  .source-evidence,
  .formation-events {
    color: var(--muted);
  }
  .source-evidence b,
  .formation-events b {
    color: var(--ink);
  }
  .formation-events {
    display: grid;
    gap: 0.2rem;
  }
  .measurements b {
    margin-right: 0.15rem;
    font-weight: 700;
  }
  .risks {
    color: #b45309;
  }
  .merge-action {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 0.3rem;
    border-radius: 0.5rem;
    background: var(--accent-soft);
    padding: 0.4rem 0.55rem;
    color: var(--accent);
    font-weight: 750;
  }
  .merge-action:disabled {
    opacity: 0.4;
  }
</style>
