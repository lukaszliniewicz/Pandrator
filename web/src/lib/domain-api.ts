import {
  apiJsonUpload,
  typedApiJson,
  type ApiSchema,
  type UploadProgressCallback,
  type UploadTransferCompleteCallback
} from './api';
import type {
  AgentRun,
  AgentStep,
  ArtifactContext,
  ArtifactRecord,
  AuthStatus,
  DocumentRecord,
  EventSnapshot,
  ForkedSessionRecord,
  GenerationRun,
  GenerationSegment,
  GenerationSegmentPage,
  GlobalDefaultsPayload,
  ItemPage,
  JobRecord,
  JobLogRecord,
  OutcomePlan,
  OutputAssembly,
  ProviderModelRecord,
  ProviderRecord,
  RuntimeCapabilities,
  RvcModelCatalogue,
  ResolvedSettings,
  SessionRecord,
  SessionSource,
  SettingsPayload,
  SourceAsset,
  StageArtifactPage,
  StageRerunImpact,
  StageSettingsMismatch,
  SubtitleReviewPayload,
  SubtitleReviewCatalog,
  TtsCatalogue,
  TtsDiscovery,
  XttsModelCatalogue,
  XttsModelDeletion,
  XttsModelUpload,
  VoiceRecord,
  WaveformData,
  WorkflowSnapshot
} from './api-models';

export const appApi = {
  authStatus: () =>
    typedApiJson<'/api/v1/auth/status', 'get', AuthStatus>(
      '/api/v1/auth/status',
      'get'
    ),
  login: (password: string) =>
    typedApiJson<
      '/api/v1/auth/login',
      'post',
      { authenticated: boolean; csrf_token: string }
    >('/api/v1/auth/login', 'post', { body: { password } }),
  logout: () =>
    typedApiJson<'/api/v1/auth/logout', 'post', void>(
      '/api/v1/auth/logout',
      'post'
    ),
  eventSnapshot: () =>
    typedApiJson<'/api/v1/events/snapshot', 'get', EventSnapshot>(
      '/api/v1/events/snapshot',
      'get'
    ),
  sessions: (includeTrashed = false) =>
    typedApiJson<'/api/v1/sessions', 'get', ItemPage<SessionRecord>>(
      '/api/v1/sessions',
      'get',
      includeTrashed
        ? { query: new URLSearchParams({ include_trashed: 'true' }) }
        : {}
    ),
  jobs: (limit = 40) =>
    typedApiJson<'/api/v1/jobs', 'get', ItemPage<JobRecord>>(
      '/api/v1/jobs',
      'get',
      { query: new URLSearchParams({ limit: String(limit) }) }
    ),
  capabilities: (refresh = false) =>
    typedApiJson<'/api/v1/capabilities', 'get', RuntimeCapabilities>(
      '/api/v1/capabilities',
      'get',
      refresh ? { query: new URLSearchParams({ refresh: 'true' }) } : {}
    )
};

export const sessionApi = {
  list: (includeTrashed = false) => appApi.sessions(includeTrashed),
  create: (body: ApiSchema<'SessionCreate'>) =>
    typedApiJson<'/api/v1/sessions', 'post', SessionRecord>(
      '/api/v1/sessions',
      'post',
      { body }
    ),
  forkAtCheckpoint: (
    sessionId: string,
    body: ApiSchema<'SessionForkRequest'>
  ) =>
    typedApiJson<
      '/api/v1/sessions/{sessionId}/forks',
      'post',
      ForkedSessionRecord
    >('/api/v1/sessions/{sessionId}/forks', 'post', {
      path: { sessionId },
      body
    }),
  get: (sessionId: string) =>
    typedApiJson<'/api/v1/sessions/{sessionId}', 'get', SessionRecord>(
      '/api/v1/sessions/{sessionId}',
      'get',
      { path: { sessionId } }
    ),
  update: (
    sessionId: string,
    revision: number,
    body: ApiSchema<'SessionUpdate'>
  ) =>
    typedApiJson<'/api/v1/sessions/{sessionId}', 'patch', SessionRecord>(
      '/api/v1/sessions/{sessionId}',
      'patch',
      {
        path: { sessionId },
        headers: { 'If-Match': `"${revision}"` },
        body
      }
    ),
  trash: (sessionId: string, revision: number) =>
    typedApiJson<'/api/v1/sessions/{sessionId}', 'delete', SessionRecord>(
      '/api/v1/sessions/{sessionId}',
      'delete',
      {
        path: { sessionId },
        headers: { 'If-Match': `"${revision}"` }
      }
    ),
  restore: (sessionId: string, revision: number) =>
    typedApiJson<'/api/v1/sessions/{sessionId}/restore', 'post', SessionRecord>(
      '/api/v1/sessions/{sessionId}/restore',
      'post',
      {
        path: { sessionId },
        headers: { 'If-Match': `"${revision}"` }
      }
    ),
  reindex: (sessionId: string) =>
    typedApiJson<
      '/api/v1/sessions/{sessionId}/reindex',
      'post',
      { reports: Record<string, unknown>[] }
    >('/api/v1/sessions/{sessionId}/reindex', 'post', {
      path: { sessionId }
    }),
  outcome: (sessionId: string) =>
    typedApiJson<
      '/api/v1/sessions/{sessionId}/outcome-plan',
      'get',
      OutcomePlan
    >('/api/v1/sessions/{sessionId}/outcome-plan', 'get', {
      path: { sessionId }
    }),
  updateOutcome: (
    sessionId: string,
    revision: number,
    value: OutcomePlan['value']
  ) =>
    typedApiJson<
      '/api/v1/sessions/{sessionId}/outcome-plan',
      'put',
      OutcomePlan
    >('/api/v1/sessions/{sessionId}/outcome-plan', 'put', {
      path: { sessionId },
      headers: { 'If-Match': `"${revision}"` },
      body: { value }
    }),
  workflow: (sessionId: string) =>
    typedApiJson<
      '/api/v1/sessions/{sessionId}/workflow',
      'get',
      WorkflowSnapshot
    >('/api/v1/sessions/{sessionId}/workflow', 'get', { path: { sessionId } }),
  settings: (sessionId: string, section: string) =>
    typedApiJson<
      '/api/v1/sessions/{sessionId}/settings/{section}',
      'get',
      SettingsPayload
    >('/api/v1/sessions/{sessionId}/settings/{section}', 'get', {
      path: { sessionId, section }
    }),
  saveSettings: (
    sessionId: string,
    section: string,
    revision: number,
    value: Record<string, unknown>
  ) =>
    typedApiJson<
      '/api/v1/sessions/{sessionId}/settings/{section}',
      'put',
      SettingsPayload
    >('/api/v1/sessions/{sessionId}/settings/{section}', 'put', {
      path: { sessionId, section },
      headers: { 'If-Match': `"${revision}"` },
      body: { value }
    }),
  resolveSettings: (
    sessionId: string,
    sections: string[],
    overrides: Record<string, unknown> = {}
  ) =>
    typedApiJson<
      '/api/v1/sessions/{sessionId}/settings/resolve',
      'post',
      ResolvedSettings
    >('/api/v1/sessions/{sessionId}/settings/resolve', 'post', {
      path: { sessionId },
      body: { sections, overrides }
    }),
  defaults: (section: string) =>
    typedApiJson<'/api/v1/defaults/{section}', 'get', GlobalDefaultsPayload>(
      '/api/v1/defaults/{section}',
      'get',
      { path: { section } }
    ),
  saveDefaults: (
    section: string,
    revision: number,
    value: Record<string, unknown>
  ) =>
    typedApiJson<'/api/v1/settings/{settingKey}', 'put', GlobalDefaultsPayload>(
      '/api/v1/settings/{settingKey}',
      'put',
      {
        path: { settingKey: `defaults.${section}` },
        headers: { 'If-Match': `"${revision}"` },
        body: { value }
      }
    ),
  stageImpact: (sessionId: string, stageKey: string) =>
    typedApiJson<
      '/api/v1/sessions/{sessionId}/stages/{stageKey}/impact',
      'get',
      StageRerunImpact
    >('/api/v1/sessions/{sessionId}/stages/{stageKey}/impact', 'get', {
      path: { sessionId, stageKey }
    }),
  stageSettingsMismatches: (sessionId: string, stageKey: string) =>
    typedApiJson<
      '/api/v1/sessions/{sessionId}/stages/{stageKey}/settings-mismatches',
      'get',
      StageSettingsMismatch
    >(
      '/api/v1/sessions/{sessionId}/stages/{stageKey}/settings-mismatches',
      'get',
      {
        path: { sessionId, stageKey }
      }
    ),
  runStage: (
    sessionId: string,
    stageKey: string,
    body: Record<string, unknown>
  ) =>
    typedApiJson<
      '/api/v1/sessions/{sessionId}/stages/{stageKey}/run',
      'post',
      JobRecord
    >('/api/v1/sessions/{sessionId}/stages/{stageKey}/run', 'post', {
      path: { sessionId, stageKey },
      body
    }),
  previewOutputMix: (
    sessionId: string,
    body: ApiSchema<'OutputMixPreviewRequest'>
  ) =>
    typedApiJson<
      '/api/v1/sessions/{sessionId}/output-mix-preview',
      'post',
      JobRecord
    >('/api/v1/sessions/{sessionId}/output-mix-preview', 'post', {
      path: { sessionId },
      body
    }),
  selectStageArtifact: (
    sessionId: string,
    stageKey: string,
    revision: number,
    artifactId: string | null
  ) =>
    typedApiJson<
      '/api/v1/sessions/{sessionId}/stages/{stageKey}/selection',
      'put',
      WorkflowSnapshot
    >('/api/v1/sessions/{sessionId}/stages/{stageKey}/selection', 'put', {
      path: { sessionId, stageKey },
      headers: { 'If-Match': `"${revision}"` },
      body: { artifact_id: artifactId }
    }),
  stageArtifacts: (
    sessionId: string,
    stageKey: string,
    beforeVersion?: number | null
  ) => {
    const query = new URLSearchParams({ limit: '50' });
    if (beforeVersion != null)
      query.set('before_version', String(beforeVersion));
    return typedApiJson<
      '/api/v1/sessions/{sessionId}/stages/{stageKey}/artifacts',
      'get',
      StageArtifactPage
    >('/api/v1/sessions/{sessionId}/stages/{stageKey}/artifacts', 'get', {
      path: { sessionId, stageKey },
      query
    });
  },
  trashStageArtifact: (
    sessionId: string,
    stageKey: string,
    artifactId: string
  ) =>
    typedApiJson<
      '/api/v1/sessions/{sessionId}/stages/{stageKey}/artifacts/{artifactId}',
      'delete',
      {
        stage_key: string;
        artifact_id: string;
        state: string;
        file_retained: boolean;
      }
    >(
      '/api/v1/sessions/{sessionId}/stages/{stageKey}/artifacts/{artifactId}',
      'delete',
      { path: { sessionId, stageKey, artifactId } }
    ),
  cancelJob: (jobId: string) =>
    typedApiJson<'/api/v1/jobs/{jobId}/cancel', 'post', JobRecord>(
      '/api/v1/jobs/{jobId}/cancel',
      'post',
      { path: { jobId } }
    ),
  ttsCatalogue: (refresh = false) =>
    typedApiJson<'/api/v1/services/tts', 'get', TtsCatalogue>(
      '/api/v1/services/tts',
      'get',
      refresh ? { query: new URLSearchParams({ refresh: 'true' }) } : {}
    ),
  voices: () =>
    typedApiJson<'/api/v1/voices', 'get', ItemPage<VoiceRecord>>(
      '/api/v1/voices',
      'get'
    ),
  discoverTts: (baseUrl: string, serviceId?: string) =>
    typedApiJson<'/api/v1/services/tts/discover', 'post', TtsDiscovery>(
      '/api/v1/services/tts/discover',
      'post',
      {
        body: {
          base_url: baseUrl,
          service_id: serviceId ?? null,
          api_key: null
        }
      }
    ),
  uploadXttsModel: (
    modelId: string,
    files: File[],
    onProgress?: UploadProgressCallback,
    onTransferComplete?: UploadTransferCompleteCallback
  ) => {
    const body = new FormData();
    body.set('model_id', modelId);
    for (const file of files) body.append('files', file, file.name);
    return apiJsonUpload<XttsModelUpload>(
      '/api/v1/services/tts/xtts/models',
      { method: 'POST', body },
      onProgress,
      onTransferComplete
    );
  },
  xttsModels: () =>
    typedApiJson<'/api/v1/services/tts/xtts/models', 'get', XttsModelCatalogue>(
      '/api/v1/services/tts/xtts/models',
      'get'
    ),
  deleteXttsModel: (modelId: string) =>
    typedApiJson<
      '/api/v1/services/tts/xtts/models/{modelId}',
      'delete',
      XttsModelDeletion
    >('/api/v1/services/tts/xtts/models/{modelId}', 'delete', {
      path: { modelId }
    }),
  providers: () =>
    typedApiJson<'/api/v1/providers', 'get', ItemPage<ProviderRecord>>(
      '/api/v1/providers',
      'get'
    ),
  providerModels: (providerId: string) =>
    typedApiJson<
      '/api/v1/providers/{providerId}/models',
      'get',
      ItemPage<ProviderModelRecord>
    >('/api/v1/providers/{providerId}/models', 'get', {
      path: { providerId }
    }),
  sources: (sessionId: string) =>
    typedApiJson<
      '/api/v1/sessions/{sessionId}/sources',
      'get',
      ItemPage<SessionSource>
    >('/api/v1/sessions/{sessionId}/sources', 'get', {
      path: { sessionId }
    }),
  attachSource: (sessionId: string, sourceAssetId: string, role = 'primary') =>
    typedApiJson<
      '/api/v1/sessions/{sessionId}/sources',
      'post',
      {
        id: string;
        session_id: string;
        source_asset_id: string;
        role: string;
        is_current: boolean;
        revision: number;
      }
    >('/api/v1/sessions/{sessionId}/sources', 'post', {
      path: { sessionId },
      body: { source_asset_id: sourceAssetId, role }
    }),
  detachSource: (sessionId: string, attachmentId: string, revision: number) =>
    typedApiJson<
      '/api/v1/sessions/{sessionId}/sources/{attachmentId}',
      'delete',
      void
    >('/api/v1/sessions/{sessionId}/sources/{attachmentId}', 'delete', {
      path: { sessionId, attachmentId },
      headers: { 'If-Match': `"${revision}"` }
    }),
  downloadSourceUrl: (sessionId: string, url: string) =>
    typedApiJson<'/api/v1/sessions/{sessionId}/sources/url', 'post', JobRecord>(
      '/api/v1/sessions/{sessionId}/sources/url',
      'post',
      {
        path: { sessionId },
        body: { url }
      }
    ),
  documents: (sessionId: string) =>
    typedApiJson<
      '/api/v1/sessions/{sessionId}/documents',
      'get',
      ItemPage<DocumentRecord>
    >('/api/v1/sessions/{sessionId}/documents', 'get', {
      path: { sessionId }
    }),
  agentRuns: (sessionId: string, kind?: string) => {
    const query = new URLSearchParams();
    if (kind) query.set('kind', kind);
    return typedApiJson<
      '/api/v1/sessions/{sessionId}/agent-runs',
      'get',
      ItemPage<AgentRun>
    >('/api/v1/sessions/{sessionId}/agent-runs', 'get', {
      path: { sessionId },
      query
    });
  },
  createAgentRun: (
    sessionId: string,
    sourceArtifactId: string,
    settings: Record<string, unknown>
  ) =>
    typedApiJson<'/api/v1/sessions/{sessionId}/agent-runs', 'post', AgentRun>(
      '/api/v1/sessions/{sessionId}/agent-runs',
      'post',
      {
        path: { sessionId },
        body: { source_artifact_id: sourceArtifactId, settings }
      }
    ),
  agentSteps: (runId: string) =>
    typedApiJson<
      '/api/v1/agent-runs/{runId}/steps',
      'get',
      ItemPage<AgentStep>
    >('/api/v1/agent-runs/{runId}/steps', 'get', {
      path: { runId }
    }),
  resumeAgentRun: (runId: string) =>
    typedApiJson<
      '/api/v1/agent-runs/{runId}/resume',
      'post',
      Pick<AgentRun, 'id' | 'job_id' | 'status'>
    >('/api/v1/agent-runs/{runId}/resume', 'post', {
      path: { runId }
    }),
  acceptAgentRun: (runId: string) =>
    typedApiJson<'/api/v1/agent-runs/{runId}/accept', 'post', AgentRun>(
      '/api/v1/agent-runs/{runId}/accept',
      'post',
      { path: { runId } }
    ),
  removeOutput: (sessionId: string, artifactId: string) =>
    typedApiJson<
      '/api/v1/sessions/{sessionId}/outputs/{artifactId}',
      'delete',
      { deleted: boolean }
    >('/api/v1/sessions/{sessionId}/outputs/{artifactId}', 'delete', {
      path: { sessionId, artifactId }
    }),
  subtitleCatalog: (sessionId: string) =>
    typedApiJson<
      '/api/v1/sessions/{sessionId}/subtitles/catalog',
      'get',
      SubtitleReviewCatalog
    >('/api/v1/sessions/{sessionId}/subtitles/catalog', 'get', {
      path: { sessionId }
    }),
  subtitleReview: (sessionId: string, artifactIds: string[]) => {
    const query = new URLSearchParams();
    for (const artifactId of artifactIds)
      query.append('artifact_id', artifactId);
    return typedApiJson<
      '/api/v1/sessions/{sessionId}/subtitles/review',
      'get',
      SubtitleReviewPayload
    >('/api/v1/sessions/{sessionId}/subtitles/review', 'get', {
      path: { sessionId },
      query
    });
  },
  saveSubtitleReview: (
    sessionId: string,
    stage: string,
    body: ApiSchema<'SubtitleReviewRequest'>
  ) =>
    typedApiJson<
      '/api/v1/sessions/{sessionId}/subtitles/{stage}/review',
      'post',
      {
        artifact_id: string;
        document_id: string;
        revision_id: string;
        revision: number;
      }
    >('/api/v1/sessions/{sessionId}/subtitles/{stage}/review', 'post', {
      path: { sessionId, stage },
      body
    })
};

export const sourceApi = {
  list: (includeTrashed = false) =>
    typedApiJson<'/api/v1/sources', 'get', ItemPage<SourceAsset>>(
      '/api/v1/sources',
      'get',
      includeTrashed
        ? { query: new URLSearchParams({ include_trashed: 'true' }) }
        : {}
    ),
  rename: (source: SourceAsset, displayName: string) =>
    typedApiJson<'/api/v1/sources/{sourceAssetId}', 'patch', SourceAsset>(
      '/api/v1/sources/{sourceAssetId}',
      'patch',
      {
        path: { sourceAssetId: source.id },
        headers: { 'If-Match': `"${source.revision}"` },
        body: { display_name: displayName }
      }
    ),
  trash: (source: SourceAsset) =>
    typedApiJson<'/api/v1/sources/{sourceAssetId}', 'delete', SourceAsset>(
      '/api/v1/sources/{sourceAssetId}',
      'delete',
      {
        path: { sourceAssetId: source.id },
        headers: { 'If-Match': `"${source.revision}"` }
      }
    ),
  restore: (source: SourceAsset) =>
    typedApiJson<
      '/api/v1/sources/{sourceAssetId}/restore',
      'post',
      SourceAsset
    >('/api/v1/sources/{sourceAssetId}/restore', 'post', {
      path: { sourceAssetId: source.id },
      headers: { 'If-Match': `"${source.revision}"` }
    })
};

export const artifactApi = {
  upload: (file: File, sessionId?: string, purpose?: string) => {
    const body = new FormData();
    if (sessionId) body.set('session_id', sessionId);
    if (purpose) body.set('purpose', purpose);
    body.set('file', file);
    return typedApiJson<'/api/v1/uploads', 'post', { artifact_id: string }>(
      '/api/v1/uploads',
      'post',
      { body }
    );
  },
  list: (
    options: {
      sessionId?: string;
      limit?: number;
      includeDeleted?: boolean;
    } = {}
  ) => {
    const query = new URLSearchParams();
    if (options.sessionId) query.set('session_id', options.sessionId);
    if (options.limit) query.set('limit', String(options.limit));
    if (options.includeDeleted) query.set('include_deleted', 'true');
    return typedApiJson<'/api/v1/artifacts', 'get', ItemPage<ArtifactRecord>>(
      '/api/v1/artifacts',
      'get',
      { query }
    );
  },
  context: (artifactId: string) =>
    typedApiJson<
      '/api/v1/artifacts/{artifactId}/context',
      'get',
      ArtifactContext
    >('/api/v1/artifacts/{artifactId}/context', 'get', {
      path: { artifactId }
    }),
  waveform: (artifactId: string, points = 1600) =>
    typedApiJson<
      '/api/v1/artifacts/{artifactId}/waveform',
      'get',
      WaveformData
    >('/api/v1/artifacts/{artifactId}/waveform', 'get', {
      path: { artifactId },
      query: new URLSearchParams({ points: String(points) })
    }),
  saveOptimizationReview: (
    artifactId: string,
    items: { index: number; text: string }[]
  ) =>
    typedApiJson<
      '/api/v1/artifacts/{artifactId}/optimization-review',
      'post',
      ArtifactRecord
    >('/api/v1/artifacts/{artifactId}/optimization-review', 'post', {
      path: { artifactId },
      body: { items }
    })
};

export const jobApi = {
  list: (limit = 100) => appApi.jobs(limit),
  get: (jobId: string) =>
    typedApiJson<'/api/v1/jobs/{jobId}', 'get', JobRecord>(
      '/api/v1/jobs/{jobId}',
      'get',
      { path: { jobId } }
    ),
  cancel: (jobId: string) => sessionApi.cancelJob(jobId),
  logs: (jobId: string, limit = 2000) =>
    typedApiJson<'/api/v1/jobs/{jobId}/logs', 'get', ItemPage<JobLogRecord>>(
      '/api/v1/jobs/{jobId}/logs',
      'get',
      {
        path: { jobId },
        query: new URLSearchParams({ limit: String(limit) })
      }
    )
};

export type GenerationSegmentChanges = Partial<
  ApiSchema<'GenerationSegmentUpdate'>
>;
export type GenerationSegmentBatchChange = {
  segment: GenerationSegment;
  changes: GenerationSegmentChanges;
};

export const generationApi = {
  runs: (sessionId: string, signal?: AbortSignal) =>
    typedApiJson<
      '/api/v1/sessions/{sessionId}/generation-runs',
      'get',
      ItemPage<GenerationRun>
    >('/api/v1/sessions/{sessionId}/generation-runs', 'get', {
      path: { sessionId },
      signal
    }),
  segments: (sessionId: string, query: URLSearchParams, signal?: AbortSignal) =>
    typedApiJson<
      '/api/v1/sessions/{sessionId}/generation-segments',
      'get',
      GenerationSegmentPage
    >('/api/v1/sessions/{sessionId}/generation-segments', 'get', {
      path: { sessionId },
      query,
      signal
    }),
  updateSegments: (
    sessionId: string,
    updates: GenerationSegmentBatchChange[]
  ) =>
    typedApiJson<
      '/api/v1/sessions/{sessionId}/generation-segments',
      'patch',
      { items: GenerationSegment[] }
    >('/api/v1/sessions/{sessionId}/generation-segments', 'patch', {
      path: { sessionId },
      body: {
        updates: updates.map(({ segment, changes }) => ({
          id: segment.id,
          revision: segment.revision,
          changes
        }))
      }
    }),
  latestAssembly: (sessionId: string, signal?: AbortSignal) =>
    typedApiJson<
      '/api/v1/sessions/{sessionId}/output-assemblies/latest',
      'get',
      { item: OutputAssembly | null }
    >('/api/v1/sessions/{sessionId}/output-assemblies/latest', 'get', {
      path: { sessionId },
      signal
    }),
  updateSegment: (
    segment: GenerationSegment,
    changes: GenerationSegmentChanges
  ) =>
    typedApiJson<
      '/api/v1/generation-segments/{segmentId}',
      'patch',
      GenerationSegment
    >('/api/v1/generation-segments/{segmentId}', 'patch', {
      path: { segmentId: segment.id },
      headers: { 'If-Match': `"${segment.revision}"` },
      body: changes
    }),
  selectTake: (segment: GenerationSegment, takeId: string) =>
    typedApiJson<
      '/api/v1/generation-segments/{segmentId}/takes/{takeId}/select',
      'post',
      { revision: number }
    >('/api/v1/generation-segments/{segmentId}/takes/{takeId}/select', 'post', {
      path: { segmentId: segment.id, takeId },
      headers: { 'If-Match': `"${segment.revision}"` }
    }),
  start: (
    sessionId: string,
    operation: 'generate' | 'regenerate' | 'rvc',
    segmentIds: string[],
    generationRunId: string | null,
    runOverride: Record<string, unknown>,
    selectedSegmentOverride: Record<string, unknown> = {}
  ) =>
    typedApiJson<
      '/api/v1/sessions/{sessionId}/generation-runs',
      'post',
      GenerationRun
    >('/api/v1/sessions/{sessionId}/generation-runs', 'post', {
      path: { sessionId },
      body: {
        operation,
        segment_ids: segmentIds,
        generation_run_id: generationRunId,
        run_override: runOverride,
        selected_segment_override: selectedSegmentOverride
      }
    }),
  rvcModels: () =>
    typedApiJson<'/api/v1/rvc/models', 'get', RvcModelCatalogue>(
      '/api/v1/rvc/models',
      'get'
    ),
  runAction: (runId: string, action: 'pause' | 'resume' | 'cancel') => {
    if (action === 'pause') {
      return typedApiJson<
        '/api/v1/generation-runs/{runId}/pause',
        'post',
        GenerationRun
      >('/api/v1/generation-runs/{runId}/pause', 'post', {
        path: { runId }
      });
    }
    if (action === 'resume') {
      return typedApiJson<
        '/api/v1/generation-runs/{runId}/resume',
        'post',
        GenerationRun
      >('/api/v1/generation-runs/{runId}/resume', 'post', {
        path: { runId }
      });
    }
    return typedApiJson<
      '/api/v1/generation-runs/{runId}/cancel',
      'post',
      GenerationRun
    >('/api/v1/generation-runs/{runId}/cancel', 'post', {
      path: { runId }
    });
  },
  createAssembly: (
    sessionId: string,
    generationRunId: string | null,
    runOverride: Record<string, unknown> = {}
  ) =>
    typedApiJson<
      '/api/v1/sessions/{sessionId}/output-assemblies',
      'post',
      OutputAssembly
    >('/api/v1/sessions/{sessionId}/output-assemblies', 'post', {
      path: { sessionId },
      body: {
        generation_run_id: generationRunId,
        run_override: runOverride
      }
    }),
  deleteRun: (runId: string) =>
    typedApiJson<'/api/v1/generation-runs/{runId}', 'delete', void>(
      '/api/v1/generation-runs/{runId}',
      'delete',
      { path: { runId } }
    )
};
