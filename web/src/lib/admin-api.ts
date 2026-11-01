import { apiJson, typedApiJson, type ApiSchema } from './api';
import type {
  ItemPage,
  JobRecord,
  RuntimeCapabilities,
  RvcModelCatalogue,
  SttCatalogue,
  TtsCatalogue,
  TtsDiscovery
} from './api-models';
import { artifactApi, jobApi } from './domain-api';
import {
  fetchTtsCatalogue,
  speechScopeQuery,
  type SpeechCatalogueScope
} from './speech-catalogue-api';

export type RevisionedValue<T> = {
  workspace_id?: string;
  can_manage?: boolean;
  setting_scope?: 'workspace' | 'installation';
  revision: number;
  value: T;
  effective?: T;
  builtin?: T;
  [key: string]: unknown;
};

export const settingApi = {
  get: <T>(settingKey: string, workspaceId?: string) =>
    typedApiJson<'/api/v1/settings/{settingKey}', 'get', RevisionedValue<T>>(
      '/api/v1/settings/{settingKey}',
      'get',
      {
        path: { settingKey },
        query: workspaceQuery(workspaceId)
      }
    ),
  put: <T>(
    settingKey: string,
    revision: number,
    value: T,
    workspaceId?: string
  ) =>
    typedApiJson<'/api/v1/settings/{settingKey}', 'put', RevisionedValue<T>>(
      '/api/v1/settings/{settingKey}',
      'put',
      {
        path: { settingKey },
        query: workspaceQuery(workspaceId),
        headers: { 'If-Match': `"${revision}"` },
        body: { value }
      }
    )
};

export type WorkspaceCatalogue<T> = ItemPage<T> & {
  workspace_id: string;
  can_manage: boolean;
  credential_backends?: string[];
};
const workspaceQuery = (workspaceId?: string) =>
  workspaceId ? { workspace_id: workspaceId } : undefined;

export const credentialApi = {
  list: <T>(workspaceId?: string) =>
    typedApiJson<'/api/v1/credentials', 'get', WorkspaceCatalogue<T>>(
      '/api/v1/credentials',
      'get',
      { query: workspaceQuery(workspaceId) }
    ),
  backends: <T>() =>
    typedApiJson<'/api/v1/credential-backends', 'get', ItemPage<T>>(
      '/api/v1/credential-backends',
      'get'
    ),
  update: <T>(
    credentialId: string,
    body: ApiSchema<'CredentialUpdate'>,
    workspaceId?: string
  ) =>
    typedApiJson<'/api/v1/credentials/{credentialId}', 'put', T>(
      '/api/v1/credentials/{credentialId}',
      'put',
      {
        path: { credentialId },
        query: workspaceQuery(workspaceId),
        body
      }
    )
};

export const providerApi = {
  list: <T>(workspaceId?: string) =>
    typedApiJson<'/api/v1/providers', 'get', WorkspaceCatalogue<T>>(
      '/api/v1/providers',
      'get',
      { query: workspaceQuery(workspaceId) }
    ),
  profiles: <T>() =>
    typedApiJson<'/api/v1/providers/profiles', 'get', ItemPage<T>>(
      '/api/v1/providers/profiles',
      'get'
    ),
  models: <T>(providerId: string, workspaceId?: string) =>
    typedApiJson<'/api/v1/providers/{providerId}/models', 'get', ItemPage<T>>(
      '/api/v1/providers/{providerId}/models',
      'get',
      {
        path: { providerId },
        query: workspaceQuery(workspaceId)
      }
    ),
  create: <T>(body: ApiSchema<'ProviderCreate'>, workspaceId?: string) =>
    typedApiJson<'/api/v1/providers', 'post', T>('/api/v1/providers', 'post', {
      body,
      query: workspaceQuery(workspaceId)
    }),
  update: <T>(
    providerId: string,
    revision: number,
    body: ApiSchema<'ProviderUpdate'>,
    workspaceId?: string
  ) =>
    typedApiJson<'/api/v1/providers/{providerId}', 'patch', T>(
      '/api/v1/providers/{providerId}',
      'patch',
      {
        path: { providerId },
        query: workspaceQuery(workspaceId),
        headers: { 'If-Match': `"${revision}"` },
        body
      }
    ),
  remove: (
    providerId: string,
    replacementModelRecordId: string | null,
    workspaceId?: string
  ) =>
    typedApiJson<'/api/v1/providers/{providerId}', 'delete', void>(
      '/api/v1/providers/{providerId}',
      'delete',
      {
        path: { providerId },
        query: workspaceQuery(workspaceId),
        body: { replacement_model_record_id: replacementModelRecordId }
      }
    ),
  createModel: <T>(
    providerId: string,
    body: ApiSchema<'ModelCreate'>,
    workspaceId?: string
  ) =>
    typedApiJson<'/api/v1/providers/{providerId}/models', 'post', T>(
      '/api/v1/providers/{providerId}/models',
      'post',
      {
        path: { providerId },
        query: workspaceQuery(workspaceId),
        body
      }
    ),
  updateModel: <T>(
    providerId: string,
    modelId: string,
    revision: number,
    body: ApiSchema<'ModelUpdate'>,
    workspaceId?: string
  ) =>
    typedApiJson<'/api/v1/providers/{providerId}/models/{modelId}', 'patch', T>(
      '/api/v1/providers/{providerId}/models/{modelId}',
      'patch',
      {
        path: { providerId, modelId },
        query: workspaceQuery(workspaceId),
        headers: { 'If-Match': `"${revision}"` },
        body
      }
    ),
  removeModel: (
    providerId: string,
    modelId: string,
    replacementModelRecordId: string | null,
    workspaceId?: string
  ) =>
    typedApiJson<
      '/api/v1/providers/{providerId}/models/{modelId}',
      'delete',
      void
    >('/api/v1/providers/{providerId}/models/{modelId}', 'delete', {
      path: { providerId, modelId },
      query: workspaceQuery(workspaceId),
      body: { replacement_model_record_id: replacementModelRecordId }
    }),
  refreshModels: <T>(providerId: string, workspaceId?: string) =>
    typedApiJson<'/api/v1/providers/{providerId}/models/refresh', 'post', T>(
      '/api/v1/providers/{providerId}/models/refresh',
      'post',
      {
        path: { providerId },
        query: workspaceQuery(workspaceId)
      }
    ),
  test: <T>(
    providerId: string,
    body: ApiSchema<'ProviderTestRequest'>,
    workspaceId?: string
  ) =>
    typedApiJson<'/api/v1/providers/{providerId}/test', 'post', T>(
      '/api/v1/providers/{providerId}/test',
      'post',
      { path: { providerId }, body, query: workspaceQuery(workspaceId) }
    )
};

export const managerApi = {
  status: <T>() =>
    typedApiJson<'/api/v1/manager/status', 'get', T>(
      '/api/v1/manager/status',
      'get'
    ),
  components: <T>() =>
    typedApiJson<'/api/v1/manager/components', 'get', T>(
      '/api/v1/manager/components',
      'get'
    ),
  doctor: <T>() =>
    typedApiJson<'/api/v1/manager/doctor', 'get', T>(
      '/api/v1/manager/doctor',
      'get'
    ),
  legacy: <T>() =>
    typedApiJson<'/api/v1/manager/legacy', 'get', T>(
      '/api/v1/manager/legacy',
      'get'
    ),
  importLegacy: <T>(body: ApiSchema<'ManagerLegacyImportRequest'>) =>
    typedApiJson<'/api/v1/manager/legacy/import', 'post', T>(
      '/api/v1/manager/legacy/import',
      'post',
      {
        body
      }
    ),
  services: <T>() =>
    typedApiJson<'/api/v1/manager/services', 'get', T>(
      '/api/v1/manager/services',
      'get'
    ),
  releases: <T>() =>
    typedApiJson<'/api/v1/manager/releases', 'get', T>(
      '/api/v1/manager/releases',
      'get'
    ),
  releasePlan: <T>(body: ApiSchema<'ManagerReleasePlanRequest'>) =>
    typedApiJson<'/api/v1/manager/releases/plans', 'post', T>(
      '/api/v1/manager/releases/plans',
      'post',
      {
        body
      }
    ),
  uninstallPlan: <T>(body: ApiSchema<'ManagerUninstallPlanRequest'>) =>
    typedApiJson<'/api/v1/manager/uninstall/plans', 'post', T>(
      '/api/v1/manager/uninstall/plans',
      'post',
      {
        body
      }
    ),
  operations: <T>() =>
    typedApiJson<'/api/v1/manager/operations', 'get', T>(
      '/api/v1/manager/operations',
      'get'
    ),
  operation: <T>(operationId: string) =>
    typedApiJson<'/api/v1/manager/operations/{operationId}', 'get', T>(
      '/api/v1/manager/operations/{operationId}',
      'get',
      { path: { operationId } }
    ),
  operationTasks: <T>(operationId: string) =>
    typedApiJson<'/api/v1/manager/operations/{operationId}/tasks', 'get', T>(
      '/api/v1/manager/operations/{operationId}/tasks',
      'get',
      { path: { operationId } }
    ),
  plan: <T>(body: ApiSchema<'ManagerPlanRequest'>) =>
    typedApiJson<'/api/v1/manager/plans', 'post', T>(
      '/api/v1/manager/plans',
      'post',
      {
        body
      }
    ),
  submit: <T>(body: ApiSchema<'ManagerOperationRequest'>) =>
    typedApiJson<'/api/v1/manager/operations', 'post', T>(
      '/api/v1/manager/operations',
      'post',
      {
        body
      }
    ),
  cancel: <T>(operationId: string) =>
    typedApiJson<'/api/v1/manager/operations/{operationId}/cancel', 'post', T>(
      '/api/v1/manager/operations/{operationId}/cancel',
      'post',
      {
        path: { operationId }
      }
    ),
  runtime: <T>(action: 'start' | 'stop' | 'restart', serviceIds: string[]) =>
    typedApiJson<'/api/v1/manager/runtime/{action}', 'post', T>(
      '/api/v1/manager/runtime/{action}',
      'post',
      {
        path: { action },
        body: { service_ids: serviceIds }
      }
    ),
  logs: <T>(serviceId: string, bytes = 65536) =>
    apiJson<T>(
      `/api/v1/manager/logs?${new URLSearchParams({
        service_id: serviceId,
        bytes: String(bytes)
      })}`
    ),
  mcpPaths: <T>() => apiJson<T>('/api/v1/automation/local-paths'),
  saveMcpPaths: <T>(body: {
    source_roots: Array<{ name: string; path: string }>;
    output_root: string | null;
  }) =>
    apiJson<T>('/api/v1/automation/local-paths', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    })
};

export const speechServiceApi = {
  catalogue: (refresh = false, scope?: SpeechCatalogueScope) =>
    fetchTtsCatalogue<TtsCatalogue>(refresh, scope),
  catalogueAs: <T>(refresh = false, scope?: SpeechCatalogueScope) =>
    fetchTtsCatalogue<T>(refresh, scope),
  discover: (
    body: ApiSchema<'TtsEndpointDiscoveryRequest'>,
    workspaceId?: string
  ) =>
    typedApiJson<'/api/v1/services/tts/discover', 'post', TtsDiscovery>(
      '/api/v1/services/tts/discover',
      'post',
      { body, query: speechScopeQuery({ workspaceId }) }
    ),
  discoverAs: <T>(
    body: ApiSchema<'TtsEndpointDiscoveryRequest'>,
    workspaceId?: string
  ) =>
    typedApiJson<'/api/v1/services/tts/discover', 'post', T>(
      '/api/v1/services/tts/discover',
      'post',
      { body, query: speechScopeQuery({ workspaceId }) }
    ),
  preview: (
    serviceId: string,
    body: ApiSchema<'TtsVoicePreviewRequest'>,
    workspaceId?: string
  ) =>
    typedApiJson<
      '/api/v1/services/tts/{serviceId}/preview',
      'post',
      JobRecord & {
        workspace_id: string;
        preview?: {
          directions: {
            source: 'inherited' | 'custom' | 'cleared';
            present: boolean;
          };
        };
      }
    >('/api/v1/services/tts/{serviceId}/preview', 'post', {
      path: { serviceId },
      query: workspaceQuery(workspaceId),
      body
    })
};

export const speechRecognitionApi = {
  catalogue: (scope?: SpeechCatalogueScope) =>
    typedApiJson<'/api/v1/services/stt', 'get', SttCatalogue>(
      '/api/v1/services/stt',
      'get',
      { query: speechScopeQuery(scope) }
    )
};

export const voiceApi = {
  designGoogle: (
    body: ApiSchema<'GoogleVoiceDesignCreate'>,
    idempotencyKey: string,
    workspaceId?: string
  ) =>
    typedApiJson<
      '/api/v1/voices/design/google',
      'post',
      JobRecord & { voice_id: string; workspace_id: string }
    >('/api/v1/voices/design/google', 'post', {
      headers: { 'Idempotency-Key': idempotencyKey },
      query: workspaceQuery(workspaceId),
      body
    }),
  list: <T>(scope?: SpeechCatalogueScope) =>
    typedApiJson<'/api/v1/voices', 'get', ItemPage<T>>(
      '/api/v1/voices',
      'get',
      {
        query: speechScopeQuery(scope)
      }
    ),
  create: <T>(body: ApiSchema<'VoiceCreate'>, workspaceId?: string) =>
    typedApiJson<'/api/v1/voices', 'post', T>('/api/v1/voices', 'post', {
      body,
      query: workspaceQuery(workspaceId)
    }),
  update: <T>(
    voiceId: string,
    revision: number,
    body: ApiSchema<'VoiceUpdate'>,
    workspaceId?: string
  ) =>
    typedApiJson<'/api/v1/voices/{voiceId}', 'patch', T>(
      '/api/v1/voices/{voiceId}',
      'patch',
      {
        path: { voiceId },
        query: workspaceQuery(workspaceId),
        headers: { 'If-Match': `"${revision}"` },
        body
      }
    ),
  delete: (voiceId: string, revision: number, workspaceId?: string) =>
    typedApiJson<'/api/v1/voices/{voiceId}', 'delete', void>(
      '/api/v1/voices/{voiceId}',
      'delete',
      {
        path: { voiceId },
        query: workspaceQuery(workspaceId),
        headers: { 'If-Match': `"${revision}"` }
      }
    ),
  samples: <T>(voiceId: string, workspaceId?: string) =>
    typedApiJson<'/api/v1/voices/{voiceId}/samples', 'get', ItemPage<T>>(
      '/api/v1/voices/{voiceId}/samples',
      'get',
      {
        path: { voiceId },
        query: workspaceQuery(workspaceId)
      }
    ),
  uploadSample: (
    voiceId: string,
    revision: number,
    body: FormData,
    workspaceId?: string
  ) =>
    typedApiJson<'/api/v1/voices/{voiceId}/samples', 'post', JobRecord>(
      '/api/v1/voices/{voiceId}/samples',
      'post',
      {
        path: { voiceId },
        query: workspaceQuery(workspaceId),
        headers: { 'If-Match': `"${revision}"` },
        body
      }
    ),
  promoteDesignPreview: (
    voiceId: string,
    revision: number,
    body: ApiSchema<'VoiceDesignedSampleCreate'>,
    workspaceId?: string
  ) =>
    typedApiJson<
      '/api/v1/voices/{voiceId}/samples/from-preview',
      'post',
      JobRecord
    >('/api/v1/voices/{voiceId}/samples/from-preview', 'post', {
      path: { voiceId },
      query: workspaceQuery(workspaceId),
      headers: { 'If-Match': `"${revision}"` },
      body
    }),
  replaceSample: (
    voiceId: string,
    sampleId: string,
    revision: number,
    body: FormData,
    workspaceId?: string
  ) =>
    typedApiJson<
      '/api/v1/voices/{voiceId}/samples/{sampleId}/replace',
      'post',
      JobRecord
    >('/api/v1/voices/{voiceId}/samples/{sampleId}/replace', 'post', {
      path: { voiceId, sampleId },
      query: workspaceQuery(workspaceId),
      headers: { 'If-Match': `"${revision}"` },
      body
    }),
  deleteSample: (
    voiceId: string,
    sampleId: string,
    revision: number,
    workspaceId?: string
  ) =>
    typedApiJson<
      '/api/v1/voices/{voiceId}/samples/{sampleId}',
      'delete',
      { id: string; status: string; voice_revision: number }
    >('/api/v1/voices/{voiceId}/samples/{sampleId}', 'delete', {
      path: { voiceId, sampleId },
      query: workspaceQuery(workspaceId),
      headers: { 'If-Match': `"${revision}"` }
    }),
  transcribeSample: (
    voiceId: string,
    sampleId: string,
    settings: Record<string, unknown>,
    workspaceId?: string
  ) =>
    typedApiJson<
      '/api/v1/voices/{voiceId}/samples/{sampleId}/transcribe',
      'post',
      JobRecord
    >('/api/v1/voices/{voiceId}/samples/{sampleId}/transcribe', 'post', {
      path: { voiceId, sampleId },
      query: workspaceQuery(workspaceId),
      body: settings
    }),
  reviewTranscript: <T>(
    voiceId: string,
    sampleId: string,
    body: ApiSchema<'VoiceTranscriptReview'>,
    workspaceId?: string
  ) =>
    typedApiJson<
      '/api/v1/voices/{voiceId}/samples/{sampleId}/transcript',
      'patch',
      T
    >('/api/v1/voices/{voiceId}/samples/{sampleId}/transcript', 'patch', {
      path: { voiceId, sampleId },
      query: workspaceQuery(workspaceId),
      body
    }),
  publish: (
    voiceId: string,
    serviceId: string,
    revision: number,
    workspaceId?: string
  ) =>
    typedApiJson<
      '/api/v1/voices/{voiceId}/providers/{serviceId}',
      'post',
      JobRecord
    >('/api/v1/voices/{voiceId}/providers/{serviceId}', 'post', {
      path: { voiceId, serviceId },
      query: workspaceQuery(workspaceId),
      headers: { 'If-Match': `"${revision}"` }
    }),
  unpublish: (
    voiceId: string,
    serviceId: string,
    revision: number,
    workspaceId?: string
  ) =>
    typedApiJson<
      '/api/v1/voices/{voiceId}/providers/{serviceId}',
      'delete',
      JobRecord
    >('/api/v1/voices/{voiceId}/providers/{serviceId}', 'delete', {
      path: { voiceId, serviceId },
      query: workspaceQuery(workspaceId),
      headers: { 'If-Match': `"${revision}"` }
    })
};

export const toolApi = {
  rvcModels: () =>
    typedApiJson<'/api/v1/rvc/models', 'get', RvcModelCatalogue>(
      '/api/v1/rvc/models',
      'get'
    ),
  upload: artifactApi.upload,
  artifacts: () => artifactApi.list(),
  training: <T>() =>
    typedApiJson<'/api/v1/training', 'get', ItemPage<T>>(
      '/api/v1/training',
      'get'
    ),
  addRvcModel: (body: ApiSchema<'RvcModelUploadRequest'>) =>
    typedApiJson<'/api/v1/rvc/models', 'post', JobRecord>(
      '/api/v1/rvc/models',
      'post',
      { body }
    ),
  convertRvc: (body: ApiSchema<'RvcConvertRequest'>) =>
    typedApiJson<'/api/v1/rvc/convert', 'post', JobRecord>(
      '/api/v1/rvc/convert',
      'post',
      { body }
    ),
  createTraining: <T>(body: ApiSchema<'TrainingCreateRequest'>) =>
    typedApiJson<'/api/v1/training', 'post', T>('/api/v1/training', 'post', {
      body
    }),
  cancelTraining: (trainingId: string) =>
    typedApiJson<'/api/v1/training/{trainingId}/cancel', 'post', JobRecord>(
      '/api/v1/training/{trainingId}/cancel',
      'post',
      {
        path: { trainingId }
      }
    ),
  retryTraining: <T>(trainingId: string) =>
    typedApiJson<'/api/v1/training/{trainingId}/retry', 'post', T>(
      '/api/v1/training/{trainingId}/retry',
      'post',
      {
        path: { trainingId }
      }
    )
};

export const pdfApi = {
  inspect: <T>(artifactId: string, firstPageSide: string) =>
    typedApiJson<'/api/v1/artifacts/{artifactId}/pdf', 'get', T>(
      '/api/v1/artifacts/{artifactId}/pdf',
      'get',
      {
        path: { artifactId },
        query: new URLSearchParams({ first_page_side: firstPageSide })
      }
    ),
  apply: (sessionId: string, body: ApiSchema<'PdfEditRequest'>) =>
    typedApiJson<
      '/api/v1/sessions/{sessionId}/pdf/apply',
      'post',
      { id: string }
    >('/api/v1/sessions/{sessionId}/pdf/apply', 'post', {
      path: { sessionId },
      body
    })
};

export const pronunciationApi = {
  list: <T>(query: URLSearchParams) =>
    typedApiJson<'/api/v1/pronunciations', 'get', ItemPage<T>>(
      '/api/v1/pronunciations',
      'get',
      { query }
    ),
  create: <T>(body: ApiSchema<'PronunciationCreate'>) =>
    typedApiJson<'/api/v1/pronunciations', 'post', T>(
      '/api/v1/pronunciations',
      'post',
      { body }
    ),
  update: <T>(
    entryId: string,
    revision: number,
    body: ApiSchema<'PronunciationUpdate'>
  ) =>
    typedApiJson<'/api/v1/pronunciations/{entryId}', 'patch', T>(
      '/api/v1/pronunciations/{entryId}',
      'patch',
      {
        path: { entryId },
        headers: { 'If-Match': `"${revision}"` },
        body
      }
    ),
  remove: (entryId: string, revision: number) =>
    typedApiJson<'/api/v1/pronunciations/{entryId}', 'delete', void>(
      '/api/v1/pronunciations/{entryId}',
      'delete',
      {
        path: { entryId },
        headers: { 'If-Match': `"${revision}"` }
      }
    )
};

export const diagnosticsApi = {
  capabilities: () =>
    typedApiJson<'/api/v1/capabilities', 'get', RuntimeCapabilities>(
      '/api/v1/capabilities',
      'get'
    ),
  job: jobApi.get
};
