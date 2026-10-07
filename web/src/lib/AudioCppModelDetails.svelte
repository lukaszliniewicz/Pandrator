<script lang="ts">
  import {
    featureLabels,
    languageName,
    readable,
    type AudioCppModelInfo
  } from './audio-cpp-catalogue';
  let {
    model,
    expanded = false
  }: { model: AudioCppModelInfo; expanded?: boolean } = $props();
  const languages = $derived(
    (model.supported_languages ?? []).map(languageName)
  );
  const capabilityNotes = $derived(
    Array.isArray(model.pandrator_features?.capability_notes)
      ? model.pandrator_features.capability_notes
      : []
  );
</script>

<details open={expanded} class="model-details text-xs">
  <summary class="cursor-pointer font-semibold text-[var(--accent)]"
    >Languages, licence and controls</summary
  >
  <div class="mt-3 space-y-3 leading-relaxed">
    {#if model.capabilities?.length}
      <div>
        <strong>Available controls:</strong>
        <ul class="mt-1 flex flex-wrap gap-1" aria-label="Available controls">
          {#each model.capabilities as capability}<li
              class="rounded border border-[var(--line)] px-2 py-0.5"
            >
              {featureLabels[capability] ?? readable(capability)}
            </li>{/each}
        </ul>
      </div>
    {/if}
    <div>
      <strong>Languages:</strong>
      {languages.length ? languages.join(', ') : 'Not specified by upstream.'}
      {#if model.language_note}<p class="muted mt-1">
          {model.language_note}
        </p>{/if}
    </div>
    <div>
      <strong>Model licence:</strong>
      {#if model.license?.url?.startsWith('https://')}
        <a
          class="underline underline-offset-2"
          href={model.license.url}
          target="_blank"
          rel="noreferrer">{model.license.name ?? 'Model terms'}</a
        >
      {:else}{model.license?.name ?? 'Not verified'}{/if}
      {#if model.license?.commercial_use}<span class="muted">
          · {readable(model.license.commercial_use)}</span
        >{/if}
    </div>
    {#if model.generation_limits}
      {@const limits = model.generation_limits}
      <section class="space-y-1" aria-label="Generation length">
        <h4 class="font-semibold">Generation length</h4>
        <p>
          Default audiobook target: {limits.default_segment_characters.toLocaleString()}
          characters. Language and configured output budgets can reduce this target.
        </p>
        {#if limits.input_characters}<p>
            Provider input limit: {limits.input_characters.toLocaleString()} characters.
          </p>{/if}
        {#if limits.input_tokens}<p>
            Provider input limit: {limits.input_tokens.toLocaleString()} tokens, including
            instructions where applicable.
          </p>{/if}
        {#if limits.output_tokens}<p>
            Provider output limit: {limits.output_tokens.toLocaleString()} tokens.
          </p>{/if}
        {#if limits.output_seconds}<p>
            Provider audio limit: {limits.output_seconds / 60} minutes per request.
          </p>{/if}
        {#if limits.runtime_chunk_characters}<p>
            Runtime text splitting default: {limits.runtime_chunk_characters.toLocaleString()}
            characters. This is configurable.
          </p>{/if}
        {#if limits.runtime_output_budget}<p>
            Runtime output budget: {limits.runtime_output_budget.value.toLocaleString()}
            {limits.runtime_output_budget.unit.replaceAll('_', ' ')}.
          </p>{/if}
        <p class="muted">
          Longer requests can improve continuity, but output budgets and voice
          quality still need checking for the selected model. Limits checked {limits.checked_at}.
        </p>
        {#if limits.source_urls.length}<a
            class="inline-block underline underline-offset-2"
            href={limits.source_urls[0]}
            target="_blank"
            rel="noreferrer">Generation documentation</a
          >{/if}
      </section>
    {/if}
    <dl class="grid gap-x-4 gap-y-1 sm:grid-cols-2">
      <div>
        <dt class="inline font-semibold">Reference audio:</dt>
        <dd class="inline">{readable(model.reference_audio)}</dd>
      </div>
      <div>
        <dt class="inline font-semibold">Reference transcript:</dt>
        <dd class="inline">{readable(model.reference_text)}</dd>
      </div>
    </dl>
    {#if model.pandrator_features || model.upstream_features}
      <div class="grid gap-4 sm:grid-cols-2">
        <div>
          <h4 class="font-semibold">Pandrator support</h4>
          <dl class="mt-1 space-y-1">
            {#each Object.entries(model.pandrator_features ?? {}) as [key, status]}
              {#if typeof status === 'string'}<div>
                  <dt class="inline">{featureLabels[key] ?? readable(key)}:</dt>
                  <dd class="muted inline">{readable(status)}</dd>
                </div>{/if}
            {/each}
          </dl>
        </div>
        <div>
          <h4 class="font-semibold">Upstream capabilities</h4>
          <p class="muted mt-1">
            Availability can depend on the variant and request mode.
          </p>
          <ul
            class="mt-1 flex flex-wrap gap-1"
            aria-label="Upstream capabilities"
          >
            {#each Object.entries(model.upstream_features ?? {}).filter(([, enabled]) => enabled) as [key]}
              <li class="rounded border border-[var(--line)] px-2 py-0.5">
                {featureLabels[key] ?? readable(key)}
              </li>
            {:else}<li class="muted">
                No additional capabilities recorded.
              </li>{/each}
          </ul>
        </div>
      </div>
    {/if}
    {#each capabilityNotes as note}<p class="muted">
        {note}
      </p>{/each}
    {#if model.verified_runtime}<p class="muted">
        Catalogue checked against audio.cpp {model.verified_runtime}. Request
        support does not certify voice quality or emotional delivery.
      </p>{/if}
    {#if model.sources?.length}<a
        class="inline-block underline underline-offset-2"
        href={model.sources[0]}
        target="_blank"
        rel="noreferrer">Upstream documentation</a
      >{/if}
  </div>
</details>
