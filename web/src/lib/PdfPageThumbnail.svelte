<script lang="ts">
  import { onMount } from 'svelte';

  let { document, pageIndex }: { document: any; pageIndex: number } = $props();
  let canvas: HTMLCanvasElement;

  onMount(() => {
    let disposed = false;
    const observer = new IntersectionObserver(async ([entry]) => {
      if (!entry?.isIntersecting || disposed || !document) return;
      observer.disconnect();
      const page = await document.getPage(pageIndex + 1);
      if (disposed) return;
      const base = page.getViewport({ scale: 1 });
      const viewport = page.getViewport({ scale: Math.min(0.28, 96 / base.width) });
      canvas.width = Math.ceil(viewport.width);
      canvas.height = Math.ceil(viewport.height);
      await page.render({ canvasContext: canvas.getContext('2d')!, viewport }).promise;
    }, { rootMargin: '240px' });
    observer.observe(canvas);
    return () => { disposed = true; observer.disconnect(); };
  });
</script>

<canvas bind:this={canvas} aria-hidden="true" class="mb-2 block aspect-[.72] w-full rounded-md bg-white object-contain shadow-sm"></canvas>
