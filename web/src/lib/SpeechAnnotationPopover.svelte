<script lang="ts">
  import {
    AudioLines,
    LoaderCircle,
    Play,
    SlidersHorizontal,
    Users,
    X
  } from '@lucide/svelte';
  import { apiJson } from './api';
  import { errorMessage } from './errors';
  import {
    deliveryDescription,
    speakerLabel,
    type SpeechPreview
  } from './speech-annotations';

  let {
    sessionId,
    revisionId,
    segmentId,
    segmentNumber,
    runId = '',
    offset,
    anchor,
    activate = false,
    canPlay = false,
    onclose,
    onplay,
    onedit,
    onloaded
  }: {
    sessionId: string;
    revisionId: string;
    segmentId: string;
    segmentNumber: number;
    runId?: string;
    offset: number;
    anchor: HTMLButtonElement;
    activate?: boolean;
    canPlay?: boolean;
    onclose: () => void;
    onplay: () => void;
    onedit: () => void;
    onloaded: (preview: SpeechPreview) => void;
  } = $props();
  let panel: HTMLDivElement;
  let result = $state<SpeechPreview | null>(null);
  let error = $state('');
  let pending = $state(true);
  let retry = $state(0);
  let closeTimer: ReturnType<typeof setTimeout> | undefined;
  const span = $derived(
    result?.spans.find((item) => item.start <= offset && offset < item.end) ??
      result?.spans[0]
  );
  const part = $derived(
    result?.parts.find((item) => item.start <= offset && offset < item.end) ??
      result?.parts[0]
  );
  const report = $derived(
    Array.isArray(part?.report)
      ? (part.report as Array<{
          status?: string;
          control?: string;
          message?: string;
        }>)
      : []
  );
  const voiceSources: Record<string, string> = {
    segment: 'Block voice override',
    character: 'Character cast',
    narrator: 'Narrator cast',
    category: 'Voice category',
    source_speaker: 'Source speaker',
    session: 'Session voice',
    inline: 'Voice in speech markup',
    default: 'Renderer default'
  };

  function cancelClose() {
    if (closeTimer) clearTimeout(closeTimer);
    closeTimer = undefined;
  }
  function close(restore = false) {
    cancelClose();
    if (restore && anchor.isConnected) anchor.focus();
    onclose();
  }
  function scheduleClose() {
    cancelClose();
    if (!activate && !panel?.contains(document.activeElement))
      closeTimer = setTimeout(() => close(), 220);
  }
  function position() {
    if (!panel || !anchor.isConnected) return;
    const rect = anchor.getBoundingClientRect();
    const size = panel.getBoundingClientRect();
    panel.style.left = `${Math.max(8, Math.min(rect.left, innerWidth - size.width - 8))}px`;
    panel.style.top = `${rect.bottom + size.height + 12 < innerHeight ? rect.bottom + 5 : Math.max(8, rect.top - size.height - 5)}px`;
  }
  $effect(() => {
    const body = {
      revision_id: revisionId,
      segment_id: segmentId,
      ...(runId ? { generation_run_id: runId } : {})
    };
    void retry;
    const controller = new AbortController();
    pending = true;
    error = '';
    result = null;
    apiJson<SpeechPreview>(
      `/sessions/${encodeURIComponent(sessionId)}/speech-plan/preview`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: controller.signal
      }
    )
      .then((value) => {
        if (controller.signal.aborted) return;
        result = value;
        onloaded(value);
      })
      .catch((caught) => {
        if (!controller.signal.aborted) error = errorMessage(caught);
      })
      .finally(() => {
        if (!controller.signal.aborted) pending = false;
      });
    return () => controller.abort();
  });
  $effect(() => {
    const target = anchor;
    if (!panel || !target.isConnected) return;
    panel.showPopover();
    position();
    target.setAttribute('aria-expanded', 'true');
    const observer = new ResizeObserver(position);
    observer.observe(panel);
    target.addEventListener('pointerleave', scheduleClose);
    target.addEventListener('pointerenter', cancelClose);
    const scroll = (event: Event) => {
      if (!panel.contains(event.target as Node)) close();
    };
    document.addEventListener('scroll', scroll, true);
    return () => {
      cancelClose();
      observer.disconnect();
      target.setAttribute('aria-expanded', 'false');
      target.removeEventListener('pointerleave', scheduleClose);
      target.removeEventListener('pointerenter', cancelClose);
      document.removeEventListener('scroll', scroll, true);
    };
  });
  $effect(() => {
    if (activate && panel) panel.focus();
  });
</script>

<svelte:window onresize={() => close()} />
<div
  bind:this={panel}
  popover="auto"
  role="dialog"
  tabindex="-1"
  aria-label={`Voices and delivery for segment ${segmentNumber}`}
  class="speech-popover font-sans"
  onpointerenter={cancelClose}
  onpointerleave={scheduleClose}
  onkeydown={(event) => {
    event.stopPropagation();
    if (event.key === 'Escape') {
      event.preventDefault();
      close(true);
    }
  }}
  onfocusout={(event) => {
    if (
      event.relatedTarget &&
      !panel.contains(event.relatedTarget as Node) &&
      event.relatedTarget !== anchor
    )
      close();
  }}
  ontoggle={(event) => {
    // Closing a virtualized row can queue this event after bind:this clears.
    // Ignore detached popovers, including obsolete ones replaced by another row.
    if (
      event.currentTarget.isConnected &&
      !event.currentTarget.matches(':popover-open')
    )
      close();
  }}
>
  <header>
    <div>
      <strong><AudioLines size={15} /> Voices &amp; delivery</strong>
      <p>
        Segment {segmentNumber} · {runId
          ? 'Recorded run setup'
          : 'Current plan preview'}
      </p>
    </div>
    <button
      class="close"
      aria-label="Close voices and delivery"
      onclick={() => close(true)}><X size={16} /></button
    >
  </header>
  {#if pending}<p class="state" role="status">
      <LoaderCircle size={16} class="animate-spin" /> Resolving this segment…
    </p>
  {:else if error}<div class="state error" role="alert">
      {error}<button class="link" onclick={() => retry++}>Try again</button>
    </div>
  {:else if result && span}
    <dl>
      <div>
        <dt>Speaker</dt>
        <dd>{speakerLabel(span)}</dd>
      </div>
      <div>
        <dt>Voice</dt>
        <dd>{span.voice || part?.voice || 'Renderer default'}</dd>
      </div>
      <div>
        <dt>Assigned by</dt>
        <dd>
          {voiceSources[span.voice_source || part?.voice_source || ''] ||
            span.voice_source ||
            part?.voice_source ||
            'Current renderer'}
        </dd>
      </div>
    </dl>
    {#if result.spans.length > 1}<p class="sequence">
        {result.spans
          .map(speakerLabel)
          .filter(
            (name, index, names) => index === 0 || name !== names[index - 1]
          )
          .join(' → ')}
      </p>{/if}
    {#if !result.casting_enabled}<p class="note">
        Character casting is off. Block voice overrides still apply.
      </p>{/if}
    {#if span.fallback || part?.fallback}<p class="note warning">
        This phrase falls back to the narrator or session voice.
      </p>{/if}
    {#if deliveryDescription(span.delivery)}<div class="directions">
        <strong>Delivery</strong>
        <p>{deliveryDescription(span.delivery)}</p>
      </div>{/if}
    {#if part?.instructions}<p class="directions">{part.instructions}</p>{/if}
    {#if !result.performance_enabled}<p class="note">
        Delivery instructions are disabled for this setup.
      </p>{/if}
    {#each report.filter((entry) => entry.message) as entry}<p
        class="note"
        class:warning={['unsupported', 'ignored', 'dropped'].includes(
          entry.status || ''
        )}
      >
        {entry.message}
      </p>{/each}
    {#if result.events?.length}<p class="note">
        {result.events.map((event) => event.kind).join(' · ')}
      </p>{/if}
    <p class="renderer">{result.service} · {result.model}</p>
  {/if}
  <footer>
    <button disabled={!canPlay} onclick={onplay}
      ><Play size={14} /> Play segment</button
    >
    {#if !runId}<button
        disabled={pending || Boolean(error)}
        onclick={() => {
          close();
          onedit();
        }}><SlidersHorizontal size={14} /> Directions</button
      ><a
        href={`/sessions/${encodeURIComponent(sessionId)}/voice#characters-cast`}
        ><Users size={14} /> Edit cast</a
      >{/if}
  </footer>
  <p class="note">
    {runId
      ? 'Voice and instruction settings are frozen with this run.'
      : 'Compilation only; this preview does not generate audio.'}
  </p>
</div>

<style>
  .speech-popover {
    position: fixed;
    margin: 0;
    width: min(370px, calc(100vw - 16px));
    max-height: calc(100vh - 24px);
    overflow: auto;
    padding: 1rem;
    border: 1px solid var(--line);
    border-radius: 0.9rem;
    color: var(--ink);
    background: var(--paper-strong);
    box-shadow: var(--shadow);
    white-space: normal;
    font-size: 0.8rem;
    text-align: left;
  }
  header {
    display: flex;
    align-items: start;
    justify-content: space-between;
    gap: 0.6rem;
    margin-bottom: 0.7rem;
  }
  header strong,
  footer button,
  footer a,
  .state {
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }
  header p,
  .note,
  .renderer {
    font-size: 0.7rem;
    line-height: 1.5;
    color: var(--muted);
    margin-top: 0.35rem;
  }
  button.close {
    display: grid;
    place-items: center;
    min-width: 32px;
    min-height: 32px;
    border-radius: 0.4rem;
    border: 1px solid var(--line);
  }
  dl > div {
    display: grid;
    grid-template-columns: 5rem minmax(0, 1fr);
    gap: 0.5rem;
    padding: 0.25rem 0;
  }
  dt {
    color: var(--muted);
  }
  dd {
    overflow-wrap: anywhere;
    font-weight: 600;
  }
  .sequence {
    color: var(--accent);
    margin: 0.6rem 0;
    line-height: 1.5;
  }
  .directions {
    background: var(--accent-soft);
    border-radius: 0.4rem;
    padding: 0.5rem 0.65rem;
    line-height: 1.5;
    margin: 0.6rem 0;
  }
  .warning {
    color: var(--warning, #a35f15);
  }
  .error {
    color: var(--danger, #b42318);
    display: block;
  }
  .link {
    display: block;
    text-decoration: underline;
    padding: 0.5rem 0;
  }
  footer {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    border-top: 1px solid var(--line);
    padding-top: 0.65rem;
    margin-top: 0.7rem;
  }
  footer button,
  footer a {
    border: 1px solid var(--line);
    border-radius: 0.5rem;
    padding: 0.5rem 0.65rem;
    min-height: 38px;
    font-size: 0.72rem;
    font-weight: 600;
  }
  button:disabled {
    opacity: 0.45;
  }
  button:focus-visible,
  a:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  @media (pointer: coarse) {
    footer button,
    footer a,
    button.close {
      min-height: 44px;
    }
  }
</style>
