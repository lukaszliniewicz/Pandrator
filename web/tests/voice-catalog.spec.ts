import { expect, test, type Page } from '@playwright/test';

async function seed(page: Page) {
  await page.goto('/voices');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(
    page.getByRole('heading', { name: 'Voice library', exact: true })
  ).toBeVisible();
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const headers = { 'X-CSRF-Token': auth.csrf_token };
  const name = `Scrooge ${crypto.randomUUID()}`;
  const response = await page.request.post('/api/v1/voices', {
    headers,
    data: {
      name,
      voice_category: 'male',
      language: 'en',
      description: 'A low, warm character voice.',
      profile: {
        pitch: 'low',
        textures: ['warm'],
        languages: [
          {
            language: 'en',
            accent: 'Scottish',
            evidence: { source: 'user', status: 'described' }
          }
        ]
      }
    }
  });
  expect(response.ok()).toBeTruthy();
  return { name, voice: await response.json() };
}

test('catalog profiles, dirty-state protection and collections persist', async ({
  page
}) => {
  const { name, voice } = await seed(page);
  await page.getByLabel('Search voices', { exact: true }).fill(name);
  await page.getByRole('button', { name, exact: true }).click();
  await page.getByRole('button', { name: 'Edit profile', exact: true }).click();
  await page
    .getByLabel('Description', { exact: true })
    .fill('Warm Scottish narration and dialogue.');
  await page
    .getByRole('button', { name: 'Back to voices', exact: true })
    .click();
  const guard = page.getByRole('dialog', {
    name: 'Keep your profile changes?'
  });
  await expect(guard).toBeVisible();
  await guard.getByRole('button', { name: 'Keep editing' }).click();
  await page.getByRole('button', { name: 'Save profile', exact: true }).click();
  await expect(
    page.getByText('Voice profile saved.', { exact: true })
  ).toBeVisible();
  await page
    .getByRole('button', { name: 'Back to voices', exact: true })
    .click();
  await expect(page.getByLabel('Search voices')).toHaveValue(name);
  await page
    .getByRole('button', { name: 'Create collection', exact: true })
    .click();
  const group = `Christmas Carol ${crypto.randomUUID()}`;
  await page.getByLabel('Collection name', { exact: true }).fill(group);
  await page
    .locator('form')
    .getByRole('button', { name: 'Create collection', exact: true })
    .click();
  await expect(
    page.getByRole('heading', { name: 'No voices match yet' })
  ).toBeVisible();
  await page
    .getByRole('combobox', { name: 'Collection', exact: true })
    .selectOption('');
  await page.getByRole('button', { name, exact: true }).click();
  await page.getByRole('checkbox', { name: group, exact: true }).check();
  await page
    .getByRole('button', { name: 'Back to voices', exact: true })
    .click();
  await page
    .getByRole('combobox', { name: 'Collection', exact: true })
    .selectOption({ label: `${group} (1)` });
  await expect(page.getByRole('button', { name, exact: true })).toBeVisible();
  const result = await (
    await page.request.get(`/api/v1/voice-catalog?query=${voice.id}`)
  ).json();
  expect(result.items[0].description).toBe(
    'Warm Scottish narration and dialogue.'
  );
  expect(result.items[0].collections[0].name).toBe(group);
});

test('phone filters are usable without horizontal overflow and restore focus', async ({
  page
}) => {
  const { name } = await seed(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByLabel('Search voices').fill(name);
  await expect(page.getByRole('button', { name, exact: true })).toBeVisible();
  const collection = await page
    .getByRole('combobox', { name: 'Collection', exact: true })
    .boundingBox();
  expect(collection?.width).toBeGreaterThan(140);
  await page.getByRole('button', { name: 'Filters', exact: true }).click();
  const drawer = page.getByRole('dialog', { name: 'Voice filters' });
  await drawer.getByLabel('Accent', { exact: true }).fill('Scottish');
  await drawer
    .getByRole('button', { name: 'Show 1 voices', exact: true })
    .click();
  await expect(drawer).toBeHidden();
  await expect(
    page.getByRole('button', { name: 'Filters (1)', exact: true })
  ).toBeFocused();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth
    )
  ).toBeTruthy();
  await page.getByRole('button', { name, exact: true }).click();
  await expect(page.getByRole('heading', { name, exact: true })).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth
    )
  ).toBeTruthy();
  await page
    .getByRole('button', { name: 'Back to voices', exact: true })
    .click();
  await page
    .getByRole('button', { name: 'Add reference', exact: true })
    .click();
  await expect(
    page.getByRole('heading', { name: 'Add a reference voice' })
  ).toBeVisible();
  await expect(
    page.getByRole('button', { name: 'Pre-built voices', exact: true })
  ).toBeHidden();
  const addButton = await page
    .getByRole('button', { name: 'Add voice', exact: true })
    .boundingBox();
  expect(addButton).not.toBeNull();
  expect(addButton!.x + addButton!.width).toBeLessThanOrEqual(390);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth
    )
  ).toBeTruthy();
});
