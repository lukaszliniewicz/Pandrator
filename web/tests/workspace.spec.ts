import { Buffer } from 'node:buffer';
import { expect, test, type Page } from '@playwright/test';

function uniqueName(prefix: string) {
  return `${prefix} ${crypto.randomUUID()}`;
}

async function signIn(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const closeTour = page.getByRole('button', { name: 'Close tour' });
  if (await closeTour.isVisible()) await closeTour.click();
}

async function createGenerationPlan(
  page: Page,
  segments: Array<{
    text: string;
    paragraph_break_after?: boolean;
    node_kind?: string;
  }>
) {
  const authStatus = await page.request.get('/api/v1/auth/status');
  expect(authStatus.ok()).toBeTruthy();
  const csrfToken = (await authStatus.json()).csrf_token;
  expect(typeof csrfToken).toBe('string');
  const headers = { 'X-CSRF-Token': csrfToken };
  const sessionResponse = await page.request.post('/api/v1/sessions', {
    headers,
    data: {
      name: uniqueName('Reading mode regression'),
      workflow_kind: 'audiobook'
    }
  });
  expect(sessionResponse.ok()).toBeTruthy();
  const session = await sessionResponse.json();
  const planResponse = await page.request.post(
    `/api/v1/sessions/${session.id}/generation-plan`,
    {
      headers,
      data: { segments }
    }
  );
  expect(planResponse.ok()).toBeTruthy();
  return session.id as string;
}

test('wizard creates a guided subtitle workspace and preserves setup return', async ({
  page
}) => {
  const sessionName = uniqueName('Playwright subtitles');
  await signIn(page);
  await expect(
    page.getByRole('heading', { name: 'What shall we make?' })
  ).toBeVisible();
  await page
    .getByRole('button', { name: /Create subtitles/ })
    .first()
    .click();
  await page.getByRole('button', { name: 'Add later' }).click();
  await page.getByRole('button', { name: 'Continue' }).click();
  await page.getByRole('button', { name: 'Review', exact: true }).click();
  await page.getByLabel('Session name').fill(sessionName);
  await page.getByRole('button', { name: 'Create workspace' }).click();
  await expect(page.getByRole('heading', { name: sessionName })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Transcribe' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Tour' })).toBeVisible();
});

test('correction and translation cards expose independent reasoning levels', async ({
  page
}) => {
  await signIn(page);
  const authStatus = await page.request.get('/api/v1/auth/status');
  const csrfToken = (await authStatus.json()).csrf_token;
  const created = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': csrfToken },
    data: {
      name: uniqueName('Task reasoning controls'),
      workflow_kind: 'voiceover',
      included_stages: ['correct', 'translate']
    }
  });
  expect(created.ok()).toBeTruthy();
  const session = await created.json();

  const translationSource = {
    artifact_id: 'correction-v1',
    role: 'correction',
    stage: 'correction',
    version: 1,
    document_id: 'document-correction-v1',
    revision_id: 'revision-correction-v1',
    revision: 1,
    reviewed: false,
    language: 'de',
    segment_count: 1,
    state: 'current',
    created_at: '2026-01-01T12:00:00Z'
  };
  await page.route(
    `**/api/v1/sessions/${session.id}/subtitles/catalog`,
    async (route) => {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          session_id: session.id,
          items: [translationSource]
        })
      });
    }
  );

  await page.goto(`/sessions/${session.id}`);
  const correctionCard = page
    .getByRole('heading', { name: 'Correct', exact: true })
    .locator('xpath=ancestor::article');
  await correctionCard.getByRole('button', { name: 'Settings' }).click();
  let dialog = page.getByRole('dialog');
  await expect(dialog.getByLabel('Reasoning level')).toHaveValue('');
  await dialog.getByLabel('Reasoning level').selectOption('high');
  await dialog
    .getByLabel('Correction guidance')
    .fill('Keep the acronym IARF unchanged.');
  await dialog.getByRole('button', { name: 'All correction settings' }).click();
  dialog = page.getByRole('dialog');
  await expect(dialog.getByLabel('Reasoning level')).toHaveValue('high');
  await expect(dialog.getByLabel('Instructions')).toHaveValue(
    'Keep the acronym IARF unchanged.'
  );

  const unsavedCorrectionSettings = await page.request.get(
    `/api/v1/sessions/${session.id}/settings/correction`
  );
  expect(unsavedCorrectionSettings.ok()).toBeTruthy();
  expect(
    (await unsavedCorrectionSettings.json()).override.reasoning_effort
  ).toBeUndefined();

  await dialog.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(dialog.getByText('Saved for this session.')).toBeVisible();
  await dialog.getByRole('button', { name: 'Close settings' }).click();
  dialog = page.getByRole('dialog');
  await expect(dialog.getByLabel('Reasoning level')).toHaveValue('high');
  await expect(dialog.getByLabel('Correction guidance')).toHaveValue(
    'Keep the acronym IARF unchanged.'
  );
  await dialog.getByRole('button', { name: 'Cancel' }).click();
  await expect(dialog).toHaveCount(0);

  const correctionSettings = await page.request.get(
    `/api/v1/sessions/${session.id}/settings/correction`
  );
  expect(correctionSettings.ok()).toBeTruthy();
  const correctionSettingsBody = await correctionSettings.json();
  expect(correctionSettingsBody.override.reasoning_effort).toBe('high');
  expect(correctionSettingsBody.override.instructions).toBe(
    'Keep the acronym IARF unchanged.'
  );

  const translationCard = page
    .getByRole('heading', { name: 'Translate', exact: true })
    .locator('xpath=ancestor::article');
  await translationCard.getByRole('button', { name: 'Settings' }).click();
  dialog = page.getByRole('dialog');
  await expect(dialog.getByLabel('Reasoning level')).toHaveValue('');
  await dialog.getByLabel('Reasoning level').selectOption('low');
  await dialog
    .getByLabel('Translate from')
    .selectOption(translationSource.artifact_id);
  await dialog.getByRole('button', { name: 'Save settings' }).click();
  await expect(dialog).toHaveCount(0);

  const translationSettings = await page.request.get(
    `/api/v1/sessions/${session.id}/settings/translation`
  );
  expect(translationSettings.ok()).toBeTruthy();
  expect((await translationSettings.json()).override.reasoning_effort).toBe(
    'low'
  );

  await translationCard.getByRole('button', { name: 'Settings' }).click();
  dialog = page.getByRole('dialog');
  await expect(dialog.getByLabel('Reasoning level')).toHaveValue('low');
  await dialog.getByLabel('Translation backend').selectOption('deepl');
  await expect(dialog.getByLabel('Reasoning level')).toHaveCount(0);
});

test('workflow history and subtitle review load exact revisions on demand', async ({
  page
}) => {
  await signIn(page);
  const authStatus = await page.request.get('/api/v1/auth/status');
  const csrfToken = (await authStatus.json()).csrf_token;
  const created = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': csrfToken },
    data: {
      name: uniqueName('Paginated workflow history'),
      workflow_kind: 'voiceover'
    }
  });
  expect(created.ok()).toBeTruthy();
  const session = await created.json();
  const canonicalModel =
    'custom:aff14ed0-c04f-4241-8034-61b6236a190a/google/gemini-3.6-flash';
  const stageArtifact = (version: number) => ({
    id: `artifact-${version}`,
    version,
    kind: 'srt',
    role: 'transcription',
    relative_path: `sessions/history/transcript-${version}.srt`,
    mime_type: 'application/x-subrip',
    size_bytes: version,
    state: version === 15 ? 'current' : 'stale',
    settings_hash: null,
    metadata_json: { model: canonicalModel },
    parent_ids: [],
    created_at: `2026-01-${String(version).padStart(2, '0')}T12:00:00`,
    is_selected: version === 15
  });
  const initialItems = Array.from({ length: 10 }, (_, index) =>
    stageArtifact(15 - index)
  );
  const olderItems = Array.from({ length: 5 }, (_, index) =>
    stageArtifact(5 - index)
  );

  await page.route(
    `**/api/v1/sessions/${session.id}/workflow`,
    async (route) => {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          session_id: session.id,
          workflow_kind: 'voiceover',
          workflow_preset: 'default',
          revision: 1,
          sources: [],
          stages: [
            {
              number: 1,
              key: 'transcribe',
              title: 'Transcribe',
              explanation: 'Create timed subtitles.',
              status: 'completed',
              executable: true,
              included: true,
              artifact: {
                ...initialItems[0],
                path: initialItems[0].relative_path
              },
              artifacts: initialItems,
              selected_artifact_id: initialItems[0].id,
              selection_revision: 15,
              artifact_history_total: 15,
              artifact_history_has_more: true,
              artifact_history_next_before_version: 6,
              job_id: null,
              progress: null,
              detail: null,
              usage: {
                input_tokens: 1200,
                cached_input_tokens: 0,
                output_tokens: 300,
                total_tokens: 1500,
                cost_usd: 0.001,
                model_id: canonicalModel,
                model_ids: [canonicalModel],
                event_count: 1,
                created_at: '2026-01-15T12:00:00Z'
              }
            }
          ]
        })
      });
    }
  );
  await page.route(
    `**/api/v1/sessions/${session.id}/stages/transcribe/artifacts?*`,
    async (route) => {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          stage_key: 'transcribe',
          selected_artifact_id: initialItems[0].id,
          revision: 15,
          items: olderItems,
          total: 15,
          limit: 50,
          before_version: 6,
          has_more: false,
          next_before_version: null
        })
      });
    }
  );
  const reviewCatalog = [15, 14].map((version) => ({
    artifact_id: `artifact-${version}`,
    role: 'transcription',
    stage: 'transcription',
    version,
    document_id: `document-${version}`,
    revision_id: `revision-${version}`,
    revision: version,
    reviewed: false,
    language: 'de',
    segment_count: 1,
    state: version === 15 ? 'current' : 'stale',
    created_at: `2026-01-${version}T12:00:00Z`
  }));
  const reviewRequests: string[][] = [];
  await page.route(
    `**/api/v1/sessions/${session.id}/subtitles/catalog`,
    async (route) => {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ session_id: session.id, items: reviewCatalog })
      });
    }
  );
  await page.route(
    `**/api/v1/sessions/${session.id}/subtitles/review?*`,
    async (route) => {
      const artifactIds = new URL(route.request().url()).searchParams.getAll(
        'artifact_id'
      );
      reviewRequests.push(artifactIds);
      const segment = (artifactId: string) => ({
        id: `segment-${artifactId}`,
        ordinal: 0,
        start_ms: 0,
        end_ms: 2000,
        text:
          artifactId === 'artifact-15'
            ? 'The newest transcription.'
            : 'The alternate transcription.',
        speaker: 'SPEAKER_0'
      });
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          session_id: session.id,
          primary_artifact_id: artifactIds[0],
          columns: artifactIds.map((artifactId) => {
            const item = reviewCatalog.find(
              (candidate) => candidate.artifact_id === artifactId
            )!;
            return {
              artifact_id: artifactId,
              role: 'transcription',
              stage: 'transcription',
              document_id: item.document_id,
              revision_id: item.revision_id,
              revision: item.revision,
              reviewed: false,
              language: 'de',
              segments: [segment(artifactId)]
            };
          }),
          rows: [
            {
              start_ms: 0,
              end_ms: 2000,
              changed: artifactIds.length > 1,
              cells: Object.fromEntries(
                artifactIds.map((artifactId) => [
                  artifactId,
                  [segment(artifactId)]
                ])
              )
            }
          ]
        })
      });
    }
  );

  await page.goto(`/sessions/${session.id}`);
  await expect(page.getByLabel('Model google/gemini-3.6-flash')).toBeVisible();
  await expect(page.locator('body')).not.toContainText(
    'aff14ed0-c04f-4241-8034-61b6236a190a'
  );
  await expect(page.getByText('15 saved results')).toBeVisible();
  const versionSelect = page.getByLabel('Selected version');
  await expect(versionSelect.locator('option')).toHaveCount(10);
  await page.getByText('Version history', { exact: true }).click();
  await page.getByRole('button', { name: 'Load earlier versions' }).click();
  await expect(versionSelect.locator('option')).toHaveCount(15);
  await expect(
    page.getByRole('button', { name: 'Load earlier versions' })
  ).toHaveCount(0);

  await page.getByRole('button', { name: 'Preview selected' }).click();
  const review = page.getByRole('dialog', { name: 'Compare and refine' });
  await expect(review).toBeVisible();
  await expect(
    review.getByRole('button', { name: 'Changed only' })
  ).toBeHidden();
  await expect(
    review.getByLabel('Find in transcription segments')
  ).toBeHidden();
  await review.getByText('Find, filter & compare', { exact: true }).click();
  await expect(
    review.getByRole('button', { name: 'Changed only' })
  ).toBeVisible();
  await expect(
    review.locator('optgroup[label="Transcriptions"] option')
  ).toHaveCount(1);
  await review.getByLabel('Add a comparison').selectOption('artifact-14');
  await review.getByRole('button', { name: 'Add', exact: true }).click();
  await expect
    .poll(() => reviewRequests)
    .toEqual([['artifact-15'], ['artifact-15', 'artifact-14']]);
  await expect(
    review.getByRole('columnheader').filter({ hasText: 'transcription v15' })
  ).toBeVisible();
  await expect(
    review.getByRole('columnheader').filter({ hasText: 'transcription v14' })
  ).toBeVisible();
  await expect(review.getByText('The alternate transcription.')).toBeVisible();

  await review.getByText('Find, filter & compare', { exact: true }).click();
  await review.getByRole('button', { name: 'Delete' }).focus();
  await page.keyboard.press('Tab');
  await expect(
    review.getByRole('button', { name: 'Save revision' })
  ).toBeFocused();
});

test('a selected correction checkpoint can fork a clean session branch', async ({
  page
}) => {
  await signIn(page);
  const authStatus = await page.request.get('/api/v1/auth/status');
  const csrfToken = (await authStatus.json()).csrf_token;
  const created = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': csrfToken },
    data: {
      name: uniqueName('Fork checkpoint'),
      workflow_kind: 'voiceover',
      included_stages: ['correct']
    }
  });
  expect(created.ok()).toBeTruthy();
  const session = await created.json();
  const artifact = {
    id: 'selected-correction-checkpoint',
    version: 1,
    kind: 'srt',
    role: 'correction',
    relative_path: 'sessions/fork-source/correction.srt',
    path: 'sessions/fork-source/correction.srt',
    mime_type: 'application/x-subrip',
    size_bytes: 128,
    state: 'current',
    settings_hash: 'correction-settings',
    metadata_json: {},
    parent_ids: [],
    created_at: '2026-08-03T12:00:00Z',
    is_selected: true
  };
  await page.route(
    `**/api/v1/sessions/${session.id}/workflow`,
    async (route) => {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          session_id: session.id,
          workflow_kind: 'voiceover',
          workflow_preset: 'default',
          revision: 1,
          sources: [],
          stages: [
            {
              number: 1,
              key: 'correct',
              title: 'Correct',
              explanation: 'Review the source-language subtitles.',
              status: 'completed',
              executable: true,
              included: true,
              artifact,
              artifacts: [artifact],
              selected_artifact_id: artifact.id,
              selection_revision: 1,
              artifact_history_total: 1,
              artifact_history_has_more: false,
              artifact_history_next_before_version: null,
              job_id: null,
              progress: null,
              detail: null,
              usage: null
            }
          ]
        })
      });
    }
  );

  const forkedId = 'forked-session-checkpoint';
  let forkPayload: Record<string, unknown> | null = null;
  await page.route(`**/api/v1/sessions/${session.id}/forks`, async (route) => {
    forkPayload = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({
      status: 201,
      contentType: 'application/json',
      body: JSON.stringify({
        ...session,
        id: forkedId,
        name: 'Polish alternate',
        forked_from_session_id: session.id,
        checkpoint_artifact_id: 'cloned-correction-checkpoint',
        copied_stages: ['correction']
      })
    });
  });

  await page.goto(`/sessions/${session.id}`);
  await page.getByRole('button', { name: 'Fork here' }).click();
  const dialog = page.getByRole('dialog', {
    name: 'Fork after correction'
  });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText(
    'Generation runs, audio takes, assemblies, and exports stay in the original session.'
  );
  await dialog.getByLabel('New session name').fill('Polish alternate');
  await dialog.getByRole('button', { name: 'Create fork' }).click();

  await expect(page).toHaveURL(`/sessions/${forkedId}`);
  expect(forkPayload).toEqual({
    checkpoint_artifact_id: artifact.id,
    name: 'Polish alternate'
  });
});

test('provider defaults and restartable tours are keyboard reachable', async ({
  page
}) => {
  await signIn(page);
  await page.getByRole('link', { name: 'Providers & services' }).click();
  await expect(
    page.getByRole('heading', { name: 'LLM connections and models' })
  ).toBeVisible();
  await page.getByRole('button', { name: 'Tour' }).click();
  await expect(
    page.getByRole('heading', { name: 'Profiles are editable starting points' })
  ).toBeVisible();
  await page.keyboard.press('Tab');
  await expect(page.locator(':focus')).toBeVisible();
});

test('theme and setup dock remain available after navigation', async ({
  page
}) => {
  await signIn(page);
  await page.getByRole('link', { name: 'Review setup' }).click();
  await expect(
    page.getByText('Return to setup', { exact: true })
  ).toBeVisible();
  await page.getByRole('link', { name: 'Continue setup' }).click();
  await expect(
    page.getByRole('heading', { name: 'Prepare your studio' })
  ).toBeVisible();
  await page.getByRole('button', { name: 'Close setup checklist' }).click();
  await page.getByRole('button', { name: /Dark mode|Light mode/ }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
});

test('sessions page launches creation and workspace source picker exposes every source mode', async ({
  page
}) => {
  await signIn(page);
  await page.getByRole('link', { name: 'Sessions' }).click();
  await page.getByRole('button', { name: 'Add session' }).click();
  await expect(
    page.getByRole('heading', { name: 'What would you like to make?' })
  ).toBeVisible();
  await page.getByRole('button', { name: 'Close', exact: true }).click();

  const authStatus = await page.request.get('/api/v1/auth/status');
  const csrfToken = (await authStatus.json()).csrf_token;
  const created = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': csrfToken },
    data: {
      name: uniqueName('Source picker regression'),
      workflow_kind: 'audiobook'
    }
  });
  expect(created.ok()).toBeTruthy();
  const session = await created.json();
  await page.goto(`/sessions/${session.id}`);

  await page.getByRole('button', { name: 'Add source' }).click();
  await expect(page.getByRole('button', { name: 'Upload' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Paste text' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Public URL' })).toBeVisible();
  await expect(
    page.getByRole('button', { name: 'Source library' })
  ).toBeVisible();
  await page.getByRole('button', { name: 'Paste text' }).click();
  await page.getByLabel('Source name').fill('Pasted source');
  await page
    .getByLabel('Text')
    .fill(
      'AİB cafe\u0301 cafe. This source source was pasted directly into an existing session.'
    );
  await page.getByLabel('Find in pasted source').fill('İ');
  await page.getByLabel('Replace in pasted source').fill('X');
  await page.getByRole('button', { name: 'Replace all' }).click();
  await expect(page.getByLabel('Text')).toHaveValue(
    'AXB cafe\u0301 cafe. This source source was pasted directly into an existing session.'
  );
  await page.getByLabel('Find in pasted source').fill('cafe');
  await page.getByLabel('Replace in pasted source').fill('bistro');
  await page.getByRole('button', { name: 'Match whole word' }).click();
  await expect(page.getByText('1 / 1', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Replace all' }).click();
  await expect(page.getByLabel('Text')).toHaveValue(
    'AXB cafe\u0301 bistro. This source source was pasted directly into an existing session.'
  );
  await page.getByLabel('Find in pasted source').fill('source');
  await page.getByLabel('Replace in pasted source').fill('asset');
  await page.getByRole('button', { name: 'Replace all' }).click();
  await expect(page.getByLabel('Text')).toHaveValue(
    'AXB cafe\u0301 bistro. This asset asset was pasted directly into an existing session.'
  );
  await page.getByRole('button', { name: 'Add and select' }).click();
  await expect(
    page.getByText('Source added and selected as the current input.')
  ).toBeVisible();
  const attached = await page.request.get(
    `/api/v1/sessions/${session.id}/sources`
  );
  expect((await attached.json()).items).toHaveLength(1);
});

test('voiceover output settings follow the video source and default to a controlled mix', async ({
  page
}) => {
  await signIn(page);
  const authStatus = await page.request.get('/api/v1/auth/status');
  const csrfToken = (await authStatus.json()).csrf_token;
  const headers = { 'X-CSRF-Token': csrfToken };
  const created = await page.request.post('/api/v1/sessions', {
    headers,
    data: {
      name: uniqueName('Video output profile'),
      workflow_kind: 'voiceover'
    }
  });
  expect(created.ok()).toBeTruthy();
  const session = await created.json();
  const uploaded = await page.request.post('/api/v1/uploads', {
    headers,
    multipart: {
      session_id: session.id,
      purpose: 'source',
      file: {
        name: 'source-video.mp4',
        mimeType: 'video/mp4',
        buffer: Buffer.from('media fixture')
      }
    }
  });
  expect(uploaded.ok()).toBeTruthy();

  await page.goto(`/sessions/${session.id}/output`);
  await expect(
    page.getByRole('heading', { name: 'Video output', exact: true })
  ).toBeVisible();
  await expect(page.getByLabel('Audio result')).toHaveValue('mixed');
  await expect(page.getByText('Soundtrack mix')).toBeVisible();
  await expect(page.getByLabel('Source level (dB)')).toHaveValue('0');
  await expect(page.getByLabel('Maximum start delay (ms)')).toHaveValue('800');
  await expect(page.getByLabel('Album / series')).toHaveCount(0);
  await expect(page.getByLabel('Genre')).toHaveCount(0);
  await expect(page.getByText('Cover artwork')).toHaveCount(0);
});

test('reading mode flows segments together and separates only saved paragraphs', async ({
  page
}) => {
  await signIn(page);
  const first = 'The first segment fills most of this test line.';
  const second = 'The second segment begins in the remaining space.';
  const third = 'This sentence starts a separate paragraph.';
  const sessionId = await createGenerationPlan(page, [
    { text: first },
    { text: second, paragraph_break_after: true },
    { text: third, paragraph_break_after: true }
  ]);

  await page.goto(`/sessions/${sessionId}`);
  await page.getByRole('button', { name: 'Generation', exact: true }).click();
  await page.getByRole('button', { name: 'Reading', exact: true }).click();

  const paragraphs = page.locator('.reading-paragraph');
  await expect(paragraphs).toHaveCount(2);
  await expect(paragraphs.nth(0)).toHaveText(`${first} ${second}`);
  await expect(paragraphs.nth(1)).toHaveText(third);
  await paragraphs.nth(0).evaluate((paragraph) => {
    paragraph.style.width = '600px';
    paragraph.style.font = '16px monospace';
    paragraph.style.lineHeight = '24px';
  });

  const segmentRects = await paragraphs
    .nth(0)
    .locator('.reading-sentence')
    .evaluateAll((segments) =>
      segments.map((segment) => ({
        display: getComputedStyle(segment).display,
        rects: Array.from(segment.getClientRects(), ({ x, y, width }) => ({
          x,
          y,
          width
        }))
      }))
    );
  expect(segmentRects[0].display).toBe('inline');
  expect(segmentRects[1].rects).toHaveLength(2);
  expect(segmentRects[1].rects[0].x).toBeGreaterThan(
    segmentRects[0].rects[0].x + 100
  );
});

test('running generation controls stay on one drawer header row', async ({
  page
}) => {
  await page.setViewportSize({ width: 1186, height: 698 });
  await signIn(page);
  const sessionId = await createGenerationPlan(page, [
    { text: 'A queued generation segment.' }
  ]);
  const run = {
    id: 'running-generation',
    session_id: sessionId,
    plan_revision_id: 'generation-plan',
    sequence_number: 1,
    operation: 'generate',
    label: 'Run 1: Chatterbox · chatterbox-multilingual',
    job_id: 'generation-job',
    status: 'running',
    progress: 0.4
  };
  await page.route(`**/api/v1/sessions/${sessionId}/generation-runs`, (route) =>
    route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ items: [run] })
    })
  );
  await page.route(
    `**/api/v1/sessions/${sessionId}/generation-segments?*`,
    (route) =>
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          items: [
            {
              id: 'queued-segment',
              ordinal: 0,
              node_kind: 'paragraph',
              paragraph_break_after: true,
              text: 'A queued generation segment.',
              optimized_text: null,
              speech_plan: {},
              optimization_status: 'not_requested',
              optimization_reviewed: false,
              marked: false,
              removed: false,
              status: 'queued',
              revision: 1,
              takes: []
            }
          ],
          total: 1,
          next_cursor: null,
          plan_revision_id: 'generation-plan'
        })
      })
  );

  await page.goto(`/sessions/${sessionId}`);
  await page.getByRole('button', { name: 'Generation', exact: true }).click();
  await expect(
    page.getByRole('button', {
      name: 'Stop safely after the current segment'
    })
  ).toHaveText(/Stop$/);
  await expect(page.getByText('Stop safely', { exact: true })).toHaveCount(0);

  const rowCenters = await page
    .locator('aside.generation-drawer > header')
    .evaluate((header) =>
      Array.from(header.children, (element) => {
        const bounds = element.getBoundingClientRect();
        return bounds.top + bounds.height / 2;
      })
    );
  expect(Math.max(...rowCenters) - Math.min(...rowCenters)).toBeLessThan(2);
});

test('generation segments support Ctrl and Shift multi-selection in both review views', async ({
  page
}) => {
  await signIn(page);
  const sessionId = await createGenerationPlan(page, [
    { text: 'Segment one.', node_kind: 'heading' },
    { text: 'Segment two.' },
    { text: 'Segment three.' },
    { text: 'Segment four.', paragraph_break_after: true }
  ]);

  await page.goto(`/sessions/${sessionId}`);
  await page.getByRole('button', { name: 'Generation', exact: true }).click();

  const rows = page.locator('tbody tr');
  await rows.nth(0).locator('td').nth(1).click();
  await rows
    .nth(2)
    .locator('td')
    .nth(1)
    .click({ modifiers: ['Control'] });
  await expect(page.locator('tbody tr.selected')).toHaveCount(2);
  await rows
    .nth(3)
    .locator('td')
    .nth(1)
    .click({ modifiers: ['Shift'] });
  await expect(rows.nth(0)).not.toHaveClass(/selected/);
  await expect(rows.nth(1)).not.toHaveClass(/selected/);
  await expect(rows.nth(2)).toHaveClass(/selected/);
  await expect(rows.nth(3)).toHaveClass(/selected/);
  await page
    .getByRole('button', { name: /RVC speech-to-speech settings/ })
    .click();
  await expect(
    page.getByRole('button', { name: 'RVC selected (2)' })
  ).toBeVisible();

  await page.getByRole('button', { name: 'Reading', exact: true }).click();
  const sentences = page.locator('.reading-sentence');
  const heading = page.locator('.reading-heading button');
  await heading.click();
  await sentences.nth(0).click({ modifiers: ['Control'] });
  await expect(page.locator('.reading-heading.selected-heading')).toHaveCount(
    1
  );
  await expect(page.locator('.reading-segment.selected-sentence')).toHaveCount(
    1
  );
  await sentences.nth(2).click({ modifiers: ['Shift'] });
  await expect(page.locator('.reading-heading.selected-heading')).toHaveCount(
    0
  );
  await expect(page.locator('.reading-segment.selected-sentence')).toHaveCount(
    3
  );
  await sentences.nth(0).click({ modifiers: ['Control'] });
  await expect(page.locator('.reading-segment.selected-sentence')).toHaveCount(
    2
  );
  await expect(
    page.getByRole('button', { name: 'RVC selected (2)' })
  ).toBeVisible();
});

test('generation segment search and replace preserves partial words and saves every edit', async ({
  page
}) => {
  await signIn(page);
  const sessionId = await createGenerationPlan(page, [
    { text: 'A cat waits.' },
    { text: 'A catfish and cat.' }
  ]);

  await page.goto(`/sessions/${sessionId}`);
  await page.getByRole('button', { name: 'Generation', exact: true }).click();
  await page.getByLabel('Find in generation segments').fill('cat');
  await page.getByLabel('Replace in generation segments').fill('dog');
  await page.getByRole('button', { name: 'Match whole word' }).click();
  await expect(page.getByText('1 / 2', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Replace all' }).click();

  const fields = page.locator('textarea[data-generation-search-index]');
  await expect(fields.nth(0)).toHaveValue('A dog waits.');
  await expect(fields.nth(1)).toHaveValue('A catfish and dog.');
  const saved = await page.request.get(
    `/api/v1/sessions/${sessionId}/generation-segments`
  );
  const savedBody = (await saved.json()) as { items: { text: string }[] };
  expect(savedBody.items.map((item) => item.text)).toEqual([
    'A dog waits.',
    'A catfish and dog.'
  ]);
});

test('generation drawer layout survives segment regeneration refreshes', async ({
  page
}) => {
  let phase: 'completed' | 'running' = 'completed';
  let revision = 0;
  let failNextRegeneration = false;
  const runId = 'layout-run';
  await page.route('**/api/v1/events?after=*', (route) => route.abort());
  await signIn(page);
  const sessionId = await createGenerationPlan(page, [
    { text: 'Synthetic generation segment.' }
  ]);
  const run = () => ({
    id: runId,
    session_id: sessionId,
    plan_revision_id: 'layout-plan',
    sequence_number: 1,
    operation: 'regenerate',
    label: 'Layout test run',
    job_id: 'layout-job',
    status: phase,
    progress: phase === 'completed' ? 1 : 0.5
  });
  await page.route(
    `**/api/v1/sessions/${sessionId}/generation-runs`,
    async (route) => {
      if (route.request().method() === 'POST') {
        if (failNextRegeneration) {
          failNextRegeneration = false;
          await route.fulfill({
            status: 500,
            contentType: 'application/json',
            body: JSON.stringify({ detail: 'Synthetic regeneration failure.' })
          });
          return;
        }
        phase = 'running';
        await route.fulfill({
          contentType: 'application/json',
          body: JSON.stringify(run())
        });
        setTimeout(() => {
          revision += 1;
          phase = 'completed';
        }, 100);
        return;
      }
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ items: [run()] })
      });
    }
  );
  await page.route(
    `**/api/v1/sessions/${sessionId}/output-assemblies/latest`,
    (route) =>
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ item: null })
      })
  );
  await page.route(
    `**/api/v1/sessions/${sessionId}/generation-segments?*`,
    (route) =>
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          items: [
            {
              id: 'synthetic-segment',
              ordinal: 0,
              node_kind: 'paragraph',
              paragraph_break_after: false,
              text: `Synthetic generation segment revision ${revision}.`,
              optimized_text: null,
              speech_plan: {},
              optimization_status: 'not_requested',
              optimization_reviewed: false,
              marked: false,
              removed: false,
              status: 'completed',
              revision,
              takes: []
            }
          ],
          total: 1,
          next_cursor: null,
          plan_revision_id: 'layout-plan'
        })
      })
  );

  await page.goto(`/sessions/${sessionId}`);
  const drawer = page.locator('[data-generation-layout]');
  await expect(drawer).toHaveAttribute('data-generation-layout', 'collapsed');
  await page.getByRole('button', { name: 'Generation', exact: true }).click();
  await expect(drawer).toHaveAttribute('data-generation-layout', 'half');
  await page.getByRole('button', { name: 'Use full height' }).click();
  await expect(drawer).toHaveAttribute('data-generation-layout', 'full');

  const regenerate = page.getByRole('button', {
    name: 'Regenerate segment 1',
    exact: true
  });
  const segmentText = page.locator(
    'tr[data-segment-id="synthetic-segment"] textarea'
  );
  await regenerate.click();
  await expect(drawer).toHaveAttribute('data-generation-layout', 'full');
  await expect(segmentText).toHaveValue(
    'Synthetic generation segment revision 1.',
    { timeout: 10_000 }
  );
  await expect(drawer).toHaveAttribute('data-generation-layout', 'full');

  await page.getByRole('button', { name: 'Use half height' }).click();
  await expect(drawer).toHaveAttribute('data-generation-layout', 'half');
  await regenerate.click();
  await expect(drawer).toHaveAttribute('data-generation-layout', 'half');
  await expect(segmentText).toHaveValue(
    'Synthetic generation segment revision 2.',
    { timeout: 10_000 }
  );
  await expect(drawer).toHaveAttribute('data-generation-layout', 'half');

  await page.getByRole('button', { name: 'Use full height' }).click();
  failNextRegeneration = true;
  await regenerate.click();
  expect(failNextRegeneration).toBeFalsy();
  await expect(drawer).toHaveAttribute('data-generation-layout', 'full');

  const unrelatedSessionId = await createGenerationPlan(page, [
    { text: 'Unrelated synthetic segment.' }
  ]);
  await page.goto(`/sessions/${unrelatedSessionId}`);
  await expect(drawer).toHaveAttribute('data-generation-layout', 'collapsed');
  await page.getByRole('button', { name: 'Generation', exact: true }).click();
  await expect(drawer).toHaveAttribute('data-generation-layout', 'half');
});

test('selecting a take from history returns to Active mix without changing another row', async ({
  page
}) => {
  await page.route('**/api/v1/events?after=*', (route) => route.abort());
  await signIn(page);
  const sessionId = await createGenerationPlan(page, [
    { text: 'History take one.' },
    { text: 'History take two.' }
  ]);
  const firstRunId = 'history-run-one';
  const secondRunId = 'history-run-two';
  const runs = [
    {
      id: secondRunId,
      session_id: sessionId,
      plan_revision_id: 'history-plan',
      sequence_number: 2,
      operation: 'regenerate',
      label: 'Run 2: newer preset',
      status: 'completed',
      progress: 1,
      take_count: 1
    },
    {
      id: firstRunId,
      session_id: sessionId,
      plan_revision_id: 'history-plan',
      sequence_number: 1,
      operation: 'generate',
      label: 'Run 1: original preset',
      status: 'completed',
      progress: 1,
      take_count: 2
    }
  ];
  const take = (id: string, generationRunId: string, isActive: boolean) => ({
    id,
    generation_run_id: generationRunId,
    artifact_id: `artifact-${id}`,
    kind: 'tts',
    status: 'completed',
    is_active: isActive,
    revision: 1,
    created_at: '2026-08-20T00:00:00Z'
  });
  let activeFirstTakeId = 'take-1-old';
  const selectedTakeRequests: string[] = [];
  await page.route(
    `**/api/v1/sessions/${sessionId}/generation-runs`,
    async (route) => {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ items: runs })
      });
    }
  );
  await page.route(
    `**/api/v1/sessions/${sessionId}/output-assemblies/latest`,
    (route) =>
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ item: null })
      })
  );
  await page.route(
    `**/api/v1/sessions/${sessionId}/generation-segments?*`,
    async (route) => {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          items: [
            {
              id: 'history-segment-one',
              ordinal: 0,
              node_kind: 'paragraph',
              paragraph_break_after: false,
              text: 'History take one.',
              marked: false,
              removed: false,
              status: 'completed',
              revision: 1,
              takes: [
                take(
                  'take-1-old',
                  firstRunId,
                  activeFirstTakeId === 'take-1-old'
                ),
                take(
                  'take-1-new',
                  secondRunId,
                  activeFirstTakeId === 'take-1-new'
                )
              ]
            },
            {
              id: 'history-segment-two',
              ordinal: 1,
              node_kind: 'paragraph',
              paragraph_break_after: true,
              text: 'History take two.',
              marked: false,
              removed: false,
              status: 'completed',
              revision: 1,
              takes: [take('take-2-old', firstRunId, true)]
            }
          ],
          total: 2,
          next_cursor: null,
          plan_revision_id: 'history-plan'
        })
      });
    }
  );
  await page.route(
    '**/api/v1/generation-segments/history-segment-one/takes/*/select',
    async (route) => {
      const takeId = route.request().url().split('/').at(-2) ?? '';
      selectedTakeRequests.push(takeId);
      activeFirstTakeId = takeId;
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ revision: 2 })
      });
    }
  );

  await page.goto(`/sessions/${sessionId}`);
  await page.getByRole('button', { name: 'Generation', exact: true }).click();
  const picker = page.locator('label.run-picker select');
  await expect(picker).toHaveValue('');
  await picker.selectOption(firstRunId);
  await expect(picker).toHaveValue(firstRunId);

  const rows = page.locator('tbody tr');
  const firstAudioTake = rows.nth(0).locator('td').nth(3).locator('select');
  const secondAudioTake = rows.nth(1).locator('td').nth(3).locator('select');
  await expect(firstAudioTake).toHaveValue('take-1-old');
  await expect(secondAudioTake).toHaveValue('take-2-old');

  await firstAudioTake.selectOption('take-1-new');
  await expect(picker).toHaveValue('');
  await expect(firstAudioTake).toHaveValue('take-1-new');
  await expect(secondAudioTake).toHaveValue('take-2-old');
  expect(selectedTakeRequests).toEqual(['take-1-new']);
});

test('generated segments return to the current filtered page after repeated regeneration', async ({
  page
}) => {
  test.setTimeout(180_000);
  let phase: 'completed' | 'running' = 'completed';
  let generatedRefreshes = 0;
  let generatedRunningRefreshes = 0;
  let regeneratedRevision = 0;
  const runId = 'generation-run';
  const planRevisionId = 'generation-plan';
  const completed = Array.from({ length: 102 }, (_, ordinal) => ({
    id: `segment-${ordinal}`,
    ordinal,
    node_kind: 'paragraph',
    paragraph_break_after: false,
    text: `Generated segment ${ordinal + 1}.`,
    optimized_text: null,
    speech_plan: {},
    optimization_status: 'not_requested',
    optimization_reviewed: false,
    marked: false,
    removed: false,
    status: 'completed',
    revision: 1,
    takes: []
  }));
  const failed = {
    ...completed[0],
    id: 'failed-segment',
    ordinal: 102,
    text: 'This failed segment must remain excluded.',
    status: 'failed'
  };
  const run = () => ({
    id: runId,
    session_id: 'mock-session',
    plan_revision_id: planRevisionId,
    sequence_number: 1,
    operation: 'generate',
    label: 'Run 1: test',
    job_id: 'generation-job',
    status: phase,
    progress: phase === 'completed' ? 1 : 0.5
  });

  await page.route('**/api/v1/events?after=*', (route) => route.abort());
  await signIn(page);
  const sessionId = await createGenerationPlan(page, [
    { text: 'Placeholder.' }
  ]);
  await page.route(
    `**/api/v1/sessions/${sessionId}/generation-runs`,
    async (route) => {
      if (route.request().method() === 'POST') {
        phase = 'running';
        await route.fulfill({
          contentType: 'application/json',
          body: JSON.stringify(run())
        });
        return;
      }
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ items: [run()] })
      });
    }
  );
  await page.route(
    `**/api/v1/sessions/${sessionId}/output-assemblies/latest`,
    (route) =>
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ item: null })
      })
  );
  await page.route(
    `**/api/v1/sessions/${sessionId}/generation-segments?*`,
    async (route) => {
      const query = new URL(route.request().url()).searchParams;
      const authoritative = completed.map((item) =>
        item.ordinal === 100 && regeneratedRevision
          ? {
              ...item,
              text: `Regenerated segment 101 · revision ${regeneratedRevision}.`,
              revision: item.revision + regeneratedRevision,
              takes: [
                {
                  id: `take-${regeneratedRevision}`,
                  generation_run_id: runId,
                  artifact_id: `artifact-${regeneratedRevision}`,
                  kind: 'tts',
                  status: 'completed',
                  is_active: true,
                  revision: regeneratedRevision,
                  created_at: `2026-01-01T00:00:0${regeneratedRevision}Z`
                }
              ]
            }
          : item
      );
      const visible =
        query.get('status') === 'completed'
          ? authoritative.filter(
              (item) => phase === 'completed' || item.ordinal !== 100
            )
          : [...authoritative, failed];
      if (query.get('status') === 'completed') {
        generatedRefreshes += 1;
        if (phase === 'running') generatedRunningRefreshes += 1;
      }
      const cursor = Number(query.get('cursor') ?? 0);
      const limit = Number(query.get('limit') ?? 100);
      const items = visible
        .filter((item) => item.ordinal >= cursor)
        .slice(0, limit);
      const hasMore = visible.some(
        (item) => item.ordinal > (items.at(-1)?.ordinal ?? -1)
      );
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          items,
          total: visible.length,
          next_cursor: hasMore ? (items.at(-1)?.ordinal ?? 0) + 1 : null,
          plan_revision_id: planRevisionId
        })
      });
    }
  );

  await page.goto(`/sessions/${sessionId}`);
  await page.getByRole('button', { name: 'Generation', exact: true }).click();
  const filter = page.getByLabel('Segments to display');
  const regeneratedRow = page.locator('tr[data-segment-id="segment-100"]');
  const regenerateSegment101 = regeneratedRow.locator(
    'button[aria-label="Regenerate segment 101"]'
  );
  const waitForRegeneratedSegment = async () => {
    await regeneratedRow.waitFor({ state: 'attached', timeout: 30_000 });
    await expect(regenerateSegment101).toBeVisible({ timeout: 10_000 });
  };
  await page.getByRole('button', { name: 'Load more' }).click();
  await waitForRegeneratedSegment();
  await expect(
    regeneratedRow.getByRole('button', { name: 'Play' })
  ).toHaveCount(0);
  await regenerateSegment101.click();
  await waitForRegeneratedSegment();
  await expect(filter).toHaveValue('all');
  regeneratedRevision += 1;
  phase = 'completed';
  await expect(regeneratedRow.locator('textarea')).toHaveValue(
    `Regenerated segment 101 · revision ${regeneratedRevision}.`,
    { timeout: 30_000 }
  );
  await expect(
    regeneratedRow.getByRole('button', { name: 'Play' })
  ).toBeVisible();
  await expect(filter).toHaveValue('all');

  const completedRefreshesBeforeFilter = generatedRefreshes;
  await filter.selectOption('completed');
  await expect
    .poll(() => generatedRefreshes)
    .toBeGreaterThan(completedRefreshesBeforeFilter);
  await page.getByRole('button', { name: 'Load more' }).click();
  await expect
    .poll(() => generatedRefreshes)
    .toBeGreaterThan(completedRefreshesBeforeFilter + 1);
  await waitForRegeneratedSegment();
  const versionPicker = page.locator('label.run-picker select');
  await expect(versionPicker).toHaveValue('');

  for (let attempt = 0; attempt < 2; attempt += 1) {
    await regenerateSegment101.click();
    await expect.poll(() => generatedRunningRefreshes).toBeGreaterThan(attempt);
    regeneratedRevision += 1;
    const completedRefreshesBeforeReconciliation = generatedRefreshes;
    phase = 'completed';
    await expect
      .poll(() => generatedRefreshes, { timeout: 30_000 })
      .toBeGreaterThan(completedRefreshesBeforeReconciliation);
    await waitForRegeneratedSegment();
    await expect(regeneratedRow.locator('textarea')).toHaveValue(
      `Regenerated segment 101 · revision ${regeneratedRevision}.`,
      { timeout: 20_000 }
    );
    await expect(filter).toHaveValue('completed');
    await expect(versionPicker).toHaveValue('');
    await expect(regenerateSegment101).toHaveCount(1);
    await expect(
      page.getByText('This failed segment must remain excluded.')
    ).toHaveCount(0);
  }
});

test('alternate regeneration sends one selected-only setting set and returns to Active mix', async ({
  page
}) => {
  const posted: Array<Record<string, unknown>> = [];
  const runId = 'source-run';
  const planRevisionId = 'alternate-plan';
  const sourceRun = {
    id: runId,
    session_id: 'mock-session',
    plan_revision_id: planRevisionId,
    sequence_number: 1,
    operation: 'generate',
    label: 'Run 1: XTTS · base · stored-voice',
    status: 'completed',
    progress: 1,
    settings_snapshot: {
      tts: {
        service: 'XTTS',
        model: 'base',
        voice: 'stored-voice',
        language: 'de'
      },
      rvc: { enabled: false }
    }
  };
  let generationStarted = false;
  await page.route('**/api/v1/events?after=*', (route) => route.abort());
  await signIn(page);
  const sessionId = await createGenerationPlan(page, [
    { text: 'First alternate sentence.' },
    { text: 'Keep this active take.' }
  ]);
  const plan = await page.request.get(
    `/api/v1/sessions/${sessionId}/generation-segments`
  );
  const segments = (await plan.json()).items as Array<{ id: string }>;
  const rows = [
    {
      id: segments[0].id,
      ordinal: 0,
      node_kind: 'paragraph',
      paragraph_break_after: false,
      text: 'First alternate sentence.',
      optimized_text: null,
      speech_plan: {},
      optimization_status: 'not_requested',
      optimization_reviewed: false,
      voice: 'stored-first',
      language: 'de',
      marked: false,
      removed: false,
      status: generationStarted ? 'completed' : 'ready',
      revision: 1,
      takes: generationStarted
        ? [
            {
              id: 'alternate-take',
              generation_run_id: 'alternate-run',
              artifact_id: 'alternate-artifact',
              kind: 'tts_rvc',
              status: 'completed',
              is_active: true,
              revision: 1
            }
          ]
        : []
    },
    {
      id: segments[1].id,
      ordinal: 1,
      node_kind: 'paragraph',
      paragraph_break_after: false,
      text: 'Keep this active take.',
      optimized_text: null,
      speech_plan: {},
      optimization_status: 'completed',
      optimization_reviewed: false,
      voice: 'stored-second',
      language: 'it',
      marked: false,
      removed: false,
      status: 'completed',
      revision: 1,
      takes: [
        {
          id: 'preserved-take',
          generation_run_id: runId,
          artifact_id: 'preserved-artifact',
          kind: 'tts',
          status: 'completed',
          is_active: true,
          revision: 1
        }
      ]
    }
  ];
  await page.route(`**/api/v1/sessions/${sessionId}/settings/tts`, (route) =>
    route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        effective: {
          service: 'XTTS',
          model: 'base',
          voice: 'stored-voice',
          language: 'de'
        }
      })
    })
  );
  await page.route('**/api/v1/services/tts?refresh=true', (route) =>
    route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        default_service: 'XTTS',
        services: [
          {
            id: 'XTTS',
            name: 'XTTS',
            online: true,
            models: ['base'],
            voices: ['stored-voice']
          },
          {
            id: 'Chatterbox',
            name: 'Chatterbox',
            online: true,
            models: ['chatterbox-alt', 'chatterbox-second'],
            voices: ['alternate-reference']
          }
        ]
      })
    })
  );
  await page.route('**/api/v1/voices', (route) =>
    route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ items: [] })
    })
  );
  await page.route('**/api/v1/rvc/models', (route) =>
    route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ available: true, items: ['alternate-rvc'] })
    })
  );
  await page.route(
    `**/api/v1/sessions/${sessionId}/generation-runs`,
    async (route) => {
      if (route.request().method() === 'POST') {
        posted.push(route.request().postDataJSON() as Record<string, unknown>);
        generationStarted = true;
        await route.fulfill({
          contentType: 'application/json',
          body: JSON.stringify({
            ...sourceRun,
            id: 'alternate-run',
            sequence_number: 2,
            status: 'running'
          })
        });
        return;
      }
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ items: [sourceRun] })
      });
    }
  );
  await page.route(
    `**/api/v1/sessions/${sessionId}/output-assemblies/latest`,
    (route) =>
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ item: null })
      })
  );
  await page.route(
    `**/api/v1/sessions/${sessionId}/generation-segments?*`,
    (route) =>
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          items: rows,
          total: rows.length,
          next_cursor: null,
          plan_revision_id: planRevisionId
        })
      })
  );

  await page.goto(`/sessions/${sessionId}`);
  await page.getByRole('button', { name: 'Generation', exact: true }).click();
  const picker = page.locator('label.run-picker select');
  await page.getByRole('checkbox', { name: 'Mark segment 1' }).check();
  await page.getByRole('checkbox', { name: 'Mark segment 2' }).check();
  await page
    .locator('[data-generation-layout] header')
    .getByRole('button', { name: 'Alternate marked takes…' })
    .click();
  const dialog = page.getByRole('dialog');
  await expect(
    dialog.getByRole('heading', {
      name: 'Regenerate 2 selected segments with…'
    })
  ).toBeVisible();
  await expect(dialog.getByText('current session settings')).toBeVisible();
  await dialog.getByLabel('Speech service').selectOption('Chatterbox');
  await expect(dialog.getByLabel('Voice / managed reference')).toHaveValue('');
  await dialog.getByLabel('Model').selectOption('chatterbox-second');
  await expect(dialog.getByLabel('Voice / managed reference')).toHaveValue('');
  await dialog.getByLabel('Model').selectOption('chatterbox-alt');
  await dialog
    .getByLabel('Voice / managed reference')
    .selectOption('alternate-reference');
  await dialog.getByLabel('Language').selectOption('fr');
  await dialog
    .getByLabel('Generation prompt / instructions')
    .fill('Warm, quiet delivery.');
  await dialog
    .getByRole('checkbox', { name: 'Convert the new take with RVC' })
    .check();
  await dialog.getByLabel('RVC model').selectOption('alternate-rvc');
  await dialog.getByRole('button', { name: 'Create alternate takes' }).click();

  await expect.poll(() => posted.length).toBe(1);
  expect(posted[0]).toMatchObject({
    operation: 'regenerate',
    segment_ids: [segments[0].id, segments[1].id],
    generation_run_id: null,
    selected_segment_override: {
      tts: {
        service: 'Chatterbox',
        model: 'chatterbox-alt',
        voice: 'alternate-reference',
        language: 'fr',
        generation_prompt: 'Warm, quiet delivery.'
      },
      rvc: { enabled: true, model: 'alternate-rvc' }
    }
  });
  await expect(picker).toHaveValue('');
  await expect(
    page.locator(`tr[data-segment-id="${segments[1].id}"] select`).last()
  ).toHaveValue('preserved-take');
});

test('editorial workspace visual smoke', async ({ page }) => {
  const isWindows = await page.evaluate(() =>
    navigator.userAgent.includes('Windows')
  );
  test.skip(
    isWindows,
    'The visual baseline is captured on Linux to avoid platform font-metric differences.'
  );
  await signIn(page);
  await expect(page).toHaveScreenshot('workspace.png', {
    fullPage: true,
    animations: 'disabled',
    maxDiffPixelRatio: 0.12
  });
});

test('voice recording can be previewed, normalized, saved, and played', async ({
  page,
  browserName
}) => {
  test.skip(
    browserName !== 'chromium',
    'Chromium provides a deterministic fake microphone for this media integration test.'
  );
  const voiceName = uniqueName('Browser recorder');
  await signIn(page);
  await page.getByRole('link', { name: 'Voices' }).click();
  await page.getByLabel('New voice name').fill(voiceName);
  await page.getByRole('button', { name: 'Add voice' }).click();
  await expect(page.getByRole('heading', { name: voiceName })).toBeVisible();

  await page.getByRole('button', { name: 'Enable microphone' }).click();
  await expect(
    page.getByRole('button', { name: 'Record', exact: true })
  ).toBeEnabled();
  await page.getByRole('button', { name: 'Record', exact: true }).click();
  await page.waitForTimeout(1_000);
  await page.getByRole('button', { name: /Stop ·/ }).click();
  await expect(
    page.getByRole('button', { name: 'Play recording' })
  ).toBeVisible();

  await page.getByRole('button', { name: 'Play recording' }).click();
  await expect(
    page.getByRole('button', { name: 'Stop recording playback' })
  ).toBeVisible();
  await page.getByRole('button', { name: 'Stop recording playback' }).click();
  await page.getByRole('button', { name: 'Save sample' }).click();

  await expect(page.getByRole('button', { name: 'Play sample' })).toBeVisible({
    timeout: 20_000
  });
  await page.getByRole('button', { name: 'Play sample' }).click();
  await expect(
    page.getByRole('button', { name: 'Stop sample playback' })
  ).toBeVisible();
  await expect(page.getByRole('alert')).toHaveCount(0);
});
