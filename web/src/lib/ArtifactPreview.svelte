<script lang="ts">
  import { projectLinkUrl } from './project-link-context';
  import { errorMessage } from './errors';
  import {
    BookOpenCheck,
    Columns2,
    Download,
    ExternalLink,
    FileQuestion,
    Globe2,
    LoaderCircle,
    TriangleAlert
  } from '@lucide/svelte';
  import { onMount, onDestroy } from 'svelte';
  import {
    artifactFilename,
    artifactRoleLabel,
    formatBytes,
    type PreviewableArtifact
  } from './artifact-display';
  import { artifactApi } from './domain-api';
  import type { ArtifactRecord, UsageSummary } from './api-models';
  import AudioPlayer from './AudioPlayer.svelte';
  import BookExportNotice from './BookExportNotice.svelte';
  import TextDiff from './TextDiff.svelte';
  import PagedTextPreview from './PagedTextPreview.svelte';
  let textPage = $state(0);
  let comparisonPage = $state(0);
  import ReviewWorkspace from './ReviewWorkspace.svelte';
  import { loadTextPreview } from './text-preview';

  let {
    artifact,
    displayName,
    onclose
  }: {
    artifact: PreviewableArtifact;
    displayName?: string;
    onclose: () => void;
  } = $props();
  let text = $state('');
  let error = $state('');
  let loading = $state(false);
  let toolsOpen = $state(false);
  let truncated = $state(false);
  let comparisonTruncated = $state(false);
  let comparisonError = $state('');
  const lifecycle = new AbortController();
  let comparisonRequest: AbortController | undefined;
  onDestroy(() => {
    lifecycle.abort();
    comparisonRequest?.abort();
  });
  let comparisonText = $state('');
  const diffAvailable = $derived(text.length + comparisonText.length <= 40_000);
  let comparisonName = $state('');
  let comparisonId = $state('');
  let comparisonChoice = $state('');
  let comparisonCandidates = $state<ArtifactRecord[]>([]);
  let comparisonLoading = $state(false);
  let comparisonMode = $state<'single' | 'side' | 'diff'>('single');
  let usageSummary = $state<UsageSummary | null>(null);
  type SubtitleTrack = {
    artifact_id: string;
    language: string;
    title: string;
    default: boolean;
  };
  type ResearchLedger = {
    evidence_count?: number;
    summary?: string;
    glossary?: { source: string; target: string }[];
    warnings?: string[];
    sources?: { url: string; title?: string }[];
  };
  let subtitleTracks = $state<SubtitleTrack[]>([]);
  const url = $derived(
    projectLinkUrl(
      `/api/v1/artifacts/${artifact.id}/content${
        artifact.content_hash
          ? `?v=${encodeURIComponent(artifact.content_hash)}`
          : ''
      }`
    )
  );
  const filename = $derived(displayName?.trim() || artifactFilename(artifact));
  const extension = $derived(filename.split('.').at(-1)?.toLowerCase() ?? '');
  const mime = $derived(String(artifact.mime_type ?? '').toLowerCase());
  const isAudio = $derived(
    mime.startsWith('audio/') ||
      ['wav', 'mp3', 'flac', 'm4a', 'ogg', 'opus', 'aac'].includes(extension)
  );
  const isVideo = $derived(
    mime.startsWith('video/') ||
      ['mp4', 'mkv', 'webm', 'mov', 'avi'].includes(extension)
  );
  const isImage = $derived(
    mime.startsWith('image/') ||
      ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'].includes(extension)
  );
  const isPdf = $derived(mime === 'application/pdf' || extension === 'pdf');
  const isText = $derived(
    mime.startsWith('text/') ||
      [
        'txt',
        'srt',
        'vtt',
        'ass',
        'ssa',
        'json',
        'xml',
        'csv',
        'md',
        'log',
        'yaml',
        'yml'
      ].includes(extension)
  );
  const research = $derived(
    (artifact.metadata_json?.research ?? null) as ResearchLedger | null
  );
  const formattedCost = $derived.by(() => {
    const value = usageSummary?.total_cost_usd;
    if (value == null) return 'Cost unavailable';
    return `$${Number(value).toFixed(Number(value) < 0.01 ? 6 : 4)}`;
  });

  function parsedSubtitleTracks(value: unknown): SubtitleTrack[] {
    if (!Array.isArray(value)) return [];
    return value.flatMap((item: unknown) => {
      const record =
        item && typeof item === 'object'
          ? (item as Record<string, unknown>)
          : null;
      return record?.artifact_id
        ? [
            {
              artifact_id: String(record.artifact_id),
              language: String(record.language || 'und'),
              title: String(record.title || 'Subtitles'),
              default: Boolean(record.default)
            }
          ]
        : [];
    });
  }

  function showDefaultSubtitleTrack(event: Event) {
    const video = event.currentTarget as HTMLVideoElement;
    Array.from(video.textTracks).forEach((track, index) => {
      track.mode = subtitleTracks[index]?.default ? 'showing' : 'disabled';
    });
  }

  function loadSubtitleTrack(event: Event, isDefault: boolean) {
    (event.currentTarget as HTMLTrackElement).track.mode = isDefault
      ? 'showing'
      : 'disabled';
  }

  function isTextArtifact(value: ArtifactRecord) {
    const name = String(value.relative_path ?? '').toLowerCase();
    const type = String(value.mime_type ?? '').toLowerCase();
    return (
      value.kind === 'text' ||
      type.startsWith('text/') ||
      [
        '.txt',
        '.md',
        '.srt',
        '.vtt',
        '.json',
        '.xml',
        '.csv',
        '.yaml',
        '.yml'
      ].some((suffix) => name.endsWith(suffix))
    );
  }

  async function loadComparison() {
    const candidate = comparisonCandidates.find(
      (item) => item.id === comparisonChoice
    );
    if (!candidate) return;
    comparisonLoading = true;
    comparisonError = '';
    comparisonRequest?.abort();
    const controller = new AbortController();
    comparisonRequest = controller;
    try {
      const preview = await loadTextPreview(
        `/artifacts/${candidate.id}/content`,
        { signal: controller.signal }
      );
      if (controller.signal.aborted) return;
      comparisonText = preview.text;
      comparisonTruncated = preview.truncated;
      comparisonPage = 0;
      comparisonId = candidate.id;
      comparisonName = artifactFilename(candidate);
      comparisonMode = 'side';
    } catch (caught) {
      if (!controller.signal.aborted) comparisonError = errorMessage(caught);
    } finally {
      if (comparisonRequest === controller) comparisonLoading = false;
    }
  }

  onMount(async () => {
    loading = isText;
    try {
      const textPromise = isText
        ? loadTextPreview(url, { signal: lifecycle.signal })
        : Promise.resolve({ text: '', truncated: false });
      const contextPromise = artifactApi.context(artifact.id).catch(() => null);

      if (isText) {
        const preview = await textPromise;
        if (lifecycle.signal.aborted) return;
        text = preview.text;
        truncated = preview.truncated;
        loading = false;
      }

      const context = await contextPromise;
      if (context && !lifecycle.signal.aborted) {
        usageSummary = context.usage ?? null;
        if (isVideo) {
          subtitleTracks = parsedSubtitleTracks(
            context.artifact?.metadata_json?.subtitle_tracks ??
              artifact.metadata_json?.subtitle_tracks
          );
          if (!subtitleTracks.length) {
            subtitleTracks = (context.parents ?? []).flatMap((item) => {
              const name = String(item.relative_path ?? '').toLowerCase();
              if (item.kind !== 'vtt' && !name.endsWith('.vtt')) return [];
              return [
                {
                  artifact_id: String(item.id),
                  language: String(item.metadata_json?.language || 'und'),
                  title: String(item.metadata_json?.title || 'Subtitles'),
                  default: Boolean(item.metadata_json?.default)
                }
              ];
            });
          }
        }
        if (isText)
          comparisonCandidates = [
            ...(context.parents ?? []),
            ...(context.children ?? [])
          ].filter(
            (item, index, items) =>
              item.id !== artifact.id &&
              isTextArtifact(item) &&
              items.findIndex((candidate) => candidate.id === item.id) === index
          );
      }
    } catch (caught) {
      if (!lifecycle.signal.aborted) error = errorMessage(caught);
    } finally {
      if (!lifecycle.signal.aborted) loading = false;
    }
  });
</script>

<ReviewWorkspace
  closeLabel="Close preview"
  titleId="artifact-preview-title"
  {onclose}
  bind:toolsOpen
  toolsTitle="Details & compare"
  toolsSummary={comparisonId ? 'Comparison loaded' : ''}
>
  {#snippet header()}
    <div class="min-w-0 flex-1">
      <h2 id="artifact-preview-title" class="truncate text-lg font-semibold">
        {filename}
      </h2>
      <p class="muted mt-1 text-xs">
        Read-only · {artifact.metadata_json?.preview === true
          ? 'Export preview'
          : artifactRoleLabel(artifact.role)}
      </p>
    </div>
    {#if comparisonId}<div class="flex gap-1">
        <button
          onclick={() =>
            (comparisonMode = comparisonMode === 'side' ? 'single' : 'side')}
          class:active={comparisonMode === 'side'}
          class="tool">Side by side</button
        ><button
          onclick={() =>
            (comparisonMode = comparisonMode === 'diff' ? 'single' : 'diff')}
          disabled={!diffAvailable}
          title={diffAvailable
            ? 'Show differences'
            : 'Large comparisons use paged side-by-side viewing'}
          class:active={comparisonMode === 'diff'}
          class="tool">Diff</button
        >
      </div>{/if}
    <a href={url} download={filename} class="tool flex items-center gap-2"
      ><Download size={16} /> Download</a
    >
  {/snippet}
  {#snippet tools()}
    <dl class="space-y-2 text-xs">
      <div>
        <dt class="muted">File type</dt>
        <dd>{artifact.mime_type || artifact.kind || 'Unknown file type'}</dd>
      </div>
      {#if artifact.size_bytes != null}<div>
          <dt class="muted">Size</dt>
          <dd>{formatBytes(artifact.size_bytes)}</dd>
        </div>{/if}
      {#if artifact.state}<div>
          <dt class="muted">State</dt>
          <dd>{artifact.state}</dd>
        </div>{/if}
    </dl>
    {#if usageSummary?.commercial || usageSummary?.total_tokens}<div
        class="cost-badge"
      >
        <span>{usageSummary.estimated ? 'Estimated cost' : 'Cost'}</span><strong
          >{formattedCost}</strong
        >{#if usageSummary.total_tokens}<small
            >{usageSummary.input_tokens?.toLocaleString() ?? 0} in ·
            {usageSummary.output_tokens?.toLocaleString() ?? 0} out</small
          >{/if}
        >{#if usageSummary.has_unpriced_usage}<small>partial</small>{/if}
      </div>{/if}
    {#if isText && comparisonCandidates.length}<div
        class="mt-5 flex flex-col gap-3"
        aria-label="Artifact comparison"
      >
        <label class="min-w-0 flex-1 text-xs font-semibold sm:max-w-lg">
          Compare with a related asset
          <select
            bind:value={comparisonChoice}
            disabled={comparisonLoading}
            class="mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-3 py-2 text-sm font-normal disabled:opacity-50"
          >
            <option value="">Choose an asset…</option>
            {#each comparisonCandidates as candidate (candidate.id)}
              <option value={candidate.id}
                >{artifactRoleLabel(candidate.role)} · {artifactFilename(
                  candidate
                )}</option
              >
            {/each}
          </select>
        </label>
        <button
          onclick={loadComparison}
          disabled={!comparisonChoice || comparisonLoading}
          class="tool flex items-center gap-2 disabled:opacity-40"
        >
          {#if comparisonLoading}<LoaderCircle
              class="animate-spin"
              size={15}
            />{:else}<Columns2 size={15} />{/if}
          {comparisonLoading ? 'Loading…' : 'Load comparison'}
        </button>
        <span class="muted pb-2 text-xs"
          >Related files stay unloaded until selected.</span
        >
      </div>{/if}
    {#if research}
      <details class="research-ledger mb-4" open>
        <summary>
          <span
            class="grid size-8 shrink-0 place-items-center rounded-xl bg-[var(--accent-soft)] text-[var(--accent)]"
            ><Globe2 size={16} /></span
          >
          <span class="min-w-0 flex-1"
            ><strong>Web research evidence</strong><small
              >{research.evidence_count ?? research.sources?.length ?? 0} verified
              {(research.evidence_count ?? research.sources?.length ?? 0) === 1
                ? 'source'
                : 'sources'} · used as bounded guidance, not inserted into the text</small
            ></span
          >
        </summary>
        <div
          class="grid gap-4 border-t border-[var(--line)] p-4 md:grid-cols-[minmax(0,1fr)_minmax(15rem,.7fr)]"
        >
          <div>
            {#if research.summary}<p class="text-sm leading-6">
                {research.summary}
              </p>{/if}
            {#if research.glossary?.length}
              <div class="mt-3">
                <div
                  class="flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-[var(--muted)]"
                >
                  <BookOpenCheck size={14} /> Terminology ledger
                </div>
                <div class="mt-2 flex flex-wrap gap-2">
                  {#each research.glossary as item}<span class="glossary-chip"
                      >{item.source}<span>→</span>{item.target}</span
                    >{/each}
                </div>
              </div>
            {/if}
            {#if research.warnings?.length}
              <div
                class="mt-3 rounded-xl bg-amber-500/10 p-3 text-xs text-amber-700"
              >
                {#each research.warnings as warning}<p class="flex gap-2">
                    <TriangleAlert class="mt-0.5 shrink-0" size={13} />{warning}
                  </p>{/each}
              </div>
            {/if}
          </div>
          <div>
            <div
              class="text-xs font-bold uppercase tracking-wider text-[var(--muted)]"
            >
              Sources
            </div>
            {#if research.sources?.length}<div class="mt-2 space-y-2">
                {#each research.sources as source}<a
                    href={source.url}
                    target="_blank"
                    rel="noreferrer"
                    class="source-link"
                    ><span class="min-w-0 flex-1 truncate"
                      >{source.title || source.url}</span
                    ><ExternalLink class="shrink-0" size={13} /></a
                  >{/each}
              </div>{:else}<p class="muted mt-2 text-xs">
                The research agent finished without retaining evidence.
              </p>{/if}
          </div>
        </div>
      </details>
    {/if}
  {/snippet}
  {#snippet notices()}
    <BookExportNotice metadata={artifact.metadata_json} />
    {#if truncated || comparisonTruncated}<p
        role="status"
        class="border-b border-amber-500/30 bg-amber-500/10 px-4 py-2 text-xs"
      >
        Preview limited to 2 MiB{comparisonTruncated
          ? ' (including the compared file)'
          : ''}. Download the file to read its complete contents.
      </p>{/if}
    {#if comparisonId && !diffAvailable}<p
        role="status"
        class="muted border-b border-[var(--line)] px-4 py-2 text-xs"
      >
        Large comparisons use paged side-by-side viewing. Download the files for
        a full diff.
      </p>{/if}
    {#if comparisonError}<p
        role="alert"
        class="border-b border-red-500/30 px-4 py-2 text-sm text-red-600"
      >
        {comparisonError}
      </p>{/if}
    {#if research?.warnings?.length}<p
        class="muted border-b border-[var(--line)] px-4 py-2 text-xs"
      >
        Research has {research.warnings.length} warning{research.warnings
          .length === 1
          ? ''
          : 's'}.
        <button class="underline" onclick={() => (toolsOpen = true)}
          >Read research details</button
        >
      </p>{/if}
  {/snippet}
  <div
    class="min-h-0 flex-1 overflow-auto bg-[var(--paper)] p-4 sm:p-6"
    data-preview-content
  >
    {#if isAudio}<div class="grid min-h-72 place-items-center">
        <div class="w-full max-w-3xl"><AudioPlayer src={url} /></div>
      </div>
    {:else if isVideo}<!-- svelte-ignore a11y_media_has_caption --><video
        controls
        preload="metadata"
        src={url}
        onloadedmetadata={showDefaultSubtitleTrack}
        class="mx-auto max-h-[72vh] max-w-full rounded-xl bg-black"
        >{#each subtitleTracks as track}<track
            kind="subtitles"
            src={projectLinkUrl(
              `/api/v1/artifacts/${track.artifact_id}/content`
            )}
            srclang={track.language}
            label={track.title}
            default={track.default}
            onload={(event) => loadSubtitleTrack(event, track.default)}
          />{/each}</video
      >
    {:else if isImage}<img
        src={url}
        alt={filename}
        class="mx-auto max-h-[74vh] max-w-full rounded-xl object-contain"
      />
    {:else if isPdf}<iframe
        src={url}
        title={filename}
        class="h-[74vh] w-full rounded-xl border border-[var(--line)] bg-white"
      ></iframe>
    {:else if isText}
      {#if loading}<div class="grid min-h-72 place-items-center">
          <LoaderCircle class="animate-spin" size={24} />
        </div>
      {:else if error}<p class="text-sm text-red-500">{error}</p>
      {:else if comparisonMode === 'side' && comparisonId}<div
          class="comparison-grid"
        >
          <section>
            <h3>Compared file · {comparisonName}</h3>
            <PagedTextPreview
              text={comparisonText}
              label="Compared file"
              bind:page={comparisonPage}
            />
          </section>
          <section>
            <h3>Current file · {filename}</h3>
            <PagedTextPreview
              {text}
              label="Current file"
              bind:page={textPage}
            />
          </section>
        </div>
      {:else if comparisonMode === 'diff' && comparisonId}<div>
          <div class="muted mb-2 text-xs">
            Removed text is red and struck through; added text is green.
          </div>
          <TextDiff before={comparisonText} after={text} />
        </div>
      {:else}<PagedTextPreview
          {text}
          label="Current file"
          bind:page={textPage}
        />{/if}
    {:else}<div class="grid min-h-72 place-items-center text-center">
        <div>
          <FileQuestion class="muted mx-auto" size={38} />
          <h3 class="mt-4 font-semibold">
            Preview not available for this file type
          </h3>
          <p class="muted mt-2 text-sm">
            Download the managed artifact to open it in a compatible
            application.
          </p>
        </div>
      </div>{/if}
  </div>
</ReviewWorkspace>

<style>
  .tool {
    border: 1px solid var(--line);
    border-radius: 0.7rem;
    padding: 0.55rem 0.75rem;
    font-size: 0.75rem;
    font-weight: 650;
  }
  .tool.active {
    background: var(--accent-soft);
    color: var(--accent);
  }
  .cost-badge {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    border: 1px solid var(--line);
    border-radius: 0.7rem;
    padding: 0.4rem 0.65rem;
    font-size: 0.65rem;
    color: var(--muted);
  }
  .cost-badge strong {
    font-size: 0.8rem;
    color: var(--ink);
  }
  .cost-badge small {
    font-size: 0.58rem;
    color: #b45309;
  }
  .research-ledger {
    overflow: hidden;
    border: 1px solid color-mix(in srgb, var(--accent) 26%, var(--line));
    border-radius: 1rem;
    background: var(--paper-strong);
  }
  .research-ledger summary {
    display: flex;
    cursor: pointer;
    list-style: none;
    align-items: center;
    gap: 0.75rem;
    padding: 0.75rem 1rem;
  }
  .research-ledger summary::-webkit-details-marker {
    display: none;
  }
  .research-ledger summary strong {
    display: block;
    font-size: 0.78rem;
  }
  .research-ledger summary small {
    display: block;
    margin-top: 0.15rem;
    color: var(--muted);
    font-size: 0.67rem;
  }
  .source-link {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    border: 1px solid var(--line);
    border-radius: 0.7rem;
    background: var(--paper);
    padding: 0.55rem 0.65rem;
    color: var(--accent);
    font-size: 0.7rem;
    font-weight: 650;
  }
  .source-link:hover {
    border-color: color-mix(in srgb, var(--accent) 50%, var(--line));
  }
  .glossary-chip {
    display: inline-flex;
    align-items: center;
    gap: 0.38rem;
    border-radius: 999px;
    background: var(--accent-soft);
    padding: 0.35rem 0.6rem;
    font-size: 0.68rem;
    font-weight: 650;
  }
  .glossary-chip span {
    color: var(--accent);
  }
  .comparison-grid {
    display: grid;
    gap: 1rem;
  }
  @media (min-width: 768px) {
    .comparison-grid {
      grid-template-columns: 1fr 1fr;
    }
  }
  .comparison-grid section {
    min-width: 0;
  }
  .comparison-grid h3 {
    position: sticky;
    top: -1rem;
    z-index: 2;
    margin-bottom: 0.5rem;
    border: 1px solid var(--line);
    border-radius: 0.7rem;
    background: var(--paper-strong);
    padding: 0.65rem 0.8rem;
    font-size: 0.7rem;
    font-weight: 750;
    color: var(--muted);
  }
  .comparison-grid :global(pre) {
    min-height: 18rem;
  }
</style>
