<script lang="ts">
  import ParameterLabel from './ParameterLabel.svelte';

  let {
    policy = $bindable('auto'),
    threshold = $bindable<number | undefined>()
  }: {
    policy?: 'auto' | 'sentences' | 'clauses';
    threshold?: number;
  } = $props();
  const componentId = $props.id();
  const helpId = `${componentId}-packing-help`;
  const validThreshold = $derived(
    Number.isInteger(threshold) &&
      Number(threshold) >= 1 &&
      Number(threshold) <= 8192
  );
</script>

<fieldset class="rounded-xl border border-[var(--line)] p-4">
  <legend class="px-1 text-sm font-semibold">Sentence packing</legend>
  <div class="grid gap-4 sm:grid-cols-2">
    <label class="text-sm font-semibold">
      <ParameterLabel
        section="text"
        name="audiobook_sentence_packing"
        label="Packing mode"
        description="Pandrator groups the sentences detected by wtpsplit into narration blocks. Prefer whole sentences starts a new block when the next sentence will not fit. A sentence too long for an empty block still splits at clauses or other suitable boundaries. Allow clause packing can combine those pieces with neighbouring sentences."
      />
      <select
        bind:value={policy}
        class="input mt-2 w-full"
        aria-describedby={helpId}
      >
        <option value="auto">Automatic · use the threshold</option>
        <option value="sentences">Prefer whole sentences</option>
        <option value="clauses">Allow clause packing</option>
      </select>
    </label>
    <label class="text-sm font-semibold">
      <ParameterLabel
        section="text"
        name="audiobook_sentence_packing_threshold"
        label="Whole-sentence threshold (characters)"
        description="A Pandrator packing setting, separate from wtpsplit sentence detection. In Automatic mode, a block limit at or above this many characters prefers whole sentences; smaller limits allow clause packing. Prefer whole sentences and Allow clause packing ignore this threshold."
      />
      <input
        class="input mt-2 w-full"
        type="number"
        min="1"
        max="8192"
        step="1"
        required
        bind:value={threshold}
        aria-invalid={!validThreshold}
        aria-describedby={helpId}
      />
    </label>
  </div>
  {#if !validThreshold}
    <p role="alert" class="mt-2 text-xs text-red-700">
      Enter a whole-sentence threshold between 1 and 8,192 characters.
    </p>
  {/if}
  <p id={helpId} class="muted mt-3 text-xs leading-relaxed">
    {policy === 'auto'
      ? 'At or above the threshold, keep sentences whole when they fit.'
      : policy === 'sentences'
        ? 'Start a new block for the next sentence when needed.'
        : 'Allow clause-sized pieces to share a block.'}
    A sentence too long for one block still splits. Existing plans keep their boundaries.
    The threshold applies only in Automatic mode.
  </p>
</fieldset>
