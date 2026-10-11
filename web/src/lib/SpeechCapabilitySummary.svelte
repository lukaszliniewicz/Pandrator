<script lang="ts">
  import { deliverySupport, type SpeechCapabilities } from './speech-support';
  import { readable } from './audio-cpp-catalogue';

  let { capabilities }: { capabilities: SpeechCapabilities } = $props();
  const direction = $derived(deliverySupport(capabilities, 'instruction'));
  const emotion = $derived(deliverySupport(capabilities, 'emotion'));
  const events = $derived(Object.keys(capabilities.event_tags ?? {}));
</script>

<section
  class="speech-capabilities"
  aria-label="Speech controls for this model"
>
  <dl>
    {#if capabilities.voice_design_workflow === 'resource_creation'}
      <div>
        <dt>Voice design</dt>
        <dd>
          Create and save a voice in the Voice Library, then select its saved
          identity for generation.
        </dd>
      </div>
    {:else if capabilities.voice_design_workflow === 'request_description'}
      <div>
        <dt>Voice design</dt>
        <dd>
          A description conditions each request. Save it in the cast to reuse
          it; voice consistency depends on the model.
        </dd>
      </div>
    {/if}
    <div>
      <dt>Directions</dt>
      <dd>
        <strong>{direction.label}</strong>
        <p>{direction.description}</p>
        {#if capabilities.requires_instruction}<p>
            A voice description is required before generation.
          </p>{/if}
        {#if capabilities.instructions && capabilities.instructions !== 'none'}<p
          >
            Scope: {capabilities.phrase_instruction_transport === 'inline_hint'
              ? 'whole request; phrase cues are best effort'
              : capabilities.phrase_instruction_transport === 'quoted_hint'
                ? 'whole request; quoted phrase directions are hints'
                : 'whole request only'}.
          </p>{/if}
      </dd>
    </div>
    <div>
      <dt>Emotion</dt>
      <dd>
        <strong>{emotion.label}</strong>
        {#if capabilities.emotion?.mode === 'enum'}<p>
            Allowed values: {capabilities.emotion.tags?.join(', ')}.
          </p>{:else}<p>{emotion.description}</p>{/if}
      </dd>
    </div>
    <div>
      <dt>Vocal cues</dt>
      <dd>
        {#if events.length}{events.map((kind) => readable(kind)).join(', ')}.
          <p>
            {capabilities.event_pause?.format === 'ssml_break'
              ? 'Pauses use provider SSML breaks within its duration limit.'
              : 'Cues and pause duration are interpreted by the model.'}
          </p>
        {:else}{capabilities.status === 'unknown' ? 'Unverified' : 'Not sent'}.
          <p>Other authored cues remain saved.</p>{/if}
      </dd>
    </div>
    <div>
      <dt>Before / after context</dt>
      <dd>
        {capabilities.semantic_context === 'field'
          ? 'Unspoken text fields'
          : capabilities.semantic_context === 'prompt'
            ? capabilities.semantic_context_transport === 'instruction_hint'
              ? 'Instruction metadata (delivery hint)'
              : 'Prompt guidance (delivery hint)'
            : capabilities.status === 'unknown'
              ? 'Unverified'
              : 'Not sent'}.
      </dd>
    </div>
    <div>
      <dt>Multiple voices</dt>
      <dd>
        Pandrator assembles separate voice requests in sequence.
        <p>Native multi-speaker input is not implemented.</p>
      </dd>
    </div>
  </dl>
</section>

<style>
  dl {
    display: grid;
    gap: 0.7rem;
  }
  dl > div {
    display: grid;
    grid-template-columns: minmax(7rem, 0.35fr) 1fr;
    gap: 0.75rem;
  }
  dt {
    font-weight: 600;
  }
  dd {
    min-width: 0;
  }
  p {
    margin-top: 0.15rem;
    color: var(--muted);
    font-weight: 400;
  }
  @media (max-width: 480px) {
    dl > div {
      grid-template-columns: 1fr;
      gap: 0.2rem;
    }
  }
</style>
