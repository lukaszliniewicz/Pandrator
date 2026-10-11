<script lang="ts">
  import type { SpeechSelectionContext } from './speech-inspector';
  let {
    reference,
    characters = []
  }: {
    reference: SpeechSelectionContext['markup_reference'];
    characters?: Array<{ id: string; display_name: string }>;
  } = $props();
</script>

<details class="markup-reference">
  <summary>Allowed XML actions</summary>
  <p>
    Keep the spoken words and whitespace unchanged. Pandrator compiles these
    actions for the selected model; valid unsupported actions remain saved.
  </p>
  <dl>
    <dt>Speaker and voice</dt>
    <dd>
      <code>&lt;narrator&gt;</code>, <code>&lt;dialogue&gt;</code>,
      <code>&lt;speaker ref="…"&gt;</code>
      and <code>&lt;span&gt;</code> wrap spoken words. A <code>voice="…"</code> override
      is allowed on narrator, speaker and span.
    </dd>
    <dt>Delivery</dt>
    <dd>
      Put a nonspoken control inside its containing segment, speaker or span. <code
        >&lt;ins&gt;Quietly&lt;/ins&gt;</code
      >
      adds a direction; <code>&lt;em&gt;calm&lt;/em&gt;</code> sets an emotion.
      Nested instructions add to their parents.
      <code>&lt;em intensity="0.7"&gt;sad&lt;/em&gt;</code> also stores an optional
      strength. Only models with a native intensity control use that number.
    </dd>
    {#each ['pace', 'cadence', 'emphasis'] as field}
      <dt><code>&lt;{field}&gt;</code></dt>
      <dd>
        {reference.delivery_schema.properties[field]?.enum
          ?.filter(Boolean)
          .join(' · ')}
      </dd>
    {/each}
    <dt>Vocal events</dt>
    <dd>
      <code>&lt;event kind="sigh" /&gt;</code> goes at its intended position.
      Kinds: {reference.vocal_event_schema.properties.kind?.enum?.join(', ')}.
      Only pause accepts <code>duration_ms</code>, a soft hint of 50–5000 ms.
      Vocalizations must also be enabled. Use <code>affinity="previous"</code>
      after a speaker's words, or
      <code>affinity="following"</code> before them, to choose the voice at a cast
      boundary. Omitted affinity uses the following words, except at the end of a
      block.
    </dd>
    {#if reference.element_tags.includes('sound')}
      <dt>Sound effects</dt>
      <dd>
        <code>&lt;sound cue="…" variant="…" placement="after" /&gt;</code>
        pins an existing sound variant. Use canonical cue and variant UUIDs from the
        sound plan. Placement is <code>before</code> at the segment start,
        <code>after</code> at a word or segment end, or <code>next_pause</code>
        after a word. Word insertions need acoustic placement before mixing. XML previews
        update the sound plan together with speech edits; these markers are removed
        before TTS. No paths, URLs or generation prompts are allowed. Up to 32 markers
        are shown; other cues remain in Project sounds.
      </dd>
    {/if}
    <dt>Structure</dt>
    <dd>
      Use one <code>&lt;segment&gt;</code>. Keep its ID and existing
      <code>boundary_after</code>. XML escaping applies to literal &lt; and
      &amp; in spoken text. Indentation between spoken elements adds spoken
      whitespace.
    </dd>
  </dl>
  {#if characters.length}
    <p class="reference-label">Known speaker references</p>
    <ul>
      {#each characters as character}<li>
          {character.display_name}: <code>{character.id}</code>
        </li>{/each}
    </ul>
  {/if}
  <details>
    <summary>Complete vocabulary and limits</summary>
    <ul>
      {#each reference.element_tags as tag}<li>
          <code>&lt;{tag}&gt;</code>{reference.element_attributes[tag]?.length
            ? ` · ${reference.element_attributes[tag].join(', ')}`
            : ''}
        </li>{/each}
    </ul>
    <p>
      {reference.limits.xml_bytes / 1024} KiB · {reference.limits.nodes} nodes · depth
      {reference.limits.depth} · {reference.limits.delivery_spans} directed spans
      · {reference.limits.events} events.
    </p>
  </details>
</details>

<style>
  .markup-reference {
    font-size: 0.75rem;
    line-height: 1.6;
  }
  summary {
    cursor: pointer;
    padding: 0.5rem 0;
    font-weight: 600;
  }
  p,
  dl,
  ul {
    margin-top: 0.6rem;
  }
  dt {
    margin-top: 0.7rem;
    font-weight: 600;
  }
  dd {
    color: var(--muted);
  }
  code {
    font-size: 0.7rem;
    color: var(--ink);
    overflow-wrap: anywhere;
  }
  .reference-label {
    font-weight: 600;
  }
</style>
