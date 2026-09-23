import { expect, test, type Page } from '@playwright/test';

async function signIn(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const closeTour = page.getByRole('button', { name: 'Close tour' });
  if (await closeTour.isVisible()) await closeTour.click();
}

async function csrf(page: Page) {
  const status = await page.request.get('/api/v1/auth/status');
  expect(status.ok()).toBeTruthy();
  return (await status.json()).csrf_token as string;
}

async function createSession(
  page: Page,
  kind: 'audiobook' | 'voiceover',
  prefix: string
) {
  const token = await csrf(page);
  const response = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': token },
    data: { name: `${prefix} ${crypto.randomUUID()}`, workflow_kind: kind }
  });
  expect(response.ok()).toBeTruthy();
  return (await response.json()).id as string;
}

async function createPlan(
  page: Page,
  sessionId: string,
  segments: Array<{ text: string }>
) {
  const token = await csrf(page);
  const response = await page.request.post(
    `/api/v1/sessions/${sessionId}/generation-plan`,
    { headers: { 'X-CSRF-Token': token }, data: { segments } }
  );
  expect(response.ok()).toBeTruthy();
}

async function setOptimizedText(
  page: Page,
  sessionId: string,
  ordinal: number,
  optimizedText: string
) {
  const token = await csrf(page);
  const listed = await page.request.get(
    `/api/v1/sessions/${sessionId}/generation-segments?limit=250`
  );
  expect(listed.ok()).toBeTruthy();
  const items = (await listed.json()).items as Array<{
    id: string;
    ordinal: number;
    revision: number;
  }>;
  const target = items.find((item) => item.ordinal === ordinal);
  expect(target).toBeTruthy();
  const patched = await page.request.patch(
    `/api/v1/sessions/${sessionId}/generation-segments`,
    {
      headers: { 'X-CSRF-Token': token },
      data: {
        updates: [
          {
            id: target!.id,
            revision: target!.revision,
            changes: { optimized_text: optimizedText }
          }
        ]
      }
    }
  );
  expect(patched.ok()).toBeTruthy();
}

async function segmentTexts(page: Page, sessionId: string) {
  const listed = await page.request.get(
    `/api/v1/sessions/${sessionId}/generation-segments?limit=250`
  );
  expect(listed.ok()).toBeTruthy();
  return (await listed.json()).items as Array<{
    ordinal: number;
    text: string;
    optimized_text: string | null;
  }>;
}

async function openDrawer(page: Page, sessionId: string) {
  await page.goto(`/sessions/${sessionId}`);
  await page.getByRole('button', { name: 'Generation', exact: true }).click();
  await expect(
    page.getByRole('button', { name: 'Search and replace' })
  ).toBeVisible();
}

test('audiobook search hides behind a toggle and keeps single-text behavior', async ({
  page
}) => {
  await signIn(page);
  const sessionId = await createSession(page, 'audiobook', 'Search toggle');
  await createPlan(page, sessionId, [
    { text: 'A cat waits.' },
    { text: 'A catfish and cat.' }
  ]);
  await openDrawer(page, sessionId);

  const toggle = page.getByRole('button', { name: 'Search and replace' });
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  await expect(page.getByLabel('Find in generation segments')).toBeHidden();
  await page.screenshot({ path: test.info().outputPath('default-hidden.png') });

  await toggle.click();
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  const find = page.getByLabel('Find in generation segments');
  await expect(find).toBeVisible();
  await expect(find).toBeFocused();
  await expect(page.getByRole('radio', { name: 'Cue text' })).toHaveCount(0);
  await expect(page.getByRole('radio', { name: 'TTS text' })).toHaveCount(0);

  await find.fill('cat');
  await page.getByLabel('Replace in generation segments').fill('dog');
  await page.getByRole('button', { name: 'Match whole word' }).click();
  await expect(page.getByText('1 / 2', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Replace all' }).click();
  const fields = page.locator('textarea[data-generation-search-index]');
  await expect(fields.nth(0)).toHaveValue('A dog waits.');
  await expect(fields.nth(1)).toHaveValue('A catfish and dog.');
  await page.screenshot({
    path: test.info().outputPath('audiobook-no-scope.png')
  });

  await toggle.click();
  await expect(find).toBeHidden();
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  await expect(fields).toHaveCount(2);
});

test('voiceover scope selector targets cue text or TTS text only', async ({
  page
}) => {
  await signIn(page);
  const sessionId = await createSession(page, 'voiceover', 'Search scope');
  await createPlan(page, sessionId, [
    { text: 'Cueword opens the scene.' },
    { text: 'A plain second cue.' }
  ]);
  await setOptimizedText(page, sessionId, 0, 'Spokenword covers the scene.');
  await openDrawer(page, sessionId);

  await page.getByRole('button', { name: 'Search and replace' }).click();
  const cueRadio = page.getByRole('radio', { name: 'Cue text' });
  const ttsRadio = page.getByRole('radio', { name: 'TTS text' });
  await expect(cueRadio).toBeVisible();
  await expect(ttsRadio).toBeVisible();
  await expect(cueRadio).toHaveAttribute('aria-checked', 'true');

  const find = page.getByLabel('Find in generation segments');
  await find.fill('spokenword');
  await expect(page.getByText('No matches')).toBeVisible();

  await ttsRadio.click();
  await expect(page.getByText('1 / 1', { exact: true })).toBeVisible();
  await page.getByLabel('Replace in generation segments').fill('VOICEWORD');
  await page.getByRole('button', { name: 'Replace', exact: true }).click();
  await expect(page.getByText('No matches')).toBeVisible();

  let saved = await segmentTexts(page, sessionId);
  expect(saved.find((item) => item.ordinal === 0)?.optimized_text).toContain(
    'VOICEWORD'
  );
  expect(saved.find((item) => item.ordinal === 0)?.text).toBe(
    'Cueword opens the scene.'
  );

  await cueRadio.click();
  await expect(cueRadio).toHaveAttribute('aria-checked', 'true');
  await find.fill('cueword');
  await expect(page.getByText('1 / 1', { exact: true })).toBeVisible();
  await page.getByLabel('Replace in generation segments').fill('CUEWORD');
  await page.getByRole('button', { name: 'Replace', exact: true }).click();
  await page.screenshot({
    path: test.info().outputPath('voiceover-scope-replace.png')
  });

  saved = await segmentTexts(page, sessionId);
  expect(saved.find((item) => item.ordinal === 0)?.text).toContain('CUEWORD');
  // Cue-scope replacement must preserve the explicitly authored TTS text.
  expect(saved.find((item) => item.ordinal === 0)?.optimized_text).toContain(
    'VOICEWORD'
  );
});

test('Escape closes search and lifts the list filter', async ({ page }) => {
  await signIn(page);
  const sessionId = await createSession(page, 'audiobook', 'Search escape');
  await createPlan(page, sessionId, [
    { text: 'A cat naps.' },
    { text: 'A dog runs.' },
    { text: 'A bird sings.' }
  ]);
  await openDrawer(page, sessionId);

  await page.getByRole('button', { name: 'Search and replace' }).click();
  await page.getByLabel('Find in generation segments').fill('cat');
  await expect(page.getByText('1 / 1', { exact: true })).toBeVisible();
  await expect(page.locator('tbody tr[data-segment-id]')).toHaveCount(1);

  await page.getByLabel('Find in generation segments').press('Escape');
  await expect(page.getByLabel('Find in generation segments')).toBeHidden();
  await expect(page.locator('tbody tr[data-segment-id]')).toHaveCount(3);
});
