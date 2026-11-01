import { apiJson } from './api';
import type { PreviewableArtifact } from './artifact-display';

type ProjectViewer = {
  user_id: string;
  login_name: string;
  display_name: string;
  is_active: boolean;
  access: 'view';
  granted_at: string;
  granted_by_user_id: string | null;
};
export type ProjectViewers = {
  project_id: string;
  revision: number;
  items: ProjectViewer[];
};
export type ProjectViewingCandidate = Pick<
  ProjectViewer,
  'user_id' | 'login_name' | 'display_name'
>;
export type SharedProject = {
  project_id: string;
  source_session_id: string;
  name: string;
  workflow_kind: string;
  source_language: string | null;
  target_language: string | null;
  updated_at: string;
};
export type SharedProjectFile = PreviewableArtifact & {
  role: string;
  kind: string;
  state: string;
  created_at: string;
};
const root = (id: string) => `/projects/${encodeURIComponent(id)}/viewers`;

export const projectSharingApi = {
  shared: (query = '', offset = 0, signal?: AbortSignal) =>
    apiJson<{ items: SharedProject[] }>(
      `/projects/shared-with-me?${new URLSearchParams({ query, limit: '50', offset: String(offset) })}`,
      { signal }
    ),
  viewers: (id: string, signal?: AbortSignal) =>
    apiJson<ProjectViewers>(root(id), { signal }),
  search: (id: string, query: string, signal?: AbortSignal) =>
    apiJson<{ items: ProjectViewingCandidate[] }>(
      `${root(id)}/users?query=${encodeURIComponent(query)}`,
      { signal }
    ),
  set: (id: string, userId: string, revision: number, revoke = false) =>
    apiJson<ProjectViewers>(
      `${root(id)}/${encodeURIComponent(userId)}${revoke ? '/revoke' : ''}`,
      {
        method: revoke ? 'POST' : 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expected_revision: revision })
      }
    ),
  files: (sessionId: string, offset = 0, signal?: AbortSignal) =>
    apiJson<{ items: SharedProjectFile[] }>(
      `/artifacts?${new URLSearchParams({ view: 'summary', session_id: sessionId, limit: '50', offset: String(offset) })}`,
      { signal }
    )
};
