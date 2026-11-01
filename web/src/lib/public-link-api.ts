import { apiJson, type ApiMutationAttempt } from './api';

import type { components } from './api.generated';

export type ProjectLink = components['schemas']['ProjectLink'];
export type PublicSharingSettings =
  components['schemas']['PublicSharingSettingsResponse'];
export type ReviewSubmission = components['schemas']['LinkSubmissionSummary'];
export type SubmissionDetail =
  components['schemas']['LinkSubmissionDetailResponse'];
export type PublicReview =
  components['schemas']['PublicTranslationReviewResponse'];
const json = (body: unknown) => ({
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body)
});
export const publicLinkApi = {
  settings: () => apiJson<PublicSharingSettings>('/admin/public-sharing'),
  configure: (
    body: Pick<PublicSharingSettings, 'enabled' | 'public_origin' | 'revision'>
  ) =>
    apiJson<PublicSharingSettings>('/admin/public-sharing', {
      method: 'PUT',
      ...json({
        enabled: body.enabled,
        public_origin: body.public_origin,
        expected_revision: body.revision
      })
    }),
  list: (projectId: string) =>
    apiJson<{ items: ProjectLink[] }>(
      `/projects/${encodeURIComponent(projectId)}/links`
    ),
  create: (
    projectId: string,
    body: {
      label: string;
      mode: ProjectLink['mode'];
      expires_in_days: number;
      target_artifact_id?: string;
      request_key: string;
    }
  ) =>
    apiJson<{
      link: ProjectLink;
      url: string | null;
      already_created: boolean;
    }>(`/projects/${encodeURIComponent(projectId)}/links`, {
      method: 'POST',
      ...json(body)
    }),
  revoke: (link: ProjectLink) =>
    apiJson(`/project-links/${link.id}/revoke`, {
      method: 'POST',
      ...json({ expected_revision: link.revision })
    }),
  redeem: (id: string, token: string) =>
    apiJson<{ link: ProjectLink; csrf_token: string }>(
      `/public-links/${encodeURIComponent(id)}/redeem`,
      { method: 'POST', ...json({ token }) }
    ),
  leave: (id: string) =>
    apiJson(`/public-links/${encodeURIComponent(id)}/logout`, {
      method: 'POST'
    }),
  preview: (id: string, offset = 0) =>
    apiJson<PublicReview>(
      `/public-links/${encodeURIComponent(id)}/review?offset=${offset}&limit=50`
    ),
  submit: (
    id: string,
    body: {
      expected_source_hash: string;
      changes: { segment_id: string; text: string }[];
      reviewer_label: string | null;
    },
    attempt: ApiMutationAttempt
  ) =>
    apiJson<{ submission_id: string; status: string; revision: number }>(
      `/public-links/${encodeURIComponent(id)}/submissions`,
      {
        method: 'POST',
        ...json(body),
        headers: {
          'Content-Type': 'application/json',
          ...attempt.headersFor(id, body)
        }
      }
    ),
  submissions: (projectId: string, status = 'pending', offset = 0) =>
    apiJson<{ items: ReviewSubmission[] }>(
      `/projects/${encodeURIComponent(projectId)}/link-submissions?status=${status}&offset=${offset}&limit=25`
    ),
  submission: (id: string, offset = 0) =>
    apiJson<SubmissionDetail>(
      `/link-submissions/${encodeURIComponent(id)}?offset=${offset}&limit=50`
    ),
  decide: (
    detail: SubmissionDetail,
    action: 'accept' | 'reject',
    attempt: ApiMutationAttempt
  ) => {
    const id = detail.submission.submission_id;
    const body = {
      expected_revision: detail.submission.revision,
      ...(action === 'accept'
        ? {
            expected_source_hash: detail.target_hash,
            expected_document_revision: detail.document_revision,
            expected_selected_artifact_id: detail.selected_artifact_id
          }
        : {})
    };
    return apiJson(`/link-submissions/${id}/${action}`, {
      method: 'POST',
      ...json(body),
      headers: {
        'Content-Type': 'application/json',
        ...attempt.headersFor(`${id}/${action}`, body)
      }
    });
  }
};
