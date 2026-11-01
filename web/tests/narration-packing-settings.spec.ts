import { expect, test, type Page } from '@playwright/test';

async function createProject(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(
    page.getByRole('heading', { name: 'Create a project', exact: true })
  ).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const created = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': auth.csrf_token },
    data: {
      name: `Narration packing ${crypto.randomUUID()}`,
      interface_mode: 'advanced',
      workflow_kind: 'audiobook',
      source_language: 'en'
    }
  });
  expect(created.ok()).toBeTruthy();
  const { id } = await created.json();
  const tts = await (
    await page.request.get(`/api/v1/sessions/${id}/settings/tts`)
  ).json();
  const selected = await page.request.put(
    `/api/v1/sessions/${id}/settings/tts`,
    {
      headers: {
        'X-CSRF-Token': auth.csrf_token,
        'If-Match': `"${tts.revision}"`
      },
      data: {
        value: { service: 'audio_cpp', model: 'qwen3_tts_1_7b_base_q8_0' }
      }
    }
  );
  expect(selected.ok()).toBeTruthy();
  await page.goto(`/sessions/${id}`);
  await page.getByTestId('narration-settings').locator('summary').click();
  return id as string;
}

async function openPacking(page: Page) {
  const card = page
    .getByRole('heading', { name: 'Segment narration', exact: true })
    .locator('xpath=ancestor::article');
  const toggle = card.getByTestId('stage-disclosure-toggle');
  if (
    (await toggle.count()) &&
    (await toggle.getAttribute('aria-expanded')) === 'false'
  )
    await toggle.click();
  await card.getByRole('button', { name: 'Settings', exact: true }).click();
  const dialog = page.getByRole('dialog', {
    name: 'Segment narration',
    exact: true
  });
  await expect(
    dialog.getByRole('button', { name: 'Save settings', exact: true })
  ).toBeEnabled();
  return dialog;
}

test('packing choices persist through stage and speech-plan settings', async ({
  page
}) => {
  const id = await createProject(page);
  const narration = page.getByRole('group', {
    name: 'Narration length',
    exact: true
  });
  await expect(
    narration.getByRole('spinbutton', { name: 'Model default (characters)' })
  ).toHaveValue('350');
  let dialog = await openPacking(page);
  const packing = dialog.getByRole('group', {
    name: 'Sentence packing',
    exact: true
  });
  await expect(
    packing.getByRole('combobox', { name: /^Packing mode/ })
  ).toHaveValue('auto');
  const threshold = packing.getByRole('spinbutton', {
    name: /^Whole-sentence threshold/
  });
  await expect(threshold).toHaveValue('300');
  await threshold.fill('450');
  await packing
    .getByRole('combobox', { name: /^Packing mode/ })
    .selectOption('clauses');
  await dialog
    .getByRole('button', { name: 'Save settings', exact: true })
    .click();
  await expect(dialog).not.toBeVisible();
  let stored = await (
    await page.request.get(`/api/v1/sessions/${id}/settings/text`)
  ).json();
  expect(stored.effective.audiobook_sentence_packing).toBe('clauses');
  expect(stored.effective.audiobook_sentence_packing_threshold).toBe(450);

  await expect(
    narration.getByRole('combobox', { name: /^Packing mode/ })
  ).toHaveValue('clauses');
  await expect(
    narration.getByRole('spinbutton', { name: /^Whole-sentence threshold/ })
  ).toHaveValue('450');
  await narration
    .getByRole('combobox', { name: /^Packing mode/ })
    .selectOption('auto');
  await narration
    .getByRole('spinbutton', { name: /^Whole-sentence threshold/ })
    .fill('325');
  await narration
    .getByRole('combobox', { name: /^Packing mode/ })
    .selectOption('sentences');
  await narration
    .getByRole('button', { name: 'Save narration settings' })
    .click();
  await expect(
    page.getByRole('status').filter({ hasText: 'Saved. Run Segment narration' })
  ).toBeVisible();
  stored = await (
    await page.request.get(`/api/v1/sessions/${id}/settings/text`)
  ).json();
  expect(stored.effective.audiobook_sentence_packing).toBe('sentences');
  expect(stored.effective.audiobook_sentence_packing_threshold).toBe(325);
  await page.reload();
  dialog = await openPacking(page);
  await expect(
    dialog.getByRole('combobox', { name: /^Packing mode/ })
  ).toHaveValue('sentences');
  await expect(
    dialog.getByRole('spinbutton', { name: /^Whole-sentence threshold/ })
  ).toHaveValue('325');
});

test('threshold validation and controls work on a narrow screen', async ({
  page
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await createProject(page);
  const dialog = await openPacking(page);
  const threshold = dialog.getByRole('spinbutton', {
    name: /^Whole-sentence threshold/
  });
  const save = dialog.getByRole('button', {
    name: 'Save settings',
    exact: true
  });
  await threshold.fill('');
  await expect(save).toBeDisabled();
  await expect(dialog.getByRole('alert')).toContainText('between 1 and 8,192');
  await threshold.fill('0');
  await expect(save).toBeDisabled();
  await threshold.fill('350');
  await expect(save).toBeEnabled();
  await threshold.focus();
  await expect(threshold).toBeFocused();
  await dialog
    .getByRole('group', { name: 'Sentence packing', exact: true })
    .scrollIntoViewIfNeeded();
  const bounds = await threshold.boundingBox();
  expect(bounds).not.toBeNull();
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390);
  await page.screenshot({
    path: testInfo.outputPath('narration-packing-mobile.png')
  });
  await save.scrollIntoViewIfNeeded();
  await expect(save).toBeInViewport();
  await save.click();
  await expect(dialog).not.toBeVisible();
});
