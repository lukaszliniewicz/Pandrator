<script lang="ts">
  import { ArrowLeft, Check, Crop, Eraser, Layers3, LoaderCircle, Redo2, RotateCcw, Save, SplitSquareHorizontal, Trash2, Undo2, X } from '@lucide/svelte';
  import { api } from './api';
  import { onMount } from 'svelte';
  import * as pdfjs from 'pdfjs-dist';
  import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
  import GuidedTour from './GuidedTour.svelte';

  pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;

  type Rect = { x0: number; y0: number; x1: number; y1: number };
  type PageInfo = { original_page: number; page_number: number; side: 'left' | 'right'; rotation: number; media_box: Rect; crop_box: Rect; width: number; height: number };
  type Metadata = { page_count: number; first_page_side: 'left' | 'right'; pages: PageInfo[] };
  type Plan = { first_page_side: 'left' | 'right'; crops: { original_page: number; rect: Rect }[]; whiteouts: { original_page: number; rect: Rect; color: number[] }[]; deleted_pages: number[] };

  let { sessionId, source, onclose }: { sessionId: string; source: { id: string; filename: string }; onclose: () => void } = $props();
  let metadata = $state<Metadata | null>(null);
  let document = $state<any>(null);
  let canvas = $state<HTMLCanvasElement>();
  let tourOpen = $state(false);
  const tourSteps = [{section:'PDF',title:'Stacks expose shared margins',body:'Compare translucent all-page, left-page, or right-page stacks. Membership always follows original page identity.'},{section:'PDF',title:'Geometry remains exact',body:'Draw crops or whiteouts on a stack or single page. Coordinates are transformed back into original PDF space.'},{section:'PDF',title:'Edits are reversible',body:'Undo, redo, mark deletions, and change first-page side. Applying edits always creates a derived PDF.'}];
  let loading = $state(true);
  let applying = $state(false);
  let error = $state('');
  let mode = $state<'all' | 'left' | 'right' | 'single' | 'selection'>('all');
  let selectedPage = $state(0);
  let pageSelection = $state('1-2');
  let whiteoutColor = $state('#ffffff');
  let opacity = $state(0.18);
  let tool = $state<'crop' | 'whiteout'>('crop');
  let drawing = $state(false);
  let dragStart = $state<{ x: number; y: number } | null>(null);
  let dragCurrent = $state<{ x: number; y: number } | null>(null);
  let activeViewport = $state<any>(null);
  let activePageIndex = $state(0);
  let plan = $state<Plan>({ first_page_side: 'right', crops: [], whiteouts: [], deleted_pages: [] });
  let undoStack = $state<Plan[]>([]);
  let redoStack = $state<Plan[]>([]);
  let resultMessage = $state('');

  const clone = (value: Plan): Plan => JSON.parse(JSON.stringify(value));

  function pushUndo() {
    undoStack = [...undoStack, clone(plan)].slice(-30);
    redoStack = [];
  }

  function undo() {
    const previous = undoStack.at(-1); if (!previous) return;
    redoStack = [...redoStack, clone(plan)]; plan = previous; undoStack = undoStack.slice(0, -1); render();
  }

  function redo() {
    const next = redoStack.at(-1); if (!next) return;
    undoStack = [...undoStack, clone(plan)]; plan = next; redoStack = redoStack.slice(0, -1); render();
  }

  async function load() {
    loading = true;
    try {
      metadata = await api<Metadata>(`/artifacts/${source.id}/pdf?first_page_side=${plan.first_page_side}`);
      document = await pdfjs.getDocument({ url: `/api/v1/artifacts/${source.id}/content`, withCredentials: true }).promise;
      await render();
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    } finally { loading = false; }
  }

  function visiblePages() {
    if (!metadata) return [];
    if (mode === 'single') return [selectedPage];
    if (mode === 'selection') {
      const pages = new Set<number>();
      for (const part of pageSelection.split(',')) {
        const [first, last] = part.trim().split('-').map((value) => Number(value));
        if (!Number.isFinite(first)) continue;
        for (let page=Math.max(1,first); page<=Math.min(metadata.page_count,Number.isFinite(last)?last:first); page++) pages.add(page-1);
      }
      return [...pages];
    }
    if (mode === 'left' || mode === 'right') return metadata.pages.filter((page) => page.side === mode).map((page) => page.original_page);
    return metadata.pages.map((page) => page.original_page);
  }

  async function render() {
    if (!canvas || !document || !metadata) return;
    const pages = visiblePages().filter((page) => !plan.deleted_pages.includes(page));
    if (!pages.length) return;
    const representative = pages.includes(selectedPage) ? selectedPage : pages[0];
    activePageIndex = representative;
    const first = await document.getPage(representative + 1);
    const baseViewport = first.getViewport({ scale: 1 });
    const scale = Math.min(1.6, 840 / baseViewport.width);
    const viewport = first.getViewport({ scale });
    activeViewport = viewport;
    canvas.width = Math.ceil(viewport.width);
    canvas.height = Math.ceil(viewport.height);
    const context = canvas.getContext('2d')!;
    context.clearRect(0, 0, canvas.width, canvas.height);
    let drawn = 0;
    for (const pageIndex of pages) {
      const page = await document.getPage(pageIndex + 1);
      const pageViewport = page.getViewport({ scale });
      const offscreen = document.createElement ? document.createElement('canvas') : window.document.createElement('canvas');
      offscreen.width = Math.ceil(pageViewport.width); offscreen.height = Math.ceil(pageViewport.height);
      await page.render({ canvasContext: offscreen.getContext('2d')!, viewport: pageViewport }).promise;
      context.globalAlpha = drawn === 0 ? 1 : opacity;
      context.drawImage(offscreen, (canvas.width - offscreen.width) / 2, (canvas.height - offscreen.height) / 2);
      drawn += 1;
    }
    context.globalAlpha = 1;
  }

  function point(event: PointerEvent) {
    if (!canvas) return { x: 0, y: 0 };
    const activeCanvas = canvas;
    const bounds = activeCanvas.getBoundingClientRect();
    return { x: (event.clientX - bounds.left) * activeCanvas.width / bounds.width, y: (event.clientY - bounds.top) * activeCanvas.height / bounds.height };
  }

  function pointerDown(event: PointerEvent) {
    if (loading || !canvas) return; drawing = true; dragStart = point(event); dragCurrent = dragStart; canvas.setPointerCapture(event.pointerId);
  }

  function pointerMove(event: PointerEvent) { if (drawing) dragCurrent = point(event); }

  function pointerUp(event: PointerEvent) {
    if (!drawing || !dragStart || !dragCurrent || !metadata || !activeViewport) return;
    dragCurrent = point(event); drawing = false;
    if (Math.abs(dragCurrent.x - dragStart.x) < 5 || Math.abs(dragCurrent.y - dragStart.y) < 5) { dragStart = null; dragCurrent = null; return; }
    const [ax, ay] = activeViewport.convertToPdfPoint(dragStart.x, dragStart.y);
    const [bx, by] = activeViewport.convertToPdfPoint(dragCurrent.x, dragCurrent.y);
    const activeInfo = metadata.pages[activePageIndex];
    const normalized = {
      x0: (Math.min(ax, bx) - activeInfo.media_box.x0) / activeInfo.width,
      y0: (Math.min(ay, by) - activeInfo.media_box.y0) / activeInfo.height,
      x1: (Math.max(ax, bx) - activeInfo.media_box.x0) / activeInfo.width,
      y1: (Math.max(ay, by) - activeInfo.media_box.y0) / activeInfo.height
    };
    const targetIds = new Set(visiblePages());
    const targets = metadata.pages.filter((page) => targetIds.has(page.original_page));
    pushUndo();
    for (const page of targets) {
      const rect = {
        x0: page.media_box.x0 + normalized.x0 * page.width,
        y0: page.media_box.y0 + normalized.y0 * page.height,
        x1: page.media_box.x0 + normalized.x1 * page.width,
        y1: page.media_box.y0 + normalized.y1 * page.height
      };
      if (tool === 'crop') {
        plan.crops = [...plan.crops.filter((item) => item.original_page !== page.original_page), { original_page: page.original_page, rect }];
      } else {
        const color = [1,3,5].map((offset)=>parseInt(whiteoutColor.slice(offset,offset+2),16)/255);
        plan.whiteouts = [...plan.whiteouts, { original_page: page.original_page, rect, color }];
      }
    }
    dragStart = null; dragCurrent = null;
  }

  function toggleDelete(page: number) {
    pushUndo();
    plan.deleted_pages = plan.deleted_pages.includes(page) ? plan.deleted_pages.filter((item) => item !== page) : [...plan.deleted_pages, page].sort((a,b) => a-b);
    render();
  }

  async function changeSide() {
    pushUndo();
    plan.first_page_side = plan.first_page_side === 'right' ? 'left' : 'right';
    metadata = await api<Metadata>(`/artifacts/${source.id}/pdf?first_page_side=${plan.first_page_side}`);
    await render();
  }

  async function applyEdits() {
    applying = true; error = ''; resultMessage = '';
    try {
      const job = await api<{ id: string }>(`/sessions/${sessionId}/pdf/apply`, {
        method: 'POST', body: JSON.stringify({ source_artifact_id: source.id, ...plan })
      });
      resultMessage = `PDF edit job queued: ${job.id.slice(0, 8)}`;
    } catch (caught) { error = caught instanceof Error ? caught.message : String(caught); }
    finally { applying = false; }
  }

  $effect(() => { opacity; mode; selectedPage; if (document && metadata) render(); });
  onMount(load);
</script>

<div class="fixed inset-0 z-50 flex flex-col bg-[var(--paper)]">
  <button onclick={()=>tourOpen=true} class="surface fixed right-5 bottom-5 z-[70] rounded-full px-4 py-2 text-sm font-semibold">PDF tour</button>
  <header class="flex flex-wrap items-center gap-3 border-b border-[var(--line)] bg-[var(--paper-strong)] px-4 py-3">
    <button onclick={onclose} class="flex items-center gap-2 rounded-lg px-2 py-2 text-sm font-semibold"><ArrowLeft size={17}/> Workspace</button>
    <div class="mx-1 h-7 w-px bg-[var(--line)]"></div><div class="min-w-0 flex-1"><div class="truncate font-semibold">{source.filename}</div><div class="muted text-xs">PDF stack editor · edits create a derived artifact</div></div>
    <div class="flex items-center gap-1 rounded-xl border border-[var(--line)] p-1"><button class:active={mode==='all'} onclick={() => mode='all'} class="tool"><Layers3 size={16}/> All</button><button class:active={mode==='left'} onclick={() => mode='left'} class="tool"><SplitSquareHorizontal size={16}/> Left</button><button class:active={mode==='right'} onclick={() => mode='right'} class="tool"><SplitSquareHorizontal size={16}/> Right</button><button class:active={mode==='single'} onclick={() => mode='single'} class="tool">Single</button><button class:active={mode==='selection'} onclick={() => mode='selection'} class="tool">Range</button></div>
    <button onclick={changeSide} class="tool border border-[var(--line)]">First page: {plan.first_page_side}</button>
    <button onclick={undo} disabled={!undoStack.length} class="tool"><Undo2 size={16}/></button><button onclick={redo} disabled={!redoStack.length} class="tool"><Redo2 size={16}/></button>
    <button onclick={applyEdits} disabled={applying} class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white">{#if applying}<LoaderCircle class="animate-spin" size={16}/>{:else}<Save size={16}/>{/if} Apply edits</button>
  </header>
  <div class="grid min-h-0 flex-1 lg:grid-cols-[14rem_1fr]">
    <aside class="overflow-auto border-r border-[var(--line)] bg-[var(--paper-strong)] p-3">
      <div class="eyebrow mb-3 px-1">Pages</div><div class="grid grid-cols-3 gap-2 lg:grid-cols-2">{#each metadata?.pages ?? [] as page}<button onclick={() => {selectedPage=page.original_page; mode='single';}} class:selected={selectedPage===page.original_page} class:deleted={plan.deleted_pages.includes(page.original_page)} class="page-chip relative rounded-xl border border-[var(--line)] p-3 text-left"><div class="text-lg font-semibold">{page.page_number}</div><div class="muted mt-1 text-[.65rem] uppercase">{page.side}</div><span onclick={(event) => {event.stopPropagation(); toggleDelete(page.original_page)}} onkeydown={(event) => event.key==='Enter' && toggleDelete(page.original_page)} role="button" tabindex="0" aria-label={`Delete page ${page.page_number}`} class="absolute right-1.5 top-1.5 rounded-md p-1 hover:bg-red-500/15"><Trash2 size={13}/></span></button>{/each}</div>
    </aside>
    <main class="relative min-h-0 overflow-auto bg-[color-mix(in_srgb,var(--paper)_88%,#000)] p-5">
      <div class="sticky top-0 z-10 mx-auto mb-4 flex w-fit flex-wrap items-center gap-2 rounded-xl border border-[var(--line)] bg-[var(--paper-strong)]/95 p-2 shadow-lg backdrop-blur"><button class:active={tool==='crop'} onclick={() => tool='crop'} class="tool"><Crop size={16}/> Crop</button><button class:active={tool==='whiteout'} onclick={() => tool='whiteout'} class="tool"><Eraser size={16}/> Whiteout</button>{#if tool==='whiteout'}<label class="muted flex items-center gap-2 px-2 text-xs">Color<input type="color" bind:value={whiteoutColor} class="size-7"/></label>{/if}{#if mode==='selection'}<label class="muted flex items-center gap-2 px-2 text-xs">Pages<input bind:value={pageSelection} oninput={render} placeholder="1-3, 7" class="w-24 rounded-lg border border-[var(--line)] bg-[var(--paper)] px-2 py-1"/></label>{/if}<label class="muted flex items-center gap-2 px-2 text-xs">Stack opacity<input type="range" bind:value={opacity} min="0.05" max="0.5" step="0.01" class="w-24 accent-[var(--accent)]"/></label></div>
      {#if error}<div class="mx-auto mb-4 max-w-3xl rounded-xl border border-red-400/40 bg-red-500/10 p-3 text-sm">{error}</div>{/if}{#if resultMessage}<div class="mx-auto mb-4 flex max-w-3xl items-center gap-2 rounded-xl border border-green-500/30 bg-green-500/10 p-3 text-sm"><Check size={16}/>{resultMessage}</div>{/if}
      <div class="relative mx-auto w-fit shadow-2xl"><canvas bind:this={canvas} onpointerdown={pointerDown} onpointermove={pointerMove} onpointerup={pointerUp} class="block max-h-[calc(100vh-13rem)] max-w-full cursor-crosshair bg-white"></canvas>{#if drawing && dragStart && dragCurrent}<div class:mask={tool==='whiteout'} class="selection-box pointer-events-none absolute" style={`left:${Math.min(dragStart.x,dragCurrent.x)/canvas.width*100}%;top:${Math.min(dragStart.y,dragCurrent.y)/canvas.height*100}%;width:${Math.abs(dragCurrent.x-dragStart.x)/canvas.width*100}%;height:${Math.abs(dragCurrent.y-dragStart.y)/canvas.height*100}%`}></div>{/if}{#if loading}<div class="absolute inset-0 grid place-items-center bg-[var(--paper)]/80"><LoaderCircle class="animate-spin text-[var(--accent)]" size={28}/></div>{/if}</div>
      <p class="muted mx-auto mt-4 max-w-2xl text-center text-xs">Draw on the stack to apply a synchronized {tool} to {mode === 'all' ? 'all pages' : mode === 'single' ? `page ${selectedPage+1}` : `${mode} pages`}. Original pages are never overwritten.</p>
    </main>
  </div>
</div>
<GuidedTour tourId="pdf" steps={tourSteps} bind:open={tourOpen}/>

<style>
  .tool { display:flex;align-items:center;gap:.4rem;border-radius:.6rem;padding:.5rem .7rem;font-size:.75rem;font-weight:650;color:var(--muted); }
  .tool:hover,.tool.active { color:var(--ink);background:var(--accent-soft); }
  .tool:disabled { opacity:.3; }
  .page-chip.selected { border-color:var(--accent);background:var(--accent-soft); }
  .page-chip.deleted { opacity:.35;text-decoration:line-through; }
  .selection-box { border:2px solid var(--accent);background:color-mix(in srgb,var(--accent) 12%,transparent); }
  .selection-box.mask { border-color:#c14343;background:rgb(255 255 255 / .82); }
</style>
