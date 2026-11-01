import { apiJson, type ApiSchema } from './api';

export type RecoverableWorkspace = ApiSchema<'WorkspaceRecoveryItem'>;
type RecoveryPage = ApiSchema<'WorkspaceRecoveryList'>;
type RecoveryResult = ApiSchema<'WorkspaceManagerRecoveryResponse'>;

export const workspaceRecoveryApi = {
  list: (offset = 0, signal?: AbortSignal) =>
    apiJson<RecoveryPage>(
      `/admin/workspace-recovery?limit=50&offset=${offset}`,
      { signal }
    ),
  promote: (workspace: RecoverableWorkspace, userId: string) =>
    apiJson<RecoveryResult>(
      `/admin/workspace-recovery/${encodeURIComponent(workspace.id)}/manager`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          user_id: userId,
          expected_revision: workspace.revision
        })
      }
    )
};
