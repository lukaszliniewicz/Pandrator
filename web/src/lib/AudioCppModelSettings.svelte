<script lang="ts">
  import { ChevronDown, RotateCcw } from '@lucide/svelte';
  import type { TtsRequestParameter } from './api-models';
  import {
    modelTuningMap,
    updateModelTuning,
    type ModelTuningMap
  } from './audio-cpp-model-settings';
  let {
    model,
    parameters,
    settings,
    overrides = {},
    onchange
  }: {
    model: string;
    parameters: Record<string, TtsRequestParameter>;
    settings: Record<string, unknown>;
    overrides?: ModelTuningMap;
    onchange: (map: ModelTuningMap) => void;
  } = $props();
  const labels: Record<string, string> = {
    seed: 'Random seed',
    temperature: 'Temperature',
    top_k: 'Top K',
    top_p: 'Top P',
    max_tokens: 'Maximum audio tokens',
    min_tokens: 'Minimum audio tokens',
    max_steps: 'Maximum steps',
    guidance_scale: 'Guidance scale',
    num_inference_steps: 'Inference steps',
    repetition_penalty: 'Repetition penalty',
    do_sample: 'Sampling',
    subtalker_do_sample: 'Subtalker sampling',
    subtalker_temperature: 'Subtalker temperature',
    subtalker_top_k: 'Subtalker top K',
    subtalker_top_p: 'Subtalker top P',
    text_chunk_size: 'Model text chunk size',
    text_chunk_mode: 'Model text chunk mode',
    retry_badcase: 'Retry abnormal output',
    retry_badcase_max_times: 'Abnormal-output retries',
    retry_badcase_ratio_threshold: 'Abnormal-output length ratio',
    exaggeration: 'Expressiveness',
    emotion_alpha: 'Emotion strength',
    min_p: 'Minimum P',
    s3gen_cfg_rate: 'Audio decoder guidance',
    stop_on_eos: 'Stop at end-of-speech token',
    speed: 'Speed',
    duration: 'Target duration (seconds)',
    t_shift: 'Time shift',
    denoise: 'Denoise reference',
    preprocess_prompt: 'Preprocess reference',
    postprocess_output: 'Postprocess audio',
    layer_penalty_factor: 'Layer penalty',
    position_temperature: 'Position temperature',
    class_temperature: 'Class temperature',
    audio_chunk_duration: 'Audio chunk duration (seconds)',
    audio_chunk_threshold: 'Audio chunk threshold (seconds)',
    noise_clamp: 'Noise clamp',
    eos_threshold: 'End-of-speech threshold',
    frames_after_eos: 'Frames after end of speech',
    truncate_clone_audio: 'Truncate cloning reference',
    stop_threshold: 'Stop threshold',
    depth_temperature: 'Depth temperature'
  };
  const map = $derived(modelTuningMap(settings.audio_cpp_model_settings));
  const modelValues = $derived(map[model]);
  const tuningSource = $derived(
    Object.prototype.hasOwnProperty.call(overrides, model)
      ? overrides[model] === null
        ? 'Model defaults'
        : 'Project tuning'
      : 'Inherited tuning'
  );
  function current(key: string) {
    if (modelValues === null) return undefined;
    if (modelValues && typeof modelValues === 'object') return modelValues[key];
    const raw = (settings.audio_cpp_options ??
      settings.options ??
      {}) as Record<string, unknown>;
    return (
      raw[key] ??
      settings[`audio_cpp_${key}`] ??
      (['seed', 'speed'].includes(key) ? settings[key] : undefined)
    );
  }
  function change(key: string, next: unknown) {
    const values =
      modelValues === null
        ? Object.fromEntries(Object.keys(parameters).map((key) => [key, null]))
        : modelValues
          ? {}
          : Object.fromEntries(
              Object.keys(parameters)
                .map((key) => [key, current(key)])
                .filter(([, value]) => value != null && value !== '')
            );
    onchange(
      updateModelTuning(overrides, model, { key, value: next ?? null }, values)
    );
  }
  const mainKeys = new Set([
    'seed',
    'temperature',
    'top_k',
    'top_p',
    'guidance_scale',
    'num_inference_steps',
    'speed',
    'max_tokens',
    'emotion_alpha',
    'exaggeration'
  ]);
  const common = $derived(
    Object.entries(parameters).filter(([key]) => mainKeys.has(key))
  );
  const advanced = $derived(
    Object.entries(parameters).filter(([key]) => !mainKeys.has(key))
  );
</script>

{#snippet control(key: string, parameter: TtsRequestParameter)}
  {@const setting = current(key)}
  {@const boundedStrength =
    ['emotion_alpha', 'exaggeration'].includes(key) &&
    parameter.type === 'number' &&
    parameter.minimum != null &&
    parameter.maximum != null &&
    parameter.maximum > parameter.minimum}
  <div class="min-w-0">
    <label class="block text-xs font-semibold">
      <span class="flex min-h-6 items-center gap-2"
        >{labels[key] ?? key.replaceAll('_', ' ')}</span
      >
      {#if parameter.type === 'boolean' || parameter.enum}
        <select
          class="field"
          value={setting == null ? '' : String(setting)}
          onchange={(event) =>
            change(
              key,
              event.currentTarget.value === ''
                ? null
                : parameter.type === 'boolean'
                  ? event.currentTarget.value === 'true'
                  : event.currentTarget.value
            )}
        >
          <option value=""
            >Model default{parameter.default != null
              ? ` (${parameter.default})`
              : ''}</option
          >
          {#if parameter.type === 'boolean'}<option value="true">Enabled</option
            ><option value="false">Disabled</option>
          {:else}{#each parameter.enum ?? [] as option}<option value={option}
                >{option.replaceAll('_', ' ')}</option
              >{/each}{/if}
        </select>
      {:else if parameter.type === 'string'}
        <input
          class="field"
          type="text"
          maxlength="1200"
          value={setting == null ? '' : String(setting)}
          placeholder={parameter.default == null
            ? 'Model default'
            : `Default: ${parameter.default}`}
          oninput={(event) => change(key, event.currentTarget.value)}
        />
      {:else}
        <input
          class="field"
          type="number"
          value={setting == null ? '' : Number(setting)}
          step={parameter.type === 'integer' ? 1 : 'any'}
          min={parameter.minimum ?? parameter.exclusive_minimum}
          max={parameter.maximum ?? parameter.exclusive_maximum}
          placeholder={parameter.default == null
            ? 'Model default'
            : `Default: ${parameter.default}`}
          oninput={(event) => {
            const input = event.currentTarget;
            if (input.value === '') change(key, null);
            else if (Number.isFinite(input.valueAsNumber))
              change(key, input.valueAsNumber);
          }}
        />
      {/if}
      {#if parameter.default_source === 'pandrator_policy'}
        <span class="muted text-xs"
          >Pandrator default: {parameter.default}. audio.cpp native default: {parameter.native_default}.</span
        >
      {:else if parameter.default_note}
        <span class="muted text-xs">{parameter.default_note}</span>
      {/if}
    </label>
    {#if boundedStrength}
      <input
        class="mt-2 w-full accent-[var(--accent)]"
        type="range"
        aria-label={`${labels[key]} slider`}
        min={parameter.minimum}
        max={parameter.maximum}
        step="0.05"
        value={Number(setting ?? parameter.default ?? parameter.minimum)}
        oninput={(event) => change(key, event.currentTarget.valueAsNumber)}
      />
    {/if}
    {#if key === 'emotion_alpha'}
      <p class="muted mt-1 text-xs">
        Scales emotion conditioning for this model across the project. Each
        segment can supply its own emotion description.
      </p>
    {/if}
    {#if setting != null && setting !== ''}<button
        class="muted mt-1 flex items-center gap-1 text-[.65rem] hover:text-[var(--accent)]"
        onclick={() => change(key, null)}
        aria-label={`Reset ${labels[key] ?? key} to model default`}
        ><RotateCcw size={10} /> Use model default</button
      >{/if}
  </div>
{/snippet}

<section
  class="mt-5 rounded-2xl border border-[var(--line)] bg-[var(--paper-strong)] p-4"
  aria-label="Model tuning"
>
  <h3 class="text-sm font-semibold">Model tuning</h3>
  <p class="mt-1 text-xs font-semibold" role="status">{tuningSource}</p>
  <p class="muted mt-1 text-xs">
    Values are saved separately for each model. A blank control uses the model's
    default. Reset all controls to ignore saved tuning for this model.
  </p>
  <div class="mt-3 flex flex-wrap gap-2">
    <button
      class="btn btn-sm"
      onclick={() => onchange(updateModelTuning(overrides, model, 'defaults'))}
    >
      Use model defaults
    </button>
    {#if Object.prototype.hasOwnProperty.call(overrides, model)}
      <button
        class="btn btn-sm"
        onclick={() => onchange(updateModelTuning(overrides, model, 'inherit'))}
      >
        Inherit saved tuning
      </button>
    {/if}
  </div>
  <div class="mt-4 grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
    {#each common as [key, parameter]}{@render control(key, parameter)}{/each}
  </div>
  {#if advanced.length}<details class="mt-4 border-t border-[var(--line)] pt-3">
      <summary
        class="flex cursor-pointer list-none items-center gap-2 text-xs font-semibold"
        ><ChevronDown size={14} /> More model controls
        <span class="muted font-normal">{advanced.length}</span></summary
      >
      <div class="mt-4 grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        {#each advanced as [key, parameter]}{@render control(
            key,
            parameter
          )}{/each}
      </div>
      {#if parameters.text_chunk_size}<p class="muted mt-3 text-xs">
          Model chunking applies inside one speech block. Edit the plan’s Block
          settings to change the reviewable blocks themselves.
        </p>{/if}
    </details>{/if}
</section>

<style>
  .field {
    width: 100%;
    margin-top: 0.35rem;
    border: 1px solid var(--line);
    border-radius: 0.7rem;
    padding: 0.65rem 0.7rem;
    background: var(--paper);
    color: var(--ink);
    font-weight: 400;
  }
  summary::-webkit-details-marker {
    display: none;
  }
</style>
