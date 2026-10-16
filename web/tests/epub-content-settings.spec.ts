import { expect, test, type Page } from '@playwright/test';

async function createProject(page: Page, workflow: 'audiobook' | 'voiceover') {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(
    page.getByRole('heading', { name: 'Create a project', exact: true })
  ).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const response = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': auth.csrf_token },
    data: {
      name: `EPUB content ${crypto.randomUUID()}`,
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

async function openBookSettings(page: Page) {
  const card = page
    .getByRole('heading', { name: 'Clean source', exact: true })
    .locator('xpath=ancestor::article');
  const disclosure = card.getByTestId('stage-disclosure-toggle');
  if (
    (await disclosure.count()) &&
    (await disclosure.getAttribute('aria-expanded')) === 'false'
  )
    await disclosure.click();
  await card.getByRole('button', { name: 'Settings', exact: true }).click();
  const dialog = page.getByRole('dialog', {
    name: 'Clean source',
    exact: true
  });
  await expect(
    dialog.getByRole('button', { name: 'Save settings', exact: true })
  ).toBeEnabled();
  return dialog;
}

test('EPUB content defaults and saved choices survive reopening', async ({
  page
}) => {
  const id = await createProject(page, 'audiobook');
  let dialog = await openBookSettings(page);
  await expect(
    dialog.getByRole('group', { name: 'EPUB tables and illustrations' })
  ).toBeVisible();
  const tables = dialog.getByRole('combobox', { name: /^Tables/ });
  const images = dialog.getByRole('combobox', { name: /^Illustrations/ });
  await expect(tables).toHaveValue('headers');
  await expect(images).toHaveValue('descriptions');
  await tables.selectOption('rows');
  await images.selectOption('captions');
  await dialog
    .getByRole('button', { name: 'Save settings', exact: true })
    .click();
  await expect(dialog).not.toBeVisible();

  const settings = await (
    await page.request.get(`/api/v1/sessions/${id}/settings/source_cleaning`)
  ).json();
  expect(settings.effective.epub_table_mode).toBe('rows');
  expect(settings.effective.epub_image_mode).toBe('captions');
  expect(settings.effective.agentic).toBe(false);

  await page.reload();
  dialog = await openBookSettings(page);
  await expect(dialog.getByRole('combobox', { name: /^Tables/ })).toHaveValue(
    'rows'
  );
  await expect(
    dialog.getByRole('combobox', { name: /^Illustrations/ })
  ).toHaveValue('captions');
});

test('EPUB controls fit a narrow viewport and remain keyboard accessible', async ({
  page
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await createProject(page, 'audiobook');
  const dialog = await openBookSettings(page);
  const tables = dialog.getByRole('combobox', { name: /^Tables/ });
  await tables.scrollIntoViewIfNeeded();
  await tables.focus();
  await expect(tables).toBeFocused();
  const imageHelp = dialog.getByRole('button', { name: 'About Illustrations' });
  await expect(imageHelp).toBeVisible();
  await page.keyboard.press('Tab');
  await expect(imageHelp).toBeFocused();
  const images = dialog.getByRole('combobox', { name: /^Illustrations/ });
  await images.focus();
  await expect(images).toBeFocused();
  const bounds = await images.boundingBox();
  expect(bounds).not.toBeNull();
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390);
  await page.screenshot({
    path: testInfo.outputPath('epub-settings-mobile.png')
  });
  const save = dialog.getByRole('button', {
    name: 'Save settings',
    exact: true
  });
  await save.scrollIntoViewIfNeeded();
  await expect(save).toBeInViewport();
});

test('voiceover keeps its own workflow without EPUB controls', async ({
  page
}) => {
  await createProject(page, 'voiceover');
  await expect(
    page.getByRole('heading', { name: 'Clean source', exact: true })
  ).toHaveCount(0);
  await expect(
    page.getByRole('group', { name: 'EPUB tables and illustrations' })
  ).toHaveCount(0);
});
