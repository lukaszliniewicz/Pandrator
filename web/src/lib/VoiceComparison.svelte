<script lang="ts">
  import { onDestroy, untrack } from 'svelte';
  import { X, Play, LoaderCircle } from '@lucide/svelte';
  import { speechServiceApi } from './admin-api';
  import { jobApi } from './domain-api';
  import { jobProgressDetail } from './job-progress';
  import type { TtsService } from './api-models';
  import { voiceLibraryApi, type CatalogVoice } from './voice-library-api';
  import { readinessLabel, setupHref } from './voice-presentation';
  import { LANGUAGE_OPTIONS } from './settings-fields';
  import { errorMessage } from './errors';
  import ReviewWorkspace from './ReviewWorkspace.svelte';
  let toolsOpen = $state(false);
  import AudioPlayer from './AudioPlayer.svelte';

  let {
    open,
    voices,
    services,
    workspaceId,
    sessionId,
    canEdit = false,
    initialService = '',
    initialModel = '',
    onclose,
    onremove,
    oninspect
  }: {
    open: boolean;
    voices: CatalogVoice[];
    services: TtsService[];
    workspaceId?: string;
    sessionId?: string;
    canEdit?: boolean;
    initialService?: string;
    initialModel?: string;
    onclose: () => void;
    onremove: (key: string) => void;
    oninspect: (voice: CatalogVoice) => void;
  } = $props();
  let service = $state(untrack(() => initialService));
  let model = $state(untrack(() => initialModel));
  let language = $state('en');
  let inheritDirections = $state(true);
  let directionNotice = $state('');
  let passage = $state(
    'The fire was burning low, but there was still time to tell the story.'
  );
  let busy = $state(false);
  let checking = $state(false);
  let jobId = '';
  let stop = false;
  let alive = true;
  let results = $state<Record<string, { artifact: string; context: string }>>(
    {}
  );
  let states = $state<Record<string, string>>({});
  let errors = $state<Record<string, string>>({});
  let bindings = $state<Record<string, CatalogVoice | null>>({});
  let checkError = $state('');
  const renderer = $derived(services.find((item) => item.id === service));
  const context = $derived(
    JSON.stringify({
      workspaceId,
      sessionId,
      service,
      model,
      language,
      passage,
      inheritDirections
    })
  );
  const keys = $derived(voices.map((voice) => voice.key).join('|'));
  const ready = $derived(voices.filter(canAudition));
  function bindingFor(voice: CatalogVoice) {
    return bindings[voice.key]?.compatibility.find(
      (item) => item.service_id === service && item.model === model
    );
  }
  function canAudition(voice: CatalogVoice) {
    const binding = bindingFor(voice);
    return Boolean(
      binding?.voice &&
      (binding.ready || binding.prepared) &&
      binding.service_available !== false &&
      !['model_unavailable', 'scope_mismatch', 'unavailable'].includes(
        binding.status
      )
    );
  }
  $effect(() => {
    void keys;
    if (!service || !model || !workspaceId) {
      bindings = {};
      checking = false;
      return;
    }
    const selectedService = service,
      selectedModel = model;
    const shortlist = [...voices];
    let stale = false;
    checking = true;
    checkError = '';
    void Promise.all(
      shortlist.map(async (voice) => {
        const result = await voiceLibraryApi.query({
          query: voice.id,
          kind: voice.kind,
          service_id: selectedService,
          model: selectedModel,
          workspace_id: workspaceId,
          session_id: sessionId,
          limit: 200
        });
        return [
          voice.key,
          result.items.find((item) => item.key === voice.key) ?? null
        ] as const;
      })
    )
      .then((items) => {
        if (!stale) bindings = Object.fromEntries(items);
      })
      .catch((caught) => {
        if (!stale) {
          bindings = {};
          checkError = errorMessage(caught);
        }
      })
      .finally(() => {
        if (!stale) checking = false;
      });
    return () => {
      stale = true;
    };
  });
  onDestroy(() => {
    alive = false;
    stop = true;
    if (jobId) void jobApi.cancel(jobId).catch(() => null);
  });
  async function cancel() {
    stop = true;
    if (jobId) {
      try {
        await jobApi.cancel(jobId);
      } catch (caught) {
        checkError = `Could not cancel the current audition: ${errorMessage(caught)}`;
      }
    }
  }
  async function generate() {
    if (
      busy ||
      checking ||
      !ready.length ||
      !passage.trim() ||
      !workspaceId ||
      !canEdit
    )
      return;
    busy = true;
    stop = false;
    errors = {};
    states = Object.fromEntries(
      voices.map((voice) => [
        voice.key,
        canAudition(voice) ? 'Queued' : readinessLabel(bindingFor(voice))
      ])
    );
    const snapshot = {
      service,
      model,
      language,
      text: passage.trim(),
      inheritDirections,
      workspace: workspaceId,
      context
    };
    const current = () =>
      alive && workspaceId === snapshot.workspace && canEdit;
    for (const voice of [...ready]) {
      if (stop || !current()) break;
      try {
        states[voice.key] = 'Generating audition…';
        const binding = bindingFor(voice);
        const queued = await speechServiceApi.preview(
          snapshot.service,
          {
            model: snapshot.model,
            language: snapshot.language,
            text: snapshot.text,
            voice: binding!.voice!,
            ...(voice.reference.kind === 'managed'
              ? { voice_id: voice.reference.voice_id }
              : {}),
            generation_prompt: snapshot.inheritDirections ? undefined : ''
          },
          snapshot.workspace
        );
        if (queued.workspace_id !== snapshot.workspace)
          throw new Error(
            'The audition returned a different workspace. Check Activity & logs before trying again.'
          );
        directionNotice = queued.preview
          ? queued.preview.directions.present
            ? 'Saved directions are included in this audition.'
            : 'This audition has no style directions.'
          : '';
        jobId = queued.id;
        if (stop || !current()) {
          await jobApi.cancel(jobId);
          break;
        }
        let finished = false;
        for (let attempt = 0; attempt < 3600 && current() && !stop; attempt++) {
          const job = await jobApi.get(queued.id);
          if (!current() || stop) break;
          states[voice.key] = jobProgressDetail(job, 'Generating audition…');
          if (job.status === 'succeeded') {
            const artifact = String(job.result_json?.artifact_id ?? '');
            if (!artifact)
              throw new Error('The audition completed without audio.');
            results[voice.key] = { artifact, context: snapshot.context };
            states[voice.key] = 'Audition ready';
            finished = true;
            break;
          }
          if (['failed', 'canceled', 'interrupted'].includes(job.status))
            throw new Error(job.error_message || `Audition ${job.status}.`);
          await new Promise((resolve) => setTimeout(resolve, 1000));
        }
        if (!finished && !stop)
          throw new Error(
            'Still running. Follow this audition in Activity & logs.'
          );
      } catch (caught) {
        if (current() && !stop) {
          errors[voice.key] = errorMessage(caught);
          states[voice.key] = '';
        }
      } finally {
        jobId = '';
      }
    }
    if (current()) {
      if (stop)
        states = Object.fromEntries(
          Object.entries(states).map(([key, state]) => [
            key,
            state !== 'Audition ready' &&
            !errors[key] &&
            ready.some((voice) => voice.key === key)
              ? 'Canceled'
              : state
          ])
        );
      busy = false;
    }
  }
</script>

{#if open}
  <ReviewWorkspace
    closeLabel="Close comparison"
    titleId="compare-title"
    {onclose}
    bind:toolsOpen
    toolsTitle="Audition settings"
  >
    {#snippet header()}<div class="min-w-0 flex-1">
        <h2 id="compare-title" class="text-lg font-semibold">Compare voices</h2>
        <p class="muted mt-1 text-xs">
          Listen to references or audition the same passage.
        </p>
      </div>{/snippet}
    {#snippet tools()}<div class="space-y-4">
        <div class="grid gap-3">
          <label class="text-sm font-semibold"
            >Audition service<select
              class="input mt-1 w-full"
              bind:value={service}
              onchange={() => (model = '')}
              disabled={busy}
              ><option value="">Choose a service</option
              >{#each services as item}<option value={item.id}
                  >{item.name}</option
                >{/each}</select
            ></label
          >
          <label class="min-w-0 text-sm font-semibold"
            >Audition model<select
              class="input mt-1 w-full"
              bind:value={model}
              disabled={busy || !service}
              ><option value="">Choose a model</option
              >{#each renderer?.models ?? [] as item}<option value={item}
                  >{renderer?.model_catalog?.find((entry) => entry.id === item)
                    ?.label ?? item}</option
                >{/each}</select
            ></label
          >
          <label class="text-sm font-semibold"
            >Audition language<select
              class="input mt-1 w-full"
              bind:value={language}
              disabled={busy}
              >{#each LANGUAGE_OPTIONS.filter((item) => item.value !== 'auto') as item}<option
                  value={item.value}>{item.label}</option
                >{/each}</select
            ></label
          >
        </div>
        <label class="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            bind:checked={inheritDirections}
            disabled={busy}
          />
          Use saved style directions
        </label>
        {#if directionNotice}<p class="muted text-xs" role="status">
            {directionNotice}
          </p>{/if}
        <label class="block text-sm font-semibold"
          >Shared passage<textarea
            class="input mt-1 w-full"
            rows="3"
            maxlength="1000"
            bind:value={passage}
            disabled={busy}></textarea></label
        >
      </div>{/snippet}
    {#snippet notices()}{#if checkError}<p
          role="alert"
          class="px-4 py-2 text-sm text-red-600"
        >
          {checkError}
        </p>{/if}{/snippet}
    {#snippet footer()}<div
        class="flex flex-wrap items-center justify-between gap-3 border-t border-[var(--line)] p-4"
      >
        <p class="muted text-xs">
          {!service || !model
            ? 'Choose audition settings to generate a shared passage.'
            : `${ready.length} of ${voices.length} voices can be auditioned with this model.`}
        </p>
        {#if busy}<button class="btn btn-secondary" onclick={cancel}
            ><LoaderCircle size={16} class="animate-spin" />Cancel auditions</button
          >{:else}<button
            class="btn btn-primary"
            disabled={checking ||
              !ready.length ||
              !passage.trim() ||
              !workspaceId ||
              !canEdit}
            onclick={generate}
            ><Play size={16} />{ready.length
              ? `Generate ${ready.length} ${ready.length === 1 ? 'audition' : 'auditions'}`
              : 'Generate auditions'}</button
          >{/if}
        {#if !service || !model}<button
            class="btn"
            onclick={() => (toolsOpen = true)}>Choose audition settings</button
          >{/if}
      </div>{/snippet}
    <div class="min-h-0 flex-1 overflow-auto p-4 sm:p-6">
      <div
        class={`grid gap-3 ${voices.length === 3 ? 'lg:grid-cols-3' : voices.length === 2 ? 'lg:grid-cols-2' : ''}`}
      >
        {#each voices as voice (voice.key)}
          {@const binding = bindingFor(voice)}
          <article
            class="min-w-0 space-y-3 rounded-xl border border-[var(--line)] p-4"
          >
            <div class="flex items-start justify-between gap-2">
              <h3 class="break-words font-semibold">{voice.name}</h3>
              <button
                class="btn btn-icon shrink-0"
                aria-label={`Remove ${voice.name} from comparison`}
                disabled={busy}
                onclick={() => onremove(voice.key)}><X size={16} /></button
              >
            </div>
            {#if voice.preview_artifact_id}<div>
                <p class="muted mb-2 text-xs">Saved reference</p>
                <AudioPlayer
                  src={`/api/v1/artifacts/${voice.preview_artifact_id}/content`}
                  label={`${voice.name} reference`}
                />
              </div>{:else}<p class="muted text-xs">
                No saved reference recording.
              </p>{/if}
            {#if service && model}<p class="text-xs">
                {checking ? 'Checking compatibility…' : readinessLabel(binding)}
              </p>
              {#if !checking && !binding?.ready}
                {#if voice.kind === 'managed'}<button
                    class="btn btn-sm"
                    disabled={busy}
                    onclick={() => oninspect(voice)}
                    >Open samples &amp; setup</button
                  >{:else}<a
                    class="text-sm text-[var(--accent)] underline"
                    href={setupHref(binding)}>Check service and model</a
                  >{/if}
              {/if}
            {/if}
            {#if errors[voice.key]}<p role="alert" class="text-sm text-red-600">
                {errors[voice.key]}
              </p>{/if}
            {#if results[voice.key]?.context === context}<div
                class="border-t border-[var(--line)] pt-3"
              >
                <p class="mb-2 text-xs font-semibold">
                  Shared passage audition
                </p>
                <AudioPlayer
                  src={`/api/v1/artifacts/${results[voice.key].artifact}/content`}
                  label={`${voice.name} shared passage audition`}
                />
              </div>{/if}
            {#if busy || results[voice.key]?.context !== context}<p
                class="muted text-xs"
                role="status"
              >
                {busy
                  ? states[voice.key]
                  : results[voice.key]
                    ? 'Settings changed. Generate a new audition to compare this passage.'
                    : states[voice.key] || 'No shared passage audition yet.'}
              </p>{/if}
          </article>
        {/each}
      </div>
    </div>
  </ReviewWorkspace>
{/if}

<style>
  .input {
    min-width: 0;
    min-height: 2.75rem;
    border: 1px solid var(--line);
    border-radius: 0.65rem;
    background: var(--paper);
    padding: 0.6rem 0.7rem;
    font-weight: 400;
    font-size: 0.875rem;
  }
</style>
