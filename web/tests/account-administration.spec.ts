import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';

async function signIn(page: Page) {
  await page.goto('/settings/users');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(
    page.getByRole('heading', { name: 'Users', exact: true })
  ).toBeVisible();
}

async function createFixtureAccount(page: Page, label: string) {
  const status = await (await page.request.get('/api/v1/auth/status')).json();
  const prefix = label.toLowerCase().replaceAll(/[^a-z0-9]+/g, '-');
  const login = `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
  const response = await page.request.post('/api/v1/admin/users', {
    headers: {
      'X-CSRF-Token': status.csrf_token,
      'Idempotency-Key': `browser-account-${login}`
    },
    data: { login_name: login, display_name: label }
  });
  expect(response.status()).toBe(201);
  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  return { record: await response.json(), login };
}

test('Administrator creates, edits and disables an account with protected initial access', async ({
  page
}) => {
  await signIn(page);
  await expect(
    page.getByRole('button', { name: 'Edit Owner', exact: true })
  ).toBeVisible();
  await page.screenshot({
    path: test.info().outputPath('users-desktop.png'),
    fullPage: true
  });
  const name = `Team editor ${Date.now()}`;
  const login = `editor-${Date.now()}`;
  await page.getByRole('button', { name: 'Add user' }).click();
  const dialog = page.getByRole('dialog', { name: 'Add user' });
  await expect(dialog.getByLabel('Role', { exact: true })).toHaveValue('user');
  await dialog.getByLabel('Display name', { exact: true }).fill(name);
  await dialog.getByLabel('Login name', { exact: true }).fill(login);
  await dialog.getByRole('button', { name: 'Create account' }).click();
  await expect(dialog).not.toBeVisible();
  const row = page.getByRole('listitem').filter({ hasText: login });
  await expect(
    row.getByText('Password not set', { exact: true })
  ).toBeVisible();
  await expect(
    page.getByText(/their sign-in is not enabled yet/)
  ).toBeVisible();
  await row.getByRole('button', { name: `Edit ${name}`, exact: true }).click();
  const edit = page.getByRole('dialog', { name: 'Edit account' });
  await expect(edit.getByLabel('Login name', { exact: true })).toBeDisabled();
  await edit.getByLabel('Role', { exact: true }).selectOption('administrator');
  await edit.getByLabel('Account enabled').uncheck();
  await edit.getByRole('button', { name: 'Save account' }).click();
  await expect(edit).not.toBeVisible();
  await expect(row.getByText('Administrator', { exact: true })).toBeVisible();
  await expect(row.getByText('Disabled', { exact: true })).toBeVisible();
  await expect(
    row.getByRole('button', { name: `Create password link for ${name}` })
  ).toBeDisabled();

  const identity = await (await page.request.get('/api/v1/auth/status')).json();
  await page
    .getByRole('button', {
      name: `Edit ${identity.principal.display_name}`,
      exact: true
    })
    .click();
  await expect(edit.getByLabel('Role', { exact: true })).toBeDisabled();
  await expect(edit.getByLabel('Account enabled')).toBeDisabled();
  await edit.getByRole('button', { name: 'Cancel', exact: true }).click();
});

test('password link works without a session, leaves the URL, and can only be redeemed once', async ({
  page,
  browser
}) => {
  await signIn(page);
  const { login } = await createFixtureAccount(page, 'Password recipient');
  const row = page.getByRole('listitem').filter({ hasText: login });
  await row
    .getByRole('button', {
      name: 'Create password link for Password recipient'
    })
    .click();
  const dialog = page.getByRole('dialog', {
    name: 'Password link for Password recipient'
  });
  const resetUrl = await dialog
    .getByLabel('Password setup link', { exact: true })
    .inputValue();
  await expect(dialog).toContainText('It can be used once and expires');
  await dialog.getByRole('button', { name: 'Close', exact: true }).click();
  await expect(
    page.getByLabel('Password setup link', { exact: true })
  ).toHaveCount(0);

  const recipient = await browser.newContext({
    baseURL: new URL(page.url()).origin
  });
  try {
    const resetPage = await recipient.newPage();
    await resetPage.goto(resetUrl);
    await expect(
      resetPage.getByLabel('New password', { exact: true })
    ).toBeVisible();
    await expect.poll(() => new URL(resetPage.url()).hash).toBe('');
    expect(
      await resetPage.evaluate(() => JSON.stringify(history.state))
    ).not.toContain(new URL(resetUrl).hash.slice('#token='.length));
    await expect(resetPage.getByLabel('Owner password')).toHaveCount(0);
    await resetPage
      .getByLabel('New password', { exact: true })
      .fill('An unrelated synthetic password phrase');
    await resetPage
      .getByLabel('Confirm new password', { exact: true })
      .fill('A different synthetic password phrase');
    await resetPage.getByRole('button', { name: 'Save password' }).click();
    await expect(resetPage.getByRole('alert')).toContainText(
      'The passwords do not match.'
    );
    await resetPage
      .getByLabel('Confirm new password', { exact: true })
      .fill('An unrelated synthetic password phrase');
    await resetPage.getByRole('button', { name: 'Save password' }).click();
    await expect(resetPage.getByRole('status')).toContainText(
      'Your password is saved.'
    );
    const auth = await (
      await recipient.request.get('/api/v1/auth/status')
    ).json();
    expect(auth.authenticated).toBe(false);
    await resetPage.goto(resetUrl);
    await resetPage
      .getByLabel('New password', { exact: true })
      .fill('Another unrelated synthetic password phrase');
    await resetPage
      .getByLabel('Confirm new password', { exact: true })
      .fill('Another unrelated synthetic password phrase');
    await resetPage.getByRole('button', { name: 'Save password' }).click();
    await expect(resetPage.getByRole('alert')).toContainText(
      /invalid|expired/i
    );
  } finally {
    await recipient.close();
  }
});

test('User and unknown roles cannot open Administrator controls or submit account requests', async ({
  page
}) => {
  await signIn(page);
  let adminRequests = 0;
  let managerWrites = 0;
  let omitPrincipal = false;
  await page.route('**/api/v1/auth/status', async (route) => {
    const response = await route.fetch();
    const status = await response.json();
    status.principal = omitPrincipal
      ? null
      : {
          ...status.principal,
          role: 'user',
          scopes: ['app.read', 'app.write', 'app.run', 'manager.read']
        };
    await route.fulfill({ response, json: status });
  });
  await page.route('**/api/v1/admin/users**', async (route) => {
    adminRequests += 1;
    await route.abort();
  });
  await page.route('**/api/v1/manager/**', async (route) => {
    const method = route.request().method();
    if (!['GET', 'HEAD'].includes(method)) managerWrites += 1;
    const path = new URL(route.request().url()).pathname;
    const payload = path.endsWith('/status')
      ? { available: true, status: { configuration_revision: 1 } }
      : path.endsWith('/releases')
        ? { current: {}, items: [] }
        : path.endsWith('/components')
          ? {
              items: [
                {
                  definition: {
                    id: 'audio_cpp',
                    service_key: 'audio_cpp',
                    label: 'audio.cpp',
                    description: 'Local audio models',
                    compute_variants: ['auto'],
                    supported_actions: ['install'],
                    models: []
                  },
                  inspection: { state: 'absent', problems: [] }
                }
              ]
            }
          : { items: [] };
    await route.fulfill({ json: payload });
  });
  await page.reload();
  await expect(page.getByRole('alert')).toContainText(
    'An Administrator account is required'
  );
  await expect(page.getByRole('button', { name: 'Add user' })).toHaveCount(0);
  expect(adminRequests).toBe(0);
  await page.goto('/providers?tab=speech&speech=local');
  await expect(
    page.getByText(/An Administrator manages local services/)
  ).toBeVisible();
  await expect(
    page.getByRole('button', { name: 'Install locally', exact: true })
  ).toBeDisabled();
  expect(managerWrites).toBe(0);
  omitPrincipal = true;
  await page.goto('/settings/users');
  await expect(page.getByRole('alert')).toContainText(
    'An Administrator account is required'
  );
  expect(adminRequests).toBe(0);
});

test('account editor supports a narrow viewport, keyboard dismissal and accessible labels', async ({
  page
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page);
  const add = page.getByRole('button', { name: 'Add user' });
  await add.click();
  const dialog = page.getByRole('dialog', { name: 'Add user' });
  await expect(dialog).toBeVisible();
  await page.screenshot({
    path: test.info().outputPath('users-mobile-editor.png'),
    fullPage: true
  });
  await expect(
    dialog.getByLabel('Display name', { exact: true })
  ).toBeVisible();
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth
      )
    )
    .toBe(true);
  const accessibility = await new AxeBuilder({ page })
    .include('[role="dialog"]')
    .withTags(['wcag2a', 'wcag2aa', 'wcag21aa'])
    .analyze();
  expect(accessibility.violations).toEqual([]);
  await page.keyboard.press('Escape');
  await expect(dialog).not.toBeVisible();
  await expect(add).toBeFocused();
});
