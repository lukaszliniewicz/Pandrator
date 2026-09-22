import { expect, test, type Page } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.locator('.app-shell')).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
});

async function createPlan(page: Page) {
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const headers = { 'X-CSRF-Token': auth.csrf_token };
  const created = await page.request.post('/api/v1/sessions', {
    headers,
    data: {
      name: `Drawer review ${crypto.randomUUID()}`,
      workflow_kind: 'audiobook'
    }
  });
  expect(created.ok()).toBeTruthy();
  const id: string = (await created.json()).id;
  const prepared = await page.request.post(
    `/api/v1/sessions/${id}/generation-plan`,
    {
      headers,
      data: {
        segments: [
          { text: 'The first part.' },
          { text: 'The second part.' },
          { text: 'The final part.' }
        ]
      }
    }
  );
  expect(prepared.ok()).toBeTruthy();
  const statusUrl = `/api/v1/sessions/${id}/generation-plan/status?summary=true`;
  const status = await (await page.request.get(statusUrl)).json();
  return { id, headers, status, statusUrl };
}

async function openDrawer(page: Page, id: string) {
  await page.goto(`/sessions/${id}`);
  const drawer = page.locator('[data-generation-layout]');
  await expect(drawer).toBeVisible();
  if ((await drawer.getAttribute('data-generation-layout')) === 'collapsed') {
    await page.getByRole('button', { name: 'Generation', exact: true }).click();
  }
  const bar = drawer.getByTestId('drawer-plan-review');
  await expect(bar).toBeVisible();
  return { drawer, bar };
}

test('review completes inside the drawer without generation or leaving the plan', async ({
  page
}) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const { id, status, statusUrl } = await createPlan(page);
  const reviewBodies: Record<string, unknown>[] = [];
  page.on('request', (request) => {
    if (
      request.method() === 'POST' &&
      request.url().endsWith(`/sessions/${id}/generation-plan/review`)
    )
      reviewBodies.push(request.postDataJSON());
  });
  const { drawer, bar } = await openDrawer(page, id);
  await expect(bar).toContainText('3 included blocks');
  await expect(bar).toContainText('whole plan');
  const mark = bar.getByTestId('drawer-mark-reviewed');
  await expect(mark).toBeEnabled();
  await mark.click();
  await expect(bar).toContainText('Reviewed');
  await expect(mark).toHaveCount(0);
  expect(reviewBodies).toEqual([
    {
      revision_id: status.selected_revision_id,
      content_signature: status.content_signature
    }
  ]);
  expect(
    (await (await page.request.get(statusUrl)).json()).items[0].reviewed
  ).toBe(true);
  expect(
    (
      await (
        await page.request.get(`/api/v1/sessions/${id}/generation-runs`)
      ).json()
    ).items
  ).toHaveLength(0);
  await expect(drawer).not.toHaveAttribute(
    'data-generation-layout',
    'collapsed'
  );
  await expect(
    drawer.getByRole('button', { name: 'Generate audio…', exact: true })
  ).toBeVisible();
  expect(errors).toEqual([]);
});

test('a changed-content conflict requires reloading and another explicit review', async ({
  page
}) => {
  const { id } = await createPlan(page);
  let posts = 0;
  await page.route(
    `**/api/v1/sessions/${id}/generation-plan/review`,
    async (route) => {
      posts++;
      if (posts === 1)
        return route.fulfill({
          status: 409,
          json: {
            error: {
              code: 'revision_conflict',
              message:
                'The speech plan changed while it was being reviewed. Inspect the new content first.'
            }
          }
        });
      await route.continue();
    }
  );
  const { bar } = await openDrawer(page, id);
  await bar.getByTestId('drawer-mark-reviewed').click();
  await expect(bar.getByRole('alert')).toContainText(
    'Inspect the new content first'
  );
  await expect(bar.getByTestId('drawer-mark-reviewed')).toBeDisabled();
  expect(posts).toBe(1);
  await bar.getByRole('button', { name: 'Reload plan for review' }).click();
  await expect(bar.getByTestId('drawer-mark-reviewed')).toBeEnabled();
  expect(posts).toBe(1);
  await bar.getByTestId('drawer-mark-reviewed').click();
  await expect(bar).toContainText('Reviewed');
  await expect(bar.getByTestId('drawer-mark-reviewed')).toHaveCount(0);
  expect(posts).toBe(2);
});

test('status-load failure has a retry and never enables an unguarded review', async ({
  page
}) => {
  const { id } = await createPlan(page);
  const url = `**/api/v1/sessions/${id}/generation-plan/status*`;
  await page.route(url, (route) =>
    route.fulfill({
      status: 503,
      json: {
        error: {
          code: 'unavailable',
          message: 'Review status is temporarily unavailable.'
        }
      }
    })
  );
  const { bar } = await openDrawer(page, id);
  await expect(bar.getByRole('alert')).toContainText('temporarily unavailable');
  await expect(bar.getByTestId('drawer-mark-reviewed')).toHaveCount(0);
  await page.unroute(url);
  await bar.getByRole('button', { name: 'Reload plan for review' }).click();
  await expect(bar.getByTestId('drawer-mark-reviewed')).toBeEnabled();
});

test('a stale signature is rejected by the real review endpoint', async ({
  page
}) => {
  const { id, headers, status } = await createPlan(page);
  const rows = await (
    await page.request.get(`/api/v1/sessions/${id}/generation-segments`)
  ).json();
  const first = rows.items[0];
  const edited = await page.request.patch(
    `/api/v1/generation-segments/${first.id}`,
    {
      headers: { ...headers, 'If-Match': `"${first.revision}"` },
      data: { text: 'Edited wording.' }
    }
  );
  expect(edited.ok()).toBeTruthy();
  const rejected = await page.request.post(
    `/api/v1/sessions/${id}/generation-plan/review`,
    {
      headers: { ...headers, 'Idempotency-Key': crypto.randomUUID() },
      data: {
        revision_id: status.selected_revision_id,
        content_signature: status.content_signature
      }
    }
  );
  expect(rejected.status()).toBe(409);
  expect(
    (
      await (
        await page.request.get(`/api/v1/sessions/${id}/generation-runs`)
      ).json()
    ).items
  ).toHaveLength(0);
});

test('drawer review remains accessible on a phone', async ({ page }) => {
  const { id } = await createPlan(page);
  await page.setViewportSize({ width: 390, height: 844 });
  const { bar } = await openDrawer(page, id);
  const mark = bar.getByTestId('drawer-mark-reviewed');
  await expect(mark).toBeVisible();
  await mark.focus();
  await page.keyboard.press('Enter');
  await expect(bar).toContainText('Reviewed');
  await expect(mark).toHaveCount(0);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth
    )
  ).toBe(true);
});

test('a failed pending text save cannot mark the old wording reviewed', async ({
  page
}) => {
  const { id } = await createPlan(page);
  let reviews = 0;
  page.on('request', (request) => {
    if (
      request.method() === 'POST' &&
      request.url().endsWith(`/sessions/${id}/generation-plan/review`)
    )
      reviews++;
  });
  await page.route('**/api/v1/generation-segments/*', (route) => {
    if (route.request().method() !== 'PATCH') return route.continue();
    return route.fulfill({
      status: 503,
      json: {
        error: {
          code: 'save_failed',
          message: 'The text edit could not be saved.'
        }
      }
    });
  });
  const { drawer, bar } = await openDrawer(page, id);
  const editor = drawer
    .locator('tr[data-segment-id]')
    .first()
    .locator('textarea')
    .first();
  await editor.fill('A draft that has not been saved.');
  const failedSave = page.waitForResponse(
    (response) =>
      response.request().method() === 'PATCH' &&
      response.url().includes('/generation-segments/')
  );
  await editor.press('Tab');
  await failedSave;
  await expect(bar.getByTestId('drawer-mark-reviewed')).toBeEnabled();
  await bar.getByTestId('drawer-mark-reviewed').click();
  await expect(bar.getByRole('alert')).toContainText('could not be saved');
  expect(reviews).toBe(0);
});

test('an inactive historical plan cannot be marked reviewed from the drawer', async ({
  page
}) => {
  const { id } = await createPlan(page);
  await page.route(
    `**/api/v1/sessions/${id}/generation-segments?*`,
    async (route) => {
      const response = await route.fetch();
      const payload = await response.json();
      await route.fulfill({
        response,
        json: {
          ...payload,
          is_active_revision: false,
          active_plan_revision_id: 'another-active-plan'
        }
      });
    }
  );
  const { bar } = await openDrawer(page, id);
  await expect(bar).toContainText('Switch to Active mix');
  await expect(bar.getByTestId('drawer-mark-reviewed')).toHaveCount(0);
});
