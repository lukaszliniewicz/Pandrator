<script lang="ts">
  import ParameterLabel from './ParameterLabel.svelte';

  let {
    tableMode = $bindable('headers'),
    imageMode = $bindable('descriptions')
  }: {
    tableMode?: 'headers' | 'rows' | 'skip';
    imageMode?: 'descriptions' | 'captions' | 'skip';
  } = $props();
</script>

<fieldset class="rounded-xl border border-[var(--line)] p-4">
  <legend class="px-1 text-sm font-semibold">
    EPUB tables and illustrations
  </legend>
  <div class="mt-2 grid gap-5">
    <label class="text-sm font-semibold">
      <ParameterLabel
        section="source_cleaning"
        name="epub_table_mode"
        label="Tables"
      />
      <select
        bind:value={tableMode}
        class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
      >
        <option value="headers">Read rows with published headings</option>
        <option value="rows">Read plain rows</option>
        <option value="skip">Skip table content</option>
      </select>
      <span class="muted mt-2 block text-xs font-normal leading-relaxed">
        Published headings keep values in context. Unmarked tables use plain
        rows, preserving poetry and other layouts. Review complex tables in the
        extracted text; each row becomes a separate paragraph.
      </span>
    </label>
    <label class="text-sm font-semibold">
      <ParameterLabel
        section="source_cleaning"
        name="epub_image_mode"
        label="Illustrations"
      />
      <select
        bind:value={imageMode}
        class="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--paper)] px-4 py-3 font-normal"
      >
        <option value="descriptions">Read captions and descriptions</option>
        <option value="captions">Read captions only</option>
        <option value="skip">Skip illustrations</option>
      </select>
      <span class="muted mt-2 block text-xs font-normal leading-relaxed">
        Read meaningful text supplied in the book, skipping decorative images
        and repeated descriptions. Images without descriptions need review.
      </span>
    </label>
    <p class="muted text-xs leading-relaxed">
      These settings apply when preparing an EPUB again. Data table rows use the
      audiobook paragraph pause during assembly and playlist playback.
    </p>
  </div>
</fieldset>
