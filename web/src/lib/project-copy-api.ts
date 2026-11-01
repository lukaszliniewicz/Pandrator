import { apiJson, type ApiMutationAttempt, type ApiSchema } from './api';

export type ProjectCopyPreview = ApiSchema<'ProjectCopyPreview'>;
export type ProjectCopyConnectionChoice =
  ApiSchema<'ProjectCopyConnectionChoice'>;
export type ProjectCopyStatus = ApiSchema<'ProjectCopyStatus'>;
export type ProjectCopyResult = ApiSchema<'ProjectCopyResultResponse'>;

export const projectCopyApi = {
  preview: (
    projectId: string,
    workspaceId: string,
    destinationConnections: ProjectCopyConnectionChoice[] = [],
    signal?: AbortSignal
  ) =>
    apiJson<ProjectCopyPreview>(
      `/projects/${encodeURIComponent(projectId)}/copy-preview`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          destination_workspace_id: workspaceId,
          ...(destinationConnections.length
            ? { destination_connections: destinationConnections }
            : {})
        }),
        signal
      }
    ),
  start: (
    projectId: string,
    body: ApiSchema<'ProjectCopyRequest'>,
    attempt: ApiMutationAttempt
  ) =>
    apiJson<ProjectCopyStatus>(
      `/projects/${encodeURIComponent(projectId)}/copy`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...attempt.headersFor(`copy-project:${projectId}`, body)
        },
        body: JSON.stringify(body)
      }
    ),
  get: (copyId: string, signal?: AbortSignal) =>
    apiJson<ProjectCopyStatus>(
      `/project-copies/${encodeURIComponent(copyId)}`,
      { signal }
    )
};
