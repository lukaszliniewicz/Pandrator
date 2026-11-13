import { expect, test, type Page } from '@playwright/test';

async function signIn(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.locator('.app-shell')).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
}

// SvelteKit renders a route from a second module request, and Vite waits for
// a stylesheet only on the first one. Without the layout's onNavigate wait, a
// slow route stylesheet shows the page unstyled and then moves its controls.
test('client navigation shows a route only once its stylesheets apply', async ({
  page
}) => {
  await signIn(page);
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const created = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': auth.csrf_token },
    data: {
      interface_mode: 'advanced',
      name: `Route styles ${crypto.randomUUID()}`,
      workflow_kind: 'audiobook'
    }
  });
  expect(created.ok()).toBeTruthy();
  const { id } = (await created.json()) as { id: string };
  await page.goto(`/sessions/${id}`);
  await expect(
    page.getByRole('region', { name: 'Voices', exact: true })
  ).toBeVisible();
  // Only stylesheets first requested by the navigation below are delayed.
  await page.route('**/_app/immutable/assets/*.css', async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 800));
    await route.continue();
  });
  await page.getByRole('link', { name: 'Generation', exact: true }).click();
  const options = page.getByRole('button', {
    name: 'Display options',
    exact: true
  });
  await expect(options).toBeVisible();
  // The toolbar's own stylesheet lays its actions out as flex items.
  expect(await options.evaluate((node) => getComputedStyle(node).display)).toBe(
    'flex'
  );
});
