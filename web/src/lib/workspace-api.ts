import { apiJson, type ApiMutationAttempt } from './api';

export type InstallationMode = {
  mode: 'solo' | 'collaboration';
  revision: number;
  collaboration_available: boolean;
  collaboration_enabled: boolean;
};
export type WorkspaceRecord = {
  id: string;
  kind: 'personal' | 'team';
  name: string;
  state: 'active' | 'archived';
  revision: number;
  role: 'owner' | 'manager' | 'member';
  allowed_actions: ('read' | 'edit' | 'manage')[];
  created_at: string;
  updated_at: string;
};
export type WorkspaceMember = {
  user_id: string;
  login_name: string;
  display_name: string;
  role: 'owner' | 'manager' | 'member';
  is_active: boolean;
};
export type WorkspaceInvitation = {
  id: string;
  workspace_id: string;
  workspace_name: string;
  user_id: string;
  display_name: string;
  invited_by_user_id: string;
  invited_by_display_name: string;
  role: 'manager' | 'member';
  state: 'pending' | 'accepted' | 'revoked' | 'declined' | 'expired';
  revision: number;
  created_at: string;
  expires_at: string;
  responded_at: string | null;
};
export type WorkspaceUser = Pick<
  WorkspaceMember,
  'user_id' | 'login_name' | 'display_name'
>;
const path = (id: string) => `/workspaces/${encodeURIComponent(id)}`;
function mutate<T>(
  url: string,
  method: string,
  body: unknown,
  attempt?: ApiMutationAttempt
) {
  return apiJson<T>(url, {
    method,
    headers: {
      'Content-Type': 'application/json',
      ...(attempt?.headersFor(url, body) ?? {})
    },
    body: JSON.stringify(body)
  });
}
export const workspaceApi = {
  installation: () => apiJson<InstallationMode>('/admin/installation'),
  enable: (revision: number) =>
    mutate<InstallationMode>('/admin/installation/collaboration', 'POST', {
      expected_revision: revision
    }),
  list: (includeArchived = false) =>
    apiJson<{ items: WorkspaceRecord[] }>(
      `/workspaces${includeArchived ? '?include_archived=true' : ''}`
    ),
  get: (id: string) => apiJson<WorkspaceRecord>(path(id)),
  create: (name: string, attempt: ApiMutationAttempt) =>
    mutate<WorkspaceRecord>('/workspaces', 'POST', { name }, attempt),
  rename: (workspace: WorkspaceRecord, name: string) =>
    mutate<WorkspaceRecord>(path(workspace.id), 'PATCH', {
      name,
      expected_revision: workspace.revision
    }),
  members: (id: string) =>
    apiJson<{ items: WorkspaceMember[] }>(`${path(id)}/members`),
  searchUsers: (id: string, query: string, signal?: AbortSignal) =>
    apiJson<{ items: WorkspaceUser[] }>(
      `${path(id)}/user-search?q=${encodeURIComponent(query)}`,
      { signal }
    ),
  setRole: (
    workspace: WorkspaceRecord,
    userId: string,
    role: 'manager' | 'member'
  ) =>
    mutate<WorkspaceRecord>(
      `${path(workspace.id)}/members/${encodeURIComponent(userId)}`,
      'PATCH',
      { role, expected_revision: workspace.revision }
    ),
  remove: (workspace: WorkspaceRecord, userId: string) =>
    mutate<{ removed: boolean }>(
      `${path(workspace.id)}/members/${encodeURIComponent(userId)}`,
      'DELETE',
      { expected_revision: workspace.revision }
    ),
  lifecycle: (
    workspace: WorkspaceRecord,
    action: 'leave' | 'archive' | 'restore'
  ) =>
    mutate<WorkspaceRecord>(`${path(workspace.id)}/${action}`, 'POST', {
      expected_revision: workspace.revision
    }),
  invitations: (id: string) =>
    apiJson<{ items: WorkspaceInvitation[] }>(`${path(id)}/invitations`),
  invite: (
    workspace: WorkspaceRecord,
    userId: string,
    role: 'manager' | 'member',
    attempt: ApiMutationAttempt
  ) =>
    mutate<WorkspaceInvitation>(
      `${path(workspace.id)}/invitations`,
      'POST',
      { user_id: userId, role, expected_revision: workspace.revision },
      attempt
    ),
  revoke: (workspace: WorkspaceRecord, invitationId: string) =>
    mutate<WorkspaceInvitation>(
      `${path(workspace.id)}/invitations/${encodeURIComponent(invitationId)}`,
      'DELETE',
      { expected_revision: workspace.revision }
    ),
  inbox: () =>
    apiJson<{ items: WorkspaceInvitation[] }>('/workspace-invitations'),
  respond: (id: string, accept: boolean) =>
    mutate<WorkspaceInvitation>(
      `/workspace-invitations/${encodeURIComponent(id)}/${accept ? 'accept' : 'decline'}`,
      'POST',
      {}
    )
};
