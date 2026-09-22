import { expect, test, type Page } from '@playwright/test';

async function signIn(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.locator('.app-shell')).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
}

async function createAudiobookSession(page: Page) {
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const response = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': auth.csrf_token },
    data: {
      name: `Cast disclosure ${crypto.randomUUID()}`,
      workflow_kind: 'audiobook'
    }
  });
  expect(response.ok()).toBeTruthy();
  return (await response.json()).id as string;
}

async function openSessionMultiVoice(page: Page, id: string) {
  await page.goto(`/sessions/${id}`);
  const card = page.getByRole('region', { name: 'Audiobook voices' });
  await expect(card).toBeVisible();
  await card.getByRole('radio', { name: /Multiple voices/ }).check();
  return card;
}

test('multi-voice cast details stay collapsed until opened and keep drafts across refresh', async ({
  page
}) => {
  await signIn(page);
  const id = await createAudiobookSession(page);
  const card = await openSessionMultiVoice(page, id);
  const disclosure = card.getByTestId('cast-disclosure');
  const summary = card.getByTestId('cast-summary');

  await expect(summary).toContainText('Characters and cast');
  await expect
    .poll(() =>
      disclosure.evaluate((node) => (node as HTMLDetailsElement).open)
    )
    .toBe(false);
  await expect(
    card.getByRole('button', { name: 'Add character', exact: true })
  ).toBeHidden();
  await expect
    .poll(() =>
      card
        .getByTestId('cast-help')
        .evaluate((node) => (node as HTMLDetailsElement).open)
    )
    .toBe(false);

  await summary.click();
  await expect
    .poll(() =>
      disclosure.evaluate((node) => (node as HTMLDetailsElement).open)
    )
    .toBe(true);
  const add = card.getByRole('button', { name: 'Add character', exact: true });
  await expect(add).toBeVisible();
  await add.click();
  const character = card.locator('article').first();
  await character.getByLabel('Name', { exact: true }).fill('Alice');
  await expect(summary).toContainText('unsaved changes');
  await expect(summary).toContainText('1 uses a default voice');
  await expect(
    card.getByText(
      'Save or discard the cast changes before switching voice mode.'
    )
  ).toBeVisible();

  await card.getByRole('button', { name: 'Refresh cast', exact: true }).click();
  await expect
    .poll(() =>
      disclosure.evaluate((node) => (node as HTMLDetailsElement).open)
    )
    .toBe(true);
  await expect(character.getByLabel('Name', { exact: true })).toHaveValue(
    'Alice'
  );

  await summary.click();
  await expect
    .poll(() =>
      disclosure.evaluate((node) => (node as HTMLDetailsElement).open)
    )
    .toBe(false);
  const hint = card.getByTestId('cast-collapsed-hint');
  await expect(hint).toContainText('Unsaved character or cast changes');
  await expect(
    hint.getByRole('button', { name: 'Review cast changes' })
  ).toBeVisible();
  await expect(
    card.getByText(
      'Save or discard the cast changes before switching voice mode.'
    )
  ).toBeVisible();

  await hint.getByRole('button', { name: 'Review cast changes' }).click();
  await expect
    .poll(() =>
      disclosure.evaluate((node) => (node as HTMLDetailsElement).open)
    )
    .toBe(true);
  await card
    .getByRole('button', { name: 'Save characters and cast', exact: true })
    .click();
  await expect(
    card.getByText('Characters and cast saved.', { exact: false })
  ).toBeVisible();
  await expect(summary).not.toContainText('unsaved changes');

  await summary.click();
  await expect
    .poll(() =>
      disclosure.evaluate((node) => (node as HTMLDetailsElement).open)
    )
    .toBe(false);
  await expect(card.getByTestId('cast-collapsed-hint')).toHaveCount(0);
  await expect(summary).toContainText('1 uses a default voice');
  await page.reload();
  await expect(summary).toContainText('1 uses a default voice');
  await expect(disclosure).not.toHaveAttribute('open');
  // The dedicated editing page stays expanded; its preference must not
  // leak back to the compact overview.
  await page.goto(`/sessions/${id}/voice`);
  await expect(page.getByTestId('cast-disclosure')).toHaveAttribute('open');
  await page.goto(`/sessions/${id}`);
  await expect(disclosure).not.toHaveAttribute('open');
});

test('cast load errors stay visible while collapsed and retry succeeds', async ({
  page
}) => {
  await signIn(page);
  const id = await createAudiobookSession(page);
  const controls = `**/api/v1/sessions/${id}/generation-controls`;
  await page.route(controls, (route) =>
    route.fulfill({
      status: 500,
      json: {
        error: { code: 'cast_unavailable', message: 'Cast unavailable for now' }
      }
    })
  );
  await page.goto(`/sessions/${id}`);
  const card = page.getByRole('region', { name: 'Audiobook voices' });
  await expect(card).toBeVisible();
  await card.getByRole('radio', { name: /Multiple voices/ }).check();
  const disclosure = card.getByTestId('cast-disclosure');
  await expect
    .poll(() =>
      disclosure.evaluate((node) => (node as HTMLDetailsElement).open)
    )
    .toBe(false);
  const alert = card.getByTestId('cast-error');
  await expect(alert).toContainText('Cast unavailable for now');
  await expect(
    card.getByRole('button', { name: 'Retry loading cast', exact: true })
  ).toBeVisible();

  await page.unroute(controls);
  await card
    .getByRole('button', { name: 'Retry loading cast', exact: true })
    .click();
  await expect(alert).toHaveCount(0);
  await expect(card.getByTestId('cast-summary')).toContainText('0 characters');
  await expect
    .poll(() =>
      disclosure.evaluate((node) => (node as HTMLDetailsElement).open)
    )
    .toBe(false);
});

test('unsaved cast draft guards navigation and keeps warnings visible', async ({
  page
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page);
  const id = await createAudiobookSession(page);
  const card = await openSessionMultiVoice(page, id);
  await card.getByTestId('cast-summary').click();
  await card
    .getByRole('button', { name: 'Add character', exact: true })
    .click();
  await card
    .locator('article')
    .first()
    .getByLabel('Identity notes')
    .fill('An unsaved disclosure change.');

  await page
    .getByRole('combobox', { name: 'Session section' })
    .selectOption(`/sessions/${id}/text`);
  const guard = page.getByRole('dialog', { name: 'Keep your cast changes?' });
  await expect(guard).toBeVisible();
  await guard.getByRole('button', { name: 'Keep editing' }).click();
  await expect(page).toHaveURL(`/sessions/${id}`);
  await expect(
    card.locator('article').first().getByLabel('Identity notes')
  ).toHaveValue('An unsaved disclosure change.');
  await expect(
    card.getByText(
      'Save or discard the cast changes before switching voice mode.'
    )
  ).toBeVisible();
});
