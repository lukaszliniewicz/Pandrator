import { expect, test, type Page } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.route('**/*', (route) =>
    ['127.0.0.1', 'localhost'].includes(new URL(route.request().url()).hostname)
      ? route.continue()
      : route.abort()
  );
});

async function replacementWizard(page: Page) {
  await page.goto('/sessions');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.locator('.app-shell')).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const headers = { 'X-CSRF-Token': auth.csrf_token };
  const name = `Replacement ${crypto.randomUUID()}`;
  const response = await page.request.post('/api/v1/sessions', {
    headers,
    data: { name }
  });
  expect(response.ok(), await response.text()).toBeTruthy();
  const original = await response.json();
  await page.reload();
  await expect(page.getByRole('link', { name, exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Add project', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog
    .getByRole('button', {
      name: 'Skip guidance and open a fully customizable project'
    })
    .click();
  await dialog.getByRole('button', { name: 'Add later', exact: true }).click();
  await dialog.getByRole('button', { name: 'Continue', exact: true }).click();
  await dialog.getByLabel('Project name', { exact: true }).fill(name);
  await expect(
    dialog.getByRole('button', { name: 'Replace older project', exact: true })
  ).toBeVisible();
  return { original, headers, dialog };
}

test('replacement sends the displayed revision and keeps the old project in Trash', async ({
  page
}) => {
  const { original, dialog } = await replacementWizard(page);
  const submitted = page.waitForRequest(
    (request) =>
      request.method() === 'POST' &&
      new URL(request.url()).pathname === '/api/v1/sessions' &&
      request.postDataJSON()?.overwrite_session_id === original.id
  );
  await dialog
    .getByRole('button', { name: 'Replace older project', exact: true })
    .click();
  const request = await submitted;
  expect(request.postDataJSON().overwrite_revision).toBe(original.revision);
  await expect(page).toHaveURL(/\/sessions\/[a-z0-9-]+$/);
  expect(new URL(page.url()).pathname).not.toBe(`/sessions/${original.id}`);
  const old = await (
    await page.request.get(`/api/v1/sessions/${original.id}`)
  ).json();
  expect(old.status).toBe('trashed');
  expect(old.trashed_at).toBeTruthy();
});

test('an edit after replacement confirmation leaves the project intact and the wizard open', async ({
  page
}, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const { original, headers, dialog } = await replacementWizard(page);
  let submissions = 0;
  await page.route('**/api/v1/sessions', async (route) => {
    const request = route.request();
    if (
      request.method() !== 'POST' ||
      request.postDataJSON()?.overwrite_session_id !== original.id
    ) {
      await route.continue();
      return;
    }
    submissions += 1;
    expect(request.postDataJSON().overwrite_revision).toBe(original.revision);
    const updated = await page.request.patch(
      `/api/v1/sessions/${original.id}`,
      {
        headers: { ...headers, 'If-Match': `"${original.revision}"` },
        data: { source_language: 'pl' }
      }
    );
    expect(updated.ok(), await updated.text()).toBeTruthy();
    await route.continue();
  });
  const rejected = page.waitForResponse(
    (response) =>
      response.request().method() === 'POST' &&
      new URL(response.url()).pathname === '/api/v1/sessions'
  );
  await dialog
    .getByRole('button', { name: 'Replace older project', exact: true })
    .click();
  const response = await rejected;
  expect(response.status()).toBe(409);
  const failure = await response.json();
  expect(failure.error.code).toBe('revision_conflict');
  await expect(dialog).toBeVisible();
  await expect(
    dialog.getByText(
      'This project changed before it could be replaced. Review the updated project before trying again.',
      { exact: true }
    )
  ).toBeVisible();
  const old = await (
    await page.request.get(`/api/v1/sessions/${original.id}`)
  ).json();
  expect(old.revision).toBe(original.revision + 1);
  expect(old.source_language).toBe('pl');
  expect(old.trashed_at).toBeNull();
  const listed = await (await page.request.get('/api/v1/sessions')).json();
  expect(
    listed.items.filter((item: { name: string }) => item.name === original.name)
  ).toHaveLength(1);
  expect(submissions).toBe(1);
  await page.screenshot({
    path: info.outputPath('replacement-conflict-mobile.png'),
    fullPage: true
  });
});

test('a later setup conflict preserves the replacement and offers its saved project', async ({
  page
}) => {
  const { original, dialog } = await replacementWizard(page);
  await page.route('**/api/v1/sessions/*/outcome-plan', async (route) => {
    if (route.request().method() !== 'PUT') {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 409,
      json: {
        error: {
          code: 'revision_conflict',
          message: 'The workflow plan changed during setup.',
          details: { current_revision: 2 },
          request_id: 'creation-setup-conflict-fixture'
        }
      }
    });
  });
  const created = page.waitForResponse(
    (response) =>
      response.request().method() === 'POST' &&
      new URL(response.url()).pathname === '/api/v1/sessions'
  );
  await dialog
    .getByRole('button', { name: 'Replace older project', exact: true })
    .click();
  const response = await created;
  expect(response.status()).toBe(201);
  const replacement = await response.json();
  await expect(
    dialog.getByText('The workflow plan changed during setup.', { exact: true })
  ).toBeVisible();
  const saved = dialog.getByRole('link', { name: 'Open saved project' });
  await expect(saved).toHaveAttribute('href', `/sessions/${replacement.id}`);
  await expect(dialog.getByText(/before it could be replaced/)).toHaveCount(0);
  await expect(
    dialog.getByRole('button', {
      name: /^(Choose an option above|Create project)$/
    })
  ).toBeDisabled();
  const old = await (
    await page.request.get(`/api/v1/sessions/${original.id}`)
  ).json();
  expect(old.status).toBe('trashed');
  expect(
    (await page.request.get(`/api/v1/sessions/${replacement.id}`)).status()
  ).toBe(200);
});
