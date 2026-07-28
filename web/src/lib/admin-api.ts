import { apiJson, typedApiJson, type ApiSchema } from './api';
import type {
  ArtifactRecord,
  ItemPage,
  JobRecord,
  RuntimeCapabilities,
  RvcModelCatalogue,
  TtsCatalogue,
  TtsDiscovery
} from './api-models';
import { artifactApi, jobApi } from './domain-api';

export type RevisionedValue<T> = {
  revision: number;
  value: T;
  effective?: T;
  builtin?: T;
  [key: string]: unknown;
};

export const settingApi = {
  get: <T>(settingKey: string) =>
    typedApiJson<
      '/api/v1/settings/{settingKey}',
      'get',
      RevisionedValue<T>
    >('/api/v1/settings/{settingKey}', 'get', {
      path: { settingKey }
    }),
  put: <T>(
    settingKey: string,
    revision: number,
    value: T
  ) =>
    typedApiJson<
      '/api/v1/settings/{settingKey}',
      'put',
      RevisionedValue<T>
    >('/api/v1/settings/{settingKey}', 'put', {
      path: { settingKey },
      headers: { 'If-Match': `"${revision}"` },
      body: { value }
    })
};

export const credentialApi = {
  list: <T>() =>
    typedApiJson<'/api/v1/credentials', 'get', ItemPage<T>>(
      '/api/v1/credentials',
      'get'
    ),
  backends: <T>() =>
    typedApiJson<'/api/v1/credential-backends', 'get', ItemPage<T>>(
      '/api/v1/credential-backends',
      'get'
    ),
  update: <T>(
    credentialId: string,
    body: ApiSchema<'CredentialUpdate'>
  ) =>
    typedApiJson<
      '/api/v1/credentials/{credentialId}',
      'put',
      T
    >('/api/v1/credentials/{credentialId}', 'put', {
      path: { credentialId },
      body
    })
};

export const providerApi = {
  list: <T>() =>
    typedApiJson<'/api/v1/providers', 'get', ItemPage<T>>(
      '/api/v1/providers',
      'get'
    ),
  profiles: <T>() =>
    typedApiJson<'/api/v1/providers/profiles', 'get', ItemPage<T>>(
      '/api/v1/providers/profiles',
      'get'
    ),
  models: <T>(providerId: string) =>
    typedApiJson<
      '/api/v1/providers/{providerId}/models',
      'get',
      ItemPage<T>
    >('/api/v1/providers/{providerId}/models', 'get', {
      path: { providerId }
    }),
  create: <T>(body: ApiSchema<'ProviderCreate'>) =>
    typedApiJson<'/api/v1/providers', 'post', T>(
      '/api/v1/providers',
      'post',
      { body }
    ),
  update: <T>(
    providerId: string,
    revision: number,
    body: ApiSchema<'ProviderUpdate'>
  ) =>
    typedApiJson<'/api/v1/providers/{providerId}', 'patch', T>(
      '/api/v1/providers/{providerId}',
      'patch',
      {
        path: { providerId },
        headers: { 'If-Match': `"${revision}"` },
        body
      }
    ),
  remove: (
    providerId: string,
    replacementModelRecordId: string | null
  ) =>
    typedApiJson<'/api/v1/providers/{providerId}', 'delete', void>(
      '/api/v1/providers/{providerId}',
      'delete',
      {
        path: { providerId },
        body: { replacement_model_record_id: replacementModelRecordId }
      }
    ),
  createModel: <T>(
    providerId: string,
    body: ApiSchema<'ModelCreate'>
  ) =>
    typedApiJson<
      '/api/v1/providers/{providerId}/models',
      'post',
      T
    >('/api/v1/providers/{providerId}/models', 'post', {
      path: { providerId },
      body
    }),
  updateModel: <T>(
    providerId: string,
    modelId: string,
    revision: number,
    body: ApiSchema<'ModelUpdate'>
  ) =>
    typedApiJson<
      '/api/v1/providers/{providerId}/models/{modelId}',
      'patch',
      T
    >('/api/v1/providers/{providerId}/models/{modelId}', 'patch', {
      path: { providerId, modelId },
      headers: { 'If-Match': `"${revision}"` },
      body
    }),
  removeModel: (
    providerId: string,
    modelId: string,
    replacementModelRecordId: string | null
  ) =>
    typedApiJson<
      '/api/v1/providers/{providerId}/models/{modelId}',
      'delete',
      void
    >('/api/v1/providers/{providerId}/models/{modelId}', 'delete', {
      path: { providerId, modelId },
      body: { replacement_model_record_id: replacementModelRecordId }
    }),
  refreshModels: <T>(providerId: string) =>
    typedApiJson<
      '/api/v1/providers/{providerId}/models/refresh',
      'post',
      T
    >('/api/v1/providers/{providerId}/models/refresh', 'post', {
      path: { providerId }
    }),
  test: <T>(
    providerId: string,
    body: ApiSchema<'ProviderTestRequest'>
  ) =>
    typedApiJson<'/api/v1/providers/{providerId}/test', 'post', T>(
      '/api/v1/providers/{providerId}/test',
      'post',
      { path: { providerId }, body }
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
        headers: { 'Idempotency-Key': crypto.randomUUID() },
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
        headers: { 'Idempotency-Key': crypto.randomUUID() },
        body
      }
    ),
  uninstallPlan: <T>(body: ApiSchema<'ManagerUninstallPlanRequest'>) =>
    typedApiJson<'/api/v1/manager/uninstall/plans', 'post', T>(
      '/api/v1/manager/uninstall/plans',
      'post',
      {
        headers: { 'Idempotency-Key': crypto.randomUUID() },
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
    typedApiJson<
      '/api/v1/manager/operations/{operationId}/tasks',
      'get',
      T
    >(
      '/api/v1/manager/operations/{operationId}/tasks',
      'get',
      { path: { operationId } }
    ),
  plan: <T>(body: ApiSchema<'ManagerPlanRequest'>) =>
    typedApiJson<'/api/v1/manager/plans', 'post', T>(
      '/api/v1/manager/plans',
      'post',
      {
      headers: { 'Idempotency-Key': crypto.randomUUID() },
      body
      }
    ),
  submit: <T>(body: ApiSchema<'ManagerOperationRequest'>) =>
    typedApiJson<'/api/v1/manager/operations', 'post', T>(
      '/api/v1/manager/operations',
      'post',
      {
        headers: { 'Idempotency-Key': crypto.randomUUID() },
        body
      }
    ),
  cancel: <T>(operationId: string) =>
    typedApiJson<
      '/api/v1/manager/operations/{operationId}/cancel',
      'post',
      T
    >(
      '/api/v1/manager/operations/{operationId}/cancel',
      'post',
      {
        headers: { 'Idempotency-Key': crypto.randomUUID() },
        path: { operationId }
      }
    ),
  runtime: <T>(
    action: 'start' | 'stop' | 'restart',
    serviceIds: string[]
  ) =>
    typedApiJson<'/api/v1/manager/runtime/{action}', 'post', T>(
      '/api/v1/manager/runtime/{action}',
      'post',
      {
        headers: { 'Idempotency-Key': crypto.randomUUID() },
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
    )
};

export const speechServiceApi = {
  catalogue: (refresh = false) =>
    typedApiJson<'/api/v1/services/tts', 'get', TtsCatalogue>(
      '/api/v1/services/tts',
      'get',
      refresh
        ? { query: new URLSearchParams({ refresh: 'true' }) }
        : {}
    ),
  catalogueAs: <T>(refresh = false) =>
    typedApiJson<'/api/v1/services/tts', 'get', T>(
      '/api/v1/services/tts',
      'get',
      refresh
        ? { query: new URLSearchParams({ refresh: 'true' }) }
        : {}
    ),
  discover: (
    body: ApiSchema<'TtsEndpointDiscoveryRequest'>
  ) =>
    typedApiJson<
      '/api/v1/services/tts/discover',
      'post',
      TtsDiscovery
    >('/api/v1/services/tts/discover', 'post', { body }),
  discoverAs: <T>(
    body: ApiSchema<'TtsEndpointDiscoveryRequest'>
  ) =>
    typedApiJson<
      '/api/v1/services/tts/discover',
      'post',
      T
    >('/api/v1/services/tts/discover', 'post', { body }),
  preview: (
    serviceId: string,
    body: ApiSchema<'TtsVoicePreviewRequest'>
  ) =>
    typedApiJson<
      '/api/v1/services/tts/{serviceId}/preview',
      'post',
      JobRecord
    >('/api/v1/services/tts/{serviceId}/preview', 'post', {
      path: { serviceId },
      body
    })
};

export const voiceApi = {
  list: <T>() =>
    typedApiJson<'/api/v1/voices', 'get', ItemPage<T>>(
      '/api/v1/voices',
      'get'
    ),
  create: <T>(body: ApiSchema<'VoiceCreate'>) =>
    typedApiJson<'/api/v1/voices', 'post', T>(
      '/api/v1/voices',
      'post',
      { body }
    ),
  samples: <T>(voiceId: string) =>
    typedApiJson<
      '/api/v1/voices/{voiceId}/samples',
      'get',
      ItemPage<T>
    >('/api/v1/voices/{voiceId}/samples', 'get', {
      path: { voiceId }
    }),
  uploadSample: (
    voiceId: string,
    body: FormData
  ) =>
    typedApiJson<
      '/api/v1/voices/{voiceId}/samples',
      'post',
      JobRecord
    >('/api/v1/voices/{voiceId}/samples', 'post', {
      path: { voiceId },
      body
    }),
  transcribeSample: (
    voiceId: string,
    sampleId: string,
    settings: Record<string, unknown>
  ) =>
    typedApiJson<
      '/api/v1/voices/{voiceId}/samples/{sampleId}/transcribe',
      'post',
      JobRecord
    >('/api/v1/voices/{voiceId}/samples/{sampleId}/transcribe', 'post', {
      path: { voiceId, sampleId },
      body: settings
    }),
  reviewTranscript: <T>(
    voiceId: string,
    sampleId: string,
    body: ApiSchema<'VoiceTranscriptReview'>
  ) =>
    typedApiJson<
      '/api/v1/voices/{voiceId}/samples/{sampleId}/transcript',
      'patch',
      T
    >('/api/v1/voices/{voiceId}/samples/{sampleId}/transcript', 'patch', {
      path: { voiceId, sampleId },
      body
    }),
  publish: (
    voiceId: string,
    serviceId: string
  ) =>
    typedApiJson<
      '/api/v1/voices/{voiceId}/providers/{serviceId}',
      'post',
      JobRecord
    >('/api/v1/voices/{voiceId}/providers/{serviceId}', 'post', {
      path: { voiceId, serviceId }
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
    typedApiJson<'/api/v1/training', 'post', T>(
      '/api/v1/training',
      'post',
      { body }
    ),
  cancelTraining: (trainingId: string) =>
    typedApiJson<
      '/api/v1/training/{trainingId}/cancel',
      'post',
      JobRecord
    >('/api/v1/training/{trainingId}/cancel', 'post', {
      path: { trainingId }
    }),
  retryTraining: <T>(trainingId: string) =>
    typedApiJson<
      '/api/v1/training/{trainingId}/retry',
      'post',
      T
    >('/api/v1/training/{trainingId}/retry', 'post', {
      path: { trainingId }
    })
};

export const pdfApi = {
  inspect: <T>(artifactId: string, firstPageSide: string) =>
    typedApiJson<
      '/api/v1/artifacts/{artifactId}/pdf',
      'get',
      T
    >('/api/v1/artifacts/{artifactId}/pdf', 'get', {
      path: { artifactId },
      query: new URLSearchParams({ first_page_side: firstPageSide })
    }),
  apply: (
    sessionId: string,
    body: ApiSchema<'PdfEditRequest'>
  ) =>
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
    typedApiJson<
      '/api/v1/pronunciations',
      'get',
      ItemPage<T>
    >('/api/v1/pronunciations', 'get', { query }),
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
    typedApiJson<
      '/api/v1/pronunciations/{entryId}',
      'patch',
      T
    >('/api/v1/pronunciations/{entryId}', 'patch', {
      path: { entryId },
      headers: { 'If-Match': `"${revision}"` },
      body
    }),
  remove: (entryId: string, revision: number) =>
    typedApiJson<
      '/api/v1/pronunciations/{entryId}',
      'delete',
      void
    >('/api/v1/pronunciations/{entryId}', 'delete', {
      path: { entryId },
      headers: { 'If-Match': `"${revision}"` }
    })
};

export const diagnosticsApi = {
  capabilities: () =>
    typedApiJson<
      '/api/v1/capabilities',
      'get',
      RuntimeCapabilities
    >('/api/v1/capabilities', 'get'),
  job: jobApi.get
};
