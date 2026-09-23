<script lang="ts">
  import { onDestroy, onMount, untrack } from 'svelte';
  import { beforeNavigate, goto } from '$app/navigation';
  import { apiJson } from './api';
  import { sessionApi } from './domain-api';
  import type { SettingsPayload } from './api-models';
  import { errorMessage } from './errors';
  import { modalFocus } from './modal-focus';
  import { invalidationBus, invalidates } from './invalidation';
  import GenerationCastPanel from './GenerationCastPanel.svelte';
  import type { Character, CastDraftController } from './generation-controls';
  import {
    controlFields,
    markupControls,
    setMarkupControl,
    clearMarkupDirections,
    markupSummary,
    markSpokenRange,
    readMarkup,
    type ControlField
  } from './speech-markup-editor';
  type Annotation = {
    decision: 'none' | 'steer';
    delivery?: Partial<Record<ControlField, string>>;
    spans?: unknown[];
    events?: unknown[];
    reason?: string;
    locked?: boolean;
    [key: string]: unknown;
  };
  type Unit = {
    id: string;
    ordinal: number;
    text: string;
    spoken_text: string;
    annotation: Annotation | null;
    speech_xml?: string | null;
  };
  type Plan = {
    id: string;
    plan_revision_id: string;
    status: string;
    version: number;
    total: number;
    filtered_total?: number;
    analysed_count: number;
    steered_count: number;
    locked_count: number;
    stale: boolean;
    job_id?: string;
    job_status?: string;
    items?: Unit[];
    settings: {
      workflow_kind?: string;
      mode?: string;
      instructions?: string;
      annotation_format?: string;
      context_before?: number;
      context_after?: number;
    };
  };
  type Capability = {
    model: string;
    instructions: string;
    semantic_context: string;
    status: string;
  };
  type Preview = {
    transcript: string;
    input: string;
    instructions: string;
    request_options?: Record<string, unknown>;
    report: { status: string; control: string; message: string }[];
    capabilities: Capability;
    parts?: {
      text: string;
      voice?: string;
      voice_source?: string;
      fallback?: boolean;
      input?: string;
      instructions?: string;
      request_options?: Record<string, unknown>;
      report?: { status: string; control: string; message: string }[];
    }[];
  };
  const voiceSourceLabels: Record<string, string> = {
    segment: 'Block voice override',
    span: 'Explicit span voice',
    character: 'Character cast',
    source_speaker: 'Source speaker cast',
    category: 'Category default',
    narrator: 'Narrator',
    inherited: 'Session voice',
    base: 'Session voice',
    mixed: 'Combined assignments'
  };
  let {
    sessionId,
    revisionId,
    initialOpen = false,
    initialSegmentId = '',
    initialOrdinal = 0,
    showCasting = true,
    externalCastPanel,
    busy = false,
    onchanged
  }: {
    sessionId: string;
    revisionId: string;
    initialOpen?: boolean;
    initialSegmentId?: string;
    initialOrdinal?: number;
    showCasting?: boolean;
    externalCastPanel?: CastDraftController;
    busy?: boolean;
    onchanged?: () => void;
  } = $props();
  let pending = $state(false),
    error = $state(''),
    message = $state('');
  let history = $state<Plan[]>([]),
    selected = $state<Plan | null>(null),
    capability = $state<Capability | null>(null);
  let stored = $state<SettingsPayload | null>(null);
  let general = $state(''),
    contextMode = $state('off'),
    before = $state(2),
    after = $state(1),
    maxChars = $state(4000);
  let enabled = $state(false),
    casting = $state(false),
    vocalizations = $state(false),
    settingsBaseline = $state('');
  let planningInstructions = $state(''),
    modelName = $state(''),
    offset = $state(untrack(() => Math.floor(initialOrdinal / 20) * 20)),
    filter = $state('all');
  let unitId = $state(untrack(() => initialSegmentId)),
    draft = $state<Annotation>({ decision: 'none' }),
    xml = $state(''),
    xmlMode = $state(false),
    locked = $state(true),
    unlock = $state(false);
  let advanced = $state(false),
    jsonBuffer = $state(''),
    parseError = $state(''),
    editorBaseline = $state('');
  let preview = $state<Preview | null>(null),
    previewKey = $state('');
  let opened = $state(untrack(() => initialOpen)),
    externalRevision = $state(0);
  let characters = $state<Character[]>([]),
    speaker = $state(''),
    category = $state('unspecified');
  let textArea = $state<HTMLTextAreaElement>(),
    selectionStart = $state(0),
    selectionEnd = $state(0);
  let navigation = $state<(() => void | Promise<void>) | null>(null),
    acceptMissing = $state(false);
  let routeNavigation = $state(false);
  let allowRouteNavigation = false;
  let castPanel = $state<{
    draftState: () => { dirty: boolean; blocked: boolean; valid: boolean };
    saveChanges: () => Promise<boolean>;
    discardChanges: () => void;
  }>();
  const activeCastPanel = $derived(externalCastPanel ?? castPanel);
  const castDraft = $derived(activeCastPanel?.draftState());
  let alive = true,
    serial = 0,
    previewSerial = 0;
  const base = $derived(
    `/sessions/${encodeURIComponent(sessionId)}/performance-plans`
  );
  const currentUnit = $derived(
    selected?.items?.find((item) => item.id === unitId)
  );
  const editable = $derived(selected?.status === 'draft' && !selected.stale);
  const blocked = $derived(pending || busy);
  const settingsKey = $derived(
    JSON.stringify({
      general,
      contextMode,
      before,
      after,
      maxChars,
      enabled,
      casting,
      vocalizations
    })
  );
  const settingsDirty = $derived(
    Boolean(stored) && settingsKey !== settingsBaseline
  );
  const editorKey = $derived(
    JSON.stringify({
      content: xmlMode ? xml : draft,
      locked,
      invalid: parseError ? jsonBuffer : ''
    })
  );
  const editorDirty = $derived(
    Boolean(currentUnit) && editorKey !== editorBaseline
  );
  const requestKey = $derived(
    JSON.stringify({
      editorKey,
      settingsKey,
      unitId,
      plan: selected?.id,
      model: capability?.model,
      storedRevision: stored?.revision,
      externalRevision
    })
  );
  const previewStale = $derived(Boolean(preview) && previewKey !== requestKey);
  const controls = $derived(
    xmlMode ? markupControls(xml) : (draft.delivery ?? {})
  );
  const summaries = $derived(
    xmlMode
      ? markupSummary(xml)
      : [
          ...(draft.spans ?? []).map(
            (item) => `Phrase: ${JSON.stringify(item)}`
          ),
          ...(draft.events ?? []).map(
            (item) => `Event: ${JSON.stringify(item)}`
          )
        ]
  );
  const total = $derived(selected?.filtered_total ?? selected?.total ?? 0);
  const validWindows = $derived(
    [before, after, maxChars].every(Number.isInteger) &&
      before >= 0 &&
      before <= 20 &&
      after >= 0 &&
      after <= 20 &&
      maxChars >= 0 &&
      maxChars <= 16000
  );
  const service = $derived(
    String(stored?.effective.service ?? stored?.effective.tts_service ?? '')
  );
  const model = $derived(
    String(
      stored?.effective.model ??
        stored?.effective.tts_model ??
        stored?.effective.xtts_model ??
        ''
    )
  );
  beforeNavigate((event) => {
    if (
      allowRouteNavigation ||
      event.willUnload ||
      !(editorDirty || settingsDirty || castDraft?.dirty)
    )
      return;
    const target = event.to?.url;
    if (
      !target ||
      (target.pathname === event.from?.url.pathname &&
        target.search === event.from?.url.search)
    )
      return;
    event.cancel();
    routeNavigation = true;
    navigation = async () => {
      allowRouteNavigation = true;
      try {
        await goto(target, { replaceState: event.type === 'popstate' });
      } finally {
        allowRouteNavigation = false;
      }
    };
  });
  onMount(() =>
    invalidationBus.subscribe((change) => {
      if (
        invalidates(change, 'generation', sessionId) ||
        invalidates(change, 'workflow', sessionId) ||
        invalidates(change, 'voices') ||
        invalidates(change, 'capabilities')
      ) {
        externalRevision++;
        if (opened && !pending) void load();
      }
    })
  );
  onDestroy(() => {
    alive = false;
    serial++;
    previewSerial++;
  });

  function selectUnit(id: string) {
    unitId = id;
    const unit = selected?.items?.find((item) => item.id === id);
    draft = $state.snapshot(unit?.annotation ?? { decision: 'none' });
    xml = unit?.speech_xml ?? '';
    xmlMode = Boolean(xml);
    locked = unit?.annotation?.locked ?? true;
    unlock = false;
    parseError = '';
    jsonBuffer = JSON.stringify(draft, null, 2);
    editorBaseline = JSON.stringify({
      content: xmlMode ? xml : draft,
      locked,
      invalid: ''
    });
    preview = null;
    advanced = false;
    selectionStart = 0;
    selectionEnd = 0;
  }
  function navigate(next: () => void | Promise<void>) {
    routeNavigation = false;
    if (editorDirty) navigation = next;
    else void next();
  }
  export function requestClose(next: () => void) {
    if (pending) return;
    if (editorDirty || settingsDirty || castDraft?.dirty) {
      routeNavigation = true;
      navigation = next;
    } else next();
  }
  function applySettings(settings: SettingsPayload) {
    general = String(settings.effective.generation_prompt ?? '');
    contextMode = String(settings.effective.tts_context_mode ?? 'off');
    before = Number(settings.effective.performance_context_before ?? 2);
    after = Number(settings.effective.performance_context_after ?? 1);
    maxChars = Number(settings.effective.performance_context_max_chars ?? 4000);
    enabled = Boolean(settings.effective.performance_enabled);
    casting = Boolean(settings.effective.casting_enabled);
    vocalizations = Boolean(settings.effective.performance_allow_vocalizations);
    settingsBaseline = JSON.stringify({
      general,
      contextMode,
      before,
      after,
      maxChars,
      enabled,
      casting,
      vocalizations
    });
  }
  async function load(planId?: string, newOffset = offset, preserve = true) {
    const request = ++serial;
    pending = true;
    error = '';
    const oldUnit = unitId,
      oldPlan = selected?.id,
      keepEditor = preserve && editorDirty,
      keepSettings = settingsDirty;
    try {
      const [listing, settings, dictionary] = await Promise.all([
        apiJson<{ items: Plan[]; capabilities: Capability }>(
          `${base}?limit=100&plan_revision_id=${encodeURIComponent(revisionId)}`
        ),
        sessionApi.settings(sessionId, 'tts'),
        apiJson<{ characters: Character[] }>(
          `/sessions/${encodeURIComponent(sessionId)}/generation-controls`
        )
      ]);
      if (!alive || request !== serial) return;
      history = listing.items;
      capability = listing.capabilities;
      characters = dictionary.characters;
      if (!keepSettings) applySettings(settings);
      // A draft based on an old revision must conflict instead of overwriting another client's settings.
      if (!keepSettings || !stored) stored = settings;
      const id = planId ?? selected?.id ?? history[0]?.id;
      if (id) {
        const result = await apiJson<Plan>(
          `${base}/${encodeURIComponent(id)}?offset=${newOffset}&limit=20&filter=${filter}`
        );
        if (!alive || request !== serial) return;
        const oldVersion = selected?.version;
        selected = result;
        offset = newOffset;
        if (
          keepEditor &&
          oldPlan === result.id &&
          result.items?.some((unit) => unit.id === oldUnit)
        ) {
          // Keep the revision the draft was based on; save must surface a conflict after concurrent changes.
          if (oldVersion !== result.version) {
            selected = { ...result, version: oldVersion ?? result.version };
            message =
              'The saved version changed. Your editor draft is preserved; save will check for conflicts.';
          }
        } else
          selectUnit(
            result.items?.some((unit) => unit.id === oldUnit)
              ? oldUnit
              : (result.items?.[0]?.id ?? '')
          );
      } else selected = null;
    } catch (caught) {
      if (alive && request === serial) error = errorMessage(caught);
    } finally {
      if (alive && request === serial) pending = false;
    }
  }
  async function action<T>(
    path: string,
    body: unknown,
    method = 'POST'
  ): Promise<T> {
    return apiJson<T>(base + path, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
  }
  async function saveSettings() {
    if (!stored || !validWindows) return false;
    pending = true;
    error = '';
    try {
      const result = await sessionApi.saveSettings(
        sessionId,
        'tts',
        stored.revision,
        {
          ...stored.override,
          generation_prompt: general,
          tts_context_mode: contextMode,
          performance_context_before: before,
          performance_context_after: after,
          performance_context_max_chars: maxChars,
          performance_enabled: enabled,
          casting_enabled: casting,
          performance_allow_vocalizations: vocalizations
        }
      );
      stored = result;
      applySettings(result);
      message =
        'Generation defaults saved. Running requests retain their snapshot.';
      onchanged?.();
      return true;
    } catch (caught) {
      error = errorMessage(caught);
      return false;
    } finally {
      pending = false;
    }
  }
  function setControl(field: ControlField, value: string) {
    try {
      if (xmlMode) xml = setMarkupControl(xml, field, value);
      else {
        const delivery = { ...draft.delivery, [field]: value };
        draft = {
          ...draft,
          delivery,
          decision:
            Object.values(delivery).some(Boolean) ||
            draft.spans?.length ||
            draft.events?.length
              ? 'steer'
              : 'none'
        };
        jsonBuffer = JSON.stringify(draft, null, 2);
      }
      parseError = '';
    } catch (caught) {
      parseError = errorMessage(caught);
    }
  }
  function editAdvanced(value: string) {
    try {
      if (xmlMode) {
        xml = value;
        readMarkup(xml);
      } else {
        jsonBuffer = value;
        const parsed = JSON.parse(value);
        if (!parsed || !['none', 'steer'].includes(parsed.decision))
          throw new Error('Annotation needs decision none or steer.');
        draft = parsed;
        locked = Boolean(parsed.locked);
      }
      parseError = '';
    } catch (caught) {
      parseError = errorMessage(caught);
    }
  }
  function itemPayload() {
    if (parseError) throw new Error(parseError);
    return xmlMode
      ? { speech_xml: xml, locked, reason: draft.reason ?? '' }
      : { annotation: { ...draft, locked } };
  }
  async function saveAnnotation() {
    if (!selected || !currentUnit || !editable) return false;
    pending = true;
    error = '';
    message = '';
    try {
      await action(
        `/${selected.id}`,
        {
          expected_version: selected.version,
          unlock_locked: unlock,
          items: [{ segment_id: currentUnit.id, ...itemPayload() }]
        },
        'PATCH'
      );
      await load(selected.id, offset, false);
      message =
        'Block directions and speaker assignments saved. Spoken words are unchanged.';
      return true;
    } catch (caught) {
      error = errorMessage(caught);
      return false;
    } finally {
      pending = false;
    }
  }
  async function create(mode: 'manual' | 'passive' | 'llm', copy = false) {
    if (!validWindows) return;
    if (settingsDirty && !(await saveSettings())) return;
    pending = true;
    error = '';
    message = '';
    try {
      const result = await action<Plan>('', {
        expected_plan_revision_id: revisionId,
        mode,
        annotation_format: copy
          ? (selected?.settings.annotation_format ?? 'pssml')
          : 'xml',
        model_name: modelName,
        instructions: planningInstructions,
        context_before: before,
        context_after: after,
        context_max_chars: maxChars,
        allow_vocalizations: vocalizations,
        ...(copy && selected ? { copy_from_id: selected.id } : {})
      });
      filter = 'all';
      await load(result.id, 0, false);
      message =
        mode === 'llm'
          ? 'Delivery analysis queued. Review the result before adopting it.'
          : mode === 'passive'
            ? 'Passive analysis is ready for an MCP worker to claim. No model or speech service was started.'
            : 'Editable speech-direction draft created.';
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      pending = false;
    }
  }
  async function compilePreview() {
    if (!selected || !currentUnit) return;
    const key = requestKey,
      request = ++previewSerial;
    pending = true;
    error = '';
    try {
      const payload = itemPayload();
      const result = await action<Preview>(`/${selected.id}/preview`, {
        segment_id: currentUnit.id,
        ...(xmlMode ? { speech_xml: xml } : payload),
        generation_prompt: general,
        context_mode: contextMode,
        context_before: before,
        context_after: after,
        context_max_chars: maxChars,
        allow_vocalizations: vocalizations,
        casting_enabled: casting,
        performance_enabled: enabled
      });
      if (alive && request === previewSerial && key === requestKey) {
        preview = result;
        previewKey = key;
      }
    } catch (caught) {
      if (alive && request === previewSerial) error = errorMessage(caught);
    } finally {
      if (alive && request === previewSerial) pending = false;
    }
  }
  async function adopt() {
    if (!selected) return;
    if (editorDirty && !(await saveAnnotation())) return;
    if (settingsDirty && !(await saveSettings())) return;
    pending = true;
    error = '';
    try {
      await action(`/${selected.id}/adopt`, {
        expected_version: selected.version,
        accept_unanalysed: acceptMissing,
        enable: true
      });
      await load(selected.id, offset, false);
      message = 'Speech directions adopted for future generation.';
      onchanged?.();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      pending = false;
    }
  }
  async function resumeAnalysis() {
    if (!selected) return;
    pending = true;
    error = '';
    try {
      await action(`/${selected.id}/analyse`, {});
      await load(selected.id);
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      pending = false;
    }
  }
  function clearDirections() {
    if (xmlMode) xml = clearMarkupDirections(xml);
    else {
      draft = { decision: 'none', locked };
      jsonBuffer = JSON.stringify(draft, null, 2);
    }
    parseError = '';
  }
  function markSelection() {
    try {
      xml = markSpokenRange(
        xml,
        selectionStart,
        selectionEnd,
        speaker,
        category
      );
      parseError = '';
    } catch (caught) {
      error = errorMessage(caught);
    }
  }
</script>

<svelte:window
  onbeforeunload={(event) => {
    if (editorDirty || settingsDirty || castDraft?.dirty)
      event.preventDefault();
  }}
/>
<details
  open={opened}
  class="speech-controls mt-5 border-t border-[var(--line)] pt-5 sm:rounded-2xl sm:border sm:p-4"
  ontoggle={(event) => {
    opened = event.currentTarget.open;
    if (event.currentTarget.open && !stored && !pending) void load();
  }}
>
  <summary class="cursor-pointer font-semibold"
    >Speech direction <span class="muted ml-2 text-xs"
      >Context · dialogue · cast</span
    ></summary
  >
  <div class="mt-4 space-y-5">
    <p class="muted text-sm">
      Keep accepted wording and segment boundaries while directing delivery and
      assigning voices. {selected?.settings.workflow_kind === 'voiceover'
        ? 'Voiceover parts share their segment’s timing window; fit is assessed after assembly.'
        : 'For long-form narration, dialogue turns can flow without paragraph-length pauses.'}
    </p>
    {#if error}<p class="text-sm text-red-700" role="alert">{error}</p>{/if}
    {#if message}<p class="muted text-sm" role="status">{message}</p>{/if}
    {#if showCasting}<GenerationCastPanel
        bind:this={castPanel}
        {sessionId}
        {service}
        {model}
        sessionVoice={String(stored?.effective.voice ?? '')}
        {busy}
        onchanged={(items) => {
          characters = items;
          preview = null;
          onchanged?.();
        }}
      />{/if}
    <fieldset disabled={blocked} class="space-y-3">
      <legend class="mb-2 font-semibold">Generation defaults</legend>
      <label class="block text-sm"
        >General speech direction<textarea
          class="input mt-1 w-full"
          rows="2"
          maxlength="4000"
          bind:value={general}
          placeholder="Natural, restrained narration."></textarea></label
      >
      <div class="grid gap-3 sm:grid-cols-2">
        <label class="text-sm"
          >Context sent to speech model<select
            class="input mt-1 w-full"
            bind:value={contextMode}
            ><option value="off">Off</option><option value="before"
              >Preceding text</option
            ><option value="both">Preceding and following text</option></select
          ></label
        >
        <p class="muted text-xs self-center">
          Only supporting models receive semantic context. Preview shows what
          the selected model can encode or approximate.
        </p>
      </div>
      <div class="grid gap-3 sm:grid-cols-3">
        <label class="text-sm"
          >Before (blocks)<input
            class="input mt-1 w-full"
            type="number"
            min="0"
            max="20"
            bind:value={before}
          /></label
        ><label class="text-sm"
          >After (blocks)<input
            class="input mt-1 w-full"
            type="number"
            min="0"
            max="20"
            bind:value={after}
          /></label
        ><label class="text-sm"
          >Context character limit<input
            class="input mt-1 w-full"
            type="number"
            min="0"
            max="16000"
            bind:value={maxChars}
          /></label
        >
      </div>
      <label class="flex items-center gap-2 text-sm"
        ><input type="checkbox" bind:checked={enabled} />Use adopted speech
        directions</label
      >
      <label class="flex items-center gap-2 text-sm"
        ><input type="checkbox" bind:checked={casting} />Use character and
        dialogue voices</label
      >
      <label class="flex items-center gap-2 text-sm"
        ><input type="checkbox" bind:checked={vocalizations} />Allow explicitly
        requested vocalizations</label
      >
      <div class="flex flex-wrap items-center gap-2">
        <button
          class="btn"
          disabled={!stored || !validWindows || !settingsDirty}
          onclick={() => void saveSettings()}>Save generation defaults</button
        >{#if settingsDirty}<span class="text-xs text-amber-700"
            >Unsaved settings</span
          ><button
            class="btn"
            onclick={() => {
              if (stored) applySettings(stored);
            }}>Discard settings changes</button
          >{/if}
      </div>
    </fieldset>
    <details class="rounded-xl border border-[var(--line)] p-3">
      <summary class="cursor-pointer font-semibold"
        >Delivery analysis <span class="muted ml-2 text-xs">Optional</span
        ></summary
      >
      <fieldset disabled={blocked || !validWindows} class="mt-3 space-y-3">
        <label class="block text-sm"
          >Analysis guidance<textarea
            class="input mt-1 w-full"
            rows="2"
            maxlength="6000"
            bind:value={planningInstructions}
            placeholder="Direct only where context changes how a line should be spoken."
          ></textarea></label
        >
        <label class="block text-sm"
          >Analysis model <span class="muted text-xs"
            >Blank uses the configured default</span
          ><input
            class="input mt-1 w-full"
            bind:value={modelName}
            maxlength="255"
          /></label
        >
        <p class="muted text-xs">
          Analyse delivery uses the configured LLM. Passive analysis lets an MCP
          client claim and submit work without starting a model. Both produce a
          draft for review and leave the words unchanged.
        </p>
        <div class="flex flex-wrap gap-2">
          <button
            class="btn btn-primary"
            onclick={() => navigate(() => create('llm'))}
            >Analyse delivery</button
          ><button class="btn" onclick={() => navigate(() => create('passive'))}
            >Prepare passive analysis</button
          >
        </div>
      </fieldset>
    </details>
    <div
      class="flex flex-wrap items-end gap-2 border-t border-[var(--line)] pt-4"
    >
      <h4 class="mr-auto font-semibold">Speech direction review</h4>
      <button
        class="btn"
        disabled={blocked}
        onclick={() => navigate(() => create('manual'))}
        >Create manual draft</button
      >{#if selected && !selected.stale}<button
          class="btn"
          disabled={blocked}
          onclick={() => navigate(() => create('manual', true))}
          >Copy for editing</button
        >{/if}<button class="btn" disabled={blocked} onclick={() => void load()}
        >Refresh</button
      >
    </div>
    {#if capability}<p class="muted text-xs">
        Model: {capability.model || 'not selected'} · Directions: {capability.instructions}
        · Context: {capability.semantic_context}. Encoded controls still need an
        audio listening check.
      </p>{/if}
    {#if history.length}<label class="block text-sm"
        >Direction version<select
          class="input mt-1 w-full"
          disabled={blocked}
          value={selected?.id ?? ''}
          onchange={(event) => {
            const id = event.currentTarget.value;
            navigate(() => load(id, 0, false));
          }}
          >{#each history as plan}<option value={plan.id}
              >{plan.status} · {plan.steered_count} directed / {plan.total} blocks{plan.stale
                ? ' · stale'
                : ''}</option
            >{/each}</select
        ></label
      >{/if}
    {#if selected}
      <p class="muted text-sm">
        {selected.analysed_count} of {selected.total} analysed · {selected.locked_count}
        protected{selected.job_status
          ? ` · Analysis ${selected.job_status}`
          : ''}.
      </p>
      {#if selected.job_id}<a class="text-sm underline" href="/activity"
          >Open analysis job</a
        >{/if}
      <details class="text-xs muted">
        <summary class="cursor-pointer">Saved analysis configuration</summary>
        <p class="mt-2 whitespace-pre-wrap">
          {selected.settings.mode ?? 'manual'} · {selected.settings
            .context_before ?? 2} before / {selected.settings.context_after ??
            1} after. {selected.settings.instructions ||
            'No extra analysis guidance.'}
        </p>
      </details>
      {#if selected.stale}<p role="status" class="text-sm text-amber-700">
          Accepted speech changed. Create a new matching draft before adopting
          directions.
        </p>{/if}
      <label class="block text-sm"
        >Show blocks<select
          class="input mt-1 w-full"
          disabled={blocked}
          value={filter}
          onchange={(event) => {
            const value = event.currentTarget.value;
            navigate(() => {
              filter = value;
              return load(selected?.id, 0, false);
            });
          }}
          ><option value="all">All blocks</option><option value="directed"
            >Directed</option
          ><option value="unreviewed">Unanalysed</option><option value="locked"
            >Protected</option
          ><option value="dialogue">Dialogue</option><option value="unresolved"
            >Unresolved speakers</option
          ></select
        ></label
      >
      <nav
        aria-label="Speech blocks"
        class="max-h-48 overflow-auto rounded-xl border border-[var(--line)] divide-y divide-[var(--line)]"
      >
        {#each selected.items ?? [] as unit}<button
            class="block w-full p-3 text-left text-sm hover:bg-[var(--accent-soft)]"
            class:bg-[var(--accent-soft)]={unitId === unit.id}
            aria-current={unitId === unit.id ? 'true' : undefined}
            disabled={blocked}
            onclick={() => navigate(() => selectUnit(unit.id))}
            ><span class="font-medium">#{unit.ordinal + 1}</span> · {unit
              .annotation?.decision === 'steer'
              ? 'Directed'
              : unit.annotation
                ? 'Reviewed'
                : 'Unanalysed'}{unit.annotation?.locked
              ? ' · Protected'
              : ''}<span class="block truncate">{unit.spoken_text}</span
            ></button
          >{/each}
      </nav>
      <div class="flex flex-wrap items-center gap-2 text-xs">
        <button
          class="btn"
          disabled={blocked || !offset}
          onclick={() =>
            navigate(() => load(selected?.id, Math.max(0, offset - 20), false))}
          >Previous blocks</button
        ><span
          >{total ? offset + 1 : 0}–{Math.min(offset + 20, total)} of {total}</span
        ><button
          class="btn"
          disabled={blocked || offset + 20 >= total}
          onclick={() => navigate(() => load(selected?.id, offset + 20, false))}
          >Next blocks</button
        >
      </div>
      {#if currentUnit}
        <label class="block text-sm font-medium"
          >Accepted spoken text <span class="muted text-xs font-normal"
            >Select words below to mark dialogue</span
          ><textarea
            class="input mt-1 w-full whitespace-pre-wrap"
            rows="4"
            readonly
            value={currentUnit.spoken_text}
            bind:this={textArea}
            onselect={() => {
              selectionStart = textArea?.selectionStart ?? 0;
              selectionEnd = textArea?.selectionEnd ?? 0;
            }}></textarea></label
        >
        {#if draft.reason}<p class="muted text-xs">
            Rationale: {draft.reason}
          </p>{/if}
        {#if xmlMode && editable}<fieldset
            disabled={blocked}
            class="flex flex-wrap items-end gap-2"
          >
            <label class="min-w-40 flex-1 text-sm"
              >Selected words belong to<select
                class="input mt-1 w-full"
                bind:value={speaker}
                ><option value="">Unnamed dialogue</option><option
                  value="__narrator">Narrator</option
                >{#each characters as character}<option value={character.id}
                    >{character.display_name}</option
                  >{/each}</select
              ></label
            >{#if !speaker}<label class="text-sm"
                >Voice category<select
                  class="input mt-1 w-full"
                  bind:value={category}
                  ><option value="unspecified">Unknown</option><option
                    value="male">Male</option
                  ><option value="female">Female</option><option
                    value="androgynous">Androgynous</option
                  ></select
                ></label
              >{/if}<button
              class="btn"
              disabled={selectionEnd <= selectionStart}
              onclick={markSelection}>Mark selected text</button
            >
          </fieldset>{/if}
        <fieldset
          disabled={blocked || !editable || Boolean(parseError)}
          class="grid gap-3 sm:grid-cols-2"
        >
          <label class="block text-sm sm:col-span-2"
            >Block delivery direction<textarea
              class="input mt-1 w-full"
              rows="2"
              maxlength="1200"
              value={controls.instruction ?? ''}
              oninput={(event) =>
                setControl('instruction', event.currentTarget.value)}
              placeholder="Add a local direction; other controls remain active."
            ></textarea></label
          >
          {#each controlFields.filter((field) => field !== 'instruction') as field}<label
              class="block text-sm"
              >{field[0].toUpperCase() +
                field.slice(1)}{#if field === 'emotion'}<input
                  class="input mt-1 w-full"
                  maxlength="120"
                  value={controls[field] ?? ''}
                  oninput={(event) =>
                    setControl(field, event.currentTarget.value)}
                  placeholder="No local emotion"
                />{:else}<select
                  class="input mt-1 w-full"
                  value={controls[field] ?? ''}
                  onchange={(event) =>
                    setControl(field, event.currentTarget.value)}
                  ><option value="">No local override</option
                  >{#each field === 'pace' ? ['natural', 'slower', 'brisk'] : field === 'cadence' ? ['continuing', 'concluding', 'questioning', 'contrast'] : ['light', 'moderate', 'strong'] as option}<option
                      value={option}>{option}</option
                    >{/each}</select
                >{/if}</label
            >{/each}
          <div class="sm:col-span-2 flex flex-wrap gap-3 items-center">
            <label class="flex items-center gap-2 text-sm"
              ><input type="checkbox" bind:checked={locked} />Protect this block
              from analysis</label
            ><button class="btn" onclick={clearDirections}
              >Clear block directions</button
            >
          </div>
        </fieldset>
        <p class="muted text-xs">
          Clearing local directions keeps speaker assignments and the general
          narration guidance.
        </p>
        {#if summaries.length}<ul
            class="rounded-xl bg-[var(--accent-soft)] p-3 text-xs space-y-1"
            aria-label="Active phrase, dialogue and event controls"
          >
            {#each summaries as summary}<li class="break-words">
                {summary}
              </li>{/each}
          </ul>{/if}
        <details bind:open={advanced} class="text-sm">
          <summary class="cursor-pointer"
            >{xmlMode ? 'XML annotation' : 'Advanced pSSML JSON'}
            {editable ? '' : '· read only'}</summary
          ><textarea
            class="input mt-2 w-full font-mono text-xs"
            aria-label={xmlMode ? 'XML annotation' : 'Annotation JSON'}
            rows="10"
            readonly={!editable}
            disabled={blocked}
            value={xmlMode ? xml : jsonBuffer}
            oninput={(event) => editAdvanced(event.currentTarget.value)}
            spellcheck="false"></textarea>
          <p class="muted mt-1 text-xs">
            {xmlMode
              ? '<ins> and <em> contain directions, never spoken words. Keep the segment ID and extracted transcript unchanged.'
              : 'This legacy annotation shares its state with the controls above.'}
          </p>
        </details>
        {#if parseError}<p role="alert" class="text-sm text-red-700">
            {parseError}
          </p>{/if}
        {#if currentUnit.annotation?.locked && editable}<label
            class="flex items-center gap-2 text-sm"
            ><input type="checkbox" bind:checked={unlock} />Authorize changing
            this protected annotation</label
          >{/if}
        <div class="flex flex-wrap items-center gap-2">
          {#if editable}<button
              class="btn btn-primary"
              disabled={blocked ||
                Boolean(parseError) ||
                (Boolean(currentUnit.annotation?.locked) && !unlock)}
              onclick={() => void saveAnnotation()}
              >Save block directions</button
            >{/if}<button
            class="btn"
            disabled={blocked || !validWindows || Boolean(parseError)}
            onclick={() => void compilePreview()}>Preview model request</button
          >{#if editorDirty}<span class="text-xs text-amber-700"
              >Unsaved block changes</span
            >{/if}
        </div>
        {#if preview}<section
            aria-label="Compiled model request"
            class="rounded-xl border border-[var(--line)] p-3 space-y-3"
          >
            <p class="font-medium text-sm">
              {previewStale
                ? 'Preview is out of date — compile it again.'
                : 'Preview of these editor settings · no audio generated'}
            </p>
            {#if !previewStale}{#each preview.report ?? [] as item}<p
                  class="text-xs"
                >
                  <strong>{item.status}</strong> · {item.message}
                </p>{/each}{#each preview.parts ?? [] as part, index}<div
                  class="text-xs rounded-lg bg-[var(--accent-soft)] p-2"
                >
                  <strong
                    >Part {index + 1}: {part.voice || 'session voice'}</strong
                  >
                  · {voiceSourceLabels[part.voice_source ?? 'base'] ??
                    part.voice_source}{part.fallback ? ' · fallback' : ''}
                  <p class="mt-1 whitespace-pre-wrap">{part.text}</p>
                </div>{/each}
              <details>
                <summary class="cursor-pointer text-xs"
                  >Exact provider input</summary
                >
                {#each preview.parts?.length ? preview.parts : [preview] as request, index}
                  {#if (preview.parts?.length ?? 0) > 1}<p
                      class="mt-3 text-xs font-semibold"
                    >
                      Part {index + 1}
                    </p>{/if}
                  {#if request.instructions}<p
                      class="mt-2 text-xs font-semibold"
                    >
                      Instructions
                    </p>
                    <pre
                      class="max-h-72 overflow-auto whitespace-pre-wrap break-words text-xs">{request.instructions}</pre>{/if}
                  <p class="mt-2 text-xs font-semibold">Input</p>
                  <pre
                    class="max-h-72 overflow-auto whitespace-pre-wrap break-words text-xs">{request.input}</pre>
                  {#if request.request_options && Object.keys(request.request_options).length}<p
                      class="mt-2 text-xs font-semibold"
                    >
                      Request options
                    </p>
                    <pre
                      class="max-h-72 overflow-auto whitespace-pre-wrap break-words text-xs">{JSON.stringify(
                        request.request_options,
                        null,
                        2
                      )}</pre>{/if}
                {/each}
              </details>{/if}
          </section>{/if}
      {/if}
      {#if editable}<div class="border-t border-[var(--line)] pt-4 space-y-3">
          {#if selected.analysed_count < selected.total}<label
              class="flex items-center gap-2 text-sm"
              ><input type="checkbox" bind:checked={acceptMissing} />Accept {selected.total -
                selected.analysed_count} unanalysed blocks without directions</label
            >{/if}
          <div class="flex flex-wrap gap-2">
            <button
              class="btn btn-primary"
              disabled={blocked ||
                Boolean(parseError) ||
                (selected.analysed_count < selected.total && !acceptMissing)}
              onclick={() => void adopt()}
              >{editorDirty || settingsDirty
                ? 'Save changes and adopt'
                : 'Adopt speech directions'}</button
            >{#if selected.settings.mode === 'llm'}<button
                class="btn"
                disabled={blocked}
                onclick={() => void resumeAnalysis()}
                >Resume delivery analysis</button
              >{/if}
          </div>
        </div>{/if}
    {:else}<p class="muted text-sm">
        Create a manual draft, run delivery analysis, or prepare work for a
        passive MCP client.
      </p>{/if}
  </div>
</details>
{#if navigation}<div
    class="fixed inset-0 z-50 grid place-items-center bg-black/40 p-4"
  >
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="speech-draft-title"
      tabindex="-1"
      use:modalFocus={{ onclose: () => (navigation = null) }}
      class="compact-confirmation w-full max-w-md max-h-[calc(100dvh-2rem)] overflow-y-auto rounded-2xl border border-[var(--line)] bg-[var(--paper-strong)] p-5 shadow-lg space-y-4"
    >
      <h3 id="speech-draft-title" class="font-semibold">
        {routeNavigation
          ? 'Save your changes before leaving?'
          : 'Save this block before leaving?'}
      </h3>
      <p class="muted text-sm">
        {routeNavigation
          ? 'You have unsaved speech directions, generation defaults, or character assignments. Save them or discard the changes before switching sections.'
          : 'The block has unsaved directions or speaker assignments. Generation-default changes stay in their own draft.'}
      </p>
      <div class="flex flex-wrap gap-2">
        <button
          class="btn btn-primary"
          disabled={blocked ||
            Boolean(parseError) ||
            (routeNavigation &&
              (!validWindows ||
                castDraft?.blocked ||
                castDraft?.valid === false))}
          onclick={async () => {
            const next = navigation;
            if (
              routeNavigation &&
              castDraft?.dirty &&
              !(await activeCastPanel?.saveChanges())
            )
              return;
            if (editorDirty && !(await saveAnnotation())) return;
            if (routeNavigation && settingsDirty && !(await saveSettings()))
              return;
            navigation = null;
            await next?.();
          }}>Save and continue</button
        ><button
          class="btn"
          disabled={blocked}
          onclick={() => {
            const next = navigation;
            selectUnit(unitId);
            if (routeNavigation) {
              if (stored) applySettings(stored);
              activeCastPanel?.discardChanges();
            }
            navigation = null;
            void next?.();
          }}>Discard and continue</button
        ><button
          class="btn"
          disabled={blocked}
          onclick={() => (navigation = null)}>Stay here</button
        >
      </div>
    </div>
  </div>{/if}
