<script lang="ts">
  import { page } from '$app/state';
  import { Clock3, Columns3, Download, Eye, FileText, History, SlidersHorizontal } from '@lucide/svelte';
  import { api } from '$lib/api';
  import ArtifactPreview from '$lib/ArtifactPreview.svelte';
  import SettingsPanel from '$lib/SettingsPanel.svelte';
  import SubtitleReview from '$lib/SubtitleReview.svelte';
  import { artifactFilename, formatBytes } from '$lib/artifact-display';

  const sessionId = String(page.params.id);
  let documents = $state<any[]>([]);
  let review = $state(false);
  let sourceArtifact = $state('');
  let activeTab = $state<'settings' | 'history'>('settings');
  let preview = $state<any | null>(null);

  async function load() {
    documents = (await api<{ items: any[] }>(`/sessions/${sessionId}/documents`)).items;
    const artifacts = await api<{ items: any[] }>(`/artifacts?session_id=${sessionId}&limit=100`);
    sourceArtifact = artifacts.items.find((item: any) => item.role === 'upload')?.id ?? '';
  }

  function duration(value: number) {
    if (!value) return '';
    const total = Math.round(value / 1000);
    const minutes = Math.floor(total / 60);
    return `${minutes}:${String(total % 60).padStart(2, '0')}`;
  }

  load();
</script>

<div class="space-y-5">
  <div class="flex flex-wrap items-end justify-between gap-4">
    <div><h2 class="text-2xl font-semibold">Text and subtitles</h2><p class="muted mt-2">Correction creates a same-language asset; translation and TTS optimization remain independent transformations.</p></div>
    <button onclick={() => review = true} disabled={!documents.some((item) => ['transcription','correction','translation'].includes(item.stage))} class="flex items-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-40"><Columns3 size={16}/> Compare subtitles</button>
  </div>

  <nav class="inline-flex rounded-xl border border-[var(--line)] bg-[var(--paper-strong)] p-1" aria-label="Text workspace sections">
    <button onclick={() => activeTab = 'settings'} class:active={activeTab === 'settings'}><SlidersHorizontal size={15}/> Settings</button>
    <button onclick={() => activeTab = 'history'} class:active={activeTab === 'history'}><History size={15}/> Document history <span>{documents.length}</span></button>
  </nav>

  {#if activeTab === 'settings'}
    <div class="space-y-5">
      <SettingsPanel {sessionId} section="text" title="Text processing"/>
      <SettingsPanel {sessionId} section="stt" title="Speech recognition and VAD"/>
      <SettingsPanel {sessionId} section="subtitles" title="Subtitle composition" description="Viewer-facing cue limits are independent from dubbing speech blocks."/>
      <SettingsPanel {sessionId} section="correction" title="Subtitle correction" description="Creates a same-language revision intended to be proper subtitles."/>
      <SettingsPanel {sessionId} section="translation" title="Translation" description="May translate directly from source or from a corrected revision."/>
    </div>
  {:else}
    <section class="surface rounded-2xl p-5">
      <div class="eyebrow">Document history</div>
      <p class="muted mt-2 text-sm">Documents are collapsed by default. Expand one to inspect its revisions and managed files.</p>
      <div class="mt-4 space-y-2">
        {#each documents.slice().reverse() as document}
          <details class="history-item">
            <summary>
              <span class="icon"><FileText size={18}/></span>
              <span class="min-w-0 flex-1"><strong class="capitalize">{document.stage.replaceAll('_',' ')}</strong><small>{document.language ?? 'Language unspecified'} · {document.revisions.length} revision(s)</small></span>
              <span class="muted hidden text-right text-xs sm:block"><Clock3 class="inline" size={13}/> {new Date(document.revisions[0]?.created_at ?? document.created_at).toLocaleString()}</span>
            </summary>
            <div class="revision-list">
              {#each document.revisions as revision}
                <article>
                  <div class="min-w-0 flex-1"><div class="flex flex-wrap items-center gap-2"><strong>Revision {revision.revision_number}</strong>{#if revision.id === document.active_revision_id}<span class="badge">Current</span>{/if}{#if revision.reviewed}<span class="badge reviewed">Reviewed</span>{/if}</div><div class="muted mt-1 flex flex-wrap gap-x-2 text-xs"><time datetime={revision.created_at}>{new Date(revision.created_at).toLocaleString()}</time><span>· {revision.segment_count} segment(s)</span>{#if revision.duration_ms}<span>· {duration(revision.duration_ms)}</span>{/if}{#if revision.artifact?.size_bytes != null}<span>· {formatBytes(revision.artifact.size_bytes)}</span>{/if}</div>{#if revision.artifact}<div class="muted mt-1 truncate text-xs">{artifactFilename(revision.artifact)} · {revision.artifact.relative_path}</div>{/if}</div>
                  {#if revision.artifact}<div class="flex shrink-0 gap-1"><button onclick={() => preview = revision.artifact} class="tool" title="Preview revision" aria-label={`Preview revision ${revision.revision_number}`}><Eye size={15}/></button><a href={`/api/v1/artifacts/${revision.artifact.id}/content`} download={artifactFilename(revision.artifact)} class="tool" title="Download revision" aria-label={`Download revision ${revision.revision_number}`}><Download size={15}/></a></div>{/if}
                </article>
              {/each}
            </div>
          </details>
        {:else}
          <p class="muted py-8 text-center text-sm">Process a source to create the first document revision.</p>
        {/each}
      </div>
    </section>
  {/if}
</div>

{#if review}<SubtitleReview {sessionId} sourceArtifactId={sourceArtifact} onclose={() => review = false} onsaved={load}/>{/if}
{#if preview}<ArtifactPreview artifact={preview} onclose={() => preview = null}/>{/if}

<style>
  nav button{display:flex;align-items:center;gap:.4rem;border-radius:.6rem;padding:.5rem .75rem;font-size:.75rem;font-weight:700;color:var(--muted)}nav button.active{background:var(--accent-soft);color:var(--ink)}nav button span{border-radius:999px;background:var(--line);padding:.05rem .35rem;font-size:.62rem}
  .history-item{overflow:hidden;border:1px solid var(--line);border-radius:.85rem}.history-item summary{display:flex;cursor:pointer;list-style:none;align-items:center;gap:.75rem;padding:.85rem 1rem}.history-item summary::-webkit-details-marker{display:none}.history-item summary:hover{background:var(--accent-soft)}.history-item summary small{display:block;margin-top:.15rem;font-size:.7rem;color:var(--muted)}.icon{display:grid;height:2.25rem;width:2.25rem;flex:none;place-items:center;border-radius:.65rem;background:var(--accent-soft);color:var(--accent)}
  .revision-list{border-top:1px solid var(--line);background:var(--paper);padding:.45rem}.revision-list article{display:flex;align-items:center;gap:.75rem;border-bottom:1px solid var(--line);padding:.75rem}.revision-list article:last-child{border-bottom:0}.badge{border-radius:999px;background:var(--accent-soft);padding:.15rem .4rem;font-size:.58rem;font-weight:750;text-transform:uppercase;color:var(--accent)}.badge.reviewed{background:#16a34a1a;color:#15803d}.tool{display:grid;height:2rem;width:2rem;place-items:center;border:1px solid var(--line);border-radius:.55rem}.tool:hover{background:var(--accent-soft)}
</style>
