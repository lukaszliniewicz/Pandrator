<script lang="ts">
  import { Check, Eye, LoaderCircle, Play, Plus, X } from '@lucide/svelte';
  import { onMount, tick, untrack } from 'svelte';
  import { apiJson } from './api';
  import { errorMessage } from './errors';
  import { modalFocus } from './modal-focus';
  import { notifySessionFlowChange } from './session-flow';
  import {
    speakerLabel,
    deliveryDescription,
    type SpeechPreview
  } from './speech-annotations';
  import {
    voiceCategories,
    type GenerationControls
  } from './generation-controls';
  import {
    controlFields,
    markupControls,
    setMarkupControl,
    type ControlField
  } from './speech-markup-editor';
  import {
    currentSpeakerValue,
    deliveryPatch,
    localDelivery,
    inheritDeliveryPatch,
    emptyDelivery,
    type SpeechDelivery,
    type SpeechSelectionContext,
    type SpeechInspectorTarget
  } from './speech-inspector';
  import DeliveryFields from './DeliveryFields.svelte';
  import CharacterSpeechDefaults from './CharacterSpeechDefaults.svelte';
  import { voiceLibraryApi, type VoiceCatalogPage } from './voice-library-api';
  import SpeechAutocomplete from './SpeechAutocomplete.svelte';
  import SpeechMarkupReference from './SpeechMarkupReference.svelte';
  import SpeechCapabilitySummary from './SpeechCapabilitySummary.svelte';
  import {
    deliverySupport,
    eventSupport,
    instructionTransportName,
    type SpeechCapabilities
  } from './speech-support';
  import { readable } from './audio-cpp-catalogue';

  type CandidatePreview = {
    preview_revision: string;
    locked: boolean;
    sound_changes?: { added: string[]; updated: string[]; removed: string[] };
    preview: {
      capabilities?: SpeechCapabilities;
      parts: Array<{
        start: number;
        end: number;
        voice?: string;
        instructions?: string;
        capabilities?: SpeechCapabilities;
        input?: string;
        report?: Array<{ status: string; message: string }>;
      }>;
      report?: Array<{ status: string; message: string }>;
    };
  };
  let {
    sessionId,
    target,
    multiVoice = false,
    blockedReason = '',
    canPlay = false,
    onplay,
    onclose,
    onapplied,
    onloaded,
    ondraft
  }: {
    sessionId: string;
    target: SpeechInspectorTarget;
    multiVoice?: boolean;
    blockedReason?: string;
    canPlay?: boolean;
    onplay: () => void;
    onclose: () => void;
    onapplied: () => void | Promise<void>;
    onloaded: (preview: SpeechPreview) => void;
    ondraft: (state: { dirty: boolean; pending: boolean }) => void;
  } = $props();
  let panel: HTMLElement;
  let context = $state<SpeechSelectionContext | null>(null);
  let frozen = $state<SpeechPreview | null>(null);
  let controls = $state<GenerationControls | null>(null);
  let loading = $state(true),
    pending = $state(false),
    error = $state(''),
    message = $state('');
  let speaker = $state('__keep__'),
    category = $state('unspecified'),
    voice = $state('__keep__');
  let direction = $state<SpeechDelivery>(
    untrack(() => ({
      ...emptyDelivery(),
      ...markupControls(
        String(
          target.item.speech_annotation_xml ??
            target.item.speech_plan?.speech_xml ??
            ''
        )
      )
    }))
  );
  let changedDirection = $state<Partial<SpeechDelivery>>({});
  let enablePerformance = $state(false),
    enableVocalizations = $state(false);
  let cueKind = $state('');
  let cuePosition = $state<'before' | 'after'>('before');
  let cueDuration = $state<number | undefined>();
  let candidate = $state<CandidatePreview | null>(null);
  let candidateBody = $state<Record<string, unknown> | null>(null);
  let action = $state<SpeechInspectorTarget['focus']>(
    untrack(() => target.focus ?? 'instruction')
  );
  const authoringMode = $derived(action === 'xml' ? 'xml' : 'fields');
  let rawXml = $state('');
  const compactInstruction = $derived(
    target.presentation === 'block-instruction'
  );
  const selectionMode = $derived(
    target.presentation !== 'inline' && !compactInstruction
  );
  const cursorMode = $derived(target.start === target.end);
  const voiceEnabled = $derived(
    multiVoice && Boolean(context?.current_flags.casting_enabled)
  );
  const editingVoice = $derived(action === 'speaker' || action === 'advanced');
  const editingCue = $derived(action === 'cue');
  const actionTitle = $derived(
    authoringMode === 'xml'
      ? 'Speech XML'
      : editingVoice
        ? 'Speaker and voice'
        : editingCue
          ? 'Vocal cue'
          : action === 'emotion'
            ? 'Emotion'
            : action === 'delivery'
              ? 'Delivery'
              : 'Instruction'
  );
  const wholeBlock = $derived(
    target.start === 0 &&
      target.end ===
        Array.from(target.item.optimized_text ?? target.item.text).length
  );
  const rootInstruction = $derived(wholeBlock && action === 'instruction');
  let voiceTimer: ReturnType<typeof setTimeout> | undefined;
  let characterOpen = $state(false),
    characterDirty = $state(false),
    characterPending = $state(false);
  let characterEditor = $state<{ discardChanges: () => void }>();
  let creating = $state(false),
    characterName = $state(''),
    newCategory = $state('unspecified');
  let pendingLeave = $state<(() => void) | null>(null),
    leaveCharacterOnly = $state(false);
  let voiceSearch = $state(''),
    voicePage = $state<VoiceCatalogPage | null>(null),
    voiceQuery = $state('');
  let voiceChoices = $state<Array<{ value: string; label: string }>>([]),
    voiceLoading = $state(false),
    voiceError = $state('');
  let alive = true,
    controller: AbortController | undefined,
    voiceSerial = 0;
  let detailsLoading = $state(false),
    detailsError = $state('');
  const base = $derived(
    `/sessions/${encodeURIComponent(sessionId)}/speech-plan`
  );
  const currentPreview = $derived(context?.preview ?? frozen);
  const currentService = $derived(
    context?.service ?? currentPreview?.service ?? ''
  );
  const currentModel = $derived(context?.model ?? currentPreview?.model ?? '');
  const voiceQueryKey = $derived(
    `${currentService}:${currentModel}:${voiceSearch.trim()}`
  );
  const editable = $derived(
    Boolean(context?.editable && !target.runId && !blockedReason)
  );
  const rootDirections = $derived(markupControls(context?.speech_xml ?? ''));
  const currentLocalDelivery = $derived(
    rootInstruction
      ? rootDirections
      : localDelivery(context?.current.delivery ?? {}, rootDirections)
  );
  const fieldPatch = $derived(
    deliveryPatch(currentLocalDelivery, changedDirection)
  );
  const initialVoice = $derived(
    context?.current.mixed_fields.includes('voice')
      ? '__keep__'
      : context?.current.voice || '__cast__'
  );
  const speakerChanged = $derived(
    speaker !== '__keep__' &&
      (speaker !== (context ? currentSpeakerValue(context) : '__keep__') ||
        (speaker === '__dialogue__' &&
          category !== (context?.current.voice_category || 'unspecified')))
  );
  const voiceChanged = $derived(voice !== '__keep__' && voice !== initialVoice);
  const localDirty = $derived(
    (authoringMode === 'xml'
      ? rawXml !== (context?.speech_xml ?? '')
      : speakerChanged ||
        voiceChanged ||
        Object.keys(fieldPatch).length > 0 ||
        Boolean(cueKind)) ||
      enablePerformance ||
      enableVocalizations
  );
  const dirty = $derived(
    localDirty || characterDirty || Boolean(creating && characterName.trim())
  );
  const busy = $derived(pending || characterPending);
  const selectedCharacter = $derived(
    controls?.characters.find((entry) => entry.id === speaker)
  );
  const inheritedCharacters = $derived(
    speakerChanged
      ? selectedCharacter
        ? [selectedCharacter]
        : []
      : (context?.inherited.characters ?? [])
  );
  const shownParts = $derived(
    candidate?.preview.parts ?? currentPreview?.parts ?? []
  );
  const relevantParts = $derived(
    shownParts.filter((part) =>
      cursorMode
        ? part.start <=
            Math.min(
              target.start,
              Math.max(
                0,
                Array.from(target.item.optimized_text ?? target.item.text)
                  .length - 1
              )
            ) &&
          part.end >
            Math.min(
              target.start,
              Math.max(
                0,
                Array.from(target.item.optimized_text ?? target.item.text)
                  .length - 1
              )
            )
        : part.start < target.end && part.end > target.start
    )
  );
  const capabilities = $derived(
    relevantParts[0]?.capabilities ??
      candidate?.preview.capabilities ??
      context?.capabilities ??
      currentPreview?.capabilities
  );
  const directionScope = $derived(
    wholeBlock ||
      (relevantParts.length === 1 &&
        relevantParts[0].start === target.start &&
        relevantParts[0].end === target.end)
      ? 'segment'
      : 'phrase'
  );
  const transportSupport = $derived(
    deliverySupport(
      capabilities,
      action === 'emotion' ? 'emotion' : 'instruction',
      direction[action === 'emotion' ? 'emotion' : 'instruction'],
      directionScope
    )
  );
  const transportSummary = $derived(
    transportSupport.status === 'unsupported'
      ? 'Saved, but this model will not receive it.'
      : transportSupport.status === 'unknown'
        ? 'Saved; support for this model route is unverified.'
        : transportSupport.status === 'hint' && directionScope === 'phrase'
          ? 'Sent as a hint for these words. The model may also affect nearby words.'
          : transportSupport.status === 'hint'
            ? 'Sent as a style hint. The model interprets the result.'
            : directionScope === 'phrase'
              ? 'Sent with the selected words. The model interprets the result.'
              : 'Sent for the whole voice request.'
  );
  const deliveryEnabled = $derived(
    Boolean(context?.current_flags.performance_enabled || enablePerformance)
  );
  const cueCapabilities = $derived(
    (cuePosition === 'after'
      ? shownParts.findLast(
          (part) => part.start < target.end && part.end >= target.end
        )
      : shownParts.find(
          (part) => part.start <= target.start && part.end > target.start
        )
    )?.capabilities ?? capabilities
  );
  const cueSupport = $derived(
    eventSupport(
      cueCapabilities,
      cueKind || 'pause',
      Boolean(
        context?.current_flags.allow_vocalizations || enableVocalizations
      ),
      Boolean(context?.current_flags.performance_enabled || enablePerformance)
    )
  );
  const reports = $derived.by(() => {
    const all =
      candidate?.preview.report ??
      shownParts.flatMap((part) => part.report ?? []);
    return [
      ...new Map(
        all.map((item) => [`${item.status}:${item.message}`, item])
      ).values()
    ];
  });
  const previewCurrent = $derived(
    Boolean(
      candidateBody && JSON.stringify(body()) === JSON.stringify(candidateBody)
    )
  );
  $effect(() => ondraft({ dirty, pending: busy }));
  $effect(() => {
    if (candidate && !previewCurrent) {
      candidate = null;
      candidateBody = null;
    }
  });
  $effect(() => {
    if (!panel || !selectionMode) return;
    if (pendingLeave) {
      panel.hidePopover();
      return;
    }
    panel.showPopover();
    const initialAnchor = target.anchor.getBoundingClientRect();
    const position = () => {
      const size = panel.getBoundingClientRect();
      const anchor = target.anchor.isConnected
        ? target.anchor.getBoundingClientRect()
        : initialAnchor;
      const left = target.rect.left + anchor.left - initialAnchor.left;
      const selectionTop = target.rect.top + anchor.top - initialAnchor.top;
      const selectionBottom =
        target.rect.bottom + anchor.top - initialAnchor.top;
      panel.style.left = `${Math.max(8, Math.min(left, document.documentElement.clientWidth - size.width - 8))}px`;
      const top =
        selectionBottom + size.height + 8 < innerHeight
          ? selectionBottom + 6
          : selectionTop - size.height - 6;
      panel.style.top = `${Math.max(8, Math.min(top, innerHeight - size.height - 8))}px`;
    };
    position();
    const observer = new ResizeObserver(position);
    observer.observe(panel);
    window.addEventListener('resize', position);
    window.addEventListener('scroll', position, true);
    return () => {
      observer.disconnect();
      window.removeEventListener('resize', position);
      window.removeEventListener('scroll', position, true);
    };
  });

  function selectionRequest() {
    return {
      revision_id: target.revisionId,
      segment_id: target.item.id,
      expected_segment_revision: target.item.revision,
      start: target.start,
      end: target.end
    };
  }
  function body(): Record<string, unknown> {
    if (authoringMode === 'xml')
      return {
        ...selectionRequest(),
        speech_xml: rawXml,
        expected_sound_plan_revision: context?.sound_plan_revision ?? 0,
        enable_performance: enablePerformance,
        enable_vocalizations: enableVocalizations
      };
    if (rootInstruction && context?.speech_xml)
      return {
        ...selectionRequest(),
        speech_xml: setMarkupControl(
          context.speech_xml,
          'instruction',
          direction.instruction.trim()
        ),
        expected_sound_plan_revision: context.sound_plan_revision ?? 0,
        enable_performance: enablePerformance,
        enable_vocalizations: enableVocalizations
      };
    const role =
      speaker === '__narrator__'
        ? 'narrator'
        : speaker === '__dialogue__'
          ? 'dialogue'
          : 'character';
    return {
      ...selectionRequest(),
      speaker: speakerChanged ? role : 'unchanged',
      ...(speakerChanged && role === 'character'
        ? { character_id: speaker }
        : {}),
      ...(speakerChanged && role === 'dialogue'
        ? { voice_category: category }
        : {}),
      ...(voiceChanged ? { voice: voice === '__cast__' ? null : voice } : {}),
      ...(Object.keys(fieldPatch).length
        ? { delivery: inheritDeliveryPatch(rootDirections, fieldPatch) }
        : {}),
      ...(cueKind
        ? {
            vocal_event: {
              kind: cueKind,
              position: cuePosition,
              ...(cueKind === 'pause' && cueDuration !== undefined
                ? { duration_ms: cueDuration }
                : {})
            }
          }
        : {}),
      enable_performance: enablePerformance,
      enable_vocalizations: enableVocalizations
    };
  }
  function initialize(next: SpeechSelectionContext) {
    rawXml = next.speech_xml ?? '';
    speaker = currentSpeakerValue(next);
    category = next.current.voice_category || 'unspecified';
    voice = next.current.mixed_fields.includes('voice')
      ? '__keep__'
      : next.current.voice || '__cast__';
    direction = {
      ...emptyDelivery(),
      ...Object.fromEntries(
        controlFields.map((field) => [
          field,
          localDelivery(
            next.current.delivery,
            markupControls(next.speech_xml ?? '')
          )[field] ?? ''
        ])
      )
    };
    direction.emotion_intensity = localDelivery(
      next.current.delivery,
      markupControls(next.speech_xml ?? '')
    ).emotion_intensity;
    if (rootInstruction)
      direction.instruction =
        markupControls(next.speech_xml ?? '').instruction ?? '';
    changedDirection = {};
    enablePerformance = false;
    enableVocalizations = false;
    cueKind = '';
    cuePosition = 'before';
    cueDuration = undefined;
    candidate = null;
    candidateBody = null;
  }
  async function load(initializeFields = true) {
    controller?.abort();
    const request = new AbortController();
    controller = request;
    loading = true;
    error = '';
    try {
      if (target.runId) {
        const preview = await apiJson<SpeechPreview>(`${base}/preview`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          signal: request.signal,
          body: JSON.stringify({
            revision_id: target.revisionId,
            segment_id: target.item.id,
            generation_run_id: target.runId
          })
        });
        if (!alive || request.signal.aborted) return;
        frozen = preview;
        onloaded(preview);
      } else {
        const [next, dictionary] = await Promise.all([
          apiJson<SpeechSelectionContext>(`${base}/selection-context`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            signal: request.signal,
            body: JSON.stringify(selectionRequest())
          }),
          apiJson<GenerationControls>(
            `/sessions/${encodeURIComponent(sessionId)}/generation-controls`,
            { signal: request.signal }
          )
        ]);
        if (!alive || request.signal.aborted) return;
        const earlyInput =
          compactInstruction && !context ? changedDirection : null;
        context = next;
        controls = dictionary;
        if (initializeFields) initialize(next);
        if (earlyInput && Object.keys(earlyInput).length) {
          direction = { ...direction, ...earlyInput };
          changedDirection = earlyInput;
        }
        if (next.preview) onloaded(next.preview);
      }
    } catch (caught) {
      if (alive && !request.signal.aborted) error = errorMessage(caught);
    } finally {
      if (alive && !request.signal.aborted) {
        loading = false;
        await tick();
        focusSection(target.focus);
      }
    }
  }
  export function focusSection(section: SpeechInspectorTarget['focus']) {
    if (!panel) return;
    action = section ?? 'instruction';
    void tick().then(() =>
      panel
        .querySelector<HTMLElement>(
          authoringMode === 'xml'
            ? '[data-speech-xml]'
            : section === 'emotion'
              ? '[data-delivery-input="emotion"]'
              : section === 'cue'
                ? '[data-vocal-cue]'
                : section === 'delivery' || section === 'instruction'
                  ? '[data-delivery-input="instruction"]'
                  : '[data-speaker-select] input'
        )
        ?.focus({ preventScroll: true })
    );
  }
  export function requestClose(next: () => void) {
    if (busy) return;
    if (dirty) {
      leaveCharacterOnly = false;
      pendingLeave = next;
    } else next();
  }
  function changeSpeaker(next: string) {
    const apply = () => {
      speaker = next;
      if (next !== '__keep__') voice = '__cast__';
      characterOpen = false;
      characterDirty = false;
      candidate = null;
    };
    if (characterDirty) {
      leaveCharacterOnly = true;
      pendingLeave = apply;
    } else apply();
  }
  function setDirection(field: ControlField, value: string) {
    direction = { ...direction, [field]: value };
    changedDirection = { ...changedDirection, [field]: value };
    if (field === 'emotion' && !value.trim()) setIntensity(null);
  }
  function setIntensity(value: number | null) {
    direction = { ...direction, emotion_intensity: value };
    changedDirection = { ...changedDirection, emotion_intensity: value };
  }
  async function inspect() {
    if (!editable || !localDirty || busy) return;
    pending = true;
    error = '';
    message = '';
    try {
      const proposed = body();
      candidate = await apiJson<CandidatePreview>(`${base}/selection-preview`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(proposed)
      });
      candidateBody = proposed;
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      pending = false;
    }
  }
  async function loadEffectiveRequest() {
    if (
      detailsLoading ||
      candidate ||
      currentPreview?.parts.some((part) => part.input !== undefined)
    )
      return;
    detailsLoading = true;
    detailsError = '';
    try {
      const preview = await apiJson<SpeechPreview>(`${base}/preview`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          revision_id: target.revisionId,
          segment_id: target.item.id,
          include_request: true,
          ...(target.runId ? { generation_run_id: target.runId } : {})
        })
      });
      if (!alive) return;
      if (context) context = { ...context, preview };
      else frozen = preview;
    } catch (caught) {
      if (alive) detailsError = errorMessage(caught);
    } finally {
      if (alive) detailsLoading = false;
    }
  }
  async function apply() {
    if (!editable || !candidate || !candidateBody || !previewCurrent || busy)
      return;
    pending = true;
    error = '';
    try {
      await apiJson(`${base}/selection`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...candidateBody,
          // Saving a manual edit authorizes replacing its protected directions.
          unlock_locked: true,
          expected_preview_revision: candidate.preview_revision
        })
      });
      notifySessionFlowChange(sessionId);
      await onapplied();
      await load();
      message = 'Saved for future audio.';
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      pending = false;
    }
  }
  async function saveEdit() {
    await inspect();
    if (candidate && previewCurrent) await apply();
  }
  async function addCharacter() {
    if (!controls || !characterName.trim() || busy || !editable) return;
    pending = true;
    error = '';
    const entry = {
      id: `c-${crypto.randomUUID()}`,
      display_name: characterName.trim(),
      aliases: [],
      voice_category: newCategory,
      notes: '',
      delivery: emptyDelivery(),
      locked: false,
      status: 'accepted',
      origin: 'manual'
    };
    try {
      controls = await apiJson<GenerationControls>(
        `/sessions/${encodeURIComponent(sessionId)}/generation-controls`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            expected_revision: controls.revision,
            characters: [...controls.characters, entry]
          })
        }
      );
      speaker = entry.id;
      voice = '__cast__';
      creating = false;
      characterName = '';
      characterOpen = false;
      candidate = null;
      candidateBody = null;
      notifySessionFlowChange(sessionId);
      message =
        'Speaker created. Save to assign the selected words to this speaker.';
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      pending = false;
    }
  }
  async function defaultsChanged(next: GenerationControls) {
    controls = next;
    characterDirty = false;
    candidate = null;
    candidateBody = null;
    notifySessionFlowChange(sessionId);
    await onapplied();
    await load(false);
    message = 'Character defaults saved for future audio.';
  }
  async function searchVoices(more = false) {
    if (
      !currentService ||
      !currentModel ||
      (more && voiceQuery !== voiceQueryKey)
    )
      return;
    const serial = ++voiceSerial;
    const queryKey = voiceQueryKey;
    voiceLoading = true;
    voiceError = '';
    try {
      const page = await voiceLibraryApi.query({
        service_id: currentService,
        model: currentModel,
        kind: 'provider',
        query: voiceSearch.trim(),
        limit: 30,
        ...(more && voicePage?.next_cursor
          ? { cursor: voicePage.next_cursor }
          : {})
      });
      if (!alive || serial !== voiceSerial) return;
      voicePage = page;
      voiceQuery = queryKey;
      const choices = page.items.flatMap((entry) => {
        const binding = entry.compatibility.find(
          (item) =>
            item.service_id === currentService &&
            item.model === currentModel &&
            (item.prepared ?? item.ready)
        );
        const value =
          binding?.voice ||
          (binding && entry.reference.kind === 'provider'
            ? entry.reference.voice
            : '');
        const availability =
          binding?.status === 'unknown'
            ? ' · connection unverified'
            : binding?.status === 'unavailable'
              ? ' · provider unavailable'
              : '';
        return value ? [{ value, label: entry.name + availability }] : [];
      });
      voiceChoices = [
        ...new Map(
          [...(more ? voiceChoices : []), ...choices].map((choice) => [
            choice.value,
            choice
          ])
        ).values()
      ];
    } catch (caught) {
      if (serial === voiceSerial) voiceError = errorMessage(caught);
    } finally {
      if (serial === voiceSerial) voiceLoading = false;
    }
  }
  function queueVoiceSearch(query: string) {
    voiceSearch = query;
    voiceSerial++;
    clearTimeout(voiceTimer);
    voiceTimer = setTimeout(() => void searchVoices(), 200);
  }
  onMount(() => {
    if (compactInstruction) void tick().then(() => focusSection('instruction'));
    void load();
    return () => {
      alive = false;
      controller?.abort();
      voiceSerial++;
      clearTimeout(voiceTimer);
    };
  });
</script>

<svelte:window
  onbeforeunload={(event) => {
    if (dirty || busy) event.preventDefault();
  }}
  onkeydown={(event) => {
    if (
      event.key === 'Escape' &&
      !pendingLeave &&
      event.target instanceof Node &&
      panel?.contains(event.target)
    ) {
      event.preventDefault();
      requestClose(onclose);
    }
  }}
/>
<div
  bind:this={panel}
  class="speech-inspector"
  class:selection-editor={selectionMode}
  class:inline-editor={!selectionMode}
  class:compact-instruction={compactInstruction}
  popover={selectionMode ? 'manual' : undefined}
  role={selectionMode ? 'dialog' : 'region'}
  aria-label={actionTitle}
  tabindex="-1"
>
  <header
    class="flex items-start justify-between gap-3 border-b border-[var(--line)] p-3"
  >
    <div>
      <h3 class="font-semibold">
        {compactInstruction ? 'Whole-segment instruction' : actionTitle}
      </h3>
      <p class="muted mt-1 text-xs" class:sr-only={compactInstruction}>
        Segment {target.item.ordinal + 1} · {target.start === 0 &&
        target.end ===
          Array.from(target.item.optimized_text ?? target.item.text).length
          ? 'Whole block'
          : cursorMode
            ? 'At cursor'
            : 'Selected text'}{target.runId ? ' · Historical run' : ''}
      </p>
    </div>
    <button
      class="grid size-9 shrink-0 place-items-center rounded-lg border border-[var(--line)]"
      aria-label={`Close ${actionTitle.toLowerCase()} editor`}
      disabled={busy}
      onclick={() => requestClose(onclose)}><X size={17} /></button
    >
  </header>
  <div class="inspector-content space-y-3 p-3">
    {#if compactInstruction}<label class="block text-sm font-medium">
        <span class="sr-only"
          >Whole-segment instruction for segment {target.item.ordinal + 1}</span
        >
        <textarea
          class="input w-full"
          rows="2"
          maxlength="1200"
          data-delivery-input="instruction"
          value={direction.instruction}
          disabled={busy || Boolean(blockedReason) || (!loading && !editable)}
          placeholder="For example: read calmly, with a little dry amusement"
          oninput={(event) =>
            setDirection('instruction', event.currentTarget.value)}
          onkeydown={(event) => {
            if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
              event.preventDefault();
              void saveEdit();
            }
          }}></textarea>
      </label>{/if}
    {#if !wholeBlock && !cursorMode}<blockquote
        class="max-h-28 overflow-auto whitespace-pre-wrap rounded-lg bg-[var(--accent-soft)] p-3 text-sm"
      >
        {Array.from(target.item.optimized_text ?? target.item.text)
          .slice(target.start, target.end)
          .join('')}
      </blockquote>{/if}
    {#if loading}<p class="muted flex items-center gap-2 text-sm" role="status">
        <LoaderCircle size={16} class="animate-spin" />Loading current
        directions…
      </p>{/if}
    {#if error}<p class="text-sm text-red-700" role="alert">
        {error}
        {#if !context && !frozen}<button
            class="underline"
            onclick={() => void load()}>Retry</button
          >{/if}
      </p>{/if}
    {#if message}<p class="text-sm text-[var(--accent)]" role="status">
        {message}
      </p>{/if}
    {#if target.runId}<p
        class="muted rounded-lg border border-[var(--line)] p-3 text-xs"
      >
        These voices and instructions were frozen with this run. Select the
        active mix to make changes.
      </p>{/if}
    {#if blockedReason}<p class="muted text-xs">{blockedReason}</p>{/if}
    {#if context?.edit_blocked_reason}<p class="muted text-sm">
        {context.edit_blocked_reason}
        <a class="underline" href={`/sessions/${sessionId}#speech-preparation`}
          >Open analysis and draft review</a
        >
      </p>{/if}
    {#if context?.preview_error}<p class="text-sm text-amber-700" role="status">
        {context.preview_error}
      </p>{/if}
    {#if context && editingVoice && !voiceEnabled}<p
        class="text-sm"
        role="status"
      >
        Multiple voices is off. Your draft is retained. Enable it in
        <a class="underline" href={`/sessions/${sessionId}#voice-setup`}
          >project voice setup</a
        >
        to assign speakers and voices.
      </p>{/if}
    {#if context && !loading}
      <fieldset
        disabled={!editable || busy || (editingVoice && !voiceEnabled)}
        class="space-y-4"
      >
        {#if authoringMode === 'xml'}
          <label class="block text-sm font-medium"
            >Whole-block speech XML
            <textarea
              class="input mt-2 w-full font-mono text-xs"
              data-speech-xml
              rows="12"
              maxlength="262144"
              spellcheck="false"
              bind:value={rawXml}></textarea>
          </label>
          <p class="muted text-xs">
            Preview validates the XML, speaker references and unchanged spoken
            text before you apply it.
          </p>
          {#if context.unrepresented_sound_cue_count}
            <p class="muted text-xs">
              {context.unrepresented_sound_cue_count} sound cue{context.unrepresented_sound_cue_count ===
              1
                ? ''
                : 's'} cannot be shown in this XML. They remain saved; review them
              in Project sounds.
            </p>
          {/if}
          <SpeechMarkupReference
            reference={context.markup_reference}
            characters={controls?.characters ?? []}
          />
        {:else}
          {#if editingVoice}<section
              class="editor-group speaker-section"
              aria-label="Speaker and voice"
            >
              {#if creating}
                <label class="block text-sm font-medium"
                  >New speaker name<input
                    class="input mt-1 w-full"
                    maxlength="255"
                    bind:value={characterName}
                  /></label
                >
                <label class="mt-3 block text-sm"
                  >Voice presentation (optional)<select
                    class="input mt-1 w-full"
                    bind:value={newCategory}
                  >
                    {#each voiceCategories as entry}<option value={entry}
                        >{entry === 'unspecified'
                          ? 'Unknown'
                          : entry[0].toUpperCase() + entry.slice(1)}</option
                      >{/each}
                  </select></label
                >
                <div class="mt-3 flex gap-2">
                  <button
                    class="btn btn-sm btn-primary"
                    disabled={!characterName.trim()}
                    onclick={() => void addCharacter()}>Create speaker</button
                  >
                  <button
                    class="btn btn-sm"
                    onclick={() => {
                      creating = false;
                      characterName = '';
                    }}>Cancel</button
                  >
                </div>
              {:else}
                <div class="flex items-end gap-2">
                  <div class="min-w-0 flex-1" data-speaker-select>
                    <SpeechAutocomplete
                      label="Speaker"
                      value={speaker}
                      choices={[
                        {
                          value: '__keep__',
                          label:
                            context.current.speaker === 'mixed'
                              ? 'Keep mixed speakers'
                              : 'Keep current speaker'
                        },
                        { value: '__narrator__', label: 'Narrator' },
                        { value: '__dialogue__', label: 'Unnamed dialogue' },
                        ...(controls?.characters ?? []).map((entry) => ({
                          value: entry.id,
                          label: entry.display_name,
                          searchText: entry.aliases.join(' ')
                        }))
                      ]}
                      onchange={changeSpeaker}
                      oncreate={(name) => {
                        characterName = name;
                        creating = true;
                      }}
                    />
                  </div>
                  <button
                    class="btn btn-sm mb-0.5"
                    aria-label="Add speaker"
                    title="Add speaker"
                    onclick={() => (creating = true)}><Plus size={15} /></button
                  >
                </div>
                {#if speaker === '__dialogue__'}<label
                    class="mt-3 block text-sm"
                    >Voice presentation (optional)<select
                      class="input mt-1 w-full"
                      bind:value={category}
                    >
                      {#each voiceCategories as entry}<option value={entry}
                          >{entry === 'unspecified'
                            ? 'Unknown'
                            : entry[0].toUpperCase() + entry.slice(1)}</option
                        >{/each}
                    </select></label
                  >{/if}
                <p class="speaker-voice-summary">
                  {localDirty && !candidate
                    ? voice !== '__cast__' && voice !== '__keep__'
                      ? `Voice override: ${voiceChoices.find((choice) => choice.value === voice)?.label ?? voice}. Preview checks your changes.`
                      : 'Uses the speaker’s cast voice. Preview checks your changes.'
                    : `Effective voice: ${[...new Set(relevantParts.map((part) => part.voice || 'Project default'))].join(' → ') || 'Available after preview'}`}
                </p>
                <details
                  class="voice-override"
                  open={voice !== '__cast__' && voice !== '__keep__'}
                >
                  <summary
                    >Override voice for {wholeBlock
                      ? 'this segment'
                      : 'these words'} (optional)</summary
                  >
                  <div class="mt-2 space-y-2">
                    <SpeechAutocomplete
                      label="Voice override"
                      value={voice}
                      choices={[
                        {
                          value: '__keep__',
                          label: 'Keep current voice assignments'
                        },
                        {
                          value: '__cast__',
                          label: selectedCharacter
                            ? `Use ${selectedCharacter.display_name}’s cast voice`
                            : 'Use speaker’s cast / project voice'
                        },
                        ...(context.current.voice
                          ? [
                              {
                                value: context.current.voice,
                                label: context.current.voice
                              }
                            ]
                          : []),
                        ...voiceChoices.filter(
                          (choice) => choice.value !== context?.current.voice
                        )
                      ]}
                      loading={voiceLoading}
                      onquery={queueVoiceSearch}
                      onchange={(next) => (voice = next)}
                    />
                    <p class="muted text-xs">
                      Overrides only this selection. The speaker’s cast voice
                      applies elsewhere.
                    </p>
                    {#if voicePage?.next_cursor}<button
                        type="button"
                        class="btn btn-sm"
                        disabled={voiceLoading || voiceQuery !== voiceQueryKey}
                        onclick={() => void searchVoices(true)}
                        >More voices</button
                      >{/if}
                    {#if voiceError}<p
                        class="text-xs text-red-700"
                        role="alert"
                      >
                        {voiceError}
                      </p>{/if}
                  </div>
                </details>
                <a
                  class="speaker-cast-link"
                  href={`/sessions/${sessionId}#characters-cast`}
                  >Manage speakers and cast voices</a
                >
              {/if}
            </section>{/if}
          {#if !editingVoice && !editingCue && !compactInstruction}<section
              class="editor-group delivery-section"
              aria-label="Delivery directions"
            >
              <DeliveryFields
                value={direction}
                mixed={rootInstruction ? [] : context.current.mixed_fields}
                {capabilities}
                scope={directionScope}
                enabled={Boolean(
                  context.current_flags.performance_enabled || enablePerformance
                )}
                mode={action === 'emotion'
                  ? 'emotion'
                  : action === 'instruction'
                    ? 'instruction'
                    : 'all'}
                advanced={action !== 'instruction'}
                showInstructionSupport={false}
                onchange={setDirection}
                onintensity={setIntensity}
              />
              <button
                class="text-xs text-[var(--accent)] underline"
                onclick={() => {
                  if (action === 'instruction' || action === 'emotion')
                    setDirection(action, '');
                  else {
                    direction = emptyDelivery();
                    changedDirection = emptyDelivery();
                  }
                }}
                >{action === 'instruction'
                  ? 'Clear instruction'
                  : action === 'emotion'
                    ? 'Clear emotion'
                    : 'Clear text directions'}</button
              >
            </section>{/if}
          {#if editingCue}<section
              class="editor-group"
              aria-label="Insert vocal cue"
            >
              <div class="mt-2 grid gap-3 sm:grid-cols-2">
                <label class="block"
                  >Cue
                  <select
                    class="input mt-1 w-full"
                    data-vocal-cue
                    bind:value={cueKind}
                  >
                    <option value="">No new cue</option>
                    {#each context.markup_reference.vocal_event_schema.properties.kind?.enum ?? [] as kind}
                      <option value={kind}>{readable(kind)}</option>
                    {/each}
                  </select>
                </label>
                <label class="block"
                  >Position
                  <select class="input mt-1 w-full" bind:value={cuePosition}>
                    <option value="before"
                      >{cursorMode
                        ? 'At cursor · following voice'
                        : 'Before selected text'}</option
                    >
                    <option value="after"
                      >{cursorMode
                        ? 'At cursor · previous voice'
                        : 'After selected text'}</option
                    >
                  </select>
                </label>
                {#if cueKind === 'pause'}
                  <label class="block"
                    >Pause hint (ms)
                    <input
                      class="input mt-1 w-full"
                      type="number"
                      min="50"
                      max="5000"
                      step="50"
                      placeholder="Model default"
                      bind:value={cueDuration}
                    />
                  </label>
                {/if}
              </div>
              {#if cueKind}<p
                  class="muted mt-2 text-xs"
                  title={cueSupport.description}
                >
                  <strong>{cueSupport.label}</strong> · {cueSupport.description}
                </p>{/if}
              <p class="muted mt-2 text-xs">
                A cue is generated by the speech model. A pause hint is not
                inserted silence; a vocal cue is not an attached sound file.
              </p>
            </section>{/if}
        {/if}
        {#if !editingVoice && !editingCue && authoringMode !== 'xml' && capabilities}
          {#if action !== 'emotion'}<div
              class="transport-feedback"
              data-support={transportSupport.status}
            >
              <strong>{transportSupport.label}</strong>
              <p>{transportSummary}</p>
              {#if !deliveryEnabled}<p class="delivery-off">
                  Delivery is off. This direction remains saved.
                </p>{/if}
            </div>{/if}
          <details class="model-notes">
            <summary>How {currentModel} receives it</summary>
            <p>{transportSupport.description}</p>
            {#if !wholeBlock && directionScope === 'segment'}<p>
                These words form a complete voice request, so the direction
                applies to that request.
              </p>{/if}
            {#if wholeBlock}<p>
                Phrase directions remain attached to their words and add to the
                whole-segment instruction.
              </p>{/if}
            <button
              type="button"
              class="mt-2 underline"
              disabled={detailsLoading || loading}
              onclick={() => void loadEffectiveRequest()}
            >
              Show actual compiled {instructionTransportName(capabilities)}
            </button>
            {#if detailsError}<p class="text-xs text-red-700" role="alert">
                {detailsError}
              </p>{/if}
            {#if currentPreview?.parts.some((part) => part.input !== undefined) || candidate}
              {#each relevantParts as part}<pre
                  class="compiled-style"
                  aria-label={`Compiled instructions for ${part.voice || 'project voice'}`}>{part.instructions ||
                    (capabilities.instructions === 'field'
                      ? 'No separate instructions are sent.'
                      : part.input ||
                        'No compiled request available.')}</pre>{/each}
              {#if localDirty && !candidate}<p>
                  Showing saved instructions. Preview or Save compiles your
                  draft.
                </p>{/if}
            {/if}
            <SpeechCapabilitySummary {capabilities} />
            {#each capabilities.notes ?? [] as note}<p>{note}</p>{/each}
          </details>
        {/if}
        {#if !context.current_flags.casting_enabled && authoringMode === 'xml'}
          <p class="muted text-xs">
            Multiple voices are off. Speaker labels are retained; the project
            voice is used.
          </p>{/if}
        {#if !context.current_flags.performance_enabled && !editingVoice}<label
            class="flex items-start gap-2 text-xs"
            ><input
              class="mt-0.5"
              type="checkbox"
              bind:checked={enablePerformance}
            />Use delivery directions for future audio</label
          >
          <p class="muted text-xs">
            Applies to saved text directions and character delivery defaults.
          </p>{/if}
        {#if !context.current_flags.allow_vocalizations && (authoringMode === 'xml' || (editingCue && cueKind && cueKind !== 'pause'))}
          <label class="flex items-start gap-2 text-xs">
            <input
              class="mt-0.5"
              type="checkbox"
              bind:checked={enableVocalizations}
            />
            Allow vocal cues for future audio
          </label>
          <p class="muted text-xs">
            Laughs, breaths and other vocal cues remain saved while off.
            Delivery must also be enabled; pause hints do not require this
            switch.
          </p>
        {/if}
      </fieldset>
      {#if !editingVoice && !editingCue && !compactInstruction}<details
          class="text-sm"
        >
          <summary class="cursor-pointer py-2">Inherited directions</summary>
          <div class="mt-2 space-y-3">
            {#if deliveryDescription(rootDirections)}<div>
                <p class="muted text-xs">Whole-segment direction</p>
                <p class="whitespace-pre-wrap text-sm">
                  {deliveryDescription(rootDirections)}
                </p>
              </div>{/if}
            <div>
              <p class="muted text-xs">Book direction</p>
              <p class="whitespace-pre-wrap text-sm">
                {context.inherited.general_direction || 'No book direction'}
              </p>
            </div>
            {#each inheritedCharacters as character}<div>
                <p class="muted text-xs">
                  {character.display_name} · character default
                </p>
                <p class="whitespace-pre-wrap text-sm">
                  {deliveryDescription(character.delivery ?? {}) ||
                    'No character direction'}
                </p>
              </div>{/each}
            <p class="muted text-xs">
              Clearing text directions retains these defaults. Edit the book
              direction in generation settings.
            </p>
          </div>
        </details>{/if}
      {#if editingVoice && selectedCharacter && controls && editable}<details
          open={characterOpen}
          ontoggle={(event) => {
            if (characterDirty && !event.currentTarget.open) {
              event.currentTarget.open = true;
              return;
            }
            characterOpen = event.currentTarget.open;
          }}
          class="border-t border-[var(--line)] pt-2 text-sm"
        >
          <summary class="cursor-pointer py-2"
            >{selectedCharacter.display_name} · identity and cast voice</summary
          >
          {#if characterOpen}{#key `${selectedCharacter.id}:${controls.revision}`}<CharacterSpeechDefaults
                bind:this={characterEditor}
                {sessionId}
                {controls}
                character={selectedCharacter}
                service={currentService}
                model={currentModel}
                {capabilities}
                disabled={pending || Boolean(blockedReason)}
                onchanged={defaultsChanged}
                ondraft={(state) => {
                  characterDirty = state.dirty;
                  characterPending = state.pending;
                }}
              />{/key}{/if}
        </details>{/if}
    {:else if frozen}
      <p class="text-sm font-medium">
        {[
          ...new Set(
            frozen.spans
              .filter(
                (span) => span.start < target.end && span.end > target.start
              )
              .map(speakerLabel)
          )
        ].join(' → ')}
      </p>
    {/if}
    {#if capabilities && (authoringMode === 'xml' || frozen)}<details
        class="model-notes"
      >
        <summary>{currentModel} · model support</summary>
        <SpeechCapabilitySummary {capabilities} />
        {#each capabilities.notes ?? [] as note}<p>{note}</p>{/each}
      </details>{/if}
    {#if candidate?.sound_changes && Object.values(candidate.sound_changes).some((ids) => ids.length)}
      <p
        class="rounded-lg border border-[var(--line)] p-3 text-xs"
        role="status"
      >
        Sound cues: {candidate.sound_changes.added.length} added,
        {candidate.sound_changes.updated.length} updated,
        {candidate.sound_changes.removed.length} removed. Saved sounds are mixed separately;
        these markers are not sent to the speech model.
      </p>
    {/if}
    {#if shownParts.length && !compactInstruction}<details
        open={Boolean(candidate)}
        ontoggle={(event) => {
          if (event.currentTarget.open) void loadEffectiveRequest();
        }}
        class="border-t border-[var(--line)] pt-2 text-sm"
      >
        <summary class="cursor-pointer py-2"
          >{candidate
            ? 'Review change'
            : 'Effective instructions and voice sequence'}</summary
        >
        <div class="mt-2 space-y-3">
          {#if detailsLoading}<p class="muted text-xs" role="status">
              Loading effective instructions…
            </p>{/if}
          {#if detailsError}<p class="text-xs text-red-700" role="alert">
              {detailsError}
            </p>{/if}
          <p class="text-sm">
            {shownParts
              .map((part) => part.voice || 'Project voice')
              .join(' → ')}
          </p>
          {#each relevantParts as part}<div
              class="rounded-lg bg-[var(--accent-soft)] p-3"
            >
              <p class="text-xs font-semibold">
                {part.voice || 'Project voice'}
              </p>
              <p class="mt-1 whitespace-pre-wrap text-xs">
                {part.instructions ||
                  part.input ||
                  'No separate delivery instructions.'}
              </p>
            </div>{/each}
          {#each reports as report}<p
              class:muted={report.status !== 'unsupported'}
              class:text-amber-700={report.status === 'unsupported'}
              class="text-xs"
            >
              {report.message}
            </p>{/each}
          <p class="muted text-xs">Compilation preview · no audio generated.</p>
        </div>
      </details>{/if}
    <div
      class="inspector-footer flex flex-wrap gap-2 border-t border-[var(--line)] pt-4"
    >
      {#if context?.editable && !target.runId}<button
          class="btn btn-primary btn-sm"
          disabled={!editable ||
            busy ||
            !localDirty ||
            loading ||
            (editingVoice && !voiceEnabled) ||
            (cursorMode && !cueKind)}
          onclick={() => void saveEdit()}
          ><Check size={15} />{busy
            ? 'Saving…'
            : compactInstruction
              ? 'Save instruction'
              : editingVoice
                ? 'Save assignment'
                : editingCue
                  ? 'Insert cue'
                  : 'Save'}</button
        >{/if}
      {#if context?.editable && !target.runId}<button
          class="btn btn-sm"
          disabled={!editable ||
            busy ||
            !localDirty ||
            (editingVoice && !voiceEnabled) ||
            (cursorMode && !cueKind)}
          onclick={() => void inspect()}><Eye size={15} />Preview</button
        >
      {/if}
      <button class="btn btn-sm" disabled={!canPlay || busy} onclick={onplay}
        ><Play size={14} />Play segment</button
      >
    </div>
  </div>
</div>

{#if pendingLeave}<div
    class="fixed inset-0 z-[120] grid place-items-center bg-black/45 p-4"
  >
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="speech-unsaved-title"
      use:modalFocus={{ onclose: () => (pendingLeave = null) }}
      class="surface w-full max-w-md rounded-2xl p-5"
    >
      <h3 id="speech-unsaved-title" class="font-semibold">
        Keep your speech changes?
      </h3>
      <p class="muted mt-2 text-sm">
        {leaveCharacterOnly
          ? 'The character defaults have unsaved changes.'
          : 'This speech edit has unsaved changes. Keep editing to review and apply them.'}
      </p>
      <div class="mt-4 flex justify-end gap-2">
        <button class="btn" onclick={() => (pendingLeave = null)}
          >Keep editing</button
        ><button
          class="btn btn-primary"
          onclick={() => {
            const next = pendingLeave;
            characterEditor?.discardChanges();
            characterDirty = false;
            if (!leaveCharacterOnly) {
              if (context) initialize(context);
              creating = false;
              characterName = '';
            }
            pendingLeave = null;
            next?.();
          }}>Discard and continue</button
        >
      </div>
    </div>
  </div>{/if}

<style>
  .speech-inspector {
    display: flex;
    flex-direction: column;
    min-width: 0;
    width: 100%;
    max-width: 100%;
    background: var(--paper-strong);
    color: var(--ink);
  }
  .inspector-content {
    min-height: 0;
    overflow-y: auto;
    flex: 1;
  }
  .editor-group {
    padding: 0.7rem;
    border: 1px solid var(--line);
    border-radius: 0.7rem;
    background: color-mix(in srgb, var(--paper-strong) 92%, var(--accent-soft));
  }
  .speech-inspector :global(.input) {
    padding: 0.5rem 0.65rem;
    border: 1px solid color-mix(in srgb, var(--line) 80%, var(--muted));
    border-radius: 0.45rem;
    background: color-mix(in srgb, var(--paper-strong) 80%, var(--accent-soft));
    color: var(--ink);
  }
  .speech-inspector :global(.input:focus-visible) {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }
  .speaker-voice-summary {
    margin-top: 0.65rem;
    font-size: 0.72rem;
    line-height: 1.5;
    color: var(--muted);
  }
  .voice-override {
    margin-top: 0.7rem;
    font-size: 0.75rem;
  }
  .voice-override summary {
    cursor: pointer;
  }
  .speaker-cast-link {
    display: inline-block;
    margin-top: 0.75rem;
    font-size: 0.72rem;
    color: var(--accent);
    text-decoration: underline;
  }
  .model-notes {
    font-size: 0.7rem;
    color: var(--muted);
    line-height: 1.5;
  }
  .model-notes summary {
    cursor: pointer;
  }
  .model-notes p {
    margin-top: 0.4rem;
  }
  .selection-editor {
    position: fixed;
    margin: 0;
    inset: auto;
    width: min(25rem, calc(100vw - 16px));
    max-height: min(34rem, calc(100dvh - 16px));
    border: 1px solid var(--line);
    border-radius: 0.85rem;
    overflow: hidden;
    box-shadow: var(--shadow);
  }
  .selection-editor blockquote {
    max-height: 4rem;
  }
  .inline-editor {
    margin: 0.75rem 0;
    border: 1px solid var(--line);
    border-radius: 0.75rem;
    overflow: hidden;
    text-align: left;
  }
  .inline-editor .inspector-content {
    max-height: none;
  }
  .compact-instruction {
    margin: 0.5rem 0;
  }
  .compact-instruction header {
    padding: 0.6rem 0.75rem;
    align-items: center;
    font-size: 0.8rem;
  }
  .compact-instruction .editor-group {
    padding: 0.5rem;
  }
  .transport-feedback {
    padding: 0.7rem;
    border: 1px solid var(--line);
    border-radius: 0.6rem;
    background: color-mix(in srgb, var(--accent-soft) 45%, var(--paper-strong));
    font-size: 0.73rem;
    line-height: 1.5;
  }
  .transport-feedback p {
    margin-top: 0.4rem;
  }
  .compiled-style {
    margin-top: 0.5rem;
    max-height: 9rem;
    overflow: auto;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font-size: 0.7rem;
  }
  .selection-editor .inspector-footer {
    position: sticky;
    bottom: -0.75rem;
    z-index: 2;
    margin: 0 -0.75rem -0.75rem;
    padding: 0.65rem 0.75rem;
    background: var(--paper-strong);
  }
</style>
