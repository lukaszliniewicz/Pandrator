<script lang="ts">
  import type { SoundCandidate, SoundModel } from './sound-api';

  let { candidate, model }: { candidate: SoundCandidate; model?: SoundModel } =
    $props();
  const settings = $derived({
    ...(candidate.provenance.num_inference_steps === undefined
      ? {}
      : { num_inference_steps: candidate.provenance.num_inference_steps }),
    ...(candidate.provenance.guidance_scale === undefined
      ? {}
      : { guidance_scale: candidate.provenance.guidance_scale }),
    ...candidate.provenance.parameters
  });
</script>

<section class="space-y-3 text-sm" aria-label="Take settings">
  {#if candidate.provenance.operation === 'import'}
    <p class="muted">Imported audio · no generation settings.</p>
  {:else}
    <dl class="settings-grid">
      <div>
        <dt>Model</dt>
        <dd>{model?.label ?? candidate.provenance.model_id ?? 'Unknown'}</dd>
      </div>
      <div>
        <dt>Requested length</dt>
        <dd>{candidate.provenance.duration_seconds ?? '—'} s</dd>
      </div>
      <div>
        <dt>Seed</dt>
        <dd>{candidate.provenance.seed ?? 'Not recorded'}</dd>
      </div>
      <div>
        <dt>Backend</dt>
        <dd>{candidate.provenance.backend?.toUpperCase() ?? 'Not recorded'}</dd>
      </div>
      {#if candidate.provenance.threads !== undefined}
        <div>
          <dt>CPU threads</dt>
          <dd>{candidate.provenance.threads}</dd>
        </div>
      {/if}
      {#each Object.entries(settings) as [key, value] (key)}
        <div>
          <dt>{model?.parameters[key]?.label ?? key.replaceAll('_', ' ')}</dt>
          <dd>{typeof value === 'boolean' ? (value ? 'On' : 'Off') : value}</dd>
        </div>
      {/each}
    </dl>
    {#if candidate.provenance.prompt}
      <div>
        <h3 class="mb-1 font-medium">Description</h3>
        <p class="whitespace-pre-wrap break-words">
          {candidate.provenance.prompt}
        </p>
      </div>
    {/if}
    {#if candidate.provenance.lyrics}
      <details>
        <summary class="cursor-pointer text-[var(--accent)]">Lyrics</summary>
        <p class="mt-2 whitespace-pre-wrap break-words">
          {candidate.provenance.lyrics}
        </p>
      </details>
    {/if}
  {/if}
  {#if candidate.sample_rate !== null}
    <p class="muted text-xs">
      {candidate.sample_rate.toLocaleString()} Hz · {candidate.channels === 1
        ? 'Mono'
        : candidate.channels === 2
          ? 'Stereo'
          : `${candidate.channels ?? '—'} channels`}
    </p>
  {/if}
</section>

<style>
  .settings-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr));
    gap: 0.75rem 1.25rem;
  }
  dt {
    color: var(--muted);
    font-size: 0.75rem;
  }
  dd {
    overflow-wrap: anywhere;
  }
</style>
