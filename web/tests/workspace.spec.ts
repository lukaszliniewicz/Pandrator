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

async function createGenerationPlan(page: Page, segments: Array<{ text: string; paragraph_break_after?: boolean; node_kind?: string }>) {
  const authStatus = await page.request.get('/api/v1/auth/status');
  expect(authStatus.ok()).toBeTruthy();
  const csrfToken = (await authStatus.json()).csrf_token;
  expect(typeof csrfToken).toBe('string');
  const headers = { 'X-CSRF-Token': csrfToken };
  const sessionResponse = await page.request.post('/api/v1/sessions', {
    headers,
    data: { name: uniqueName('Reading mode regression'), workflow_kind: 'audiobook' }
  });
  expect(sessionResponse.ok()).toBeTruthy();
  const session = await sessionResponse.json();
  const planResponse = await page.request.post(`/api/v1/sessions/${session.id}/generation-plan`, {
    headers,
    data: { segments }
  });
  expect(planResponse.ok()).toBeTruthy();
  return session.id as string;
}

test('wizard creates a guided subtitle workspace and preserves setup return', async ({ page }) => {
  const sessionName = uniqueName('Playwright subtitles');
  await signIn(page);
  await expect(page.getByRole('heading', { name: 'What shall we make?' })).toBeVisible();
  await page.getByRole('button', { name: /Create subtitles/ }).first().click();
  await page.getByRole('button', { name: 'Add later' }).click();
  await page.getByRole('button', { name: 'Continue' }).click();
  await page.getByRole('button', { name: 'Review', exact: true }).click();
  await page.getByLabel('Session name').fill(sessionName);
  await page.getByRole('button', { name: 'Create workspace' }).click();
  await expect(page.getByRole('heading', { name: sessionName })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Transcribe' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Tour' })).toBeVisible();
});

test('provider defaults and restartable tours are keyboard reachable', async ({ page }) => {
  await signIn(page);
  await page.getByRole('link', { name: 'Providers & services' }).click();
  await expect(page.getByRole('heading', { name: 'LLM connections and models' })).toBeVisible();
  await page.getByRole('button', { name: 'Tour' }).click();
  await expect(page.getByRole('heading', { name: 'Profiles are editable starting points' })).toBeVisible();
  await page.keyboard.press('Tab');
  await expect(page.locator(':focus')).toBeVisible();
});

test('theme and setup dock remain available after navigation', async ({ page }) => {
  await signIn(page);
  await page.getByRole('link', { name: 'Review setup' }).click();
  await expect(page.getByText('Return to setup', { exact: true })).toBeVisible();
  await page.getByRole('link', { name: 'Continue setup' }).click();
  await expect(page.getByRole('heading', { name: 'Prepare your studio' })).toBeVisible();
  await page.getByRole('button', { name: 'Close setup checklist' }).click();
  await page.getByRole('button', { name: /Dark mode|Light mode/ }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
});

test('sessions page launches creation and workspace source picker exposes every source mode', async ({ page }) => {
  await signIn(page);
  await page.getByRole('link', { name: 'Sessions' }).click();
  await page.getByRole('button', { name: 'Add session' }).click();
  await expect(page.getByRole('heading', { name: 'What would you like to make?' })).toBeVisible();
  await page.getByRole('button', { name: 'Close', exact: true }).click();

  const authStatus = await page.request.get('/api/v1/auth/status');
  const csrfToken = (await authStatus.json()).csrf_token;
  const created = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': csrfToken },
    data: { name: uniqueName('Source picker regression'), workflow_kind: 'audiobook' }
  });
  expect(created.ok()).toBeTruthy();
  const session = await created.json();
  await page.goto(`/sessions/${session.id}`);

  await page.getByRole('button', { name: 'Add source' }).click();
  await expect(page.getByRole('button', { name: 'Upload' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Paste text' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Public URL' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Source library' })).toBeVisible();
  await page.getByRole('button', { name: 'Paste text' }).click();
  await page.getByLabel('Source name').fill('Pasted source');
  await page.getByLabel('Text').fill('This source was pasted directly into an existing session.');
  await page.getByRole('button', { name: 'Add and select' }).click();
  await expect(page.getByText('Source added and selected as the current input.')).toBeVisible();
  const attached = await page.request.get(`/api/v1/sessions/${session.id}/sources`);
  expect((await attached.json()).items).toHaveLength(1);
});

test('voiceover output settings follow the video source and default to a controlled mix', async ({ page }) => {
  await signIn(page);
  const authStatus = await page.request.get('/api/v1/auth/status');
  const csrfToken = (await authStatus.json()).csrf_token;
  const headers = { 'X-CSRF-Token': csrfToken };
  const created = await page.request.post('/api/v1/sessions', {
    headers,
    data: { name: uniqueName('Video output profile'), workflow_kind: 'voiceover' }
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
        buffer: (globalThis as any).Buffer.from('media fixture')
      }
    }
  });
  expect(uploaded.ok()).toBeTruthy();

  await page.goto(`/sessions/${session.id}/output`);
  await expect(page.getByRole('heading', { name: 'Video output', exact: true })).toBeVisible();
  await expect(page.getByLabel('Audio result')).toHaveValue('mixed');
  await expect(page.getByText('Soundtrack mix')).toBeVisible();
  await expect(page.getByLabel('Source level (dB)')).toHaveValue('0');
  await expect(page.getByLabel('Maximum start delay (ms)')).toHaveValue('2000');
  await expect(page.getByLabel('Album / series')).toHaveCount(0);
  await expect(page.getByLabel('Genre')).toHaveCount(0);
  await expect(page.getByText('Cover artwork')).toHaveCount(0);
});

test('reading mode flows segments together and separates only saved paragraphs', async ({ page }) => {
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

  const segmentRects = await paragraphs.nth(0).locator('.reading-sentence').evaluateAll((segments) =>
    segments.map((segment) => ({
      display: getComputedStyle(segment).display,
      rects: Array.from(segment.getClientRects(), ({ x, y, width }) => ({ x, y, width }))
    }))
  );
  expect(segmentRects[0].display).toBe('inline');
  expect(segmentRects[1].rects).toHaveLength(2);
  expect(segmentRects[1].rects[0].x).toBeGreaterThan(segmentRects[0].rects[0].x + 100);
});

test('generation segments support Ctrl and Shift multi-selection in both review views', async ({ page }) => {
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
  await rows.nth(2).locator('td').nth(1).click({ modifiers: ['Control'] });
  await expect(page.locator('tbody tr.selected')).toHaveCount(2);
  await rows.nth(3).locator('td').nth(1).click({ modifiers: ['Shift'] });
  await expect(rows.nth(0)).not.toHaveClass(/selected/);
  await expect(rows.nth(1)).not.toHaveClass(/selected/);
  await expect(rows.nth(2)).toHaveClass(/selected/);
  await expect(rows.nth(3)).toHaveClass(/selected/);
  await page.getByRole('button', { name: /RVC speech-to-speech settings/ }).click();
  await expect(page.getByRole('button', { name: 'RVC selected (2)' })).toBeVisible();

  await page.getByRole('button', { name: 'Reading', exact: true }).click();
  const sentences = page.locator('.reading-sentence');
  const heading = page.locator('.reading-heading button');
  await heading.click();
  await sentences.nth(0).click({ modifiers: ['Control'] });
  await expect(page.locator('.reading-heading.selected-heading')).toHaveCount(1);
  await expect(page.locator('.reading-segment.selected-sentence')).toHaveCount(1);
  await sentences.nth(2).click({ modifiers: ['Shift'] });
  await expect(page.locator('.reading-heading.selected-heading')).toHaveCount(0);
  await expect(page.locator('.reading-segment.selected-sentence')).toHaveCount(3);
  await sentences.nth(0).click({ modifiers: ['Control'] });
  await expect(page.locator('.reading-segment.selected-sentence')).toHaveCount(2);
  await expect(page.getByRole('button', { name: 'RVC selected (2)' })).toBeVisible();
});

test('editorial workspace visual smoke', async ({ page }) => {
  const isWindows = await page.evaluate(() => navigator.userAgent.includes('Windows'));
  test.skip(isWindows, 'The visual baseline is captured on Linux to avoid platform font-metric differences.');
  await signIn(page);
  await expect(page).toHaveScreenshot('workspace.png', {
    fullPage: true,
    animations: 'disabled',
    maxDiffPixelRatio: 0.12
  });
});

test('voice recording can be previewed, normalized, saved, and played', async ({ page, browserName }) => {
  test.skip(browserName !== 'chromium', 'Chromium provides a deterministic fake microphone for this media integration test.');
  const voiceName = uniqueName('Browser recorder');
  await signIn(page);
  await page.getByRole('link', { name: 'Voices' }).click();
  await page.getByLabel('New voice name').fill(voiceName);
  await page.getByRole('button', { name: 'Add voice' }).click();
  await expect(page.getByRole('heading', { name: voiceName })).toBeVisible();

  await page.getByRole('button', { name: 'Enable microphone' }).click();
  await expect(page.getByRole('button', { name: 'Record', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'Record', exact: true }).click();
  await page.waitForTimeout(1_000);
  await page.getByRole('button', { name: /Stop ·/ }).click();
  await expect(page.getByRole('button', { name: 'Play recording' })).toBeVisible();

  await page.getByRole('button', { name: 'Play recording' }).click();
  await expect(page.getByRole('button', { name: 'Stop recording playback' })).toBeVisible();
  await page.getByRole('button', { name: 'Stop recording playback' }).click();
  await page.getByRole('button', { name: 'Save sample' }).click();

  await expect(page.getByRole('button', { name: 'Play sample' })).toBeVisible({ timeout: 20_000 });
  await page.getByRole('button', { name: 'Play sample' }).click();
  await expect(page.getByRole('button', { name: 'Stop sample playback' })).toBeVisible();
  await expect(page.getByRole('alert')).toHaveCount(0);
});
