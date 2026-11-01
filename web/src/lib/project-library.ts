import type { JobRecord, SessionRecord } from './api-models';
import { languageLabel } from './language-registry';
import { isStorageWait } from './job-status';

export type ProjectFolderRecord = {
  id: string;
  workspace_id?: string;
  name: string;
  parent_id: string | null;
  revision: number;
  created_at: string;
  updated_at: string;
};

export type ProjectFolderMembership = {
  folder_id: string;
  /** Legacy source-session alias; use source_session_id for navigation. */
  project_id: string;
  source_session_id?: string;
  project_root_id?: string;
  position: number;
};

export type ProjectFolderSnapshot = {
  workspace_id?: string;
  items: ProjectFolderRecord[];
  memberships: ProjectFolderMembership[];
};

export type LibraryProject = {
  /** Source-session identity retained for existing URLs and folder writes. */
  id: string;
  projectId: string | null;
  name: string;
  sourceId: string;
  source: SessionRecord | undefined;
  membership: SessionRecord['translation_project'];
  versions: SessionRecord[];
};

function canonicalSourceSessionId(item: SessionRecord) {
  return item.translation_project?.source_session_id || item.id;
}

export function folderMembershipSourceId(member: ProjectFolderMembership) {
  return member.source_session_id ?? member.project_id;
}

export function libraryProjects(items: SessionRecord[]): LibraryProject[] {
  const grouped = new Map<string, SessionRecord[]>();
  for (const item of items) {
    const id = item.project_id || canonicalSourceSessionId(item);
    const versions = grouped.get(id) ?? [];
    versions.push(item);
    grouped.set(id, versions);
  }
  return [...grouped.values()].map((versions) => {
    const membership = versions.find(
      (item) => item.translation_project
    )?.translation_project;
    const sourceId = membership?.source_session_id || versions[0].id;
    const source = versions.find((item) => item.id === sourceId);
    return {
      id: sourceId,
      projectId: versions.find((item) => item.project_id)?.project_id ?? null,
      sourceId,
      source,
      membership,
      name: membership?.name || source?.name || versions[0].name,
      versions: [...versions].sort(
        (left, right) =>
          Number(right.id === sourceId) - Number(left.id === sourceId) ||
          versionLanguageLabel(left).localeCompare(versionLanguageLabel(right))
      )
    };
  });
}

export function versionLanguageLabel(item: SessionRecord) {
  return languageLabel(
    item.target_language ||
      item.translation_project?.target_language ||
      item.source_language
  );
}

function activeProjectJob(item: SessionRecord, jobs: JobRecord[]) {
  return jobs.find(
    (job) =>
      job.session_id === item.id &&
      ['running', 'queued', 'retrying', 'waiting_for_resource'].includes(
        job.status
      )
  );
}

export function projectActivityStatus(item: SessionRecord, jobs: JobRecord[]) {
  return activeProjectJob(item, jobs)?.status ?? item.status;
}

export function projectActivity(item: SessionRecord, jobs: JobRecord[]) {
  const active = activeProjectJob(item, jobs);
  if (active && isStorageWait(active)) return 'Waiting for storage';
  if (active)
    return active.status === 'running'
      ? active.progress_detail ||
          `${active.kind.replaceAll(/[._]/g, ' ')} in progress`
      : active.status.replaceAll('_', ' ');
  return item.status.replaceAll('_', ' ');
}

export function folderPath(
  folder: ProjectFolderRecord,
  folders: ProjectFolderRecord[]
) {
  const parts = [folder.name];
  const seen = new Set([folder.id]);
  let parent = folders.find((item) => item.id === folder.parent_id);
  while (parent && !seen.has(parent.id)) {
    parts.unshift(parent.name);
    seen.add(parent.id);
    parent = folders.find((item) => item.id === parent?.parent_id);
  }
  return parts.join(' / ');
}

export function folderDescendants(id: string, folders: ProjectFolderRecord[]) {
  const ids = new Set([id]);
  let pending = [id];
  while (pending.length) {
    const next = folders.filter(
      (folder) =>
        folder.parent_id &&
        pending.includes(folder.parent_id) &&
        !ids.has(folder.id)
    );
    for (const folder of next) ids.add(folder.id);
    pending = next.map((folder) => folder.id);
  }
  return ids;
}

export function projectHref(id: string, folder = '', suffix = '') {
  return `/sessions/${encodeURIComponent(id)}${suffix}${folder ? `?folder=${encodeURIComponent(folder)}` : ''}`;
}
