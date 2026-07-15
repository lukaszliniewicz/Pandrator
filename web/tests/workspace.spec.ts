import { expect, test, type Page } from '@playwright/test';

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
    data: { name: 'Reading mode regression', workflow_kind: 'audiobook' }
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
  await signIn(page);
  await expect(page.getByRole('heading', { name: 'What shall we make?' })).toBeVisible();
  await page.getByRole('button', { name: /Create subtitles/ }).first().click();
  await page.getByRole('button', { name: 'Add later' }).click();
  await page.getByRole('button', { name: 'Continue' }).click();
  await page.getByRole('button', { name: 'Review', exact: true }).click();
  await page.getByLabel('Session name').fill('Playwright subtitles');
  await page.getByRole('button', { name: 'Create workspace' }).click();
  await expect(page.getByRole('heading', { name: 'Playwright subtitles' })).toBeVisible();
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

test('editorial workspace visual baseline', async ({ page }) => {
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
  await signIn(page);
  await page.getByRole('link', { name: 'Voices' }).click();
  await page.getByLabel('New voice name').fill('Browser recorder');
  await page.getByRole('button', { name: 'Add voice' }).click();
  await expect(page.getByRole('heading', { name: 'Browser recorder' })).toBeVisible();

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
