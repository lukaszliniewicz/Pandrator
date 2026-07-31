<script lang="ts">
  import { RefreshCw, RotateCcw, Trash2, WandSparkles } from '@lucide/svelte';
  import type { GenerationSegment } from './api-models';
  import type { GenerationSegmentChanges } from './domain-api';
  import type { PlayableTake } from './generation-view-models';
  import type { SettingOption } from './settings-fields';
  import type { VoiceDescriptor } from './voice-catalog';
  import AudioPlayer from './AudioPlayer.svelte';
  import WaveformPeaks from './WaveformPeaks.svelte';

  let {
    items,
    selectedRows,
    loading,
    speechOptionsLoading,
    selectedTtsServiceName,
    selectedTtsModel,
    inheritedVoice,
    inheritedLanguage,
    onselect,
    onpatch,
    onreview,
    onvoices,
    onvoicelabel,
    onlanguages,
    onlanguagelabel,
    onlanguagechange,
    onactivetake,
    onselecttake,
    ontakelabel,
    onverificationtitle,
    onregenerate
  }: {
    items: GenerationSegment[];
    selectedRows: string[];
    loading: boolean;
    speechOptionsLoading: boolean;
    selectedTtsServiceName?: string | null;
    selectedTtsModel: string;
    inheritedVoice: string;
    inheritedLanguage: string;
    onselect: (item: GenerationSegment, event: MouseEvent) => void;
    onpatch: (
      item: GenerationSegment,
      changes: GenerationSegmentChanges
    ) => unknown;
    onreview: (item: GenerationSegment) => void;
    onvoices: (item: GenerationSegment) => VoiceDescriptor[];
    onvoicelabel: (voice: VoiceDescriptor) => string;
    onlanguages: (item: GenerationSegment) => SettingOption[];
    onlanguagelabel: (value: string) => string;
    onlanguagechange: (item: GenerationSegment, language: string) => unknown;
    onactivetake: (item: GenerationSegment) => PlayableTake | undefined;
    onselecttake: (item: GenerationSegment, takeId: string) => unknown;
    ontakelabel: (take: GenerationSegment['takes'][number]) => string;
    onverificationtitle: (take: GenerationSegment['takes'][number]) => string;
    onregenerate: (item: GenerationSegment) => unknown;
  } = $props();
</script>

<table class="w-full border-collapse text-sm">
  <thead class="sticky top-0 z-10 bg-[var(--paper-strong)]">
    <tr>
      <th class="w-12">Mark</th>
      <th class="w-14">#</th>
      <th class="text-left">Generation text and delivery</th>
      <th class="w-52">Audio take</th>
      <th class="w-24">Status</th>
      <th class="w-24"></th>
    </tr>
  </thead>
  <tbody>
    {#each items as item, itemIndex (item.id)}
      {@const selectedTake = onactivetake(item)}
      <tr
        onclick={(event) => onselect(item, event)}
        class:selected={selectedRows.includes(item.id)}
        class:removed={item.removed}
        data-segment-id={item.id}
        data-segment-ordinal={item.ordinal}
      >
        <td>
          <input
            type="checkbox"
            checked={item.marked}
            onchange={(event) =>
              onpatch(item, { marked: event.currentTarget.checked })}
          />
        </td>
        <td class="muted font-mono text-xs">{item.ordinal + 1}</td>
        <td>
          {#if item.speaker}
            <span
              class="mb-1 inline-flex rounded-full bg-[var(--accent-soft)] px-2 py-0.5 text-[.62rem] font-semibold text-[var(--accent)]"
              >{item.speaker}</span
            >
          {/if}
          <textarea
            value={item.text}
            data-generation-search-index={itemIndex}
            onblur={(event) => {
              const text = event.currentTarget.value;
              if (text !== item.text) onpatch(item, { text });
            }}
            rows="2"
            class="w-full resize-y rounded-lg border border-transparent bg-transparent p-2 focus:border-[var(--line)]"
          ></textarea>
          {#if item.optimized_text || selectedTake?.llm_optimized}
            <button
              onclick={(event) => {
                event.stopPropagation();
                onreview(item);
              }}
              class="mb-2 flex max-w-full items-center gap-1.5 rounded-lg bg-[var(--accent-soft)] px-2.5 py-1.5 text-left text-[.68rem] font-semibold text-[var(--accent)]"
            >
              <WandSparkles size={12} />
              <span class="truncate"
                >{item.speech_plan?.version
                  ? 'Review speech plan'
                  : 'Compare speech optimization'}</span
              >
              <span
                class="rounded-full bg-[var(--paper)] px-1.5 py-0.5 text-[.58rem] uppercase"
                >{item.speech_plan?.mode_used ??
                  item.optimization_status ??
                  'generated'}</span
              >
              {#if item.speech_plan?.proposals?.length}
                <span
                  class="rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[.58rem] uppercase text-amber-700"
                  >{item.speech_plan.proposals.length} proposed</span
                >
              {/if}
            </button>
          {/if}
          <div class="flex flex-wrap gap-2">
            <select
              value={item.node_kind ?? 'paragraph'}
              onchange={(event) =>
                onpatch(item, {
                  node_kind: event.currentTarget
                    .value as GenerationSegment['node_kind']
                })}
              aria-label="Segment role"
              class="mini"
            >
              <option value="paragraph">Paragraph</option>
              <option value="heading">Heading</option>
              <option value="chapter_marker">Chapter start</option>
              <option value="subtitle_cue">Subtitle cue</option>
            </select>
            <select
              value={item.voice ?? ''}
              onchange={(event) =>
                onpatch(item, { voice: event.currentTarget.value || null })}
              aria-label={`Voice for segment ${item.ordinal + 1}`}
              title={`${selectedTtsServiceName ?? 'TTS service'} · ${selectedTtsModel || 'default model'}`}
              disabled={speechOptionsLoading}
              class="mini max-w-52"
            >
              <option value="">
                Inherited{inheritedVoice
                  ? ` · ${onvoices(item).find((voice) => voice.id.toLowerCase() === inheritedVoice.toLowerCase())?.name ?? inheritedVoice}`
                  : ' · service default'}
              </option>
              {#each onvoices(item) as voice}
                <option value={voice.id}>{onvoicelabel(voice)}</option>
              {/each}
            </select>
            <select
              value={item.language ?? ''}
              onchange={(event) =>
                onlanguagechange(item, event.currentTarget.value)}
              aria-label={`Language for segment ${item.ordinal + 1}`}
              disabled={speechOptionsLoading}
              class="mini max-w-48"
            >
              <option value=""
                >Inherited · {onlanguagelabel(inheritedLanguage)}</option
              >
              {#each onlanguages(item) as language}
                <option value={language.value}>{language.label}</option>
              {/each}
            </select>
          </div>
        </td>
        <td>
          {#if selectedTake}
            <AudioPlayer
              compact
              preload="none"
              src={`/api/v1/artifacts/${selectedTake.artifact_id}/content`}
              label={`Segment ${item.ordinal + 1}`}
            />
            <WaveformPeaks artifactId={selectedTake.artifact_id} />
            <select
              value={selectedTake.id}
              onchange={(event) =>
                onselecttake(item, event.currentTarget.value)}
              class="mini mt-1 w-full"
            >
              {#each item.takes as take}
                <option value={take.id}
                  >{ontakelabel(take)} · {take.status}</option
                >
              {/each}
            </select>
            {#if selectedTake.audio_verification}
              <span
                class="verification-badge {selectedTake.audio_verification
                  .status}"
                title={onverificationtitle(selectedTake)}
              >
                Signal check: {selectedTake.audio_verification.status}
              </span>
            {/if}
          {:else}
            <span class="muted text-xs">Not generated</span>
          {/if}
        </td>
        <td><span class="status">{item.status}</span></td>
        <td>
          <div class="flex justify-center gap-1">
            <button
              onclick={(event) => {
                event.stopPropagation();
                onregenerate(item);
              }}
              disabled={loading || item.removed}
              class="action icon-action"
              title="Regenerate this segment"
              aria-label={`Regenerate segment ${item.ordinal + 1}`}
            >
              <RefreshCw size={14} />
            </button>
            <button
              onclick={(event) => {
                event.stopPropagation();
                onpatch(item, { removed: !item.removed });
              }}
              class="action icon-action"
              aria-label={item.removed ? 'Restore segment' : 'Remove segment'}
            >
              {#if item.removed}<RotateCcw size={14} />{:else}<Trash2
                  size={14}
                />{/if}
            </button>
          </div>
        </td>
      </tr>
    {/each}
  </tbody>
</table>

<style>
  th,
  td {
    border-bottom: 1px solid var(--line);
    padding: 0.55rem;
    text-align: center;
    vertical-align: middle;
  }
  tr.removed {
    opacity: 0.42;
  }
  tr.selected {
    background: var(--accent-soft);
  }
  .status {
    font-size: 0.68rem;
    text-transform: uppercase;
    color: var(--muted);
  }
  .mini {
    border: 1px solid var(--line);
    border-radius: 0.45rem;
    background: var(--paper);
    padding: 0.3rem 0.45rem;
    font-size: 0.68rem;
  }
  .verification-badge {
    display: inline-flex;
    margin-top: 0.35rem;
    border-radius: 999px;
    padding: 0.2rem 0.45rem;
    font-size: 0.6rem;
    font-weight: 750;
    text-transform: uppercase;
    background: color-mix(in srgb, var(--accent) 12%, transparent);
    color: var(--accent);
  }
  .verification-badge.warning {
    background: rgba(245, 158, 11, 0.13);
    color: #b45309;
  }
  .verification-badge.failed {
    background: rgba(239, 68, 68, 0.13);
    color: #dc2626;
  }
  .action {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    border: 1px solid var(--line);
    border-radius: 0.55rem;
    padding: 0.4rem 0.6rem;
    font-size: 0.7rem;
    font-weight: 700;
  }
  .action:disabled {
    opacity: 0.35;
  }
  .icon-action {
    padding: 0.42rem;
  }
</style>
