<script lang="ts">
  import type { GenerationSegment } from './api-models';
  import type { SpeechSelection, SpeechPreview } from './speech-annotations';
  import type { SpeechCapabilities } from './speech-support';
  import SpeechEditingText from './SpeechEditingText.svelte';
  let {
    item,
    layer = 'display',
    preview,
    capabilities,
    performanceEnabled = true,
    vocalizationsEnabled = false,
    multiVoice = false,
    speakerNames = {},
    detailsVisible = false,
    oninspect,
    onselection
  }: {
    item: GenerationSegment;
    layer?: 'display' | 'speech';
    preview?: SpeechPreview;
    capabilities?: SpeechCapabilities;
    performanceEnabled?: boolean;
    vocalizationsEnabled?: boolean;
    multiVoice?: boolean;
    speakerNames?: Record<string, string>;
    detailsVisible?: boolean;
    oninspect: (
      item: GenerationSegment,
      offset: number,
      anchor: HTMLButtonElement,
      activate: boolean
    ) => void;
    onselection?: (selection: SpeechSelection) => void;
  } = $props();
</script>

<SpeechEditingText
  {item}
  {layer}
  {preview}
  {capabilities}
  {performanceEnabled}
  {vocalizationsEnabled}
  {multiVoice}
  {speakerNames}
  {detailsVisible}
  {oninspect}
  {onselection}
  readOnly
/>
