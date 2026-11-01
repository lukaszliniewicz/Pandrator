import { apiJson } from './api';
import { ResourceState } from './resource-state.svelte';
import { folderMembershipSourceId } from './project-library';
import type {
  ProjectFolderRecord,
  ProjectFolderSnapshot
} from './project-library';

export class ProjectFoldersStore {
  private readonly resource = new ResourceState<ProjectFolderSnapshot>({
    items: [],
    memberships: []
  });
  private readonly retryKeys = new Map<string, string>();

  constructor(readonly workspaceId: string | null = null) {}

  private path(path: string) {
    return this.workspaceId
      ? `${path}?workspace_id=${encodeURIComponent(this.workspaceId)}`
      : path;
  }

  private scoped<T extends ProjectFolderSnapshot>(snapshot: T): T {
    if (this.workspaceId && snapshot.workspace_id !== this.workspaceId)
      throw new Error('The folder response belongs to a different workspace.');
    return snapshot;
  }

  get value() {
    return this.resource.value;
  }
  get loading() {
    return this.resource.loading;
  }
  get error() {
    return this.resource.error;
  }

  load(force = false) {
    return this.resource.load(
      async () =>
        this.scoped(
          await apiJson<ProjectFolderSnapshot>(this.path('/project-folders'))
        ),
      { force }
    );
  }

  private async mutate<T extends ProjectFolderSnapshot>(
    path: string,
    method: string,
    body: unknown
  ) {
    const signature = JSON.stringify([path, method, body]);
    let key = this.retryKeys.get(signature);
    if (!key) {
      key = crypto.randomUUID();
      this.retryKeys.set(signature, key);
    }
    try {
      const result = this.scoped(
        await apiJson<T>(this.path(path), {
          method,
          headers: {
            'Content-Type': 'application/json',
            'Idempotency-Key': key
          },
          body: JSON.stringify(body)
        })
      );
      const snapshot = {
        workspace_id: result.workspace_id,
        items: result.items,
        memberships: result.memberships
      };
      this.resource.reset(snapshot);
      this.resource.replace(snapshot);
      this.retryKeys.delete(signature);
      return result;
    } catch (error) {
      await this.load(true).catch(() => undefined);
      throw error;
    }
  }

  create(name: string, parentId: string | null) {
    return this.mutate<ProjectFolderSnapshot & { folder: ProjectFolderRecord }>(
      '/project-folders',
      'POST',
      { name, parent_id: parentId }
    );
  }

  update(folder: ProjectFolderRecord, name: string, parentId: string | null) {
    return this.mutate(
      `/project-folders/${encodeURIComponent(folder.id)}`,
      'PATCH',
      { expected_revision: folder.revision, name, parent_id: parentId }
    );
  }

  remove(folder: ProjectFolderRecord) {
    return this.mutate(
      `/project-folders/${encodeURIComponent(folder.id)}`,
      'DELETE',
      { expected_revision: folder.revision }
    );
  }

  reorder(folder: ProjectFolderRecord, ids: string[]) {
    return this.mutate(
      `/project-folders/${encodeURIComponent(folder.id)}/projects`,
      'PUT',
      { expected_revision: folder.revision, project_ids: ids }
    );
  }

  assign(projectId: string, folderIds: string[], snapshot = this.value) {
    if (
      snapshot.workspace_id &&
      this.value.workspace_id &&
      snapshot.workspace_id !== this.value.workspace_id
    )
      throw new Error('Reload this project in its current workspace.');
    const current = snapshot.memberships
      .filter((item) => folderMembershipSourceId(item) === projectId)
      .map((item) => item.folder_id);
    const union = new Set([...current, ...folderIds]);
    const revisions = Object.fromEntries(
      snapshot.items
        .filter((folder) => union.has(folder.id))
        .map((folder) => [folder.id, folder.revision])
    );
    return this.mutate(
      `/sessions/${encodeURIComponent(projectId)}/project-folders`,
      'PUT',
      { folder_ids: folderIds, expected_revisions: revisions }
    );
  }
}
