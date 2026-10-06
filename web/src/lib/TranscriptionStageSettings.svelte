<script lang="ts">
  import VocalIsolationControl from './VocalIsolationControl.svelte';
  import ParameterLabel from './ParameterLabel.svelte';
  import LanguageSelect from './LanguageSelect.svelte';
  import QwenTranscriptionControls from './QwenTranscriptionControls.svelte';
  import SubtitleLimitsSummary from './SubtitleLimitsSummary.svelte';
  import { type StageSettingsDraft } from './stage-settings-draft.svelte';
  import {
    sttLanguageProblem,
    sttLanguageOptions
  } from './stt-language-policy';
  import type {
    RuntimeCapabilities,
    SessionRecord,
    SettingsPayload,
    SttCatalogue
  } from './api-models';

  let {
    draft,
    workflowKind,
    hasAttachedCaptions,
    capabilities,
    sttCatalogue,
    sttLanguageIssue,
    subtitleSettingsPayload
  }: {
    draft: StageSettingsDraft;
    workflowKind: SessionRecord['workflow_kind'];
    hasAttachedCaptions: boolean;
    capabilities: RuntimeCapabilities;
    sttCatalogue: SttCatalogue;
    sttLanguageIssue: string;
    subtitleSettingsPayload: SettingsPayload | null;
  } = $props();

  const supportsSttCompute = (name: string) =>
    name === 'auto' ||
    (capabilities?.stt?.compute_backends ?? []).includes(name);

  const isCloudStt = (engine: string) =>
    sttCatalogue.services.some(
      (service) =>
        service.id.replaceAll('-', '_') === engine.replaceAll('-', '_')
    );

  const sttOptionLabel = (engineId: string, label: string, timing: string) => {
    const info = capabilities?.stt?.models?.[engineId] ?? {};
    const readiness = info.default
      ? 'default'
      : info.installed
        ? 'ready'
        : 'downloads on first use';
    return `${label} · ${timing} · ${readiness}${sttLanguageProblem(capabilities, engineId, draft.originalLanguage) ? ' · unsupported language' : ''}`;
  };
</script>

<VocalIsolationControl bind:value={draft.transcriptionVocalIsolation} />
{#if workflowKind === 'media_edit'}<div
    class="rounded-xl border border-[var(--line)] bg-[var(--accent-soft)] p-4"
  >
    <div class="text-sm font-semibold">
      {hasAttachedCaptions
        ? 'Align attached captions'
        : 'Generate a word-timed transcript'}
    </div>
    {#if hasAttachedCaptions}<p class="muted mt-1 text-xs leading-relaxed">
        Zoom wording and speakers remain authoritative. Local CTC aligns those
        exact words against short, VAD-checked audio windows; overlapping cues
        stay together and uncertain cues retry in isolation. The resulting word
        data follows the subtitles into Pandrator's native document for later
        layout, speech-block, and cut-boundary work.
      </p>{:else}<p class="muted mt-1 text-xs leading-relaxed">
        With no captions attached, this model creates the authoritative
        transcript, cue timing, and word timing used by the editor.
      </p>{/if}
  </div>{/if}
{#if hasAttachedCaptions}
  <label class="text-sm font-semibold"
    ><ParameterLabel
      section="stt"
      name="caption_alignment_method"
      label="Caption alignment method"
    /><select
      bind:value={draft.captionAlignmentMethod}
      class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
      ><option value="ctc">Local CTC forced alignment · recommended</option
      ><option value="ctc_asr_fallback"
        >Local CTC, then ASR if coverage is low</option
      ><option value="asr">ASR lexical projection · legacy</option></select
    ><span class="muted mt-1 block text-xs font-normal"
      >{draft.captionAlignmentMethod === 'ctc'
        ? 'Uses only the supplied captions and local acoustic evidence. It does not generate replacement wording.'
        : draft.captionAlignmentMethod === 'ctc_asr_fallback'
          ? 'Runs CTC first. ASR is loaded only when eligible CTC coverage falls below the threshold, then fills rejected cues without replacing accepted CTC timing.'
          : 'Transcribes the whole recording, then matches recognized words back to nearby caption cues.'}</span
    ></label
  >
  {#if draft.captionAlignmentMethod !== 'asr'}
    <div
      class="rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] p-4"
    >
      <div class="text-sm font-semibold">Target-local Canary CTC</div>
      <p class="muted mt-1 text-xs leading-relaxed">
        Each cleaned cue is evaluated independently with bounded
        following-caption context; only that target cue's timing is kept. VAD
        checks word placement, and CTC blank tails are capped by the shifted
        caption duration. Rejected cues retain their original timing. Automatic
        uses Qwen3 for Japanese, Chinese, Korean and Cantonese; otherwise
        Canary. Qwen3 requires audio.cpp and downloads a verified 1.13 GB model
        once. Audio and text stay local.
      </p>
      <div class="mt-3 grid gap-3 sm:grid-cols-2">
        <label class="text-xs font-semibold"
          ><ParameterLabel
            section="stt"
            name="caption_alignment_ctc_model"
            label="Forced aligner"
            compact
          /><select
            bind:value={draft.captionAlignmentCtcModel}
            class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
            ><option value="auto">Automatic · source language</option><option
              value="canary-ctc-aligner">Canary CTC · European languages</option
            ><option value="qwen3-forced-aligner"
              >Qwen3 · Japanese + 10 languages</option
            ></select
          ></label
        ><label class="text-xs font-semibold"
          ><ParameterLabel
            section="stt"
            name="caption_alignment_padding_ms"
            label="Cue padding (ms)"
            compact
          /><input
            type="number"
            min="250"
            max="5000"
            step="50"
            bind:value={draft.captionAlignmentPaddingMs}
            class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
          /></label
        ><label class="text-xs font-semibold"
          ><ParameterLabel
            section="stt"
            name="caption_alignment_batch_seconds"
            label="Maximum context window (s)"
            compact
          /><input
            type="number"
            min="5"
            max="60"
            step="1"
            bind:value={draft.captionAlignmentBatchSeconds}
            class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
          /></label
        ><label class="text-xs font-semibold"
          ><ParameterLabel
            section="stt"
            name="caption_alignment_min_confidence"
            label="Minimum timing quality"
            compact
          /><span
            class="mt-1 grid min-h-10 grid-cols-[1fr_2.5rem] items-center gap-2 rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3"
            ><input
              type="range"
              min="0.5"
              max="1"
              step="0.05"
              bind:value={draft.captionAlignmentMinConfidence}
              class="w-full accent-[var(--accent)]"
            /><output class="text-right text-xs font-bold"
              >{Number(draft.captionAlignmentMinConfidence).toFixed(2)}</output
            ></span
          ></label
        >{#if draft.captionAlignmentMethod === 'ctc_asr_fallback'}<label
            class="text-xs font-semibold sm:col-span-2"
            ><ParameterLabel
              section="stt"
              name="caption_alignment_fallback_coverage"
              label="Run ASR below eligible coverage"
              compact
            /><span
              class="mt-1 grid min-h-10 grid-cols-[1fr_3rem] items-center gap-2 rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3"
              ><input
                type="range"
                min="0"
                max="1"
                step="0.05"
                bind:value={draft.captionAlignmentFallbackCoverage}
                class="w-full accent-[var(--accent)]"
              /><output class="text-right text-xs font-bold"
                >{Math.round(
                  Number(draft.captionAlignmentFallbackCoverage) * 100
                )}%</output
              ></span
            ></label
          >{/if}
      </div>
    </div>
  {/if}
  {#if draft.captionAlignmentMethod === 'ctc'}
    <div
      class="rounded-xl border border-[var(--line)] bg-[var(--accent-soft)] p-4"
    >
      <div class="text-sm font-semibold">Local acoustic runtime</div>
      <p class="muted mt-1 text-xs leading-relaxed">
        The selected VAD model first maps speech across the recording. Canary
        CTC then processes bounded caption batches on the selected compute
        backend; no ASR model is loaded.
      </p>
      <div class="mt-3 grid gap-3 sm:grid-cols-2">
        <label class="text-xs font-semibold"
          ><ParameterLabel
            section="stt"
            name="stt_compute_backend"
            label="Compute backend"
            compact
          /><select
            bind:value={draft.sttComputeBackend}
            class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
            ><option value="auto">Automatic</option><option
              value="cpu"
              disabled={!supportsSttCompute('cpu')}>CPU</option
            ><option value="cuda" disabled={!supportsSttCompute('cuda')}
              >CUDA</option
            ><option value="vulkan" disabled={!supportsSttCompute('vulkan')}
              >Vulkan</option
            ><option value="metal" disabled={!supportsSttCompute('metal')}
              >Metal</option
            ></select
          ></label
        ><label class="text-xs font-semibold"
          ><ParameterLabel
            section="stt"
            name="stt_compute_device"
            label="Device"
            compact
          /><input
            type="number"
            min="0"
            disabled={['auto', 'cpu'].includes(draft.sttComputeBackend)}
            bind:value={draft.sttDevice}
            class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal disabled:opacity-40"
          /></label
        ><label class="flex items-center gap-3 text-xs font-semibold"
          ><input
            type="checkbox"
            bind:checked={draft.vadEnabled}
            class="size-4 accent-[var(--accent)]"
          /><ParameterLabel
            section="stt"
            name="crispasr_vad_enabled"
            label="Validate against VAD"
            compact
          /></label
        >{#if draft.vadEnabled}<label class="text-xs font-semibold"
            ><ParameterLabel
              section="stt"
              name="crispasr_vad_model"
              label="VAD model"
              compact
            /><select
              bind:value={draft.vadModel}
              class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
              ><option value="silero">Silero · recommended</option><option
                value="firered">FireRedVAD · robust</option
              ><option value="marblenet">MarbleNet · compact</option><option
                value="whisper-vad">Whisper VAD · experimental</option
              ></select
            ></label
          ><label class="text-xs font-semibold sm:col-span-2"
            ><ParameterLabel
              section="stt"
              name="crispasr_vad_threshold"
              label="VAD speech threshold"
              compact
            /><span
              class="mt-1 grid min-h-10 grid-cols-[1fr_2.5rem] items-center gap-2 rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3"
              ><input
                type="range"
                min="0"
                max="1"
                step="0.05"
                bind:value={draft.vadThreshold}
                class="w-full accent-[var(--accent)]"
              /><output class="text-right text-xs font-bold"
                >{Number(draft.vadThreshold).toFixed(2)}</output
              ></span
            ></label
          >{/if}
      </div>
    </div>
  {/if}
{/if}
{#if !hasAttachedCaptions || draft.captionAlignmentMethod !== 'ctc'}
  <label class="text-sm font-semibold"
    ><ParameterLabel
      section="stt"
      name="stt_engine"
      label={hasAttachedCaptions &&
      draft.captionAlignmentMethod === 'ctc_asr_fallback'
        ? 'Fallback recognition model'
        : 'Recognition model'}
    /><select
      bind:value={draft.sttEngine}
      aria-label={hasAttachedCaptions
        ? 'Fallback recognition model'
        : 'Recognition model'}
      onchange={() =>
        (draft.sttQuantization = String(
          capabilities?.stt?.models?.[draft.sttEngine]?.precision ?? 'f16'
        ))}
      class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
      ><option value="auto"
        >Automatic · Parakeet, then eligible Qwen, then Whisper</option
      ><option
        value="whisper"
        disabled={Boolean(
          sttLanguageProblem(capabilities, 'whisper', draft.originalLanguage)
        )}
        >{sttOptionLabel(
          'whisper',
          'Whisper large-v3',
          'DTW timestamps'
        )}</option
      ><option
        value="parakeet"
        disabled={Boolean(
          sttLanguageProblem(capabilities, 'parakeet', draft.originalLanguage)
        )}
        >{sttOptionLabel(
          'parakeet',
          'Parakeet TDT 0.6B v3',
          'native timestamps'
        )}</option
      ><option
        value="moss"
        disabled={Boolean(
          sttLanguageProblem(capabilities, 'moss', draft.originalLanguage)
        )}
        >{sttOptionLabel(
          'moss',
          'MOSS Transcribe-Diarize 0.9B',
          'native speakers + CTC words'
        )}</option
      ><option
        value="qwen3"
        disabled={Boolean(
          sttLanguageProblem(capabilities, 'qwen3', draft.originalLanguage)
        )}
        >{sttOptionLabel(
          'qwen3',
          'Qwen3 ASR',
          'separate word alignment'
        )}</option
      >{#each sttCatalogue.services as service}<option value={service.id}
          >{service.name} · cloud word timestamps</option
        >{/each}
    </select><span class="muted mt-1 block text-xs"
      >{isCloudStt(draft.sttEngine)
        ? 'The selected connection runs remotely; audio is sent to its configured provider.'
        : draft.sttEngine === 'qwen3'
          ? 'Qwen3 runs through CrispASR; recognition, alignment and voice detection models download when needed.'
          : draft.sttEngine === 'auto'
            ? 'Prefers Parakeet for supported languages, then Qwen with a supported aligner, then Whisper. Automatic source language uses a short local Tiny sample; the chosen route is recorded with the transcript.'
            : 'CrispASR downloads the explicitly selected model on first use.'}</span
    ></label
  >
  {#if sttLanguageIssue}<p class="text-sm text-red-600" role="alert">
      {sttLanguageIssue}
    </p>{/if}

  {#if isCloudStt(draft.sttEngine)}
    <div
      class="rounded-xl border border-[var(--line)] bg-[var(--accent-soft)] p-4"
    >
      <div class="text-sm font-semibold">Remote timed transcription</div>
      <p class="muted mt-1 text-xs leading-relaxed">
        Pandrator sends the normalized WAV to this provider and accepts the
        result only when it includes genuine word-level spans. Diarization is
        not available for this profile.
      </p>
      <a
        href="/providers?tab=speech&service=stt"
        class="mt-3 inline-flex text-xs font-semibold text-[var(--accent)]"
        >Manage recognition connection</a
      >
    </div>
    <div class="grid gap-3 sm:grid-cols-2">
      <div>
        <LanguageSelect
          bind:value={draft.originalLanguage}
          options={sttLanguageOptions(capabilities, draft.sttEngine)}
          label="Source language"
          allowAuto
          allowCustom
        />
      </div>
      <label class="text-sm font-semibold"
        ><ParameterLabel
          section="stt"
          name="stt_transcribe_style"
          label="Transcript style"
        /><select
          bind:value={draft.sttTranscribeStyle}
          class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
          ><option value="readability">Readable transcript</option><option
            value="verbatim">Verbatim · preserve fillers</option
          ></select
        ></label
      >
    </div>
    <label class="text-sm font-semibold"
      ><ParameterLabel
        section="stt"
        name="stt_hotwords"
        label="Phrase hints"
      /><textarea
        rows="2"
        bind:value={draft.sttHotwords}
        placeholder="Names and terminology, comma-separated"
        class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
      ></textarea><span class="muted mt-1 block text-xs font-normal"
        >Sent as the provider's phrase list; useful for names and specialist
        terms.</span
      ></label
    >
  {:else if draft.sttEngine === 'qwen3'}
    <QwenTranscriptionControls
      bind:model={draft.qwenAsrModel}
      bind:language={draft.originalLanguage}
      bind:backend={draft.sttComputeBackend}
      {capabilities}
    />
    <label class="text-sm font-semibold"
      >Speech chunking
      <select
        bind:value={draft.qwenChunkMode}
        class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
      >
        <option value="auto">Automatic · follow voice detection setting</option>
        <option value="vad">Always detect speech with Silero</option>
        <option value="fixed">Fixed windows without voice detection</option>
        {#if draft.qwenChunkMode === 'none'}<option value="none"
            >Fixed windows · legacy selection</option
          >{/if}
      </select>
    </label>
    {#if draft.qwenChunkMode === 'auto'}
      <label class="flex items-center gap-3 text-sm font-semibold">
        <input
          type="checkbox"
          bind:checked={draft.vadEnabled}
          class="size-4 accent-[var(--accent)]"
        />
        Voice activity detection
      </label>
    {/if}
    <p class="muted text-xs">
      Audio is processed in bounded chunks. Word alignment runs automatically
      for the selected source language.
    </p>
  {:else}
    <label class="text-sm font-semibold"
      ><ParameterLabel
        section="stt"
        name="stt_model_quantization"
        label="Model precision"
      /><select
        bind:value={draft.sttQuantization}
        class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
        ><option value="f16">Full F16</option
        >{#if draft.sttEngine === 'whisper'}<option value="q5_0"
            >Q5_0 · 1.08 GB</option
          >{:else if draft.sttEngine === 'parakeet'}<option value="q8_0"
            >Q8_0 · 745 MB</option
          ><option value="q5_0">Q5_0 · 541 MB</option><option value="q4_k"
            >Q4_K · 489 MB</option
          >{:else}<option value="q8_0">Q8_0 · recommended</option><option
            value="q4_k">Q4_K</option
          >{/if}</select
      ><span class="muted mt-1 block text-xs"
        >F16 maximizes fidelity; quantized files reduce download and memory use.</span
      ></label
    >
    <div class="grid gap-3 sm:grid-cols-[1fr_7rem]">
      <label class="text-sm font-semibold"
        ><ParameterLabel
          section="stt"
          name="stt_compute_backend"
          label="Compute backend"
        /><select
          bind:value={draft.sttComputeBackend}
          class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
          ><option value="auto">Automatic</option><option
            value="cpu"
            disabled={!supportsSttCompute('cpu')}>CPU</option
          ><option value="cuda" disabled={!supportsSttCompute('cuda')}
            >CUDA</option
          ><option value="vulkan" disabled={!supportsSttCompute('vulkan')}
            >Vulkan</option
          ><option value="metal" disabled={!supportsSttCompute('metal')}
            >Metal</option
          ></select
        ><span class="muted mt-1 block text-xs"
          >Only backends compiled into the installed CrispASR runtime can be
          forced.</span
        ></label
      ><label class="text-sm font-semibold"
        ><ParameterLabel
          section="stt"
          name="stt_compute_device"
          label="Device"
        /><input
          type="number"
          min="0"
          disabled={['auto', 'cpu'].includes(draft.sttComputeBackend)}
          bind:value={draft.sttDevice}
          class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal disabled:opacity-40"
        /></label
      >
    </div>
    {#if draft.sttEngine === 'moss'}
      <div
        class="rounded-xl border border-[var(--line)] bg-[var(--accent-soft)] p-4"
      >
        <div class="text-sm font-semibold">
          Native speaker turns with local CTC timing
        </div>
        <p class="muted mt-1 text-xs leading-relaxed">
          MOSS detects the language and speaker changes. Each turn is then
          aligned separately with the selected forced aligner and a small
          acoustic margin, avoiding long-recording alignment drift.
        </p>
        <div class="mt-3 grid gap-3 sm:grid-cols-2">
          <label class="flex items-center gap-3 text-xs font-semibold"
            ><input
              type="checkbox"
              bind:checked={draft.mossCtcAlignmentEnabled}
              class="size-4 accent-[var(--accent)]"
            />
            <ParameterLabel
              section="stt"
              name="moss_ctc_alignment_enabled"
              label="Forced alignment"
              compact
            /></label
          ><label class="text-xs font-semibold"
            >Forced aligner
            <select
              bind:value={draft.mossCtcAlignerModel}
              disabled={!draft.mossCtcAlignmentEnabled}
              class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
            >
              <option value="auto">Automatic · source language</option>
              <option value="canary-ctc-aligner"
                >Canary CTC · European languages</option
              >
              <option value="qwen3-forced-aligner"
                >Qwen3 · Japanese + 10 languages</option
              >
            </select>
          </label><label class="text-xs font-semibold"
            ><ParameterLabel
              section="stt"
              name="moss_ctc_padding_seconds"
              label="Alignment padding (s)"
              compact
            /><input
              type="number"
              min="0"
              max="2"
              step="0.1"
              disabled={!draft.mossCtcAlignmentEnabled}
              bind:value={draft.mossCtcPaddingSeconds}
              class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal disabled:opacity-40"
            /></label
          >
        </div>
      </div>
    {:else}
      <div class="grid gap-3 sm:grid-cols-2">
        <div>
          <LanguageSelect
            bind:value={draft.originalLanguage}
            options={sttLanguageOptions(capabilities, draft.sttEngine)}
            label="Source language"
            allowAuto
            allowCustom
          />
        </div>
        {#if draft.sttEngine === 'auto'}<p class="muted text-xs">
            Routing detection: multilingual Whisper Tiny, up to 15 seconds on
            CPU. Silence or uncertain detection falls back to native Whisper
            detection.
          </p>{:else}<label class="text-sm font-semibold"
            ><ParameterLabel
              section="stt"
              name="stt_lid_backend"
              label="Language detector"
            /><select
              bind:value={draft.sttLidBackend}
              class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
              ><option value="whisper">Whisper tiny</option><option
                value="ecapa">ECAPA (recommended)</option
              ><option value="silero">Silero</option><option value="off"
                >Off</option
              ></select
            ></label
          >{/if}
      </div>
    {/if}
    {#if draft.sttEngine === 'moss'}<label
        class="flex items-start gap-3 text-sm font-semibold"
        ><input
          type="checkbox"
          bind:checked={draft.mossVadEnabled}
          class="mt-0.5 size-4 accent-[var(--accent)]"
        />
        <span
          ><ParameterLabel
            section="stt"
            name="moss_vad_enabled"
            label="Voice activity detection"
          /><span class="muted mt-1 block text-xs font-normal"
            >Off by default so native speaker tracking keeps the longest
            context. The normal chunker still seeks low-energy cut points.</span
          ></span
        ></label
      >{:else}<label class="flex items-center gap-3 text-sm font-semibold"
        ><input
          type="checkbox"
          bind:checked={draft.vadEnabled}
          class="size-4 accent-[var(--accent)]"
        />
        <ParameterLabel
          section="stt"
          name="crispasr_vad_enabled"
          label="Voice activity detection"
        /></label
      >{/if}
    {#if draft.sttEngine === 'moss' ? draft.mossVadEnabled : draft.vadEnabled}<div
        class="grid grid-cols-2 gap-3"
      >
        <label class="text-xs font-semibold"
          ><ParameterLabel
            section="stt"
            name="crispasr_vad_model"
            label="VAD model"
            compact
          /><select
            bind:value={draft.vadModel}
            class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
            ><option value="silero">Silero · general purpose</option><option
              value="firered">FireRedVAD · robust</option
            ><option value="marblenet">MarbleNet · compact</option><option
              value="whisper-vad">Whisper VAD · experimental</option
            ></select
          ></label
        ><label class="text-xs font-semibold"
          ><ParameterLabel
            section="stt"
            name="crispasr_vad_threshold"
            label="VAD threshold"
            compact
          /><span
            class="mt-1 grid min-h-10 grid-cols-[1fr_2.5rem] items-center gap-2 rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3"
            ><input
              type="range"
              min="0"
              max="1"
              step="0.05"
              bind:value={draft.vadThreshold}
              class="w-full accent-[var(--accent)]"
            /><output class="text-right text-xs font-bold"
              >{Number(draft.vadThreshold).toFixed(2)}</output
            ></span
          ></label
        ><label class="text-xs font-semibold"
          ><ParameterLabel
            section="stt"
            name="crispasr_vad_min_speech_ms"
            label="Minimum speech (ms)"
            compact
          /><input
            type="number"
            min="0"
            bind:value={draft.vadMinSpeech}
            class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
          /></label
        ><label class="text-xs font-semibold"
          ><ParameterLabel
            section="stt"
            name="crispasr_vad_min_silence_ms"
            label="Minimum silence (ms)"
            compact
          /><input
            type="number"
            min="0"
            bind:value={draft.vadMinSilence}
            class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
          /></label
        ><label class="text-xs font-semibold"
          ><ParameterLabel
            section="stt"
            name="crispasr_vad_max_speech_seconds"
            label="Maximum speech (s)"
            compact
          /><input
            type="number"
            min="1"
            bind:value={draft.vadMaxSpeech}
            class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
          /></label
        ><label class="text-xs font-semibold"
          ><ParameterLabel
            section="stt"
            name="crispasr_vad_speech_pad_ms"
            label="Speech padding (ms)"
            compact
          /><input
            type="number"
            min="0"
            bind:value={draft.vadSpeechPad}
            class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
          /></label
        >
      </div>{/if}
    <details class="rounded-xl border border-[var(--line)] p-4">
      <summary class="cursor-pointer text-sm font-semibold"
        >Decoder and long-form controls</summary
      >
      <div class="mt-4 grid grid-cols-2 gap-3">
        <label class="text-xs font-semibold"
          ><ParameterLabel
            section="stt"
            name="stt_threads"
            label="Threads (0 = automatic)"
            compact
          /><input
            type="number"
            min="0"
            bind:value={draft.sttThreads}
            class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
          /></label
        ><label class="text-xs font-semibold"
          ><ParameterLabel
            section="stt"
            name="stt_beam_size"
            label="Beam size"
            compact
          /><input
            type="number"
            min="1"
            max="16"
            bind:value={draft.sttBeamSize}
            class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
          /></label
        >{#if draft.sttEngine === 'parakeet'}<label
            class="text-xs font-semibold"
            ><ParameterLabel
              section="stt"
              name="parakeet_decoder"
              label="Parakeet decoder"
              compact
            /><select
              bind:value={draft.parakeetDecoder}
              class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
              ><option value="tdt">TDT greedy / beam</option><option
                value="maes">MAES beam</option
              ><option value="ctc">CTC greedy</option></select
            ></label
          >{/if}{#if draft.sttEngine === 'moss'}<label
            class="text-xs font-semibold"
            ><ParameterLabel
              section="stt"
              name="moss_max_chunk_seconds"
              label="Maximum MOSS context (s)"
              compact
            /><input
              type="number"
              min="30"
              max="120"
              step="1"
              bind:value={draft.mossMaxChunkSeconds}
              class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
            /></label
          >{:else}<label class="text-xs font-semibold"
            ><ParameterLabel
              section="stt"
              name="stt_chunk_seconds"
              label="Forced chunk size (s, 0 = default)"
              compact
            /><input
              type="number"
              min="0"
              step="1"
              bind:value={draft.sttChunkSeconds}
              class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
            /></label
          >{/if}{#if draft.sttEngine === 'moss'}<label
            class="text-xs font-semibold"
            ><ParameterLabel
              section="stt"
              name="moss_chunk_overlap_seconds"
              label="MOSS chunk overlap (s)"
              compact
            /><input
              type="number"
              min="0"
              step="0.5"
              bind:value={draft.mossChunkOverlap}
              class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
            /><span class="muted mt-1 block font-normal"
              >0 prevents duplicated speech and conflicting speaker IDs at chunk
              seams.</span
            ></label
          >{:else}<label class="text-xs font-semibold"
            ><ParameterLabel
              section="stt"
              name="stt_chunk_overlap_seconds"
              label="Chunk overlap (s)"
              compact
            /><input
              type="number"
              min="0"
              step="0.5"
              bind:value={draft.sttChunkOverlap}
              class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
            /></label
          >{/if}
        ><label class="col-span-2 text-xs font-semibold"
          ><ParameterLabel
            section="stt"
            name="stt_hotwords"
            label="Hotwords"
            compact
          /><textarea
            rows="2"
            bind:value={draft.sttHotwords}
            placeholder="Names and terminology, comma-separated"
            class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
          ></textarea></label
        >
      </div>
      {#if draft.sttEngine === 'moss'}<p class="muted mt-3 text-xs">
          Pandrator uses the longest safe MOSS window, then lets CrispASR seek
          the lowest-energy point near its limit. Speaker IDs remain local to a
          chunk; speaker-change boundaries are preserved.
        </p>{:else}<p class="muted mt-3 text-xs">
          Parakeet normally preserves full context and handles long recordings
          internally. Force chunking only for constrained systems or
          diagnostics.
        </p>{/if}
    </details>
  {/if}
{/if}
<div class="rounded-xl border border-[var(--line)] p-4">
  <div class="text-sm font-semibold">Readable subtitle composition</div>
  <p class="muted mt-1 text-xs">
    Pandrator's deterministic word-timed composer uses these limits; they are
    not sent to the correction model. It may regroup source cues while
    preserving speakers and hard pauses, aiming for the reading-speed target and
    configured display capacity.
  </p>
  <label class="mt-3 flex items-center gap-2 text-xs font-semibold">
    <input type="checkbox" bind:checked={draft.subtitleLanguageDefaults} />
    Automatic language limits
  </label>
  <SubtitleLimitsSummary
    automatic={draft.subtitleLanguageDefaults}
    profiles={subtitleSettingsPayload?.subtitle_automatic_profiles}
    chars={draft.subtitleChars}
    cps={draft.subtitleCps}
    lines={draft.subtitleLines}
  />
  <div class="mt-3 grid grid-cols-2 gap-3">
    {#if !draft.subtitleLanguageDefaults}<label class="text-xs font-semibold"
        ><ParameterLabel
          section="subtitles"
          name="max_chars_per_line"
          label="Display units / line"
          compact
        /><input
          type="number"
          min="8"
          max="100"
          bind:value={draft.subtitleChars}
          class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
        /></label
      >{/if}<label class="text-xs font-semibold"
      ><ParameterLabel
        section="subtitles"
        name="max_lines"
        label="Lines"
        compact
      /><input
        type="number"
        min="1"
        max="3"
        bind:value={draft.subtitleLines}
        class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
      /></label
    ><label class="text-xs font-semibold"
      ><ParameterLabel
        section="subtitles"
        name="min_duration_ms"
        label="Minimum duration (ms)"
        compact
      /><input
        type="number"
        min="250"
        bind:value={draft.subtitleMinDuration}
        class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
      /></label
    ><label class="text-xs font-semibold"
      ><ParameterLabel
        section="subtitles"
        name="max_duration_ms"
        label="Maximum duration (ms)"
        compact
      /><input
        type="number"
        min="1000"
        bind:value={draft.subtitleMaxDuration}
        class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
      /></label
    >{#if !draft.subtitleLanguageDefaults}<label class="text-xs font-semibold"
        ><ParameterLabel
          section="subtitles"
          name="max_cps"
          label="Reading-speed target (units/second)"
          compact
        /><input
          type="number"
          min="1"
          max="40"
          step="0.5"
          bind:value={draft.subtitleCps}
          class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
        /></label
      >{/if}<label class="text-xs font-semibold"
      ><ParameterLabel
        section="subtitles"
        name="min_gap_ms"
        label="Minimum cue gap (ms)"
        compact
      /><input
        type="number"
        min="0"
        max="500"
        bind:value={draft.subtitleMinGap}
        class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
      /></label
    ><label class="text-xs font-semibold"
      ><ParameterLabel
        section="subtitles"
        name="phrase_gap_ms"
        label="Subtitle grouping gap (ms)"
        compact
      /><input
        type="number"
        min="100"
        max="3000"
        bind:value={draft.subtitlePhraseGap}
        class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
      /></label
    ><label class="text-xs font-semibold"
      ><ParameterLabel
        section="subtitles"
        name="hard_gap_ms"
        label="Hard silence boundary (ms)"
        compact
      /><input
        type="number"
        min="250"
        max="5000"
        bind:value={draft.subtitleHardGap}
        class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
      /></label
    ><label class="text-xs font-semibold"
      ><ParameterLabel
        section="subtitles"
        name="sentence_boundary_threshold"
        label="Sentence boundary threshold"
        compact
      /><input
        type="number"
        min="0.01"
        max="0.99"
        step="0.01"
        bind:value={draft.subtitleSentenceBoundaryThreshold}
        class="mt-1 w-full rounded-lg border border-[var(--line)] bg-[var(--paper)] px-3 py-2 font-normal"
      /></label
    >
  </div>
</div>
