export type LoadState = 'idle' | 'loading' | 'ready' | 'empty' | 'stale' | 'failed';

export type ItemPage<T> = {
  items: T[];
};

export type SessionRecord = {
  id: string;
  name: string;
  storage_key: string;
  workflow_kind: 'audiobook' | 'subtitles' | 'voiceover';
  source_language: string;
  target_language: string | null;
  workflow_preset: string;
  included_stages_json: string[];
  status: string;
  revision: number;
  created_at: string;
  updated_at: string;
};

export type JobRecord = {
  id: string;
  kind: string;
  session_id?: string | null;
  workflow_run_id?: string | null;
  payload_json?: Record<string, unknown>;
  result_json?: Record<string, unknown> | null;
  status: string;
  progress: number;
  progress_detail?: string | null;
  error_code?: string | null;
  error_message?: string | null;
  attempts?: number;
  max_attempts?: number;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  updated_at?: string;
};

export type GpuDevice = {
  name: string;
  vendor: string;
  memory_bytes?: number | null;
  vram_mb?: number | null;
  apis?: string[];
};

export type RuntimeCapabilities = {
  ffmpeg?: {
    available?: boolean;
    burn_video_encoders?: {
      id: string;
      label: string;
      hardware: boolean;
      codec: string;
    }[];
  };
  stt?: {
    crispasr?: boolean;
    compute_backends?: string[];
    default_engine?: string;
    default_model_quantization?: string;
    models?: Record<string, {
      default?: boolean;
      installed?: boolean;
      precision?: string;
      [key: string]: unknown;
    }>;
  };
  gpu?: {
    available?: boolean;
    devices?: GpuDevice[];
    guidance?: string;
  };
  recording?: {
    browser_media_recorder?: boolean;
  };
  services?: Record<string, boolean>;
  [key: string]: unknown;
};

export type AuthStatus = {
  authenticated: boolean;
  initialized: boolean;
  csrf_token?: string;
  remote_access?: boolean;
  security_warning?: string;
};

export type EventSnapshot = {
  cursor: number;
  retained_after: number;
  sessions: ItemPage<SessionRecord>;
  jobs: ItemPage<JobRecord>;
  capabilities: RuntimeCapabilities;
};

export type OutcomePlanValue = {
  workflow_kind?: SessionRecord['workflow_kind'];
  focus?: string;
  transformations?: Record<string, boolean>;
  deliverables?: Record<string, boolean>;
  inputs: Record<string, string>;
  export?: Record<string, unknown>;
  [key: string]: unknown;
};

export type OutcomePlan = {
  revision: number;
  value: OutcomePlanValue;
  pipeline: {
    key?: string;
    title: string;
    [key: string]: unknown;
  }[];
  [key: string]: unknown;
};

export type WorkflowUsage = {
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number | null;
  model_id: string;
  created_at: string;
};

export type ArtifactRecord = {
  id: string;
  session_id?: string | null;
  kind: string;
  role: string;
  raw_role?: string;
  path?: string;
  relative_path: string;
  mime_type: string | null;
  size_bytes: number;
  content_hash: string;
  state: string;
  metadata_json?: Record<string, unknown>;
  created_at: string;
};

export type SourceAsset = {
  id: string;
  artifact_id: string;
  display_name: string;
  kind: string;
  mime_type: string | null;
  external_path?: string | null;
  size_bytes: number;
  content_hash: string;
  state: string;
  metadata?: Record<string, unknown>;
  revision: number;
  reference_count: number;
  current_reference_count: number;
  created_at: string;
  updated_at: string;
};

export type SessionSource = SourceAsset & {
  attachment: {
    id: string;
    role: string;
    is_current: boolean;
    revision: number;
  };
};

export type DocumentRevisionRecord = {
  id: string;
  revision_number: number;
  parent_revision_id?: string | null;
  reviewed: boolean;
  content_hash: string;
  created_at: string;
  segment_count: number;
  duration_ms: number;
  artifact: ArtifactRecord | null;
};

export type DocumentRecord = {
  id: string;
  stage: string;
  language?: string | null;
  active_revision_id?: string | null;
  created_at: string;
  revisions: DocumentRevisionRecord[];
};

export type AgentRun = {
  id: string;
  kind?: string;
  session_id?: string;
  source_artifact_id?: string;
  result_artifact_id?: string | null;
  job_id?: string | null;
  status: string;
  settings_json?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
};

export type AgentStep = {
  id: string;
  agent_run_id: string;
  ordinal: number;
  phase: string;
  status: string;
  summary?: string | null;
  input_json?: Record<string, unknown>;
  output_json?: Record<string, unknown>;
  cost_usd?: number | null;
  created_at?: string;
};

export type ResolvedSettings = {
  value: Record<string, unknown>;
  settings_hash: string;
};

export type JobLogRecord = {
  id: number;
  event_type: string;
  payload_json: Record<string, unknown>;
  created_at: string;
};

export type WaveformData = {
  points?: number[];
  peaks?: number[];
  min?: number[];
  max?: number[];
  duration_ms?: number;
  [key: string]: unknown;
};

export type ArtifactContext = {
  artifact?: ArtifactRecord;
  parents?: ArtifactRecord[];
  children?: ArtifactRecord[];
  usage?: UsageSummary | null;
  [key: string]: unknown;
};

export type SubtitleSegment = {
  id?: string;
  ordinal: number;
  start_ms: number;
  end_ms: number;
  text: string;
  speaker?: string | null;
};

export type SubtitleStage = {
  revision: number;
  segments: SubtitleSegment[];
};

export type SubtitleComparisonRow = {
  start_ms: number;
  end_ms: number;
  changed: boolean;
  transcription?: SubtitleSegment[];
  correction?: SubtitleSegment[];
  translation?: SubtitleSegment[];
  tts_optimization?: SubtitleSegment[];
};

export type SubtitleReviewPayload = {
  stages: Record<string, SubtitleStage>;
  rows: SubtitleComparisonRow[];
};

export type WorkflowStageArtifact = ArtifactRecord & {
  version: number;
  created_at: string;
  is_selected: boolean;
  parent_ids: string[];
  settings_hash?: string | null;
};

export type WorkflowStage = {
  number: number;
  key: string;
  title: string;
  explanation: string;
  status: 'unavailable' | 'ready' | 'running' | 'completed' | 'stale' | 'failed';
  executable: boolean;
  toggle?: boolean;
  toggle_only?: boolean;
  enabled?: boolean | null;
  optimization_timing?: 'document' | 'generation';
  included: boolean;
  required?: boolean;
  artifact?: WorkflowStageArtifact | null;
  artifacts?: WorkflowStageArtifact[];
  selected_artifact_id?: string | null;
  selection_revision?: number;
  artifact_history_total?: number;
  artifact_history_has_more?: boolean;
  artifact_history_next_before_version?: number | null;
  job_id?: string | null;
  progress?: number | null;
  detail?: string | null;
  usage?: WorkflowUsage | null;
};

export type WorkflowSnapshot = {
  session_id: string;
  workflow_kind: string;
  workflow_preset: string;
  revision: number;
  stages: WorkflowStage[];
  sources: {
    id: string;
    filename: string;
    kind: string;
    role: string;
  }[];
};

export type SettingsPayload = {
  revision: number;
  effective: Record<string, unknown>;
  override: Record<string, unknown>;
  global?: Record<string, unknown>;
  [key: string]: unknown;
};

export type GlobalDefaultsPayload = {
  revision: number;
  value: Record<string, unknown>;
  builtin?: Record<string, unknown>;
  effective?: Record<string, unknown>;
  [key: string]: unknown;
};

export type VoiceProviderRegistration = {
  status?: string;
  voice_id?: string;
  [key: string]: unknown;
};

export type VoiceRecord = {
  id: string;
  name: string;
  language?: string;
  description?: string | null;
  metadata_json?: {
    providers?: Record<string, VoiceProviderRegistration>;
    [key: string]: unknown;
  };
  revision?: number;
};

export type TtsLanguage = {
  id?: string;
  name?: string;
  code?: string;
  [key: string]: unknown;
};

export type TtsModel = {
  id: string;
  label?: string;
  display_name?: string;
  name?: string;
  license?: {
    url: string;
    name?: string;
    id?: string;
  };
  languages?: (string | TtsLanguage)[];
  [key: string]: unknown;
};

export type TtsService = {
  id: string;
  name: string;
  description?: string;
  source_url?: string;
  kind?: string;
  provider?: string;
  adapter?: string;
  api_base?: string;
  api_key_env?: string;
  secret_ref?: string;
  speech_path?: string;
  models_path?: string;
  voices_path?: string;
  request_fields?: unknown;
  request_defaults?: Record<string, unknown>;
  auth_mode?: string;
  direct_http?: boolean;
  vertex_project?: string;
  vertex_location?: string;
  online?: boolean;
  available?: boolean;
  availability_reason?: string;
  models?: string[];
  model_catalog?: TtsModel[];
  default_model?: string;
  voices?: string[];
  live_voices?: string[];
  voice_catalogues?: Record<string, string[]>;
  voice_metadata?: Record<string, Record<string, unknown>>;
  default_voice?: string;
  default_voices?: Record<string, string>;
  default_voices_by_language?: Record<string, Record<string, string>>;
  generation_prompt_models?: string[];
  supports_voice_cloning?: boolean;
  supports_prebuilt_voices?: boolean;
  settings?: Record<string, unknown>;
  credential_configured?: boolean;
  credential_source?: string;
  credential_backend?: 'database' | 'environment' | 'keyring' | 'file';
  credential_reference?: string | null;
  clear_api_key?: boolean;
  delete_previous_credential?: boolean;
  api_key?: string;
  [key: string]: unknown;
};

export type TtsPreviewRecord = {
  artifact_id: string;
  service_id: string;
  model: string;
  voice: string;
  language: string;
  preview_text?: string;
  updated_at?: string;
};

export type TtsSettingsValue = {
  provider_configs?: TtsService[];
  [key: string]: unknown;
};

export type TtsCatalogue = {
  services: TtsService[];
  profiles?: TtsService[];
  value?: TtsSettingsValue;
  revision?: number;
  default_value?: Record<string, unknown>;
  default_service?: string;
  default_revision?: number;
  builtin_defaults?: Record<string, unknown>;
  previews?: TtsPreviewRecord[];
  [key: string]: unknown;
};

export type TtsDiscovery = Partial<TtsService> & {
  success?: boolean;
  languages?: string[];
  message?: string;
  confidence?: string;
  error?: string;
};

export type ProviderRecord = {
  id: string;
  kind?: string;
  provider_key: string;
  label: string;
  enabled: boolean;
  base_url?: string | null;
  options_json?: Record<string, unknown>;
  revision?: number;
  [key: string]: unknown;
};

export type ProviderModelRecord = {
  id: string;
  provider_id: string;
  model_id: string;
  is_active?: boolean;
  is_default?: boolean;
  [key: string]: unknown;
};

export type StageRerunImpact = {
  dependent_selections?: {
    role: string;
    [key: string]: unknown;
  }[];
  descendant_total?: number;
  descendants?: {
    stage?: string;
    [key: string]: unknown;
  }[];
  [key: string]: unknown;
};

export type StageSettingsMismatch = {
  mismatches: {
    stage: string;
    changed_fields: string[];
  }[];
  [key: string]: unknown;
};

export type StageArtifactPage = {
  items: WorkflowStageArtifact[];
  total: number;
  has_more: boolean;
  next_before_version: number | null;
  revision?: number;
  selected_artifact_id?: string | null;
  [key: string]: unknown;
};

export type SpeechPlanDecision = {
  span_id?: string;
  action?: string;
  confidence?: string | number;
  [key: string]: unknown;
};

export type SpeechPlanCandidate = {
  id: string;
  text: string;
  task?: string;
  signals?: string[];
  [key: string]: unknown;
};

export type SpeechPlanDiscovery = {
  source_text?: string;
  [key: string]: unknown;
};

export type SpeechPlan = {
  version?: number;
  status?: string;
  mode_used?: string;
  model?: string;
  language?: string;
  voice_language?: string;
  decisions?: SpeechPlanDecision[];
  candidates?: SpeechPlanCandidate[];
  discoveries?: SpeechPlanDiscovery[];
  known_pronunciations?: { text: string; spoken: string }[];
  proposals?: { source_form: string; phonetic: string }[];
  validation?: {
    errors?: string[];
    warnings?: string[];
  };
  [key: string]: unknown;
};

export type AudioVerification = {
  status?: string;
  metrics?: {
    rms_dbfs?: number;
    peak_dbfs?: number;
    tail_rms_dbfs?: number;
    [key: string]: unknown;
  };
  issues?: {
    code?: string;
    message?: string;
    [key: string]: unknown;
  }[];
  [key: string]: unknown;
};

export type AudioTake = {
  id: string;
  generation_run_id?: string | null;
  artifact_id?: string | null;
  parent_take_id?: string | null;
  kind?: string;
  status: string;
  duration_ms?: number | null;
  is_active: boolean;
  revision: number;
  created_at?: string;
  source_text?: string | null;
  synthesized_text?: string | null;
  llm_optimized?: boolean;
  llm_model?: string | null;
  audio_verification?: AudioVerification | null;
};

export type GenerationSegment = {
  id: string;
  ordinal: number;
  node_kind: 'paragraph' | 'heading' | 'chapter_marker' | 'subtitle_cue';
  paragraph_break_after: boolean;
  speaker?: string | null;
  text: string;
  source_segment_ids?: string[];
  optimized_text?: string | null;
  speech_plan?: SpeechPlan;
  optimization_status?: string | null;
  optimization_reviewed?: boolean;
  optimization_model?: string | null;
  voice_id?: string | null;
  voice?: string | null;
  language?: string | null;
  silence_after_ms?: number;
  marked: boolean;
  removed: boolean;
  status: string;
  revision: number;
  takes: AudioTake[];
};

export type GenerationSegmentPage = {
  items: GenerationSegment[];
  next_cursor: number | null;
  total: number;
  plan_revision_id: string | null;
};

export type OutputAssembly = {
  id: string;
  session_id: string;
  generation_run_id?: string | null;
  job_id?: string | null;
  artifact_id?: string | null;
  status: string;
  progress: number;
  progress_detail?: string | null;
  settings_hash?: string | null;
  error_message?: string | null;
  settings?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
};

export type UsageSummary = {
  total_cost_usd?: number | null;
  commercial?: boolean;
  estimated?: boolean;
  has_unpriced_usage?: boolean;
  [key: string]: unknown;
};

export type GenerationRun = {
  id: string;
  session_id: string;
  plan_revision_id: string;
  sequence_number: number;
  operation: string;
  label: string;
  job_id?: string | null;
  status: string;
  progress: number;
  progress_detail?: string | null;
  pause_requested?: boolean;
  cancel_requested?: boolean;
  settings_hash?: string;
  error_message?: string | null;
  take_count?: number;
  usage?: UsageSummary;
  assembly?: OutputAssembly | null;
  created_at?: string;
  updated_at?: string;
};

export type RvcModelCatalogue = {
  available: boolean;
  items: string[];
};
