<script lang="ts">
  import { onDestroy } from 'svelte';
  import { apiJson } from './api';
  import { sessionApi } from './domain-api';
  import type { SettingsPayload } from './api-models';
  import { errorMessage } from './errors';

  type Annotation = {
    schema?: string;
    decision: 'none' | 'steer';
    delivery?: {
      instruction?: string;
      emotion?: string;
      pace?: string;
      cadence?: string;
      emphasis?: string;
    };
    spans?: unknown[];
    events?: unknown[];
    reason?: string;
    confidence?: string;
    locked?: boolean;
  };
  type Unit = {
    id: string;
    ordinal: number;
    text: string;
    spoken_text: string;
    annotation: Annotation | null;
  };
  type Plan = {
    id: string;
    plan_revision_id: string;
    status: string;
    version: number;
    total: number;
    analysed_count: number;
    steered_count: number;
    locked_count: number;
    stale: boolean;
    job_id?: string;
    job_status?: string;
    items?: Unit[];
    settings: { workflow_kind?: string; mode?: string; instructions?: string };
  };
  type Capability = {
    model: string;
    dialect: string;
    status: string;
    instructions: string;
    semantic_context: string;
    notes?: string[];
    event_tags?: Record<string, string>;
  };
  type Preview = {
    transcript: string;
    input: string;
    instructions: string;
    request_options?: Record<string, unknown>;
    report: { status: string; control: string; message: string }[];
    capabilities: Capability;
  };

  let {
    sessionId,
    revisionId,
    busy = false,
    onchanged
  }: {
    sessionId: string;
    revisionId: string;
    busy?: boolean;
    onchanged?: () => void;
  } = $props();
  let opened = $state(false);
  let pending = $state(false);
  let error = $state('');
  let message = $state('');
  let history = $state<Plan[]>([]);
  let selected = $state<Plan | null>(null);
  let capability = $state<Capability | null>(null);
  let stored = $state<SettingsPayload | null>(null);
  let general = $state('');
  let contextMode = $state('off');
  let before = $state(2);
  let after = $state(1);
  let maxChars = $state(4000);
  let enabled = $state(false);
  let vocalizations = $state(false);
  let planningInstructions = $state('');
  let modelName = $state('');
  let offset = $state(0);
  let unitId = $state('');
  let direction = $state('');
  let locked = $state(true);
  let unlock = $state(false);
  let advanced = $state(false);
  let annotationJson = $state('');
  let preview = $state<Preview | null>(null);
  let alive = true;
  let serial = 0;
  const base = $derived(
    `/sessions/${encodeURIComponent(sessionId)}/performance-plans`
  );
  const currentUnit = $derived(
    selected?.items?.find((unit) => unit.id === unitId)
  );
  const editable = $derived(selected?.status === 'draft' && !selected.stale);
  const blocked = $derived(pending || busy);
  const validWindows = $derived(
    [before, after, maxChars].every(Number.isInteger) &&
      before >= 0 &&
      before <= 20 &&
      after >= 0 &&
      after <= 20 &&
      maxChars >= 0 &&
      maxChars <= 16000
  );
  onDestroy(() => {
    alive = false;
    serial += 1;
  });

  function chooseUnit(id: string) {
    unitId = id;
    const unit = selected?.items?.find((item) => item.id === id);
    direction = unit?.annotation?.delivery?.instruction ?? '';
    locked = unit?.annotation?.locked ?? true;
    unlock = false;
    advanced = false;
    annotationJson = JSON.stringify(
      unit?.annotation ?? { decision: 'none', locked: true },
      null,
      2
    );
    preview = null;
  }

  async function load(planId?: string, newOffset = 0) {
    const requestId = ++serial;
    error = '';
    pending = true;
    try {
      const [listing, settings] = await Promise.all([
        apiJson<{ items: Plan[]; capabilities: Capability }>(
          `${base}?limit=100&plan_revision_id=${encodeURIComponent(revisionId)}`
        ),
        sessionApi.settings(sessionId, 'tts')
      ]);
      if (!alive || requestId !== serial) return;
      history = listing.items.filter(
        (plan) => plan.plan_revision_id === revisionId
      );
      capability = listing.capabilities;
      stored = settings;
      general = String(settings.effective.generation_prompt ?? '');
      contextMode = String(settings.effective.tts_context_mode ?? 'off');
      before = Number(settings.effective.performance_context_before ?? 2);
      after = Number(settings.effective.performance_context_after ?? 1);
      maxChars = Number(
        settings.effective.performance_context_max_chars ?? 4000
      );
      enabled = Boolean(settings.effective.performance_enabled);
      vocalizations = Boolean(
        settings.effective.performance_allow_vocalizations
      );
      const id = planId ?? history[0]?.id;
      if (id) {
        const result = await apiJson<Plan>(
          `${base}/${encodeURIComponent(id)}?offset=${newOffset}&limit=20`
        );
        if (!alive || requestId !== serial) return;
        selected = result;
        offset = newOffset;
        chooseUnit(result.items?.[0]?.id ?? '');
      } else {
        selected = null;
      }
    } catch (caught) {
      if (alive && requestId === serial) error = errorMessage(caught);
    } finally {
      if (alive && requestId === serial) pending = false;
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
    if (!stored || !validWindows) return;
    pending = true;
    error = '';
    message = '';
    try {
      await sessionApi.saveSettings(sessionId, 'tts', stored.revision, {
        ...stored.override,
        generation_prompt: general,
        tts_context_mode: contextMode,
        performance_context_before: before,
        performance_context_after: after,
        performance_context_max_chars: maxChars,
        performance_enabled: enabled,
        performance_allow_vocalizations: vocalizations
      });
      await load(selected?.id, offset);
      message =
        'Speech direction and context settings saved. New requests use these settings; running requests keep their snapshot.';
      onchanged?.();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      pending = false;
    }
  }

  async function create(mode: 'manual' | 'llm', copy = false) {
    if (!validWindows) return;
    pending = true;
    error = '';
    message = '';
    try {
      const result = await action<Plan>('', {
        expected_plan_revision_id: revisionId,
        mode,
        model_name: modelName,
        instructions: planningInstructions,
        context_before: before,
        context_after: after,
        context_max_chars: maxChars,
        allow_vocalizations: vocalizations,
        ...(copy && selected ? { copy_from_id: selected.id } : {})
      });
      await load(result.id);
      message =
        mode === 'llm'
          ? 'Analysis queued in Jobs. Refresh to review its completed batches; nothing is adopted automatically.'
          : 'Editable performance draft created. Accepted speech text and block boundaries are unchanged.';
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      pending = false;
    }
  }

  function editedAnnotation(): Annotation {
    if (advanced) return JSON.parse(annotationJson) as Annotation;
    const original = currentUnit?.annotation;
    const delivery = { ...original?.delivery, instruction: direction.trim() };
    const hasControls =
      Object.values(delivery).some(Boolean) ||
      Boolean(original?.spans?.length || original?.events?.length);
    return {
      ...original,
      decision: hasControls ? 'steer' : 'none',
      delivery: hasControls ? delivery : {},
      locked
    };
  }

  async function saveAnnotation() {
    if (!selected || !currentUnit || !editable) return;
    const id = selected.id;
    pending = true;
    error = '';
    message = '';
    try {
      await action(
        `/${id}`,
        {
          expected_version: selected.version,
          unlock_locked: unlock,
          items: [
            { segment_id: currentUnit.id, annotation: editedAnnotation() }
          ]
        },
        'PATCH'
      );
      await load(id, offset);
      message = 'Performance annotation saved without changing the transcript.';
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      pending = false;
    }
  }

  async function compilePreview() {
    if (!selected || !currentUnit) return;
    pending = true;
    error = '';
    try {
      preview = await action<Preview>(`/${selected.id}/preview`, {
        segment_id: currentUnit.id,
        annotation: editedAnnotation(),
        generation_prompt: general,
        context_mode: contextMode,
        allow_vocalizations: vocalizations
      });
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
    message = '';
    try {
      await action(`/${selected.id}/analyse`, {});
      await load(selected.id, offset);
      message =
        'Analysis queued for unfinished batches. Completed results and protected manual annotations are retained.';
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      pending = false;
    }
  }

  async function adopt() {
    if (!selected) return;
    const missing = selected.total - selected.analysed_count;
    if (
      missing > 0 &&
      !window.confirm(
        `Keep ${missing} unanalysed blocks unchanged and adopt the saved directions?`
      )
    )
      return;
    if (
      !window.confirm(
        'Adopt the saved performance plan and enable it for future generation? Unsaved editor changes are not included.'
      )
    )
      return;
    pending = true;
    error = '';
    message = '';
    try {
      await action(`/${selected.id}/adopt`, {
        expected_version: selected.version,
        accept_unanalysed: missing > 0,
        enable: true
      });
      await load(selected.id, offset);
      message =
        'Performance adopted. Future generation uses these directions; existing takes and running requests are not overwritten.';
      onchanged?.();
    } catch (caught) {
      error = errorMessage(caught);
    } finally {
      pending = false;
    }
  }
</script>

<details
  class="mt-4 rounded-2xl border border-[var(--line)] p-4"
  bind:open={opened}
  ontoggle={(event) => {
    // The event fires before Svelte updates the bound open value.
    if (event.currentTarget.open && !stored && !pending) void load();
  }}
>
  <summary class="cursor-pointer font-semibold"
    >Context and performance <span class="muted text-xs">Optional · pSSML</span
    ></summary
  >
  <div class="mt-4 space-y-4">
    <p class="muted text-sm">
      Direct short utterances using surrounding meaning, without reading the
      context or changing speech blocks. Audiobooks use the same optional pass
      with paragraph and scene context. No preceding generated audio is used.
    </p>
    {#if error}<p class="text-sm text-red-700" role="alert">{error}</p>{/if}
    {#if message}<p class="muted text-sm" role="status">{message}</p>{/if}
    {#if capability}<p class="muted text-xs">
        Selected model: {capability.model || 'not selected'} · Directions: {capability.instructions}
        · Semantic context: {capability.semantic_context}. Capabilities are {capability.status},
        not a guarantee of acoustic compliance.
      </p>{/if}
    <fieldset disabled={blocked} class="space-y-3">
      <legend class="mb-2 font-medium"
        >General direction and synthesis context</legend
      >
      <label class="block text-sm"
        >General direction / stable narrator description
        <textarea
          class="input mt-1 w-full"
          rows="3"
          maxlength="4000"
          bind:value={general}
          placeholder="Natural, restrained explanatory narration."
          aria-label="General speech direction"></textarea>
      </label>
      <div class="grid gap-3 sm:grid-cols-2">
        <label class="text-sm"
          >Context sent directly to TTS
          <select class="input mt-1 w-full" bind:value={contextMode}>
            <option value="off">Off</option><option value="before"
              >Preceding text</option
            ><option value="both">Preceding and following text</option>
          </select>
        </label>
        <p class="muted text-xs">
          Gemini can use prompt-separated semantic context. For other models,
          run the performance pass to turn context into supported directions.
          Separation is prompt-mediated, not an enforced hidden channel.
        </p>
      </div>
      <div class="grid grid-cols-3 gap-3">
        <label class="text-sm"
          >Before (blocks)<input
            class="input mt-1 w-full"
            type="number"
            min="0"
            max="20"
            bind:value={before}
          /></label
        >
        <label class="text-sm"
          >After (blocks)<input
            class="input mt-1 w-full"
            type="number"
            min="0"
            max="20"
            bind:value={after}
          /></label
        >
        <label class="text-sm"
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
        ><input type="checkbox" bind:checked={enabled} />Use the adopted
        performance plan for generation</label
      >
      <label class="flex items-center gap-2 text-sm"
        ><input type="checkbox" bind:checked={vocalizations} />Allow explicitly
        requested vocalizations, such as a sigh or laugh</label
      >
      <button
        class="btn"
        disabled={!stored || !validWindows}
        onclick={() => void saveSettings()}>Save speech settings</button
      >
    </fieldset>
    <fieldset
      disabled={blocked || !validWindows}
      class="space-y-3 border-t border-[var(--line)] pt-4"
    >
      <legend class="font-medium">Optional contextual performance pass</legend>
      <label class="block text-sm"
        >Planning guidance (not spoken)
        <textarea
          class="input mt-1 w-full"
          rows="2"
          maxlength="6000"
          bind:value={planningInstructions}
          placeholder="Steer only where the isolated utterance loses an important rhetorical cue."
        ></textarea>
      </label>
      <label class="block text-sm"
        >Analysis model <span class="muted text-xs"
          >Blank uses your configured default LLM</span
        >
        <input
          class="input mt-1 w-full"
          bind:value={modelName}
          maxlength="255"
        />
      </label>
      <p class="muted text-xs">
        Save changes to the general narrator direction before analysing.
        Analysis can incur your configured LLM’s usage charges. It never starts
        TTS or changes accepted wording.
      </p>
      <div class="flex flex-wrap gap-2">
        <button class="btn btn-primary" onclick={() => void create('llm')}
          >Analyse performance</button
        >
        <button class="btn" onclick={() => void create('manual')}
          >Create manual draft</button
        >
        {#if selected && !selected.stale}<button
            class="btn"
            onclick={() => void create('manual', true)}>Copy for editing</button
          >{/if}
      </div>
    </fieldset>
    <div
      class="flex flex-wrap items-end gap-2 border-t border-[var(--line)] pt-4"
    >
      <label class="min-w-48 flex-1 text-sm"
        >Performance version
        <select
          class="input mt-1 w-full"
          disabled={blocked || !history.length}
          value={selected?.id ?? ''}
          onchange={(event) => void load(event.currentTarget.value)}
        >
          {#each history as plan}<option value={plan.id}
              >{plan.status} · {plan.steered_count} directed / {plan.total} blocks{plan.stale
                ? ' · stale'
                : ''}</option
            >{/each}
        </select>
      </label>
      <button
        class="btn"
        disabled={blocked}
        onclick={() => void load(selected?.id, offset)}>Refresh</button
      >
    </div>
    {#if selected}
      <p class="muted text-sm">
        {selected.analysed_count} of {selected.total} analysed · {selected.locked_count}
        locked. {selected.job_id ? `Analysis job: ${selected.job_id}.` : ''}
      </p>
      {#if selected.stale}<p role="status" class="text-sm text-amber-700">
          The speech plan changed. Create a new performance plan; these
          directions cannot be adopted.
        </p>{/if}
      <label class="block text-sm"
        >Speech block
        <select
          class="input mt-1 w-full"
          value={unitId}
          disabled={blocked}
          onchange={(event) => chooseUnit(event.currentTarget.value)}
        >
          {#each selected.items ?? [] as unit}<option value={unit.id}
              >#{unit.ordinal + 1} · {unit.annotation?.decision ?? 'unanalysed'} ·
              {unit.text.slice(0, 80)}</option
            >{/each}
        </select>
      </label>
      <div class="flex items-center gap-3 text-sm">
        <button
          class="btn"
          disabled={blocked || offset === 0}
          onclick={() => void load(selected?.id, Math.max(0, offset - 20))}
          >Previous blocks</button
        >
        <span
          >{offset + 1}–{Math.min(offset + 20, selected.total)} of {selected.total}</span
        >
        <button
          class="btn"
          disabled={blocked || offset + 20 >= selected.total}
          onclick={() => void load(selected?.id, offset + 20)}
          >Next blocks</button
        >
      </div>
      {#if currentUnit}
        <blockquote
          class="rounded-xl bg-[var(--accent-soft)] p-3 text-sm whitespace-pre-wrap"
        >
          {currentUnit.spoken_text}
        </blockquote>
        {#if currentUnit.annotation?.reason}<p class="muted text-xs">
            Rationale: {currentUnit.annotation.reason}
          </p>{/if}
        <fieldset disabled={blocked || !editable} class="space-y-3">
          <label class="block text-sm"
            >Block delivery direction
            <textarea
              class="input mt-1 w-full"
              rows="2"
              maxlength="1200"
              bind:value={direction}
              placeholder="Leave blank for normal delivery."></textarea>
          </label>
          <label class="flex items-center gap-2 text-sm"
            ><input type="checkbox" bind:checked={locked} />Protect this manual
            annotation from reanalysis</label
          >
          {#if currentUnit.annotation?.locked}<label
              class="flex items-center gap-2 text-sm"
              ><input type="checkbox" bind:checked={unlock} />Explicitly unlock
              the saved annotation to change it</label
            >{/if}
          <label class="flex items-center gap-2 text-sm"
            ><input type="checkbox" bind:checked={advanced} />Edit pSSML JSON
            for phrase controls and events</label
          >
          {#if advanced}<textarea
              class="input w-full font-mono text-xs"
              rows="12"
              bind:value={annotationJson}
              aria-label="pSSML annotation JSON"></textarea>
            <p class="muted text-xs">
              Advanced JSON is authoritative, including its locked field. Phrase
              anchors must match the spoken text exactly; repeated phrases
              require an occurrence number.
            </p>{/if}
          <button
            class="btn"
            disabled={Boolean(currentUnit.annotation?.locked && !unlock)}
            onclick={() => void saveAnnotation()}>Save annotation</button
          >
        </fieldset>
        <button
          class="btn"
          disabled={blocked || selected.stale}
          onclick={() => void compilePreview()}
          >Preview compiled request (no audio)</button
        >
      {/if}
      {#if preview}
        <div class="space-y-2 rounded-xl border border-[var(--line)] p-3">
          <h4 class="font-medium">Compiled for {preview.capabilities.model}</h4>
          {#each preview.report as item}<p class="text-xs">
              <strong>{item.status} · {item.control}:</strong>
              {item.message}
            </p>{/each}
          <details>
            <summary class="cursor-pointer text-sm"
              >Provider request and clean transcript</summary
            >
            <pre
              class="mt-2 max-h-80 overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify(
                {
                  input: preview.input,
                  instructions: preview.instructions,
                  request_options: preview.request_options,
                  transcript: preview.transcript
                },
                null,
                2
              )}</pre>
          </details>
        </div>
      {/if}
      {#if editable}
        <div class="flex flex-wrap gap-2">
          <button
            class="btn btn-primary"
            disabled={blocked}
            onclick={() => void adopt()}>Adopt saved performance plan</button
          >
          {#if selected.analysed_count < selected.total}<button
              class="btn"
              disabled={blocked ||
                ['queued', 'running', 'retrying', 'cancelling'].includes(
                  selected.job_status ?? ''
                )}
              onclick={() => void resumeAnalysis()}
              >Analyse remaining blocks</button
            >{/if}
        </div>
      {/if}
      <p class="muted text-xs">
        Adopted performance is immutable. Copy it to edit. Unsupported controls
        are reported rather than spoken; timing and expression remain
        model-dependent. Automatic split/regroup passes are disabled while
        performance or semantic context is enabled; explicit block changes
        require a fresh review.
      </p>
    {/if}
  </div>
</details>
