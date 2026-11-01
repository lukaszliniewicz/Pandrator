import { typedApiJson, type ApiMutationAttempt, type ApiSchema } from './api';

export type AccountRole = 'user' | 'administrator';

export type AccountRecord = ApiSchema<'AccountSummaryResponse'>;

export type AccountList = ApiSchema<'AccountListResponse'>;

export type PasswordResetLink = ApiSchema<'AccountResetIssueResponse'>;

export const accountApi = {
  list: () =>
    typedApiJson<'/api/v1/admin/users', 'get', AccountList>(
      '/api/v1/admin/users',
      'get'
    ),
  create: (
    body: ApiSchema<'AccountCreateRequest'>,
    attempt: ApiMutationAttempt
  ) =>
    typedApiJson<'/api/v1/admin/users', 'post', AccountRecord>(
      '/api/v1/admin/users',
      'post',
      { body, headers: attempt.headersFor('accounts:create', body) }
    ),
  update: (
    userId: string,
    body: ApiSchema<'AccountUpdateRequest'>,
    attempt: ApiMutationAttempt
  ) =>
    typedApiJson<'/api/v1/admin/users/{userId}', 'patch', AccountRecord>(
      '/api/v1/admin/users/{userId}',
      'patch',
      {
        path: { userId },
        body,
        headers: attempt.headersFor(`accounts:update:${userId}`, body)
      }
    ),
  issueResetLink: (userId: string, expectedAuthRevision: number) =>
    typedApiJson<
      '/api/v1/admin/users/{userId}/password-reset',
      'post',
      PasswordResetLink
    >('/api/v1/admin/users/{userId}/password-reset', 'post', {
      path: { userId },
      body: { expected_auth_revision: expectedAuthRevision }
    }),
  redeemReset: (token: string, newPassword: string) =>
    typedApiJson<'/api/v1/auth/password-reset', 'post', void>(
      '/api/v1/auth/password-reset',
      'post',
      { body: { token, new_password: newPassword } }
    )
};
