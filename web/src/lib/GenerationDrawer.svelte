<script lang="ts">
  import {
    ChevronDown,
    ChevronUp,
    BookOpenText,
    Download,
    Flag,
    ListMusic,
    Pause,
    Play,
    RefreshCw,
    RotateCcw,
    Save,
    Sparkles,
    Square,
    Trash2,
    WandSparkles,
    X
  } from '@lucide/svelte';
  import { onMount } from 'svelte';
  import { api } from './api';
  import WaveformPeaks from './WaveformPeaks.svelte';
  import TtsServicesModal from './TtsServicesModal.svelte';
  import AudioPlayer from './AudioPlayer.svelte';

  let { sessionId }: { sessionId: string } = $props();
  let mode = $state<'collapsed' | 'half' | 'full'>('collapsed');
  let payload = $state<any>({ items: [], total: 0, next_cursor: null });
  let run = $state<any>(null);
  let runs = $state<any[]>([]);
  let selectedRunId = $state('');
  let assembly = $state<any>(null);
  let filter = $state<'all' | 'marked' | 'failed' | 'stale' | 'completed' | 'queued'>('all');
  let error = $state('');
  let loading = $state(false);
  let timer: number | undefined;
  let selectedRow = $state('');
  let viewMode = $state<'segments' | 'reading'>('segments');
  let loadedFilter = $state('');
  let playlistAudio: HTMLAudioElement | null = null;
  let playlistResolve: (() => void) | null = null;
  let playlistToken = 0;
  let playlistActive = $state(false);
  let playlistPaused = $state(false);
  let activePlayingId = $state('');
  let rvcModels = $state<string[]>([]);
  let rvcModel = $state('');
  let rvcPitch = $state(0);
  let rvcF0 = $state('rmvpe');
  let rvcIndexRate = $state(0.3);
  let showRvc = $state(false);
  let ttsServicesOpen = $state(false);
  let comparisonItem = $state<any>(null);
  let comparisonText = $state('');
  let regenerateAfterReview = $state(true);
  let initialized = false;

  const marked = $derived(payload.items.filter((item: any) => item.marked).map((item: any) => item.id));
  const selectedRun = $derived(runs.find((item: any) => item.id === selectedRunId) ?? null);
  const selectedAssembly = $derived(selectedRun?.assembly ?? (!selectedRun ? assembly : null));
  const readingBlocks = $derived.by(() => {
    const blocks: { key: string; kind: string; items: any[]; closed?: boolean }[] = [];
    for (const item of payload.items) {
      const standalone = ['heading', 'chapter_marker'].includes(item.node_kind);
      if (standalone) {
        blocks.push({ key: `standalone-${item.id}`, kind: item.node_kind, items: [item], closed: true });
        continue;
      }
      let paragraph = blocks.at(-1);
      if (!paragraph || paragraph.kind !== 'paragraph' || paragraph.closed) {
        paragraph = { key: `paragraph-${item.id}`, kind: 'paragraph', items: [] };
        blocks.push(paragraph);
      }
      paragraph.items.push(item);
      paragraph.closed = Boolean(item.paragraph_break_after);
    }
    return blocks;
  });

  async function load(reset = true, preserveLoaded = reset) {
    try {
      const query = new URLSearchParams({ limit: '100' });
      if (filter === 'marked') query.set('marked', 'true');
      else if (filter !== 'all') query.set('status', filter);
      if (!reset && payload.next_cursor != null) query.set('cursor', String(payload.next_cursor));
      const [next, runPayload, latestAssembly] = await Promise.all([
        api<any>(`/sessions/${sessionId}/generation-segments?${query}`),
        api<any>(`/sessions/${sessionId}/generation-runs`),
        api<any>(`/sessions/${sessionId}/output-assemblies/latest`)
      ]);
      const previousTotal = payload.total;
      const previousRunId = run?.id ?? '';
      if (!reset) {
        const known = new Set(payload.items.map((item: any) => item.id));
        payload = { ...next, items: [...payload.items, ...next.items.filter((item: any) => !known.has(item.id))] };
      } else if (preserveLoaded && loadedFilter === filter && payload.items.length > next.items.length) {
        const incoming = new Map(next.items.map((item: any) => [item.id, item]));
        const lastOrdinal = next.items.at(-1)?.ordinal ?? -1;
        payload = {
          ...next,
          items: [
            ...next.items,
            ...payload.items.filter((item: any) => item.ordinal > lastOrdinal && !incoming.has(item.id))
          ],
          next_cursor: payload.next_cursor
        };
      } else payload = next;
      loadedFilter = filter;
      runs = runPayload.items ?? [];
      run = runs[0] ?? null;
      if (!selectedRunId || !runs.some((item: any) => item.id === selectedRunId)) selectedRunId = run?.id ?? '';
      assembly = latestAssembly.item;
      if (
        initialized
        && (
          (previousTotal === 0 && payload.total > 0)
          || (run?.id && run.id !== previousRunId && ['queued', 'running', 'pausing'].includes(run.status))
        )
      ) mode = 'half';
      initialized = true;
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    }
  }

  async function patchSegment(item: any, changes: Record<string, unknown>) {
    try {
      const updated = await api<any>(`/generation-segments/${item.id}`, {
        method: 'PATCH',
        headers: { 'If-Match': `"${item.revision}"` },
        body: JSON.stringify(changes)
      });
      Object.assign(item, updated);
      payload = { ...payload, items: [...payload.items] };
      if ('node_kind' in changes || 'silence_after_ms' in changes || 'removed' in changes) await refreshAssembly();
      return updated;
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    }
  }

  function openOptimizationReview(item: any) {
    comparisonItem = item;
    comparisonText = String(item.optimized_text ?? activeTake(item)?.synthesized_text ?? item.text ?? '');
  }

  async function saveOptimizationReview() {
    if (!comparisonItem || !comparisonText.trim()) return;
    const item = comparisonItem;
    const updated = await patchSegment(item, { optimized_text: comparisonText.trim() });
    if (!updated) return;
    comparisonItem = null;
    if (regenerateAfterReview) await start('regenerate', [item.id]);
  }

  async function refreshAssembly() {
    try {
      assembly = (await api<any>(`/sessions/${sessionId}/output-assemblies/latest`)).item;
    } catch {
      assembly = null;
    }
  }

  async function selectTake(item: any, takeId: string) {
    const result = await api<any>(`/generation-segments/${item.id}/takes/${takeId}/select`, {
      method: 'POST',
      headers: { 'If-Match': `"${item.revision}"` }
    });
    item.revision = result.revision;
    for (const take of item.takes) take.is_active = take.id === takeId;
    const selectedTake = item.takes.find((take: any) => take.id === takeId);
    if (selectedTake?.generation_run_id) selectedRunId = selectedTake.generation_run_id;
    payload = { ...payload, items: [...payload.items] };
    await refreshAssembly();
  }

  async function start(operation: 'generate' | 'regenerate' | 'rvc' = 'generate', ids: string[] = []) {
    if (operation === 'rvc' && !rvcModel) {
      showRvc = true;
      mode = 'half';
      error = 'Choose an RVC model before converting audio.';
      return;
    }
    loading = true;
    error = '';
    try {
      const run_override = operation === 'rvc'
        ? { rvc: { enabled: true, model: rvcModel, rvc_model: rvcModel, pitch: rvcPitch, f0_method: rvcF0, index_rate: rvcIndexRate, source_run_id: selectedRunId || null } }
        : {};
      run = await api(`/sessions/${sessionId}/generation-runs`, {
        method: 'POST',
        body: JSON.stringify({ operation, segment_ids: ids, run_override })
      });
      selectedRunId = run.id;
      mode = 'half';
      await load();
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    } finally {
      loading = false;
    }
  }

  async function loadRvc() {
    try {
      const result = await api<{ items: string[] }>('/rvc/models');
      rvcModels = result.items ?? [];
      rvcModel ||= rvcModels[0] ?? '';
    } catch {
      rvcModels = [];
    }
  }

  async function action(name: 'pause' | 'resume' | 'cancel') {
    if (!run) return;
    run = await api(`/generation-runs/${run.id}/${name}`, { method: 'POST' });
    await load();
  }

  async function assemble() {
    loading = true;
    error = '';
    try {
      assembly = await api(`/sessions/${sessionId}/output-assemblies`, {
        method: 'POST',
        body: JSON.stringify({ generation_run_id: selectedRunId || null })
      });
      await load();
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    } finally {
      loading = false;
    }
  }

  function activeTake(item: any) {
    if (selectedRun) {
      const sequences = new Map(runs.map((item: any) => [item.id, Number(item.sequence_number || 0)]));
      const targetSequence = Number(selectedRun.sequence_number || 0);
      const candidates = (item.takes ?? [])
        .filter((take: any) => take.artifact_id && ['completed', 'stale'].includes(take.status) && take.generation_run_id && Number(sequences.get(take.generation_run_id) ?? Number.POSITIVE_INFINITY) <= targetSequence)
        .sort((left: any, right: any) => Number(sequences.get(right.generation_run_id) ?? 0) - Number(sequences.get(left.generation_run_id) ?? 0) || String(right.created_at).localeCompare(String(left.created_at)));
      if (candidates.length) return candidates[0];
      return item.takes?.find((take: any) => !take.generation_run_id && take.is_active && take.artifact_id)
        ?? item.takes?.find((take: any) => !take.generation_run_id && take.artifact_id);
    }
    return item.takes?.find((take: any) => take.is_active && take.artifact_id)
      ?? item.takes?.find((take: any) => !take.generation_run_id && take.artifact_id);
  }

  function takeLabel(take: any) {
    const owner = runs.find((item: any) => item.id === take.generation_run_id);
    return owner ? `${owner.label} · ${String(take.kind || 'audio').toUpperCase()}` : `Legacy take · ${String(take.kind || 'audio').toUpperCase()}`;
  }

  async function deleteSelectedRun() {
    if (!selectedRun || ['queued', 'running', 'pausing', 'cancel_requested'].includes(selectedRun.status)) return;
    if (!window.confirm(`Delete ${selectedRun.label} and all audio takes created by it?`)) return;
    loading = true;
    error = '';
    try {
      await api(`/generation-runs/${selectedRun.id}`, { method: 'DELETE' });
      selectedRunId = '';
      await load(true, false);
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    } finally {
      loading = false;
    }
  }

  function stopPlayback() {
    playlistToken += 1;
    playlistAudio?.pause();
    playlistAudio = null;
    playlistResolve?.();
    playlistResolve = null;
    playlistActive = false;
    playlistPaused = false;
    activePlayingId = '';
  }

  function togglePlaylistPause() {
    if (!playlistActive) return;
    playlistPaused = !playlistPaused;
    if (playlistPaused) playlistAudio?.pause();
    else playlistAudio?.play().catch(() => { error = 'Playback could not be resumed.'; });
  }

  function togglePlaylistPlayback() {
    if (!playlistActive) {
      void playFromSelection();
      return;
    }
    togglePlaylistPause();
  }

  function readingSegmentText(item: any) {
    return String(activeTake(item)?.synthesized_text || item.optimized_text || item.text || '').replace(/\s+/g, ' ').trim();
  }

  async function waitForSilence(milliseconds: number, token: number) {
    let remaining = Math.max(0, milliseconds);
    let previous = performance.now();
    while (remaining > 0 && token === playlistToken) {
      await new Promise((resolve) => window.setTimeout(resolve, Math.min(remaining, 50)));
      const now = performance.now();
      if (!playlistPaused) remaining -= now - previous;
      previous = now;
    }
  }

  async function playTake(item: any, token: number) {
    const take = activeTake(item);
    if (!take || item.removed) return;
    activePlayingId = item.id;
    selectedRow = item.id;
    await new Promise<void>((resolve) => {
      playlistResolve = resolve;
      playlistAudio = new Audio(`/api/v1/artifacts/${take.artifact_id}/content`);
      playlistAudio.onended = () => resolve();
      playlistAudio.onerror = () => resolve();
      playlistAudio.play().catch(resolve);
    });
    playlistResolve = null;
    playlistAudio = null;
    if (token === playlistToken) await waitForSilence(Number(item.silence_after_ms || 0), token);
  }

  async function playFromSelection(startId = selectedRow) {
    stopPlayback();
    const token = playlistToken;
    playlistActive = true;
    let index = Math.max(0, payload.items.findIndex((item: any) => item.id === startId));
    while (token === playlistToken) {
      if (index >= payload.items.length) {
        if (payload.next_cursor == null) break;
        const previousLength = payload.items.length;
        await load(false);
        if (payload.items.length === previousLength) break;
      }
      const item = payload.items[index++];
      if (item) await playTake(item, token);
    }
    if (token === playlistToken) stopPlayback();
  }

  async function playOnly(item: any) {
    stopPlayback();
    const token = playlistToken;
    playlistActive = true;
    await playTake(item, token);
    if (token === playlistToken) stopPlayback();
  }

  function keyboard(event: KeyboardEvent) {
    const index = payload.items.findIndex((item: any) => item.id === selectedRow);
    if (event.key === 'ArrowDown') {
      selectedRow = payload.items[Math.min(payload.items.length - 1, index + 1)]?.id ?? '';
      event.preventDefault();
    } else if (event.key === 'ArrowUp') {
      selectedRow = payload.items[Math.max(0, index - 1)]?.id ?? '';
      event.preventDefault();
    } else if (event.key === ' ' && index >= 0) {
      patchSegment(payload.items[index], { marked: !payload.items[index].marked });
      event.preventDefault();
    } else if (event.key === 'Delete' && index >= 0) {
      patchSegment(payload.items[index], { removed: !payload.items[index].removed });
      event.preventDefault();
    }
  }

  onMount(() => {
    const refresh = () => load(true, true);
    loadRvc();
    window.addEventListener('pandrator:generation-changed', refresh);
    return () => {
      window.removeEventListener('pandrator:generation-changed', refresh);
      if (timer) window.clearTimeout(timer);
      stopPlayback();
    };
  });
  $effect(() => { filter; load(true, false); });
  $effect(() => {
    if (timer) clearTimeout(timer);
    if ((run && ['queued', 'running', 'pausing', 'cancel_requested'].includes(run.status)) || (selectedAssembly && ['queued', 'running'].includes(selectedAssembly.status))) {
      timer = window.setTimeout(() => load(true, true), 1200);
    }
  });
</script>

{#if payload.total > 0 || run}
  <aside class:full={mode === 'full'} class:half={mode === 'half'} class="generation-drawer fixed inset-x-3 bottom-3 z-50 overflow-hidden rounded-2xl md:left-[calc(var(--sidebar-offset,5rem)+.75rem)]">
    <header class="flex flex-wrap items-center gap-3 border-b border-[var(--line)] px-4 py-3">
      <button onclick={() => mode = mode === 'collapsed' ? 'half' : 'collapsed'} class="flex items-center gap-2 font-semibold">
        {#if mode === 'collapsed'}<ChevronUp size={17} />{:else}<ChevronDown size={17} />{/if}
        Generation
      </button>
      <span class="muted text-xs">{payload.total} segments · {selectedRun?.label ?? 'No run selected'}{#if selectedAssembly} · output {selectedAssembly.status}{/if}</span>
      {#if run && ['queued', 'running', 'pausing', 'cancel_requested'].includes(run.status)}
        <div class="run-progress" aria-label={`Generation progress ${Math.round((run.progress ?? 0) * 100)} percent`}>
          <span style={`width:${Math.max(1, (run.progress ?? 0) * 100)}%`}></span>
        </div>
        <span class="muted text-[.65rem]">{Math.round((run.progress ?? 0) * 100)}%</span>
      {/if}
      <div class="header-playback flex items-center gap-1">
        <button
          onclick={togglePlaylistPlayback}
          class:active={playlistActive}
          class="action"
          title={playlistActive ? (playlistPaused ? 'Resume playlist' : 'Pause playlist') : 'Play from the selected segment'}
          aria-label={playlistActive ? (playlistPaused ? 'Resume playlist' : 'Pause playlist') : 'Play as playlist'}
        >
          {#if playlistActive && !playlistPaused}<Pause size={14}/>{:else}<Play size={14}/>{/if}
          {playlistActive ? (playlistPaused ? 'Resume' : 'Pause') : 'Play as playlist'}
        </button>
        {#if playlistActive}
          <button onclick={stopPlayback} class="action icon-action" title="Stop playlist" aria-label="Stop playlist"><Square size={14}/></button>
        {/if}
      </div>
      <div class="ml-auto flex flex-wrap gap-2">
        {#if !run || ['completed', 'failed', 'canceled'].includes(run.status)}
          <button onclick={() => start()} class="action primary"><Play size={14} /> Start</button>
        {:else if run.status === 'paused'}
          <button onclick={() => action('resume')} class="action primary"><Play size={14} /> Resume</button>
        {:else if ['queued', 'running'].includes(run.status)}
          <button onclick={() => action('pause')} class="action"><Pause size={14} /> Stop safely</button>
          <button onclick={() => action('cancel')} class="action text-red-500"><Square size={14} /> Cancel</button>
        {/if}
        {#if mode !== 'collapsed'}
          <button onclick={() => mode = mode === 'full' ? 'half' : 'full'} class="action">{mode === 'full' ? 'Half height' : 'Full height'}</button>
        {/if}
      </div>
    </header>

    {#if mode !== 'collapsed'}
      <div class="flex h-[calc(100%-3.8rem)] min-h-0 flex-col">
        <div class="flex flex-wrap items-center gap-2 border-b border-[var(--line)] p-3">
          {#if runs.length}
            <label class="run-picker flex items-center gap-2 text-xs font-semibold">Version
              <select bind:value={selectedRunId} class="mini max-w-[22rem]">
                {#each runs as item}<option value={item.id}>{item.label} · {item.status}</option>{/each}
              </select>
            </label>
            <button onclick={deleteSelectedRun} disabled={loading || !selectedRun || ['queued','running','pausing','cancel_requested'].includes(selectedRun.status)} class="action text-red-500" title="Delete the selected run and its generated takes"><Trash2 size={14}/> Delete run</button>
            <span class="h-6 w-px bg-[var(--line)]"></span>
          {/if}
          {#each ['all', 'completed', 'queued', 'marked', 'failed', 'stale'] as value}
            <button onclick={() => filter = value as typeof filter} class:active={filter === value} class="filter capitalize">{value === 'completed' ? 'generated' : value}</button>
          {/each}
          <div class="view-switch" aria-label="Generation review view">
            <button onclick={() => viewMode='segments'} class:active={viewMode==='segments'}><ListMusic size={13}/> Segments</button>
            <button onclick={() => viewMode='reading'} class:active={viewMode==='reading'}><BookOpenText size={13}/> Reading</button>
          </div>
          <button onkeydown={keyboard} class="action" title="Focus, then use arrows, Space to mark, and Delete to remove">Keyboard navigation</button>
          <button onclick={() => start('regenerate', marked)} disabled={!marked.length} class="action"><RefreshCw size={14} /> Regenerate marked</button>
          <button onclick={assemble} disabled={loading || selectedAssembly?.status === 'queued' || selectedAssembly?.status === 'running'} class="action primary">
            <Sparkles size={14} /> {selectedAssembly?.status === 'stale' ? 'Reassemble output' : 'Assemble output'}
          </button>
          <a href={`/sessions/${sessionId}/output`} class="action">Output settings</a>
          <button onclick={() => ttsServicesOpen = true} class="action">Speech services</button>
        </div>

        {#if selectedAssembly?.status === 'completed' && selectedAssembly.artifact_id}
          <div class="flex flex-wrap items-center gap-3 border-b border-[var(--line)] bg-[var(--accent-soft)] px-4 py-2">
            <strong class="text-xs">Assembled output</strong>
            <div class="min-w-64 flex-1"><AudioPlayer src={`/api/v1/artifacts/${selectedAssembly.artifact_id}/content`} label="Assembled output"/></div>
            <a class="action" download href={`/api/v1/artifacts/${selectedAssembly.artifact_id}/content`}><Download size={14} /> Download</a>
          </div>
        {:else if selectedAssembly?.status === 'stale'}
          <div class="border-b border-amber-400/30 bg-amber-500/10 px-4 py-2 text-xs text-amber-700">The output is out of date because segment order, chapter boundaries, silence, or selected takes changed. Reassemble to apply the changes.</div>
        {:else if selectedAssembly?.status === 'failed'}
          <div class="border-b border-red-400/30 bg-red-500/10 px-4 py-2 text-xs text-red-600">Assembly failed: {selectedAssembly.error_message}</div>
        {/if}

        {#if run?.status === 'failed'}
          <div class="border-b border-red-400/30 bg-red-500/10 px-4 py-2 text-xs text-red-600">Generation failed: {run.error_message || 'Open Activity & logs for details, then retry.'}</div>
        {/if}

        <div class="border-b border-[var(--line)] px-3 py-2">
          <button onclick={() => showRvc = !showRvc} class="action"><WandSparkles size={14} /> RVC speech-to-speech {showRvc ? 'settings ▲' : 'settings ▼'}</button>
          {#if showRvc}
            <div class="mt-2 flex flex-wrap items-end gap-2 rounded-xl bg-[var(--accent-soft)] p-3">
              <label class="text-xs font-semibold">Model
                <select bind:value={rvcModel} class="mini ml-2"><option value="">Choose a model</option>{#each rvcModels as item}<option value={item}>{item}</option>{/each}</select>
              </label>
              <label class="text-xs font-semibold">Pitch <input type="number" min="-24" max="24" bind:value={rvcPitch} class="mini ml-2 w-16" /></label>
              <label class="text-xs font-semibold">Detector
                <select bind:value={rvcF0} class="mini ml-2"><option value="rmvpe">RMVPE</option><option value="harvest">Harvest</option><option value="crepe">CREPE</option><option value="pm">PM</option></select>
              </label>
              <label class="text-xs font-semibold">Index rate <input type="number" min="0" max="1" step="0.05" bind:value={rvcIndexRate} class="mini ml-2 w-20" /></label>
              <button onclick={() => start('rvc', selectedRow ? [selectedRow] : [])} disabled={!selectedRow || !rvcModel} class="action">RVC selected</button>
              <button onclick={() => start('rvc', marked)} disabled={!marked.length || !rvcModel} class="action">RVC marked</button>
              <button onclick={() => start('rvc', [])} disabled={!rvcModel} class="action">RVC all</button>
              <a href="/rvc" class="action">Manage models</a>
            </div>
          {/if}
        </div>

        {#if error}<p class="p-3 text-sm text-red-500">{error}</p>{/if}

        <div class="min-h-0 flex-1 overflow-auto">
          {#if viewMode === 'segments'}
          <table class="w-full border-collapse text-sm">
            <thead class="sticky top-0 z-10 bg-[var(--paper-strong)]">
              <tr><th class="w-12">Mark</th><th class="w-14">#</th><th class="text-left">Generation text and delivery</th><th class="w-52">Audio take</th><th class="w-24">Status</th><th class="w-24"></th></tr>
            </thead>
            <tbody>
              {#each payload.items as item}
                <tr onclick={() => selectedRow = item.id} class:selected={selectedRow === item.id} class:removed={item.removed}>
                  <td><input type="checkbox" checked={item.marked} onchange={(event) => patchSegment(item, { marked: (event.currentTarget as HTMLInputElement).checked })} /></td>
                  <td class="muted font-mono text-xs">{item.ordinal + 1}</td>
                  <td>
                    <textarea value={item.text} onblur={(event) => { const text = (event.currentTarget as HTMLTextAreaElement).value; if (text !== item.text) patchSegment(item, { text }); }} rows="2" class="w-full resize-y rounded-lg border border-transparent bg-transparent p-2 focus:border-[var(--line)]"></textarea>
                    {#if item.optimized_text || activeTake(item)?.llm_optimized}
                      <button onclick={(event) => { event.stopPropagation(); openOptimizationReview(item); }} class="mb-2 flex max-w-full items-center gap-1.5 rounded-lg bg-[var(--accent-soft)] px-2.5 py-1.5 text-left text-[.68rem] font-semibold text-[var(--accent)]"><WandSparkles size={12}/><span class="truncate">Compare speech optimization</span><span class="rounded-full bg-[var(--paper)] px-1.5 py-0.5 text-[.58rem] uppercase">{item.optimization_status ?? 'generated'}</span></button>
                    {/if}
                    <div class="flex flex-wrap gap-2">
                      <select value={item.node_kind ?? 'paragraph'} onchange={(event) => patchSegment(item, { node_kind: (event.currentTarget as HTMLSelectElement).value })} aria-label="Segment role" class="mini">
                        <option value="paragraph">Paragraph</option>
                        <option value="heading">Heading</option>
                        <option value="chapter_marker">Chapter start</option>
                        <option value="subtitle_cue">Subtitle cue</option>
                      </select>
                      <input value={item.voice_id ?? ''} onblur={(event) => patchSegment(item, { voice_id: (event.currentTarget as HTMLInputElement).value || null })} placeholder="Voice" class="mini" />
                      <input value={item.language ?? ''} onblur={(event) => patchSegment(item, { language: (event.currentTarget as HTMLInputElement).value || null })} placeholder="Language" class="mini" />
                      <label class="mini flex items-center gap-1">Pause <input type="number" min="0" value={item.silence_after_ms} onblur={(event) => patchSegment(item, { silence_after_ms: Number((event.currentTarget as HTMLInputElement).value) })} aria-label="Silence after in milliseconds" class="w-20 bg-transparent" /> ms</label>
                      <label class="mini flex items-center gap-1"><input type="checkbox" checked={item.paragraph_break_after} onchange={(event) => patchSegment(item, { paragraph_break_after: (event.currentTarget as HTMLInputElement).checked })}/> Paragraph end</label>
                    </div>
                  </td>
                  <td>
                    {#if activeTake(item)}
                      <AudioPlayer compact preload="none" src={`/api/v1/artifacts/${activeTake(item).artifact_id}/content`} label={`Segment ${item.ordinal + 1}`}/>
                      <WaveformPeaks artifactId={activeTake(item).artifact_id} />
                      <select value={activeTake(item).id} onchange={(event) => selectTake(item, (event.currentTarget as HTMLSelectElement).value)} class="mini mt-1 w-full">
                        {#each item.takes as take}<option value={take.id}>{takeLabel(take)} · {take.status}</option>{/each}
                      </select>
                    {:else}<span class="muted text-xs">Not generated</span>{/if}
                  </td>
                  <td><span class="status">{item.status}</span></td>
                  <td><div class="flex justify-center gap-1"><button onclick={(event) => { event.stopPropagation(); start('regenerate', [item.id]); }} disabled={loading || item.removed} class="action icon-action" title="Regenerate this segment" aria-label={`Regenerate segment ${item.ordinal + 1}`}><RefreshCw size={14}/></button><button onclick={(event) => { event.stopPropagation(); patchSegment(item, { removed: !item.removed }); }} class="action icon-action" aria-label={item.removed ? 'Restore segment' : 'Remove segment'}>{#if item.removed}<RotateCcw size={14} />{:else}<Trash2 size={14} />{/if}</button></div></td>
                </tr>
              {/each}
            </tbody>
          </table>
          {:else}
            <div class="reading-view mx-auto max-w-4xl px-5 py-7 sm:px-8">
              <div class="mb-7 flex flex-wrap items-end justify-between gap-3 border-b border-[var(--line)] pb-4"><div><div class="eyebrow">Continuous review</div><h3 class="mt-1 text-xl font-semibold">Narration text</h3><p class="muted mt-1 text-xs">Reviewing {selectedRun?.label ?? 'the active takes'}. Select any sentence to hear that version; paragraphs are separated only at saved paragraph boundaries.</p></div><span class="muted text-xs">Loaded {payload.items.length} of {payload.total}</span></div>
              {#each readingBlocks as block (block.key)}
                {#if ['heading','chapter_marker'].includes(block.kind)}
                  <h4 class:now-playing={block.items.some((item)=>item.id===activePlayingId)} class="reading-heading">
                    {#each block.items as item}<button onclick={() => playOnly(item)} disabled={!activeTake(item) || item.removed}>{item.text}</button>{/each}
                  </h4>
                {:else}
                  <p class="reading-paragraph">
                    {#each block.items as item, index}
                      <span class:now-playing={item.id===activePlayingId} class:selected-sentence={item.id===selectedRow} class:removed={item.removed} class="reading-segment">
                        <button onclick={() => { selectedRow=item.id; if (activeTake(item) && !item.removed) playOnly(item); }} class="reading-sentence" title={activeTake(item)?`Play segment ${item.ordinal + 1}`:'Select segment actions'}>{readingSegmentText(item)}</button>
                        <span class="reading-actions" aria-label={`Actions for segment ${item.ordinal + 1}`}>
                          <button onclick={(event)=>{event.stopPropagation();playOnly(item)}} disabled={!activeTake(item)||item.removed} title="Play segment" aria-label={`Play segment ${item.ordinal + 1}`}><Play size={13}/></button>
                          <button onclick={(event)=>{event.stopPropagation();start('regenerate',[item.id])}} disabled={loading||item.removed} title="Regenerate segment" aria-label={`Regenerate segment ${item.ordinal + 1}`}><RefreshCw size={13}/></button>
                          <button onclick={(event)=>{event.stopPropagation();patchSegment(item,{marked:!item.marked})}} class:active={item.marked} title={item.marked?'Unmark segment':'Mark for bulk regeneration'} aria-label={item.marked?`Unmark segment ${item.ordinal + 1}`:`Mark segment ${item.ordinal + 1}`}><Flag size={13}/></button>
                          <button onclick={(event)=>{event.stopPropagation();patchSegment(item,{removed:!item.removed})}} title={item.removed?'Restore segment':'Remove segment'} aria-label={item.removed?`Restore segment ${item.ordinal + 1}`:`Remove segment ${item.ordinal + 1}`}>{#if item.removed}<RotateCcw size={13}/>{:else}<Trash2 size={13}/>{/if}</button>
                        </span>
                      </span>{#if index < block.items.length - 1}{' '}{/if}
                    {/each}
                  </p>
                {/if}
              {/each}
              {#if !readingBlocks.length}<p class="muted py-16 text-center">No segments match this filter.</p>{/if}
            </div>
          {/if}
          {#if payload.next_cursor != null}<button onclick={() => load(false)} class="m-4 w-[calc(100%-2rem)] rounded-xl border border-[var(--line)] py-2 text-sm font-semibold">Load more</button>{/if}
        </div>
      </div>
    {/if}
  </aside>
{/if}
{#if ttsServicesOpen}<TtsServicesModal onclose={() => ttsServicesOpen=false}/>{/if}
{#if comparisonItem}
  <div class="fixed inset-0 z-[95] grid place-items-center bg-black/55 p-3 backdrop-blur-sm" role="presentation" onclick={(event)=>event.target===event.currentTarget&&(comparisonItem=null)}>
    <div class="comparison-modal flex max-h-[92vh] w-full max-w-5xl flex-col overflow-hidden rounded-3xl" role="dialog" aria-modal="true" aria-labelledby="segment-optimization-title">
      <header class="flex items-start gap-4 border-b border-[var(--line)] px-5 py-4"><div class="min-w-0 flex-1"><div class="eyebrow">Generation segment {comparisonItem.ordinal + 1}</div><h2 id="segment-optimization-title" class="mt-1 text-xl font-semibold">Review speech optimization</h2><p class="muted mt-1 text-xs">The source remains unchanged. Saving the optimized delivery marks existing takes stale.</p></div><button onclick={()=>comparisonItem=null} class="rounded-xl p-2" aria-label="Close"><X size={20}/></button></header>
      <div class="grid min-h-0 flex-1 gap-4 overflow-auto p-5 md:grid-cols-2"><section><h3 class="mb-2 text-xs font-bold uppercase tracking-wider text-[var(--muted)]">Original generation text</h3><div class="h-full min-h-44 rounded-2xl border border-[var(--line)] bg-[var(--paper)] p-4 text-sm leading-7">{comparisonItem.text}</div></section><section><h3 class="mb-2 text-xs font-bold uppercase tracking-wider text-[var(--muted)]">Optimized delivery · editable</h3><textarea bind:value={comparisonText} class="h-full min-h-44 w-full resize-y rounded-2xl border border-[var(--line)] bg-[var(--paper)] p-4 text-sm leading-7"></textarea></section></div>
      <footer class="flex flex-wrap items-center justify-end gap-3 border-t border-[var(--line)] px-5 py-4"><label class="mr-auto flex items-center gap-2 text-xs font-semibold"><input type="checkbox" bind:checked={regenerateAfterReview} class="accent-[var(--accent)]"/> Regenerate this segment after saving</label><button onclick={()=>comparisonItem=null} class="action">Cancel</button><button onclick={saveOptimizationReview} disabled={!comparisonText.trim()} class="action primary"><Save size={14}/> Save review</button></footer>
    </div>
  </div>
{/if}

<style>
  .generation-drawer{height:3.9rem;border:1px solid var(--line);background:var(--paper-strong);box-shadow:0 18px 55px color-mix(in srgb,var(--ink) 18%,transparent);transition:height .18s ease}.generation-drawer.half{height:min(52vh,38rem)}.generation-drawer.full{height:calc(100vh - 1.5rem)}
  .run-progress{height:.34rem;width:min(9rem,18vw);overflow:hidden;border-radius:999px;background:var(--line)}.run-progress span{display:block;height:100%;border-radius:inherit;background:var(--accent);transition:width .2s ease}
  .comparison-modal{border:1px solid var(--line);background:var(--paper-strong);box-shadow:0 22px 70px rgba(0,0,0,.25)}
  .action{display:flex;align-items:center;gap:.35rem;border:1px solid var(--line);border-radius:.55rem;padding:.4rem .6rem;font-size:.7rem;font-weight:700}.action.primary{background:var(--action-bg);color:white}.action.primary:hover{background:var(--action-hover)}.action:disabled{opacity:.35}
  .icon-action{padding:.42rem}
  .filter{border-radius:.5rem;padding:.4rem .65rem;color:var(--muted);font-size:.72rem;font-weight:650}.filter.active{background:var(--accent-soft);color:var(--ink)}
  .view-switch{display:flex;border:1px solid var(--line);border-radius:.6rem;background:var(--paper);padding:.15rem}.view-switch button{display:flex;align-items:center;gap:.3rem;border-radius:.45rem;padding:.3rem .5rem;font-size:.68rem;font-weight:700;color:var(--muted)}.view-switch button.active{background:var(--accent-soft);color:var(--ink)}
  th,td{border-bottom:1px solid var(--line);padding:.55rem;text-align:center;vertical-align:middle}tr.removed{opacity:.42}tr.selected{background:var(--accent-soft)}
  .status{font-size:.68rem;text-transform:uppercase;color:var(--muted)}.mini{border:1px solid var(--line);border-radius:.45rem;background:var(--paper);padding:.3rem .45rem;font-size:.68rem}
  .reading-view{font-family:Georgia,'Times New Roman',serif}.reading-heading{margin:2rem 0 .75rem;font-size:1.32rem;font-weight:700;line-height:1.35}.reading-heading button{text-align:left}.reading-heading.now-playing button{color:var(--accent)}
  .reading-paragraph{margin:0 0 1.2rem;font-size:1.02rem;line-height:1.9;white-space:normal}.reading-segment{position:relative;display:inline}.reading-sentence{display:inline;white-space:normal;border-radius:.28rem;padding:.03rem .06rem;text-align:left;transition:background .12s ease,color .12s ease}.reading-segment:hover .reading-sentence,.reading-segment:focus-within .reading-sentence,.reading-segment.selected-sentence .reading-sentence{background:var(--accent-soft)}.reading-segment.now-playing .reading-sentence{background:var(--action-bg);color:white;box-shadow:0 0 0 .16rem color-mix(in srgb,var(--accent) 18%,transparent)}.reading-segment.removed .reading-sentence{text-decoration:line-through;opacity:.42}.reading-actions{position:absolute;bottom:calc(100% + .32rem);left:50%;z-index:25;display:flex;gap:.18rem;border:1px solid var(--line);border-radius:.65rem;background:var(--paper-strong);padding:.22rem;box-shadow:var(--shadow);opacity:0;pointer-events:none;transform:translate(-50%,.25rem);transition:opacity .12s ease,transform .12s ease}.reading-segment:hover .reading-actions,.reading-segment:focus-within .reading-actions{opacity:1;pointer-events:auto;transform:translate(-50%,0)}.reading-actions button{display:grid;height:1.8rem;width:1.8rem;place-items:center;border-radius:.45rem;color:var(--muted)}.reading-actions button:hover:not(:disabled),.reading-actions button:focus-visible,.reading-actions button.active{background:var(--accent-soft);color:var(--accent)}.reading-actions button:disabled{opacity:.35}
  @media(prefers-reduced-motion:reduce){.generation-drawer{transition:none}}
</style>
