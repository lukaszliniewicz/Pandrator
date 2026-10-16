import { expect, test, type Page } from '@playwright/test';

async function createProject(page: Page, workflow: 'audiobook' | 'voiceover') {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const response = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': auth.csrf_token },
    data: {
      name: `Dialogue flow ${crypto.randomUUID()}`,
      interface_mode: 'advanced',
      workflow_kind: workflow,
      source_language: 'en'
    }
  });
  expect(response.ok()).toBeTruthy();
  const { id } = await response.json();
  await page.goto(`/sessions/${id}`);
  return id as string;
}

async function openStageSettings(page: Page, title: string) {
  const card = page
    .getByRole('heading', { name: title, exact: true })
    .locator('xpath=ancestor::article');
  const disclosure = card.getByTestId('stage-disclosure-toggle');
  if (
    (await disclosure.count()) &&
    (await disclosure.getAttribute('aria-expanded')) === 'false'
  )
    await disclosure.click();
  await card
    .getByRole('button', { name: /^(Settings|Timing & settings)$/ })
    .click();
  const dialog = page.getByRole('dialog', { name: title, exact: true });
  await expect(dialog).toHaveAttribute('aria-busy', 'false');
  return dialog;
}

test('audiobook dialogue settings are reachable, persist, and keep annotation separate from casting', async ({
  page
}) => {
  const id = await createProject(page, 'audiobook');
  let dialog = await openStageSettings(page, 'Segment narration');
  await expect(dialog.getByLabel('Dialogue flow')).toHaveValue('auto');
  await dialog.getByLabel('Dialogue flow').selectOption('off');
  await dialog
    .getByRole('button', { name: 'Save settings', exact: true })
    .click();
  await expect(dialog).not.toBeVisible();
  dialog = await openStageSettings(page, 'Segment narration');
  await expect(dialog.getByLabel('Dialogue flow')).toHaveValue('off');
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();

  const optimization = page
    .getByRole('heading', { name: 'Optimize text for speech', exact: true })
    .locator('xpath=ancestor::article');
  await optimization
    .getByRole('checkbox', { name: 'Disabled', exact: true })
    .check();
  await expect(
    optimization.getByRole('checkbox', { name: 'Enabled', exact: true })
  ).toBeChecked();
  dialog = await openStageSettings(page, 'Optimize text for speech');
  await expect(
    dialog.getByText('Optional AI dialogue recognition', { exact: true })
  ).toBeVisible();
  await expect(dialog.getByLabel('Annotation level')).toHaveValue('off');
  await dialog.getByLabel('Annotation level').selectOption('dialogue');
  await expect(
    dialog.getByLabel('Annotate only', { exact: false })
  ).toBeChecked();
  await expect(dialog.getByLabel('Speech-planning policy')).toBeDisabled();
  await dialog
    .getByRole('button', { name: 'Save settings', exact: true })
    .click();
  await expect(dialog).not.toBeVisible();

  const text = await (
    await page.request.get(`/api/v1/sessions/${id}/settings/text`)
  ).json();
  expect(text.effective.audiobook_dialogue_flow).toBe('off');
  expect(text.effective.llm_tts_annotation_mode).toBe('dialogue');
  expect(text.effective.llm_tts_annotation_only).toBe(true);
  expect(text.effective.llm_tts_document_optimization).toBe(true);
  const tts = await (
    await page.request.get(`/api/v1/sessions/${id}/settings/tts`)
  ).json();
  expect(Boolean(tts.effective.casting_enabled)).toBe(false);

  await page.setViewportSize({ width: 390, height: 844 });
  dialog = await openStageSettings(page, 'Optimize text for speech');
  await expect(dialog.getByLabel('Annotation level')).toHaveValue('dialogue');
  await dialog.getByLabel('Annotation level').scrollIntoViewIfNeeded();
  await page.screenshot({
    path: '/tmp/pandrator-dialogue-implementation/checks/dialogue-settings-mobile.png'
  });
  const save = dialog.getByRole('button', {
    name: 'Save settings',
    exact: true
  });
  await save.scrollIntoViewIfNeeded();
  await expect(save).toBeInViewport();
});

test('voiceover does not expose audiobook paragraph grouping or new dialogue controls', async ({
  page
}) => {
  await createProject(page, 'voiceover');
  await expect(
    page.getByRole('heading', { name: 'Segment narration', exact: true })
  ).toHaveCount(0);
  const dialog = await openStageSettings(page, 'Optimize text for speech');
  await expect(dialog.getByLabel('Dialogue flow')).toHaveCount(0);
  await expect(
    dialog.getByText('Optional AI dialogue recognition', { exact: true })
  ).toHaveCount(0);
});
