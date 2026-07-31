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

test('workflow version history loads older pages on demand', async ({
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
    metadata_json: {},
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
              usage: null
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

  await page.goto(`/sessions/${session.id}`);
  await expect(page.getByText('15 saved results')).toBeVisible();
  const versionSelect = page.getByLabel('Selected version');
  await expect(versionSelect.locator('option')).toHaveCount(10);
  await page.getByText('Version history', { exact: true }).click();
  await page.getByRole('button', { name: 'Load earlier versions' }).click();
  await expect(versionSelect.locator('option')).toHaveCount(15);
  await expect(
    page.getByRole('button', { name: 'Load earlier versions' })
  ).toHaveCount(0);
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
    .fill('This source source was pasted directly into an existing session.');
  await page.getByLabel('Find in pasted source').fill('source');
  await page.getByLabel('Replace in pasted source').fill('asset');
  await page.getByRole('button', { name: 'Replace all' }).click();
  await expect(page.getByLabel('Text')).toHaveValue(
    'This asset asset was pasted directly into an existing session.'
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
  await expect(page.getByLabel('Maximum start delay (ms)')).toHaveValue('2000');
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
  await expect(versionPicker).toHaveValue(runId);

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
    await expect(versionPicker).toHaveValue(runId);
    await expect(regenerateSegment101).toHaveCount(1);
    await expect(
      page.getByText('This failed segment must remain excluded.')
    ).toHaveCount(0);
  }
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
