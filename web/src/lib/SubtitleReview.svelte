<script lang="ts">
  import { Columns3, Filter, Merge, Play, Save, Scissors, Trash2, X } from '@lucide/svelte';
  import { api } from './api';
  import { onMount } from 'svelte';
  import GuidedTour from './GuidedTour.svelte';

  type Segment = { id?: string; ordinal: number; start_ms: number; end_ms: number; text: string; speaker?: string | null };
  type Stage = { revision: number; segments: Segment[] };
  type Row = { start_ms: number; end_ms: number; changed: boolean; transcription?: Segment[]; correction?: Segment[]; translation?: Segment[]; tts_optimization?: Segment[] };
  type Payload = { stages: Record<string, Stage>; rows: Row[] };

  let { sessionId, sourceArtifactId, onclose, onsaved }: { sessionId: string; sourceArtifactId?: string; onclose: () => void; onsaved: () => void } = $props();
  let payload = $state<Payload | null>(null);
  let error = $state('');
  let loading = $state(true);
  let changedOnly = $state(false);
  type ReviewStage = 'transcription' | 'correction' | 'translation' | 'tts_optimization';
  let editStage = $state<ReviewStage>('translation');
  let saving = $state(false);
  let audioPreview = $state<HTMLAudioElement>();
  let tourOpen = $state(false);
  const tourSteps = [{section:'Review',title:'Lineage keeps changes together',body:'Rows group transcription, correction, and translation through split/merge lineage, with temporal overlap for legacy artifacts.'},{section:'Review',title:'Edit the selected revision',body:'Change text and boundaries, split a segment, or merge it with the next while comparison columns remain visible.'},{section:'Review',title:'Saving creates history',body:'A save creates a reviewed immutable revision and invalidates only affected descendants.'}];
  const availableStages = $derived((['transcription', 'correction', 'translation', 'tts_optimization'] as const).filter((stage) => payload?.stages[stage]));
  const visibleRows = $derived((payload?.rows ?? []).filter((row) => !changedOnly || row.changed));

  async function load() {
    loading = true;
    try {
      payload = await api<Payload>(`/sessions/${sessionId}/subtitles`);
      if (!payload.stages[editStage]) editStage = (availableStages.at(-1) ?? 'transcription') as typeof editStage;
    } catch (caught) { error = caught instanceof Error ? caught.message : String(caught); }
    finally { loading = false; }
  }

  function split(segment: Segment) {
    const records = payload?.stages[editStage]?.segments;
    if (!records) return;
    const index = records.indexOf(segment);
    const midpoint = Math.max(1, Math.floor(segment.text.length / 2));
    const space = segment.text.lastIndexOf(' ', midpoint);
    const boundary = space > 0 ? space : midpoint;
    const time = Math.floor((segment.start_ms + segment.end_ms) / 2);
    const first = { ...segment, id: undefined, text: segment.text.slice(0, boundary).trim(), end_ms: time };
    const second = { ...segment, id: undefined, text: segment.text.slice(boundary).trim(), start_ms: time };
    if (first.text && second.text) records.splice(index, 1, first, second);
  }

  function mergeNext(segment: Segment) {
    const records = payload?.stages[editStage]?.segments;
    if (!records) return;
    const index = records.indexOf(segment);
    const next = records[index + 1];
    if (next) records.splice(index, 2, { ...segment, id: undefined, end_ms: next.end_ms, text: `${segment.text} ${next.text}`.trim() });
  }

  function removeSegment(segment: Segment) { const records=payload?.stages[editStage]?.segments; if(records) records.splice(records.indexOf(segment),1); }
  function previewSegment(segment: Segment) { if(!audioPreview)return; audioPreview.currentTime=segment.start_ms/1000; audioPreview.play(); window.setTimeout(()=>{if(audioPreview&&audioPreview.currentTime>=segment.end_ms/1000)audioPreview.pause()},Math.max(100,segment.end_ms-segment.start_ms)); }

  async function save() {
    const stage = payload?.stages[editStage];
    if (!stage) return;
    saving = true; error = '';
    try {
      await api(`/sessions/${sessionId}/subtitles/${editStage}/review`, {
        method: 'POST',
        body: JSON.stringify({ expected_revision: stage.revision, segments: stage.segments.map(({ start_ms, end_ms, text, speaker }) => ({ start_ms, end_ms, text, speaker })) })
      });
      await load(); onsaved();
    } catch (caught) { error = caught instanceof Error ? caught.message : String(caught); }
    finally { saving = false; }
  }
  onMount(load);
</script>

<div class="fixed inset-0 z-50 bg-black/45 p-3 backdrop-blur-sm sm:p-6" role="presentation">
  <button onclick={()=>tourOpen=true} class="surface fixed right-7 bottom-7 z-[70] rounded-full px-4 py-2 text-sm font-semibold">Review tour</button>
  <div class="surface mx-auto flex h-full max-w-[96rem] flex-col overflow-hidden rounded-[1.5rem]" role="dialog" aria-modal="true" aria-labelledby="review-title">
    <header class="flex flex-wrap items-center justify-between gap-4 border-b border-[var(--line)] px-5 py-4 sm:px-7">
      <div><div class="eyebrow">Subtitle review</div><h2 id="review-title" class="mt-1 flex items-center gap-2 text-xl font-semibold"><Columns3 size={20}/> Compare and refine</h2></div>
      <div class="flex flex-wrap items-center gap-2">
        <button onclick={() => changedOnly = !changedOnly} class:active={changedOnly} class="flex items-center gap-2 rounded-xl border border-[var(--line)] px-3 py-2 text-sm font-semibold"><Filter size={16}/> Changed only</button>
        <select bind:value={editStage} class="rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-sm" aria-label="Stage to edit">{#each availableStages as stage}<option value={stage}>{stage}</option>{/each}</select>
        <button onclick={save} disabled={saving || !payload?.stages[editStage]} class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2 text-sm font-semibold text-white disabled:opacity-40"><Save size={16}/> {saving ? 'Saving…' : 'Save reviewed revision'}</button>
        <button onclick={onclose} aria-label="Close subtitle review" class="rounded-xl border border-[var(--line)] p-2"><X size={18}/></button>
      </div>
    </header>
    {#if sourceArtifactId}<div class="border-b border-[var(--line)] px-5 py-3 sm:px-7"><audio bind:this={audioPreview} class="h-9 w-full" controls src={`/api/v1/artifacts/${sourceArtifactId}/content`}></audio></div>{/if}
    {#if error}<div class="mx-5 mt-4 rounded-xl border border-red-400/40 bg-red-500/10 px-4 py-3 text-sm sm:mx-7">{error}</div>{/if}
    <div class="min-h-0 flex-1 overflow-auto">
      {#if loading}<div class="grid h-full place-items-center"><div class="eyebrow animate-pulse">Aligning subtitle lineage…</div></div>
      {:else if !visibleRows.length}<div class="grid h-full place-items-center"><p class="muted">No comparable subtitle rows are available.</p></div>
      {:else}
        <table class="w-full min-w-[66rem] border-collapse text-sm">
          <thead class="sticky top-0 z-10 bg-[var(--paper-strong)]"><tr><th class="w-32 border-b border-r border-[var(--line)] p-3 text-left">Timing</th>{#each availableStages as stage}<th class="border-b border-r border-[var(--line)] p-3 text-left capitalize last:border-r-0">{stage}</th>{/each}</tr></thead>
          <tbody>{#each visibleRows as row}<tr class:changed={row.changed} class="align-top"><td class="muted border-b border-r border-[var(--line)] p-3 font-mono text-xs">{(row.start_ms/1000).toFixed(2)}<br/>→ {(row.end_ms/1000).toFixed(2)}</td>
            {#each availableStages as stage}<td class="border-b border-r border-[var(--line)] p-3 last:border-r-0"><div class="space-y-3">{#each row[stage] ?? [] as segment}<div class="rounded-xl border border-[var(--line)] bg-[var(--paper)] p-2.5">
              {#if stage === editStage}<div class="mb-2 grid grid-cols-2 gap-2"><label class="muted text-[.68rem]">Start ms<input type="number" bind:value={segment.start_ms} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-transparent px-2 py-1"/></label><label class="muted text-[.68rem]">End ms<input type="number" bind:value={segment.end_ms} class="mt-1 w-full rounded-lg border border-[var(--line)] bg-transparent px-2 py-1"/></label></div><textarea bind:value={segment.text} rows="3" class="w-full resize-y rounded-lg border border-[var(--line)] bg-transparent p-2 leading-relaxed"></textarea><div class="mt-2 flex flex-wrap gap-2"><button onclick={() => previewSegment(segment)} class="flex items-center gap-1 rounded-lg border border-[var(--line)] px-2 py-1 text-xs"><Play size={13}/> Play</button><button onclick={() => split(segment)} class="flex items-center gap-1 rounded-lg border border-[var(--line)] px-2 py-1 text-xs"><Scissors size={13}/> Split</button><button onclick={() => mergeNext(segment)} class="flex items-center gap-1 rounded-lg border border-[var(--line)] px-2 py-1 text-xs"><Merge size={13}/> Merge next</button><button onclick={() => removeSegment(segment)} class="flex items-center gap-1 rounded-lg border border-red-400/40 px-2 py-1 text-xs text-red-500"><Trash2 size={13}/> Delete</button></div>{:else}<p class="whitespace-pre-wrap leading-relaxed">{segment.text}</p>{/if}
            </div>{/each}</div></td>{/each}
          </tr>{/each}</tbody>
        </table>
      {/if}
    </div>
  </div>
</div>
<GuidedTour tourId="subtitle-review" steps={tourSteps} bind:open={tourOpen}/>

<style>
  tr.changed > td { background: color-mix(in srgb, var(--accent-soft) 22%, transparent); }
  button.active { color: var(--accent); background: var(--accent-soft); }
</style>
