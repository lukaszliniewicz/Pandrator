import { expect, test, type Page } from '@playwright/test';
import {
  SOURCE_PASSAGE_DEFAULTS,
  coerceSourcePassageValues,
  sourcePassagePayload,
  validateSourcePassageValues
} from '../src/lib/source-passages';

test('source-passage defaults match the backend contract baseline', () => {
  expect(SOURCE_PASSAGE_DEFAULTS).toEqual({
    min_chars: 60,
    preferred_chars: 160,
    sentence_lookahead_chars: 20,
    cue_join_gap_ms: 650,
    diagnostic_span_ms: 8000
  });
});

test('source-passage coercion fills gaps and ignores booleans', () => {
  expect(coerceSourcePassageValues(null)).toEqual(SOURCE_PASSAGE_DEFAULTS);
  expect(
    coerceSourcePassageValues({ min_chars: true, preferred_chars: '200' })
  ).toEqual({ ...SOURCE_PASSAGE_DEFAULTS, preferred_chars: 200 });
  expect(coerceSourcePassageValues({ cue_join_gap_ms: 12.5 })).toEqual(
    SOURCE_PASSAGE_DEFAULTS
  );
});

test('source-passage validation enforces ranges and min <= preferred', () => {
  expect(validateSourcePassageValues({ ...SOURCE_PASSAGE_DEFAULTS })).toEqual(
    {}
  );
  const errors = validateSourcePassageValues({
    ...SOURCE_PASSAGE_DEFAULTS,
    min_chars: 200,
    preferred_chars: 100,
    sentence_lookahead_chars: -1,
    cue_join_gap_ms: 10001,
    diagnostic_span_ms: 500
  });
  expect(Object.keys(errors).sort()).toEqual([
    'cue_join_gap_ms',
    'diagnostic_span_ms',
    'preferred_chars',
    'sentence_lookahead_chars'
  ]);
  expect(
    validateSourcePassageValues({ ...SOURCE_PASSAGE_DEFAULTS, min_chars: true })
      .min_chars
  ).toBeTruthy();
});

test('source-passage payload never carries unknown keys', () => {
  expect(
    sourcePassagePayload({ ...SOURCE_PASSAGE_DEFAULTS, injected: 'x' })
  ).toEqual(SOURCE_PASSAGE_DEFAULTS);
});

const PASSAGE_DEFAULTS_BODY = {
  section: 'source_passages',
  builtin: { ...SOURCE_PASSAGE_DEFAULTS },
  global: {},
  override: {},
  session_context: {},
  effective: { ...SOURCE_PASSAGE_DEFAULTS },
  revision: 7,
  global_revision: 1
};

async function signIn(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const closeTour = page.getByRole('button', { name: 'Close tour' });
  if (await closeTour.isVisible()) await closeTour.click();
}

async function createIsolatedSession(page: Page) {
  const authStatus = await page.request.get('/api/v1/auth/status');
  expect(authStatus.ok()).toBeTruthy();
  const csrfToken = (await authStatus.json()).csrf_token;
  const created = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': csrfToken },
    data: {
      name: `Source passages ${crypto.randomUUID()}`,
      workflow_kind: 'subtitles'
    }
  });
  expect(created.ok()).toBeTruthy();
  return (await created.json()).id as string;
}

async function openTranscribeSettings(
  page: Page,
  sessionId: string,
  stageTitle = 'Transcribe'
) {
  await page.goto(`/sessions/${sessionId}`);
  const card = page
    .getByRole('heading', { name: stageTitle, exact: true })
    .locator('xpath=ancestor::article');
  await card.getByRole('button', { name: 'Settings' }).click();
  const dialog = page.getByRole('dialog');
  await expect(
    dialog.getByRole('group', { name: 'Source logical passages' })
  ).toBeVisible();
  return dialog;
}

/** Point the transcribe stage at an isolated fixture artifact id. */
async function selectFixtureArtifact(page: Page, sessionId: string) {
  const response = await page.request.get(
    `/api/v1/sessions/${sessionId}/workflow`
  );
  const body = await response.json();
  const transcribe = body.stages.find(
    (stage: { key: string }) => stage.key === 'transcribe'
  );
  if (transcribe) transcribe.selected_artifact_id = 'artifact-src-1';
  await page.route(`**/api/v1/sessions/${sessionId}/workflow`, (route) =>
    route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify(body)
    })
  );
}

test('transcribe dialog shows passage defaults and keeps an explicit 48-char subtitle override', async ({
  page
}) => {
  await signIn(page);
  const sessionId = await createIsolatedSession(page);
  await page.route(
    `**/api/v1/sessions/${sessionId}/settings/source_passages`,
    (route) =>
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify(PASSAGE_DEFAULTS_BODY)
      })
  );
  await page.route(
    `**/api/v1/sessions/${sessionId}/settings/stt`,
    async (route) => {
      if (route.request().method() !== 'GET') {
        await route.continue();
        return;
      }
      const response = await route.fetch();
      const body = await response.json();
      const effective = { ...(body.effective ?? {}) };
      delete effective.subtitle_max_chars_per_line;
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ ...body, effective })
      });
    }
  );
  const dialog = await openTranscribeSettings(page, sessionId);
  await expect(
    dialog.getByLabel('Minimum substantial-clause size', { exact: true })
  ).toHaveValue('60');
  await expect(
    dialog.getByLabel('Preferred passage size (soft)', { exact: true })
  ).toHaveValue('160');
  await expect(
    dialog.getByLabel('Sentence fit window (soft)', { exact: true })
  ).toHaveValue('20');
  await expect(
    dialog.getByLabel('Cue join gap (guarded joining)', { exact: true })
  ).toHaveValue('650');
  await expect(
    dialog.getByLabel('Diagnostic span (not a cap)', { exact: true })
  ).toHaveValue('8000');
  // Missing backend key falls back to 60, never the old 48.
  await expect(
    dialog.getByRole('spinbutton', { name: 'Characters / line' })
  ).toHaveValue('60');
});

test('invalid passage values block saving with inline errors', async ({
  page
}) => {
  await signIn(page);
  const sessionId = await createIsolatedSession(page);
  await page.route(
    `**/api/v1/sessions/${sessionId}/settings/source_passages`,
    (route) =>
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify(PASSAGE_DEFAULTS_BODY)
      })
  );
  const dialog = await openTranscribeSettings(page, sessionId);
  await dialog
    .getByLabel('Preferred passage size (soft)', { exact: true })
    .fill('40');
  await expect(
    dialog.getByText('Preferred size must be at least the minimum clause size.')
  ).toBeVisible();
  await expect(
    dialog.getByRole('button', { name: 'Save settings' })
  ).toBeDisabled();
  await dialog
    .getByLabel('Preferred passage size (soft)', { exact: true })
    .fill('200');
  await expect(
    dialog.getByRole('button', { name: 'Save settings' })
  ).toBeEnabled();
});

test('passage save sends exactly five keys and surfaces revision conflicts', async ({
  page
}) => {
  await signIn(page);
  const sessionId = await createIsolatedSession(page);
  let putBody: Record<string, unknown> | null = null;
  await page.route(
    `**/api/v1/sessions/${sessionId}/settings/source_passages`,
    async (route) => {
      if (route.request().method() === 'GET') {
        await route.fulfill({
          contentType: 'application/json',
          body: JSON.stringify(PASSAGE_DEFAULTS_BODY)
        });
        return;
      }
      putBody = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({
          error: {
            code: 'revision_conflict',
            message: 'Settings changed (revision 8). Reload and retry.'
          }
        })
      });
    }
  );
  const dialog = await openTranscribeSettings(page, sessionId);
  await dialog
    .getByLabel('Minimum substantial-clause size', { exact: true })
    .fill('70');
  await dialog.getByRole('button', { name: 'Save settings' }).click();
  await expect(
    dialog.getByText('Settings changed (revision 8). Reload and retry.')
  ).toBeVisible();
  await expect(dialog).toBeVisible();
  expect(putBody).not.toBeNull();
  const value = (putBody as unknown as { value: Record<string, unknown> })
    .value;
  expect(Object.keys(value).sort()).toEqual([
    'cue_join_gap_ms',
    'diagnostic_span_ms',
    'min_chars',
    'preferred_chars',
    'sentence_lookahead_chars'
  ]);
  expect(value.min_chars).toBe(70);
});

const STATUS_PINNED = {
  artifact_id: 'artifact-src-1',
  revision_id: 'rev-src-1',
  content_hash: 'sha256:abc',
  pinned: true,
  policy_version: 'source_provisional_v1',
  source_passage_settings: { ...SOURCE_PASSAGE_DEFAULTS },
  source_passage_settings_hash: 'hash-pinned',
  source_passage_settings_revision: 7,
  passage_count: 12,
  display_content_hash: 'sha256:display'
};

const PREVIEW_RESPONSE = {
  artifact_id: 'artifact-src-1',
  revision_id: 'rev-src-1',
  content_hash: 'sha256:abc',
  settings_revision: 7,
  passage_count: 14,
  items: [],
  policy_version: 'source_provisional_v1',
  effective_settings: { ...SOURCE_PASSAGE_DEFAULTS },
  settings_hash: 'hash-preview-1',
  pinned: true,
  truncated: false,
  warnings: ['Existing passages remain unchanged.']
};

test('preview shows a numeric delta only; rebuild carries all guards and preserves selection', async ({
  page
}, info) => {
  await signIn(page);
  const sessionId = await createIsolatedSession(page);
  await selectFixtureArtifact(page, sessionId);
  await page.route(
    `**/api/v1/sessions/${sessionId}/settings/source_passages`,
    (route) =>
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify(PASSAGE_DEFAULTS_BODY)
      })
  );
  await page.route(
    `**/api/v1/sessions/${sessionId}/sources/*/passages`,
    (route) =>
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify(STATUS_PINNED)
      })
  );
  await page.route(
    `**/api/v1/sessions/${sessionId}/sources/*/passages/preview`,
    (route) =>
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify(PREVIEW_RESPONSE)
      })
  );
  let rebuildBody: Record<string, unknown> | null = null;
  let selectionWrites = 0;
  await page.route(
    `**/api/v1/sessions/${sessionId}/sources/*/passages/rebuild`,
    async (route) => {
      rebuildBody = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({
          branch_artifact_id: 'branch-art-1',
          branch_revision_id: 'branch-rev-1',
          branch_document_id: 'branch-doc-1',
          passage_count: 14,
          policy_version: 'source_provisional_v1',
          effective_settings: SOURCE_PASSAGE_DEFAULTS,
          settings_hash: 'hash-preview-1',
          preserved: {
            original_artifact_id: 'artifact-src-1',
            downstream_untouched: true,
            selection_unchanged: true
          }
        })
      });
    }
  );
  await page.route(
    `**/api/v1/sessions/${sessionId}/stages/*/selection`,
    (route) => {
      selectionWrites += 1;
      return route.continue();
    }
  );

  const dialog = await openTranscribeSettings(page, sessionId);
  await dialog.locator('summary', { hasText: 'Preview & rebuild' }).click();
  await dialog.getByRole('button', { name: 'Reload status' }).click();
  await expect(dialog.getByText(/Pinned passages: 12/)).toBeVisible();
  await dialog.getByRole('button', { name: 'Preview passages' }).click();
  await expect(
    dialog.getByText('Preview: 14 passages · pinned: 12 · difference +2')
  ).toBeVisible();
  await expect(
    dialog.getByText('Existing passages remain unchanged.')
  ).toBeVisible();
  await page.screenshot({
    path: `../tmp/source-passage-ui-20260915/preview-${info.project.name}.png`
  });
  await dialog.getByRole('button', { name: 'Rebuild as new branch' }).click();
  await expect(
    dialog.getByText('New branch: 14 passages · branch-art-1')
  ).toBeVisible();
  await expect(
    dialog.getByText('Find the new branch in the existing source picker.')
  ).toBeVisible();
  expect(rebuildBody).toMatchObject({
    expected_source_revision_id: 'rev-src-1',
    expected_source_content_hash: 'sha256:abc',
    expected_settings_revision: 7,
    expected_settings_hash: 'hash-preview-1'
  });
  expect(
    (rebuildBody as unknown as { source_passages: Record<string, unknown> })
      .source_passages
  ).toEqual(SOURCE_PASSAGE_DEFAULTS);
  expect(selectionWrites).toBe(0);
});

test('a stale rebuild refreshes state and asks for a fresh preview instead of retrying', async ({
  page
}) => {
  await signIn(page);
  const sessionId = await createIsolatedSession(page);
  await selectFixtureArtifact(page, sessionId);
  await page.route(
    `**/api/v1/sessions/${sessionId}/settings/source_passages`,
    (route) =>
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify(PASSAGE_DEFAULTS_BODY)
      })
  );
  await page.route(
    `**/api/v1/sessions/${sessionId}/sources/*/passages`,
    (route) =>
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify(STATUS_PINNED)
      })
  );
  await page.route(
    `**/api/v1/sessions/${sessionId}/sources/*/passages/preview`,
    (route) =>
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify(PREVIEW_RESPONSE)
      })
  );
  let rebuildCalls = 0;
  await page.route(
    `**/api/v1/sessions/${sessionId}/sources/*/passages/rebuild`,
    (route) => {
      rebuildCalls += 1;
      return route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({
          error: {
            code: 'source_changed',
            message: 'Source changed (rev-src-2).'
          }
        })
      });
    }
  );
  const dialog = await openTranscribeSettings(page, sessionId);
  await dialog.locator('summary', { hasText: 'Preview & rebuild' }).click();
  await dialog.getByRole('button', { name: 'Reload status' }).click();
  await dialog.getByRole('button', { name: 'Preview passages' }).click();
  await expect(
    dialog.getByText('Preview: 14 passages · pinned: 12 · difference +2')
  ).toBeVisible();
  await dialog.getByRole('button', { name: 'Rebuild as new branch' }).click();
  await expect(dialog.getByText('Source changed (rev-src-2).')).toBeVisible();
  await expect(
    dialog.getByText(/run a fresh preview, then rebuild again/i)
  ).toBeVisible();
  expect(rebuildCalls).toBe(1);
  // The stale preview is consumed: rebuilding again is impossible without previewing.
  await expect(
    dialog.getByRole('button', { name: 'Rebuild as new branch' })
  ).toBeDisabled();
});

test('real API preview and rebuild preserve the selected source', async ({
  page
}, info) => {
  await signIn(page);
  const sessionId = await createIsolatedSession(page);
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const headers = { 'X-CSRF-Token': auth.csrf_token };
  const uploaded = await page.request.post('/api/v1/uploads', {
    headers,
    multipart: {
      session_id: sessionId,
      file: {
        name: 'passage-integration.srt',
        mimeType: 'application/x-subrip',
        buffer: Buffer.from(
          '1\n00:00:00,000 --> 00:00:03,000\nHello world. Another complete thought.\n\n2\n00:00:03,200 --> 00:00:06,000\nThe original words must stay unchanged.\n'
        )
      }
    }
  });
  expect(uploaded.ok(), await uploaded.text()).toBeTruthy();
  const upload = await uploaded.json();
  const adopted = await page.request.post(
    `/api/v1/sessions/${sessionId}/sources/adopt-subtitles`,
    {
      headers,
      data: { source_asset_id: upload.source_asset_id, role: 'primary' }
    }
  );
  expect(adopted.ok(), await adopted.text()).toBeTruthy();
  const workflowBefore = await (
    await page.request.get(`/api/v1/sessions/${sessionId}/workflow`)
  ).json();
  const originalId = workflowBefore.subtitle_source.subtitle_artifact_id;
  expect(originalId).toBeTruthy();
  const dialog = await openTranscribeSettings(page, sessionId, 'Correct');
  await dialog
    .getByRole('spinbutton', { name: 'Preferred passage size (soft)' })
    .fill('200');
  await dialog.locator('summary', { hasText: 'Preview & rebuild' }).click();
  const previewResponsePromise = page.waitForResponse((response) =>
    response.url().endsWith('/passages/preview')
  );
  await dialog.getByRole('button', { name: 'Preview passages' }).click();
  const previewResponse = await previewResponsePromise;
  expect(previewResponse.ok(), await previewResponse.text()).toBeTruthy();
  const preview = await previewResponse.json();
  expect(previewResponse.request().headers()['content-type']).toContain(
    'application/json'
  );
  expect(preview.effective_settings.preferred_chars).toBe(200);
  expect(preview.passage_count).toBeGreaterThan(0);
  await expect(
    dialog.getByText(new RegExp(`Preview: ${preview.passage_count} passages`))
  ).toBeVisible();
  await expect(
    dialog.getByRole('button', { name: 'Rebuild as new branch' })
  ).toBeEnabled();
  await page.screenshot({
    path: `../tmp/source-passage-ui-20260915/real-preview-${info.project.name}.png`
  });
  const branchResponsePromise = page.waitForResponse((response) =>
    response.url().endsWith('/passages/rebuild')
  );
  await dialog.getByRole('button', { name: 'Rebuild as new branch' }).click();
  const branchResponse = await branchResponsePromise;
  expect(branchResponse.status(), await branchResponse.text()).toBe(201);
  const branch = await branchResponse.json();
  expect(branchResponse.request().headers()['content-type']).toContain(
    'application/json'
  );
  expect(branch.effective_settings.preferred_chars).toBe(200);
  await expect(
    dialog.getByText(
      `New branch: ${branch.passage_count} passages · ${branch.branch_artifact_id}`
    )
  ).toBeVisible();
  expect(branch.branch_artifact_id).not.toBe(originalId);
  const workflowAfter = await (
    await page.request.get(`/api/v1/sessions/${sessionId}/workflow`)
  ).json();
  expect(workflowAfter.subtitle_source.subtitle_artifact_id).toBe(originalId);
  const branchStatus = await page.request.get(
    `/api/v1/sessions/${sessionId}/sources/${branch.branch_artifact_id}/passages`
  );
  expect(branchStatus.ok()).toBeTruthy();
  expect((await branchStatus.json()).pinned).toBe(true);
});

test('real API source-passage overrides stay sparse and reset to defaults', async ({
  page
}) => {
  await signIn(page);
  const sessionId = await createIsolatedSession(page);
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const endpoint = `/api/v1/sessions/${sessionId}/settings/source_passages`;
  let state = await (await page.request.get(endpoint)).json();
  for (const value of [{ min_chars: 80 }, { cue_join_gap_ms: 0 }, {}]) {
    const response = await page.request.put(endpoint, {
      headers: {
        'X-CSRF-Token': auth.csrf_token,
        'If-Match': `"${state.revision}"`
      },
      data: { value }
    });
    expect(response.ok(), await response.text()).toBeTruthy();
    state = await response.json();
    expect(state.override).toEqual(value);
    expect(state.effective).toEqual({ ...SOURCE_PASSAGE_DEFAULTS, ...value });
  }
});
