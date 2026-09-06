<script lang="ts">
  import type { MediaEditRange } from './api-models';
  import { onDestroy, onMount } from 'svelte';

  let {
    durationMs,
    currentMs,
    keepRanges,
    peaks,
    detailPeaks = [],
    detailPeaksStartMs = 0,
    detailPeaksEndMs = 0,
    windowMs = 60_000,
    onseek
  }: {
    durationMs: number;
    currentMs: number;
    keepRanges: MediaEditRange[];
    peaks: number[];
    detailPeaks?: number[];
    detailPeaksStartMs?: number;
    detailPeaksEndMs?: number;
    windowMs?: number;
    onseek: (timeMs: number) => void;
  } = $props();

  let overview = $state<HTMLCanvasElement>();
  let detail = $state<HTMLCanvasElement>();
  let observer: ResizeObserver | undefined;

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

  const detailStart = $derived(
    Math.max(
      0,
      Math.min(
        durationMs - Math.min(windowMs, durationMs),
        currentMs - windowMs / 2
      )
    )
  );
  const detailEnd = $derived(Math.min(durationMs, detailStart + windowMs));

  function draw() {
    drawWaveform(overview, 0, durationMs, peaks, 0, durationMs);
    const hasDetail =
      detailPeaks.length > 0 && detailPeaksEndMs > detailPeaksStartMs;
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

  $effect(() => {
    void durationMs;
    void currentMs;
    void keepRanges;
    void peaks;
    void detailPeaks;
    void detailPeaksStartMs;
    void detailPeaksEndMs;
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
    <canvas
      bind:this={overview}
      class="block h-20 w-full cursor-crosshair rounded-xl border border-[var(--line)]"
      aria-label="Whole-recording waveform; click to seek"
      onclick={(event) => seekFromPointer(event, overview!, 0, durationMs)}
    ></canvas>
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
    <canvas
      bind:this={detail}
      class="block h-28 w-full cursor-crosshair rounded-xl border border-[var(--line)]"
      aria-label="Detailed waveform around the playhead; click to seek"
      onclick={(event) =>
        seekFromPointer(event, detail!, detailStart, detailEnd)}
    ></canvas>
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
