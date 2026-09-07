<script lang="ts">
  import type { MediaEditRange } from './api-models';
  import { onDestroy, onMount } from 'svelte';

  let {
    durationMs,
    currentMs,
    keepRanges,
    cutRanges = [],
    peaks,
    detailPeaks = [],
    detailPeaksStartMs = 0,
    detailPeaksEndMs = 0,
    detailLoading = false,
    windowMs = 60_000,
    onseek,
    onboundaryinput = () => {},
    onboundarycommit = () => {}
  }: {
    durationMs: number;
    currentMs: number;
    keepRanges: MediaEditRange[];
    cutRanges?: MediaEditRange[];
    peaks: number[];
    detailPeaks?: number[];
    detailPeaksStartMs?: number;
    detailPeaksEndMs?: number;
    detailLoading?: boolean;
    windowMs?: number;
    onseek: (timeMs: number) => void;
    onboundaryinput?: (
      rangeId: string,
      edge: 'start_ms' | 'end_ms',
      timeMs: number
    ) => void;
    onboundarycommit?: (
      rangeId: string,
      edge: 'start_ms' | 'end_ms',
      timeMs: number
    ) => void;
  } = $props();

  let overview = $state<HTMLCanvasElement>();
  let detail = $state<HTMLCanvasElement>();
  let observer: ResizeObserver | undefined;
  let activeBoundary = $state('');
  const boundaryEdges: ('start_ms' | 'end_ms')[] = ['start_ms', 'end_ms'];
  let drag:
    | {
        pointerId: number;
        rangeId: string;
        edge: 'start_ms' | 'end_ms';
        canvas: HTMLCanvasElement;
        startMs: number;
        endMs: number;
        originClientX: number;
        originTimeMs: number;
        timeMs: number;
      }
    | undefined;

  function isKept(timeMs: number) {
    return keepRanges.some(
      (range) => timeMs >= range.start_ms && timeMs < range.end_ms
    );
  }

  function formatAxis(timeMs: number) {
    const totalSeconds = Math.max(0, Math.floor(timeMs / 1000));
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    return `${hours ? `${hours}:` : ''}${String(minutes).padStart(hours ? 2 : 1, '0')}:${String(seconds).padStart(2, '0')}`;
  }

  function drawWaveform(
    canvas: HTMLCanvasElement | undefined,
    startMs: number,
    endMs: number,
    values: number[],
    valuesStartMs: number,
    valuesEndMs: number
  ) {
    if (!canvas || durationMs <= 0) return;
    const width = Math.max(1, canvas.clientWidth);
    const height = Math.max(1, canvas.clientHeight);
    const scale = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(height * scale);
    const context = canvas.getContext('2d');
    if (!context) return;
    context.scale(scale, scale);
    context.clearRect(0, 0, width, height);
    context.fillStyle = getComputedStyle(canvas).getPropertyValue('--paper');
    context.fillRect(0, 0, width, height);

    const span = Math.max(1, endMs - startMs);
    const valuesSpan = Math.max(1, valuesEndMs - valuesStartMs);
    const peakStart = Math.floor(
      ((startMs - valuesStartMs) / valuesSpan) * values.length
    );
    const peakEnd = Math.ceil(
      ((endMs - valuesStartMs) / valuesSpan) * values.length
    );
    const visiblePeaks = values.slice(
      Math.max(0, peakStart),
      Math.min(values.length, Math.max(peakStart + 1, peakEnd))
    );
    context.strokeStyle = getComputedStyle(canvas).getPropertyValue('--muted');
    context.globalAlpha = 0.7;
    context.lineWidth = 1;
    context.beginPath();
    for (let x = 0; x < width; x += 1) {
      const index = Math.min(
        visiblePeaks.length - 1,
        Math.floor((x / width) * visiblePeaks.length)
      );
      const value = Math.max(0.015, Number(visiblePeaks[index] ?? 0));
      context.moveTo(x + 0.5, height / 2 - value * (height / 2 - 4));
      context.lineTo(x + 0.5, height / 2 + value * (height / 2 - 4));
    }
    context.stroke();
    context.globalAlpha = 1;

    for (let x = 0; x < width; x += 1) {
      const time = startMs + (x / width) * span;
      if (isKept(time)) continue;
      context.fillStyle = 'rgba(220, 75, 75, .22)';
      context.fillRect(x, 0, 1, height);
    }

    if (currentMs >= startMs && currentMs <= endMs) {
      const x = ((currentMs - startMs) / span) * width;
      context.strokeStyle =
        getComputedStyle(canvas).getPropertyValue('--accent');
      context.lineWidth = 2;
      context.beginPath();
      context.moveTo(x, 0);
      context.lineTo(x, height);
      context.stroke();
    }
  }

  const hasDetail = $derived(
    detailPeaks.length > 0 && detailPeaksEndMs > detailPeaksStartMs
  );
  const fallbackDetailStart = $derived(
    Math.max(
      0,
      Math.min(
        durationMs - Math.min(windowMs, durationMs),
        currentMs - windowMs / 2
      )
    )
  );
  const detailStart = $derived(
    hasDetail ? detailPeaksStartMs : fallbackDetailStart
  );
  const detailEnd = $derived(
    hasDetail
      ? detailPeaksEndMs
      : Math.min(durationMs, fallbackDetailStart + windowMs)
  );

  function draw() {
    drawWaveform(overview, 0, durationMs, peaks, 0, durationMs);
    drawWaveform(
      detail,
      detailStart,
      detailEnd,
      hasDetail ? detailPeaks : peaks,
      hasDetail ? detailPeaksStartMs : 0,
      hasDetail ? detailPeaksEndMs : durationMs
    );
  }

  function seekFromPointer(
    event: MouseEvent,
    canvas: HTMLCanvasElement,
    startMs: number,
    endMs: number
  ) {
    const bounds = canvas.getBoundingClientRect();
    const fraction = Math.max(
      0,
      Math.min(1, (event.clientX - bounds.left) / Math.max(1, bounds.width))
    );
    onseek(Math.round(startMs + fraction * (endMs - startMs)));
  }

  function positionPercent(timeMs: number, startMs: number, endMs: number) {
    return Math.max(
      0,
      Math.min(100, ((timeMs - startMs) / Math.max(1, endMs - startMs)) * 100)
    );
  }

  function boundaryKey(rangeId: string, edge: 'start_ms' | 'end_ms') {
    return `${rangeId}:${edge}`;
  }

  function beginBoundaryDrag(
    event: PointerEvent,
    rangeId: string,
    edge: 'start_ms' | 'end_ms',
    initialTimeMs: number,
    canvas: HTMLCanvasElement,
    startMs: number,
    endMs: number
  ) {
    event.preventDefault();
    event.stopPropagation();
    const button = event.currentTarget as HTMLButtonElement;
    button.setPointerCapture(event.pointerId);
    drag = {
      pointerId: event.pointerId,
      rangeId,
      edge,
      canvas,
      startMs,
      endMs,
      originClientX: event.clientX,
      originTimeMs: initialTimeMs,
      timeMs: initialTimeMs
    };
    activeBoundary = boundaryKey(rangeId, edge);
    onboundaryinput(rangeId, edge, initialTimeMs);
  }

  function moveBoundary(event: PointerEvent) {
    if (!drag || event.pointerId !== drag.pointerId) return;
    event.preventDefault();
    const bounds = drag.canvas.getBoundingClientRect();
    const timeMs = Math.round(
      drag.originTimeMs +
        ((event.clientX - drag.originClientX) / Math.max(1, bounds.width)) *
          (drag.endMs - drag.startMs)
    );
    drag.timeMs = timeMs;
    onboundaryinput(drag.rangeId, drag.edge, timeMs);
  }

  function finishBoundaryDrag(event: PointerEvent) {
    if (!drag || event.pointerId !== drag.pointerId) return;
    event.preventDefault();
    const completed = drag;
    drag = undefined;
    onboundarycommit(completed.rangeId, completed.edge, completed.timeMs);
  }

  function nudgeBoundary(
    event: KeyboardEvent,
    range: MediaEditRange,
    edge: 'start_ms' | 'end_ms'
  ) {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    const amount =
      (event.shiftKey ? 100 : 20) * (event.key === 'ArrowLeft' ? -1 : 1);
    const timeMs = range[edge] + amount;
    activeBoundary = boundaryKey(range.id, edge);
    onboundaryinput(range.id, edge, timeMs);
    onboundarycommit(range.id, edge, timeMs);
  }

  $effect(() => {
    void durationMs;
    void currentMs;
    void keepRanges;
    void cutRanges;
    void peaks;
    void detailPeaks;
    void detailPeaksStartMs;
    void detailPeaksEndMs;
    void detailLoading;
    void detailStart;
    requestAnimationFrame(draw);
  });

  onMount(() => {
    observer = new ResizeObserver(draw);
    if (overview) observer.observe(overview);
    if (detail) observer.observe(detail);
    draw();
  });
  onDestroy(() => observer?.disconnect());
</script>

<div class="space-y-3">
  <div>
    <div
      class="mb-1 flex items-center justify-between text-[.65rem] font-semibold uppercase tracking-[.12em] text-[var(--muted)]"
    >
      <span>Whole recording</span><span>red = removed</span>
    </div>
    <div class="relative">
      <canvas
        bind:this={overview}
        class="block h-20 w-full cursor-crosshair rounded-xl border border-[var(--line)]"
        aria-label="Whole-recording waveform; click to seek"
        onclick={(event) => seekFromPointer(event, overview!, 0, durationMs)}
      ></canvas>
      {#each cutRanges as range, index (range.id)}
        {#each boundaryEdges as edge}
          <button
            type="button"
            class:active={activeBoundary === boundaryKey(range.id, edge)}
            class="boundary-handle"
            style={`--boundary-position:${positionPercent(range[edge], 0, durationMs)}%`}
            aria-label={`Adjust removal ${index + 1} ${edge === 'start_ms' ? 'start' : 'end'} at ${formatAxis(range[edge])}. Drag, or use arrow keys; hold Shift for 100 milliseconds.`}
            title={`Drag the removal ${edge === 'start_ms' ? 'start' : 'end'} edge · arrows 20 ms · Shift + arrows 100 ms`}
            onfocus={() => (activeBoundary = boundaryKey(range.id, edge))}
            onblur={() => !drag && (activeBoundary = '')}
            onkeydown={(event) => nudgeBoundary(event, range, edge)}
            onpointerdown={(event) =>
              beginBoundaryDrag(
                event,
                range.id,
                edge,
                range[edge],
                overview!,
                0,
                durationMs
              )}
            onpointermove={moveBoundary}
            onpointerup={finishBoundaryDrag}
            onpointercancel={finishBoundaryDrag}
          ></button>
        {/each}
      {/each}
    </div>
    <div
      class="mt-1 flex justify-between text-[.6rem] tabular-nums text-[var(--muted)]"
    >
      <span>{formatAxis(0)}</span>
      <span>{formatAxis(durationMs / 2)}</span>
      <span>{formatAxis(durationMs)}</span>
    </div>
  </div>
  <div>
    <div
      class="mb-1 flex items-center justify-between text-[.65rem] font-semibold uppercase tracking-[.12em] text-[var(--muted)]"
    >
      <span>Playhead detail</span><span
        >{Math.round(windowMs / 1000)} second window</span
      >
    </div>
    <div class="relative">
      <canvas
        bind:this={detail}
        class="block h-28 w-full cursor-crosshair rounded-xl border border-[var(--line)]"
        aria-label="Detailed waveform around the playhead; click to seek"
        aria-busy={detailLoading}
        onclick={(event) =>
          seekFromPointer(event, detail!, detailStart, detailEnd)}
      ></canvas>
      {#each cutRanges as range, index (range.id)}
        {#each boundaryEdges as edge}
          {#if range[edge] >= detailStart && range[edge] <= detailEnd}
            <button
              type="button"
              class:active={activeBoundary === boundaryKey(range.id, edge)}
              class="boundary-handle boundary-handle-detail"
              style={`--boundary-position:${positionPercent(range[edge], detailStart, detailEnd)}%`}
              aria-label={`Adjust removal ${index + 1} ${edge === 'start_ms' ? 'start' : 'end'} at ${formatAxis(range[edge])}. Drag, or use arrow keys; hold Shift for 100 milliseconds.`}
              title={`Drag the removal ${edge === 'start_ms' ? 'start' : 'end'} edge · arrows 20 ms · Shift + arrows 100 ms`}
              onfocus={() => (activeBoundary = boundaryKey(range.id, edge))}
              onblur={() => !drag && (activeBoundary = '')}
              onkeydown={(event) => nudgeBoundary(event, range, edge)}
              onpointerdown={(event) =>
                beginBoundaryDrag(
                  event,
                  range.id,
                  edge,
                  range[edge],
                  detail!,
                  detailStart,
                  detailEnd
                )}
              onpointermove={moveBoundary}
              onpointerup={finishBoundaryDrag}
              onpointercancel={finishBoundaryDrag}
            ></button>
          {/if}
        {/each}
      {/each}
      {#if detailLoading}<div
          class="pointer-events-none absolute inset-x-3 top-3 flex justify-end"
          role="status"
        >
          <span
            class="rounded-full bg-[var(--paper-strong)]/90 px-2.5 py-1 text-[.62rem] font-semibold text-[var(--muted)] shadow-sm"
            >Loading detail…</span
          >
        </div>{/if}
    </div>
    <div
      class="mt-1 flex justify-between text-[.6rem] tabular-nums text-[var(--muted)]"
    >
      <span>{formatAxis(detailStart)}</span>
      <span>{formatAxis((detailStart + detailEnd) / 2)}</span>
      <span>{formatAxis(detailEnd)}</span>
    </div>
  </div>
  <label class="sr-only" for="media-timeline-seek">Seek in recording</label>
  <input
    id="media-timeline-seek"
    type="range"
    min="0"
    max={Math.max(1, durationMs)}
    step="20"
    value={currentMs}
    oninput={(event) =>
      onseek(Number((event.currentTarget as HTMLInputElement).value))}
    class="w-full accent-[var(--accent)]"
  />
</div>

<style>
  .boundary-handle {
    --handle-color: color-mix(in srgb, #dc4b4b 82%, var(--ink));
    position: absolute;
    z-index: 2;
    top: 0;
    bottom: 0;
    left: clamp(0.4rem, var(--boundary-position), calc(100% - 0.4rem));
    width: 0.8rem;
    touch-action: none;
    transform: translateX(-50%);
    cursor: ew-resize;
  }

  .boundary-handle::before {
    position: absolute;
    inset-block: 0;
    left: 50%;
    width: 2px;
    transform: translateX(-50%);
    border-radius: 999px;
    background: var(--handle-color);
    content: '';
    opacity: 0.72;
  }

  .boundary-handle::after {
    position: absolute;
    top: 0.3rem;
    left: 50%;
    width: 0.62rem;
    height: 1.05rem;
    transform: translateX(-50%);
    border: 2px solid var(--paper-strong);
    border-radius: 999px;
    background: var(--handle-color);
    box-shadow: 0 1px 4px rgb(0 0 0 / 0.24);
    content: '';
  }

  .boundary-handle:hover::before,
  .boundary-handle:focus-visible::before,
  .boundary-handle.active::before {
    width: 3px;
    opacity: 1;
  }

  .boundary-handle:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }

  .boundary-handle-detail::after {
    top: 0.45rem;
    width: 0.72rem;
    height: 1.2rem;
  }
</style>
