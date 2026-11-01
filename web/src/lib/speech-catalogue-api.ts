import { typedApiJson } from './api';
import type { TtsServiceDetailResponse } from './api-models';

export type SpeechCatalogueScope = {
  workspaceId?: string;
  sessionId?: string;
};

export function speechScopeQuery(scope: SpeechCatalogueScope = {}) {
  const query = new URLSearchParams();
  if (scope.workspaceId) query.set('workspace_id', scope.workspaceId);
  if (scope.sessionId) query.set('session_id', scope.sessionId);
  return query;
}

function catalogueQuery(scope: SpeechCatalogueScope, refresh: boolean) {
  if (refresh && scope.sessionId)
    throw new Error('Refresh requires the catalogue workspace.');
  return speechScopeQuery(scope);
}

/** Reading a catalogue is passive; probing a connection is an explicit POST. */
export function fetchTtsCatalogue<T>(
  refresh = false,
  scope: SpeechCatalogueScope = {},
  view?: 'compact' | 'summary'
): Promise<T> {
  const query = catalogueQuery(scope, refresh);
  if (view) query.set('view', view);
  return refresh
    ? typedApiJson<'/api/v1/services/tts/refresh', 'post', T>(
        '/api/v1/services/tts/refresh',
        'post',
        { query }
      )
    : typedApiJson<'/api/v1/services/tts', 'get', T>(
        '/api/v1/services/tts',
        'get',
        { query }
      );
}

type TtsDetailOptions = SpeechCatalogueScope & {
  refresh?: boolean;
  models?: string[] | string;
};

export function fetchTtsServiceDetail(
  serviceId: string,
  options: TtsDetailOptions = {}
): Promise<TtsServiceDetailResponse> {
  const query = catalogueQuery(options, options.refresh ?? false);
  const models =
    options.models == null
      ? []
      : Array.isArray(options.models)
        ? options.models
        : [options.models];
  if (models.length > 1) query.set('models', models.join(','));
  else if (models.length === 1) query.set('model', models[0]);
  return options.refresh
    ? typedApiJson<
        '/api/v1/services/tts/{serviceId}/refresh',
        'post',
        TtsServiceDetailResponse
      >('/api/v1/services/tts/{serviceId}/refresh', 'post', {
        path: { serviceId },
        query
      })
    : typedApiJson<
        '/api/v1/services/tts/{serviceId}',
        'get',
        TtsServiceDetailResponse
      >('/api/v1/services/tts/{serviceId}', 'get', {
        path: { serviceId },
        query
      });
}
