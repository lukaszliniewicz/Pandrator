<script lang="ts">
  import {
    ChevronDown,
    ChevronUp,
    Download,
    ListMusic,
    Pause,
    Play,
    RefreshCw,
    RotateCcw,
    Sparkles,
    Square,
    Trash2,
    WandSparkles
  } from '@lucide/svelte';
  import { onMount } from 'svelte';
  import { api } from './api';
  import WaveformPeaks from './WaveformPeaks.svelte';
  import TtsServicesModal from './TtsServicesModal.svelte';

  let { sessionId }: { sessionId: string } = $props();
  let mode = $state<'collapsed' | 'half' | 'full'>('collapsed');
  let payload = $state<any>({ items: [], total: 0, next_cursor: null });
  let run = $state<any>(null);
  let assembly = $state<any>(null);
  let filter = $state<'all' | 'marked' | 'failed' | 'stale' | 'completed' | 'queued'>('all');
  let error = $state('');
  let loading = $state(false);
  let timer: number | undefined;
  let selectedRow = $state('');
  let playing = $state<HTMLAudioElement | null>(null);
  let rvcModels = $state<string[]>([]);
  let rvcModel = $state('');
  let rvcPitch = $state(0);
  let rvcF0 = $state('rmvpe');
  let rvcIndexRate = $state(0.3);
  let showRvc = $state(false);
  let ttsServicesOpen = $state(false);

  const marked = $derived(payload.items.filter((item: any) => item.marked).map((item: any) => item.id));

  async function load(reset = true) {
    try {
      const query = new URLSearchParams({ limit: '100' });
      if (filter === 'marked') query.set('marked', 'true');
      else if (filter !== 'all') query.set('status', filter);
      if (!reset && payload.next_cursor != null) query.set('cursor', String(payload.next_cursor));
      const [next, latestRun, latestAssembly] = await Promise.all([
        api<any>(`/sessions/${sessionId}/generation-segments?${query}`),
        api<any>(`/sessions/${sessionId}/generation-runs/latest`),
        api<any>(`/sessions/${sessionId}/output-assemblies/latest`)
      ]);
      payload = reset ? next : { ...next, items: [...payload.items, ...next.items] };
      run = latestRun.item;
      assembly = latestAssembly.item;
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
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    }
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
        ? { rvc: { enabled: true, model: rvcModel, rvc_model: rvcModel, pitch: rvcPitch, f0_method: rvcF0, index_rate: rvcIndexRate } }
        : {};
      run = await api(`/sessions/${sessionId}/generation-runs`, {
        method: 'POST',
        body: JSON.stringify({ operation, segment_ids: ids, run_override })
      });
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
        body: JSON.stringify({ generation_run_id: run?.id ?? null })
      });
      await load();
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    } finally {
      loading = false;
    }
  }

  function activeTake(item: any) {
    return item.takes?.find((take: any) => take.is_active && take.artifact_id);
  }

  function stopPlayback() {
    playing?.pause();
    playing = null;
  }

  async function playFromSelection() {
    stopPlayback();
    const startIndex = Math.max(0, payload.items.findIndex((item: any) => item.id === selectedRow));
    for (const item of payload.items.slice(startIndex)) {
      const take = activeTake(item);
      if (!take || item.removed) continue;
      await new Promise<void>((resolve) => {
        playing = new Audio(`/api/v1/artifacts/${take.artifact_id}/content`);
        playing.onended = () => resolve();
        playing.onerror = () => resolve();
        playing.play().catch(() => resolve());
      });
      if (!playing) return;
    }
    playing = null;
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

  onMount(() => { load(); loadRvc(); });
  $effect(() => { filter; load(); });
  $effect(() => {
    if (timer) clearTimeout(timer);
    if ((run && ['queued', 'running', 'pausing', 'cancel_requested'].includes(run.status)) || (assembly && ['queued', 'running'].includes(assembly.status))) {
      timer = window.setTimeout(() => load(), 1200);
    }
  });
</script>

{#if payload.total > 0 || run}
  <aside class:full={mode === 'full'} class:half={mode === 'half'} class="generation-drawer surface fixed inset-x-3 bottom-3 z-50 overflow-hidden rounded-2xl md:left-[calc(var(--sidebar-offset,5rem)+.75rem)]">
    <header class="flex flex-wrap items-center gap-3 border-b border-[var(--line)] px-4 py-3">
      <button onclick={() => mode = mode === 'collapsed' ? 'half' : 'collapsed'} class="flex items-center gap-2 font-semibold">
        {#if mode === 'collapsed'}<ChevronUp size={17} />{:else}<ChevronDown size={17} />{/if}
        Generation
      </button>
      <span class="muted text-xs">{payload.total} segments · {run?.status ?? 'ready'}{#if assembly} · output {assembly.status}{/if}</span>
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
          {#each ['all', 'completed', 'queued', 'marked', 'failed', 'stale'] as value}
            <button onclick={() => filter = value as typeof filter} class:active={filter === value} class="filter capitalize">{value === 'completed' ? 'generated' : value}</button>
          {/each}
          <button onkeydown={keyboard} class="action" title="Focus, then use arrows, Space to mark, and Delete to remove">Keyboard navigation</button>
          <button onclick={playFromSelection} class="action ml-auto"><ListMusic size={14} /> Play from selection</button>
          {#if playing}<button onclick={stopPlayback} class="action"><Square size={14} /> Stop playback</button>{/if}
          <button onclick={() => start('regenerate', marked)} disabled={!marked.length} class="action"><RefreshCw size={14} /> Regenerate marked</button>
          <button onclick={assemble} disabled={loading || assembly?.status === 'queued' || assembly?.status === 'running'} class="action primary">
            <Sparkles size={14} /> {assembly?.status === 'stale' ? 'Reassemble output' : 'Assemble output'}
          </button>
          <a href={`/sessions/${sessionId}/output`} class="action">Output settings</a>
          <button onclick={() => ttsServicesOpen = true} class="action">Speech services</button>
        </div>

        {#if assembly?.status === 'completed' && assembly.artifact_id}
          <div class="flex flex-wrap items-center gap-3 border-b border-[var(--line)] bg-[var(--accent-soft)] px-4 py-2">
            <strong class="text-xs">Assembled output</strong>
            <audio controls preload="metadata" src={`/api/v1/artifacts/${assembly.artifact_id}/content`} class="h-8 min-w-64 flex-1"></audio>
            <a class="action" download href={`/api/v1/artifacts/${assembly.artifact_id}/content`}><Download size={14} /> Download</a>
          </div>
        {:else if assembly?.status === 'stale'}
          <div class="border-b border-amber-400/30 bg-amber-500/10 px-4 py-2 text-xs text-amber-700">The output is out of date because segment order, chapter boundaries, silence, or selected takes changed. Reassemble to apply the changes.</div>
        {:else if assembly?.status === 'failed'}
          <div class="border-b border-red-400/30 bg-red-500/10 px-4 py-2 text-xs text-red-600">Assembly failed: {assembly.error_message}</div>
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
          <table class="w-full border-collapse text-sm">
            <thead class="sticky top-0 z-10 bg-[var(--paper-strong)]">
              <tr><th class="w-12">Mark</th><th class="w-14">#</th><th class="text-left">Generation text and delivery</th><th class="w-52">Audio take</th><th class="w-24">Status</th><th class="w-16"></th></tr>
            </thead>
            <tbody>
              {#each payload.items as item}
                <tr onclick={() => selectedRow = item.id} class:selected={selectedRow === item.id} class:removed={item.removed}>
                  <td><input type="checkbox" checked={item.marked} onchange={(event) => patchSegment(item, { marked: (event.currentTarget as HTMLInputElement).checked })} /></td>
                  <td class="muted font-mono text-xs">{item.ordinal + 1}</td>
                  <td>
                    <textarea value={item.text} onblur={(event) => { const text = (event.currentTarget as HTMLTextAreaElement).value; if (text !== item.text) patchSegment(item, { text }); }} rows="2" class="w-full resize-y rounded-lg border border-transparent bg-transparent p-2 focus:border-[var(--line)]"></textarea>
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
                    </div>
                  </td>
                  <td>
                    {#if activeTake(item)}
                      <audio controls preload="none" src={`/api/v1/artifacts/${activeTake(item).artifact_id}/content`} class="h-8 w-48"></audio>
                      <WaveformPeaks artifactId={activeTake(item).artifact_id} />
                      <select value={activeTake(item).id} onchange={(event) => selectTake(item, (event.currentTarget as HTMLSelectElement).value)} class="mini mt-1 w-full">
                        {#each item.takes as take}<option value={take.id}>{take.kind} · {take.status} · {take.id.slice(0, 6)}</option>{/each}
                      </select>
                    {:else}<span class="muted text-xs">Not generated</span>{/if}
                  </td>
                  <td><span class="status">{item.status}</span></td>
                  <td><button onclick={(event) => { event.stopPropagation(); patchSegment(item, { removed: !item.removed }); }} class="action" aria-label={item.removed ? 'Restore segment' : 'Remove segment'}>{#if item.removed}<RotateCcw size={14} />{:else}<Trash2 size={14} />{/if}</button></td>
                </tr>
              {/each}
            </tbody>
          </table>
          {#if payload.next_cursor != null}<button onclick={() => load(false)} class="m-4 w-[calc(100%-2rem)] rounded-xl border border-[var(--line)] py-2 text-sm font-semibold">Load more</button>{/if}
        </div>
      </div>
    {/if}
  </aside>
{/if}
{#if ttsServicesOpen}<TtsServicesModal onclose={() => ttsServicesOpen=false}/>{/if}

<style>
  .generation-drawer{height:3.9rem;transition:height .18s ease}.generation-drawer.half{height:min(52vh,38rem)}.generation-drawer.full{height:calc(100vh - 1.5rem)}
  .action{display:flex;align-items:center;gap:.35rem;border:1px solid var(--line);border-radius:.55rem;padding:.4rem .6rem;font-size:.7rem;font-weight:700}.action.primary{background:var(--accent);color:white}.action:disabled{opacity:.35}
  .filter{border-radius:.5rem;padding:.4rem .65rem;color:var(--muted);font-size:.72rem;font-weight:650}.filter.active{background:var(--accent-soft);color:var(--ink)}
  th,td{border-bottom:1px solid var(--line);padding:.55rem;text-align:center;vertical-align:middle}tr.removed{opacity:.42}tr.selected{background:var(--accent-soft)}
  .status{font-size:.68rem;text-transform:uppercase;color:var(--muted)}.mini{border:1px solid var(--line);border-radius:.45rem;background:var(--paper);padding:.3rem .45rem;font-size:.68rem}
  @media(prefers-reduced-motion:reduce){.generation-drawer{transition:none}}
</style>
