<script lang="ts">
  import { ArrowLeft, AudioWaveform, BrainCircuit, LoaderCircle, Play, RefreshCw, Upload } from '@lucide/svelte';
  import { onMount } from 'svelte';
  import { api, type JobRecord } from './api';

  let { onback, capabilities = {} }: { onback: () => void; capabilities?: Record<string, any> } = $props();
  type Artifact = { id: string; role: string; relative_path: string; mime_type?: string | null; state: string };
  type Training = { id: string; model_name: string; status: string; error_message?: string | null; job_id?: string | null; created_at: string };

  let rvc = $state<{available:boolean;items:string[]}>({available:false,items:[]});
  let artifacts = $state<Artifact[]>([]);
  let training = $state<Training[]>([]);
  let weights = $state<File|null>(null);
  let index = $state<File|null>(null);
  let sourceArtifact = $state('');
  let sourceTextArtifact = $state('');
  let rvcModel = $state('');
  let pitch = $state(0);
  let f0Method = $state('rmvpe');
  let filterRadius = $state(3);
  let indexRate = $state(0.3);
  let volumeEnvelope = $state(1);
  let protect = $state(0.3);
  let modelName = $state('');
  let language = $state('en');
  let whisperModel = $state('large-v3');
  let sampleRate = $state(22050);
  let maxDuration = $state(11.6);
  let maxTextLength = $state(200);
  let trainingSplit = $state('80/20');
  let methodProportion = $state('6/4');
  let sampleMethod = $state('random');
  let alignmentModel = $state('');
  let chapterPerAudio = $state(1);
  let voiceSampleMode = $state('Longest');
  let voiceSamplesCount = $state(3);
  let voiceSampleOnlySentence = $state(true);
  let epochs = $state(6);
  let batches = $state(2);
  let gradient = $state(1);
  let enableDenoise = $state(false);
  let enableBreathRemoval = $state(false);
  let enableNormalize = $state(true);
  let enableCompress = $state(false);
  let enableDess = $state(false);
  let lufsValue = $state(-16);
  let compressProfile = $state('speech');
  let busy = $state(false);
  let message = $state('');
  let error = $state('');

  const audioArtifacts = $derived(artifacts.filter((item) => item.state === 'current' && (item.mime_type?.startsWith('audio/') || ['audiobook_audio','dubbing_audio','voice_sample','rvc_audio','upload'].includes(item.role))));
  const textArtifacts = $derived(artifacts.filter((item) => item.state === 'current' && (item.mime_type?.startsWith('text/') || ['clean_text','upload'].includes(item.role))));

  async function load() {
    error='';
    try {
      const [models, artifactPayload, trainingPayload] = await Promise.all([
        api<{available:boolean;items:string[]}>('/rvc/models'),
        api<{items:Artifact[]}>('/artifacts'),
        api<{items:Training[]}>('/training')
      ]);
      rvc=models; artifacts=artifactPayload.items; training=trainingPayload.items;
      rvcModel ||= models.items[0] ?? '';
      sourceArtifact ||= audioArtifacts[0]?.id ?? '';
    } catch (caught) { error=caught instanceof Error?caught.message:String(caught); }
  }

  async function uploadFile(file: File) {
    const form=new FormData(); form.set('file',file);
    return api<{artifact_id:string}>('/uploads',{method:'POST',body:form});
  }

  async function uploadModel() {
    if(!weights||!index) return;
    busy=true; error=''; message='Uploading model…';
    try {
      const [pth,indexFile]=await Promise.all([uploadFile(weights),uploadFile(index)]);
      const job=await api<JobRecord>('/rvc/models',{method:'POST',body:JSON.stringify({pth_artifact_id:pth.artifact_id,index_artifact_id:indexFile.artifact_id})});
      message=`RVC model upload queued as job ${job.id.slice(0,8)}.`;
    } catch(caught){error=caught instanceof Error?caught.message:String(caught);} finally{busy=false;}
  }

  async function convert() {
    if(!sourceArtifact||!rvcModel) return;
    busy=true; error='';
    try {
      const job=await api<JobRecord>('/rvc/convert',{method:'POST',body:JSON.stringify({source_artifact_id:sourceArtifact,settings:{rvc_model:rvcModel,pitch,f0_method:f0Method,filter_radius:filterRadius,index_rate:indexRate,volume_envelope:volumeEnvelope,protect}})});
      message=`RVC conversion queued as job ${job.id.slice(0,8)}.`;
    } catch(caught){error=caught instanceof Error?caught.message:String(caught);} finally{busy=false;}
  }

  async function train() {
    if(!sourceArtifact||!modelName.trim()) return;
    busy=true; error='';
    try {
      const result=await api<JobRecord & {training_id:string}>('/training',{method:'POST',body:JSON.stringify({model_name:modelName.trim(),source_artifact_id:sourceArtifact,source_text_artifact_id:sourceTextArtifact||null,settings:{
        model_language:language,whisper_model:whisperModel,sample_rate:sampleRate,max_duration:maxDuration,
        max_text_length:maxTextLength,training_split:trainingSplit,method_proportion:methodProportion,
        sample_method:sampleMethod,alignment_model:alignmentModel,chapter_per_audio:chapterPerAudio,
        voice_sample_mode:voiceSampleMode,voice_samples_count:voiceSamplesCount,
        voice_sample_only_sentence:voiceSampleOnlySentence,epochs,batches,gradient,
        enable_denoise:enableDenoise,enable_breath_removal:enableBreathRemoval,
        enable_normalize:enableNormalize,enable_compress:enableCompress,enable_dess:enableDess,
        lufs_value:String(lufsValue),compress_profile:compressProfile
      }})});
      message=`XTTS training queued as ${result.training_id.slice(0,8)}.`;
      await load();
    } catch(caught){error=caught instanceof Error?caught.message:String(caught);} finally{busy=false;}
  }

  async function cancel(item:Training){if(!item.job_id)return;await api(`/training/${item.id}/cancel`,{method:'POST'});await load();}
  onMount(load);
</script>

<div class="flex min-h-[calc(100vh-5rem)] flex-col">
  <header class="mb-7 flex flex-wrap items-start justify-between gap-4"><div><button onclick={onback} class="muted mb-3 flex items-center gap-2 text-sm"><ArrowLeft size={15}/> Workspace</button><div class="eyebrow">Speech laboratory</div><h1 class="mt-2 text-3xl font-semibold">RVC and XTTS training</h1><p class="muted mt-2 max-w-3xl">Install conversion models, create derived voice variants, and train XTTS models through durable jobs. Source files remain registered artifacts.</p></div><button onclick={load} class="rounded-xl border border-[var(--line)] p-2.5" aria-label="Refresh"><RefreshCw size={17}/></button></header>
  {#if error}<div class="mb-5 rounded-xl border border-red-400/40 bg-red-500/10 px-4 py-3 text-sm">{error}</div>{/if}
  {#if message}<div class="mb-5 rounded-xl border border-[var(--line)] bg-[var(--accent-soft)]/40 px-4 py-3 text-sm">{message}</div>{/if}
  <div class="grid gap-5 xl:grid-cols-2">
    <section class="surface rounded-3xl p-6"><div class="flex items-center gap-3"><div class="grid size-11 place-items-center rounded-2xl bg-[var(--accent-soft)] text-[var(--accent)]"><AudioWaveform size={21}/></div><div><h2 class="text-xl font-semibold">RVC conversion</h2><p class="muted text-sm">Service {rvc.available?'ready':'unavailable'}</p></div></div>
      <div class="mt-6 grid gap-4"><label class="text-sm font-semibold">Weights (.pth)<input type="file" accept=".pth" onchange={(event)=>weights=(event.currentTarget as HTMLInputElement).files?.[0]??null} class="mt-1 block w-full text-xs"/></label><label class="text-sm font-semibold">Index (.index)<input type="file" accept=".index,.idx" onchange={(event)=>index=(event.currentTarget as HTMLInputElement).files?.[0]??null} class="mt-1 block w-full text-xs"/></label><button onclick={uploadModel} disabled={busy||!rvc.available||!weights||!index} class="flex items-center justify-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 font-semibold text-white disabled:opacity-40">{#if busy}<LoaderCircle class="animate-spin" size={16}/>{:else}<Upload size={16}/>{/if} Install model</button></div>
      <div class="my-6 border-t border-[var(--line)]"></div>
      <div class="grid gap-4 sm:grid-cols-2"><label class="text-sm font-semibold sm:col-span-2">Audio artifact<select bind:value={sourceArtifact} class="field">{#each audioArtifacts as item}<option value={item.id}>{item.role} · {item.relative_path.split('/').at(-1)}</option>{/each}</select></label><label class="text-sm font-semibold">RVC model<select bind:value={rvcModel} class="field">{#each rvc.items as item}<option value={item}>{item}</option>{/each}</select></label><label class="text-sm font-semibold">Pitch<input type="number" bind:value={pitch} min="-24" max="24" class="field"/></label></div>
      <details class="mt-4 rounded-2xl border border-[var(--line)] p-4"><summary class="cursor-pointer text-sm font-semibold">Conversion settings</summary><div class="mt-4 grid gap-4 sm:grid-cols-2"><label class="text-sm font-semibold">Pitch detector<select bind:value={f0Method} class="field"><option value="rmvpe">RMVPE</option><option value="harvest">Harvest</option><option value="crepe">CREPE</option><option value="pm">PM</option></select></label><label class="text-sm font-semibold">Filter radius<input class="field" type="number" bind:value={filterRadius} min="0" max="7"/></label><label class="text-sm font-semibold">Index rate<input class="field" type="number" bind:value={indexRate} min="0" max="1" step="0.05"/></label><label class="text-sm font-semibold">Volume envelope<input class="field" type="number" bind:value={volumeEnvelope} min="0" max="1" step="0.05"/></label><label class="text-sm font-semibold">Protect unvoiced sounds<input class="field" type="number" bind:value={protect} min="0" max="0.5" step="0.05"/></label></div></details><button onclick={convert} disabled={busy||!rvc.available||!sourceArtifact||!rvcModel} class="mt-4 flex w-full items-center justify-center gap-2 rounded-xl border border-[var(--line)] px-4 py-2.5 font-semibold disabled:opacity-40"><Play size={16}/> Queue conversion</button>
    </section>
    <section class="surface rounded-3xl p-6"><div class="flex items-center gap-3"><div class="grid size-11 place-items-center rounded-2xl bg-[var(--accent-soft)] text-[var(--accent)]"><BrainCircuit size={21}/></div><div><h2 class="text-xl font-semibold">XTTS fine-tuning</h2><p class="muted text-sm">Long-running, recoverable worker job</p></div></div>
      <div class="mt-6 grid gap-4 sm:grid-cols-2"><label class="text-sm font-semibold sm:col-span-2">Model name<input bind:value={modelName} class="field"/></label><label class="text-sm font-semibold sm:col-span-2">Training audio<select bind:value={sourceArtifact} class="field">{#each audioArtifacts as item}<option value={item.id}>{item.role} · {item.relative_path.split('/').at(-1)}</option>{/each}</select></label><label class="text-sm font-semibold sm:col-span-2">Optional source text<select bind:value={sourceTextArtifact} class="field"><option value="">Automatic transcription</option>{#each textArtifacts as item}<option value={item.id}>{item.role} · {item.relative_path.split('/').at(-1)}</option>{/each}</select></label><label class="text-sm font-semibold">Language<input bind:value={language} class="field"/></label><label class="text-sm font-semibold">Epochs<input type="number" bind:value={epochs} min="1" max="100" class="field"/></label></div>
      <details class="mt-4 rounded-2xl border border-[var(--line)] p-4"><summary class="cursor-pointer text-sm font-semibold">Dataset and training settings</summary><div class="mt-4 grid gap-4 sm:grid-cols-3"><label class="text-sm font-semibold">Whisper model<input bind:value={whisperModel} class="field"/></label><label class="text-sm font-semibold">Sample rate<select bind:value={sampleRate} class="field"><option value={22050}>22,050 Hz</option><option value={44100}>44,100 Hz</option></select></label><label class="text-sm font-semibold">Max clip seconds<input type="number" bind:value={maxDuration} min="1" step="0.1" class="field"/></label><label class="text-sm font-semibold">Max text length<input type="number" bind:value={maxTextLength} min="20" class="field"/></label><label class="text-sm font-semibold">Training split<input bind:value={trainingSplit} class="field"/></label><label class="text-sm font-semibold">Method proportion<input bind:value={methodProportion} class="field"/></label><label class="text-sm font-semibold">Sampling<select bind:value={sampleMethod} class="field"><option value="random">Random</option><option value="length">By length</option><option value="sequential">Sequential</option></select></label><label class="text-sm font-semibold">Alignment model<input bind:value={alignmentModel} placeholder="Automatic" class="field"/></label><label class="text-sm font-semibold">Chapters per audio<input type="number" bind:value={chapterPerAudio} min="1" max="50" class="field"/></label><label class="text-sm font-semibold">Voice sample mode<input bind:value={voiceSampleMode} class="field"/></label><label class="text-sm font-semibold">Voice samples<input type="number" bind:value={voiceSamplesCount} min="1" max="20" class="field"/></label><label class="text-sm font-semibold">Batches<input type="number" bind:value={batches} min="1" class="field"/></label><label class="text-sm font-semibold">Gradient accumulation<input type="number" bind:value={gradient} min="1" class="field"/></label><label class="check"><input type="checkbox" bind:checked={voiceSampleOnlySentence}/> Complete sentences only</label></div></details>
      <details class="mt-3 rounded-2xl border border-[var(--line)] p-4"><summary class="cursor-pointer text-sm font-semibold">Audio preparation</summary><div class="mt-4 grid gap-3 sm:grid-cols-2"><label class="check"><input type="checkbox" bind:checked={enableDenoise}/> Denoise</label><label class="check"><input type="checkbox" bind:checked={enableBreathRemoval}/> Remove breaths</label><label class="check"><input type="checkbox" bind:checked={enableNormalize}/> Loudness normalization</label><label class="check"><input type="checkbox" bind:checked={enableCompress}/> Compression</label><label class="check"><input type="checkbox" bind:checked={enableDess}/> De-ess</label><label class="text-sm font-semibold">Target LUFS<input type="number" bind:value={lufsValue} min="-30" max="-8" class="field"/></label><label class="text-sm font-semibold">Compression profile<input bind:value={compressProfile} class="field"/></label></div></details><button onclick={train} disabled={busy||!sourceArtifact||!modelName.trim()} class="mt-4 flex w-full items-center justify-center gap-2 rounded-xl bg-[var(--accent)] px-4 py-2.5 font-semibold text-white disabled:opacity-40"><BrainCircuit size={16}/> Start training</button>
      <div class="mt-6 space-y-2">{#each training.slice(0,6) as item}<div class="flex items-center gap-3 rounded-xl border border-[var(--line)] px-3 py-2.5"><span class="size-2 rounded-full" class:bg-green-500={item.status==='succeeded'} class:bg-amber-500={['queued','running','cancel_requested'].includes(item.status)} class:bg-red-500={item.status==='failed'}></span><div class="min-w-0 flex-1"><div class="truncate text-sm font-semibold">{item.model_name}</div><div class="muted text-xs">{item.status}{item.error_message?` · ${item.error_message}`:''}</div></div>{#if ['queued','running'].includes(item.status)}<button onclick={()=>cancel(item)} class="text-xs font-semibold">Cancel</button>{/if}</div>{:else}<p class="muted text-sm">No training runs yet.</p>{/each}</div>
    </section>
  </div>
</div>

<style>
  .field{margin-top:.35rem;width:100%;border:1px solid var(--line);border-radius:.75rem;background:var(--paper);padding:.6rem .7rem;font-weight:400}
  .check{display:flex;align-items:center;gap:.5rem;font-size:.875rem}
</style>
