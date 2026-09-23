import { expect, test, type Page } from '@playwright/test';

async function fixture(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const created = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': auth.csrf_token },
    data: {
      name: `Model details ${crypto.randomUUID()}`,
      workflow_kind: 'audiobook'
    }
  });
  expect(created.ok()).toBeTruthy();
  const sessionId = (await created.json()).id as string;
  const settings = await (
    await page.request.get(`/api/v1/sessions/${sessionId}/settings/tts`)
  ).json();
  await page.route(
    `**/api/v1/sessions/${sessionId}/settings/tts`,
    async (route) => {
      if (route.request().method() !== 'GET') return route.continue();
      await route.fulfill({
        json: {
          ...settings,
          effective: {
            ...settings.effective,
            service: 'audio_cpp',
            model: 'model-a',
            audio_cpp_model_settings: {
              'model-a': { temperature: 0.5 },
              'model-b': { temperature: 0.5 }
            }
          },
          override: { service: 'audio_cpp', model: 'model-a' }
        }
      });
    }
  );
  const service = {
    id: 'audio_cpp',
    name: 'audio.cpp',
    adapter: 'audio_cpp',
    available: true,
    models: ['model-a', 'model-b'],
    default_model: 'model-a',
    voices: [],
    model_catalog: ['model-a', 'model-b'].map((id) => ({
      id,
      label: id,
      supported_languages: ['en']
    }))
  };
  const catalogueQueries: string[] = [];
  const detailModels: string[] = [];
  let releaseFirst: (() => void) | undefined;
  let slowFirst = false;
  let failFirst = false;
  let firstSeen = false;
  const detailBody = (model: string) => ({
    view: 'detail',
    selected_models: [model],
    service: {
      ...service,
      model_catalog: [
        {
          id: model,
          label: model,
          request_parameters: {
            temperature: {
              type: 'number',
              minimum: 0,
              maximum: model === 'model-a' ? 1 : 2
            }
          }
        }
      ]
    }
  });
  await page.route('**/api/v1/services/tts*', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname !== '/api/v1/services/tts') return route.continue();
    catalogueQueries.push(url.searchParams.get('view') ?? 'full');
    await route.fulfill({
      json: {
        view: 'compact',
        services: [service],
        default_service: 'audio_cpp'
      }
    });
  });
  await page.route('**/api/v1/services/tts/audio_cpp?*', async (route) => {
    const model =
      new URL(route.request().url()).searchParams.get('model') ?? '';
    detailModels.push(model);
    if (!firstSeen && model === 'model-a') {
      firstSeen = true;
      if (slowFirst)
        await new Promise<void>((resolve) => {
          releaseFirst = resolve;
        });
      if (failFirst) {
        await route.fulfill({
          status: 503,
          json: { error: { message: 'Model metadata temporarily unavailable' } }
        });
        return;
      }
    }
    await route.fulfill({ json: detailBody(model) });
  });
  return {
    sessionId,
    catalogueQueries,
    detailModels,
    delayFirst() {
      slowFirst = true;
    },
    failFirst() {
      failFirst = true;
    },
    releaseFirst() {
      releaseFirst?.();
    }
  };
}

test('session settings use compact choices and ignore late metadata from another model', async ({
  page
}) => {
  const data = await fixture(page);
  data.delayFirst();
  const pageErrors: string[] = [];
  page.on('pageerror', (error) => pageErrors.push(String(error)));
  try {
    await page.goto(`/sessions/${data.sessionId}/voice`);
    const settings = page.locator('.settings-panel').filter({
      has: page.getByRole('heading', {
        name: 'Speech generation',
        exact: true
      })
    });
    const model = settings.getByRole('combobox', {
      name: 'Model',
      exact: true
    });
    await expect(model).toHaveValue('model-a');
    await expect.poll(() => data.detailModels.includes('model-a')).toBe(true);
    await expect(
      settings.getByRole('button', { name: 'Save', exact: true })
    ).toBeDisabled();
    await model.selectOption('model-b');
    const temperature = settings.getByLabel('Temperature', { exact: true });
    await expect(temperature).toHaveAttribute('max', '2');
    await expect(
      settings.getByRole('button', { name: 'Save', exact: true })
    ).toBeEnabled();
    data.releaseFirst();
    await page.waitForTimeout(150);
    await expect(model).toHaveValue('model-b');
    await expect(temperature).toHaveAttribute('max', '2');
    await expect(model.locator('option')).toHaveCount(3);
    expect(data.catalogueQueries.length).toBeGreaterThan(0);
    expect(data.catalogueQueries.every((view) => view === 'compact')).toBe(
      true
    );
    expect(data.detailModels).toEqual(['model-a', 'model-b']);
    expect(pageErrors).toEqual([]);
  } finally {
    data.releaseFirst();
  }
});

test('missing selected-model metadata exposes retry and does not silently validate tuning', async ({
  page
}) => {
  const data = await fixture(page);
  data.failFirst();
  await page.goto(`/sessions/${data.sessionId}/voice`);
  const settings = page.locator('.settings-panel').filter({
    has: page.getByRole('heading', { name: 'Speech generation', exact: true })
  });
  await expect(
    settings
      .getByRole('alert')
      .filter({ hasText: 'Could not load model details' })
  ).toContainText('Model metadata temporarily unavailable');
  await expect(
    settings.getByRole('button', { name: 'Save', exact: true })
  ).toBeDisabled();
  await settings.getByRole('button', { name: 'Retry model details' }).click();
  await expect(
    settings.getByLabel('Temperature', { exact: true })
  ).toHaveAttribute('max', '1');
  await expect(
    settings.getByRole('button', { name: 'Save', exact: true })
  ).toBeEnabled();
  expect(data.detailModels).toEqual(['model-a', 'model-a']);
});
