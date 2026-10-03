import { expect, test, type Page, type Route } from '@playwright/test';

async function fixture(page: Page, discovery = false) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const headers = { 'X-CSRF-Token': auth.csrf_token };
  const created = await page.request.post('/api/v1/sessions', {
    headers,
    data: {
      name: `Drawer speech lifetime ${crypto.randomUUID()}`,
      workflow_kind: 'audiobook'
    }
  });
  expect(created.ok()).toBeTruthy();
  const sessionId = (await created.json()).id as string;
  const plan = await page.request.post(
    `/api/v1/sessions/${sessionId}/generation-plan`,
    {
      headers,
      data: { segments: [{ text: 'A controlled narration sentence.' }] }
    }
  );
  expect(plan.ok()).toBeTruthy();
  const settingsUrl = `/api/v1/sessions/${sessionId}/settings/tts`;
  const effective = (old = false) => ({
    service: 'speech-fixture',
    tts_service: 'speech-fixture',
    model: old ? 'old-model' : 'current-model',
    xtts_model: old ? 'old-model' : 'current-model',
    voice: old ? 'old-voice' : 'current-voice',
    speaker: old ? 'old-voice' : 'current-voice',
    language: 'en'
  });
  let providerBase = 'http://old-speech.invalid';
  let heldDiscovery: Route | undefined;
  await page.route('**/api/v1/services/tts/discover', async (route) => {
    if (
      route.request().postDataJSON().base_url === 'http://old-speech.invalid'
    ) {
      heldDiscovery = route;
      return;
    }
    await route.fulfill({
      json: {
        success: true,
        models: ['current-model'],
        voices: ['fresh-voice']
      }
    });
  });
  await page.route(/\/api\/v1\/services\/tts(?:\?.*)?$/, (route) =>
    route.fulfill({
      json: {
        default_service: 'speech-fixture',
        services: [
          {
            ...(discovery ? { api_base: providerBase } : {}),
            id: 'speech-fixture',
            name: 'Speech fixture',
            available: true,
            online: true,
            models: ['current-model', 'old-model'],
            default_model: 'current-model',
            voices: ['current-voice'],
            default_voice: 'current-voice',
            voice_catalogues: {
              'current-model': ['current-voice'],
              'old-model': ['old-voice']
            }
          }
        ]
      }
    })
  );
  await page.route('**/api/v1/voices', (route) =>
    route.fulfill({ json: { items: [] } })
  );
  await page.route(`**${settingsUrl}`, (route) =>
    route.fulfill({ json: { effective: effective() } })
  );
  await page.goto(`/sessions/${sessionId}`);
  let heldFirst: Route | undefined;
  let heldSecond: Route | undefined;
  let requests = 0;
  let holdSecond = false;
  await page.route(`**${settingsUrl}`, async (route) => {
    requests++;
    if (requests === 1) {
      heldFirst = route;
      return;
    }
    if (holdSecond && requests === 2) {
      heldSecond = route;
      return;
    }
    await route.fulfill({ json: { effective: effective() } });
  });
  await page.getByRole('button', { name: 'Generation', exact: true }).click();
  await expect.poll(() => Boolean(heldFirst)).toBe(true);
  await page
    .getByRole('button', { name: 'Options for segment 1', exact: true })
    .click();
  const voice = page.getByRole('combobox', {
    name: 'Voice for segment 1',
    exact: true
  });
  await expect(voice).toBeDisabled();
  const settle = async (route: Route, fail: boolean, old: boolean) => {
    const response = page.waitForResponse(
      (candidate) => candidate.request() === route.request(),
      { timeout: 5000 }
    );
    await route.fulfill(
      fail
        ? {
            status: 503,
            json: { error: { message: 'Controlled old settings failure' } }
          }
        : { json: { effective: effective(old) } }
    );
    await (await response).finished();
    await page.evaluate(
      () =>
        new Promise<void>((resolve) => {
          requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
        })
    );
  };
  return {
    voice,
    async waitForDiscovery() {
      await expect.poll(() => Boolean(heldDiscovery)).toBe(true);
    },
    async releaseDiscovery() {
      if (!heldDiscovery) return;
      const route = heldDiscovery;
      heldDiscovery = undefined;
      const response = page.waitForResponse(
        (candidate) => candidate.request() === route.request(),
        { timeout: 5000 }
      );
      await route.fulfill({
        json: {
          success: true,
          models: ['obsolete-model'],
          voices: ['obsolete-voice']
        }
      });
      await (await response).finished();
      await page.evaluate(
        () =>
          new Promise<void>((resolve) => {
            requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
          })
      );
    },
    async reload(hold = false) {
      holdSecond = hold;
      providerBase = 'http://current-speech.invalid';
      await page
        .getByRole('button', { name: 'Settings and speech services' })
        .click();
      await page
        .getByRole('button', {
          name: 'Speech services & catalogue',
          exact: true
        })
        .click();
      await page.getByRole('button', { name: 'Close TTS services' }).click();
      await expect.poll(() => requests).toBeGreaterThanOrEqual(2);
      await page
        .getByRole('button', { name: 'Options for segment 1', exact: true })
        .click();
    },
    async releaseFirst(fail = false, old = true) {
      if (!heldFirst) return;
      const route = heldFirst;
      heldFirst = undefined;
      await settle(route, fail, old);
    },
    async releaseSecond(fail = false) {
      if (!heldSecond) return;
      const route = heldSecond;
      heldSecond = undefined;
      await settle(route, fail, false);
    }
  };
}

for (const fail of [false, true]) {
  test(`an older drawer speech ${fail ? 'failure' : 'response'} cannot replace newer options`, async ({
    page
  }) => {
    const data = await fixture(page);
    try {
      await data.reload();
      await expect(data.voice).toBeEnabled();
      await expect(
        data.voice.locator('option[value="current-voice"]')
      ).toHaveCount(1);
      await data.releaseFirst(fail);
      await expect(
        data.voice.locator('option[value="current-voice"]')
      ).toHaveCount(1);
      await expect(data.voice.locator('option[value="old-voice"]')).toHaveCount(
        0
      );
    } finally {
      await data.releaseFirst();
      await data.releaseSecond();
    }
  });
}

test('an older drawer speech completion cannot clear a newer loading state', async ({
  page
}) => {
  const data = await fixture(page);
  try {
    await data.reload(true);
    await data.releaseFirst();
    await expect(data.voice).toBeDisabled();
    await data.releaseSecond();
    await expect(data.voice).toBeEnabled();
    await expect(
      data.voice.locator('option[value="current-voice"]')
    ).toHaveCount(1);
  } finally {
    await data.releaseFirst();
    await data.releaseSecond();
  }
});

test('a failed current drawer speech refresh keeps usable options and can be retried', async ({
  page
}, info) => {
  const data = await fixture(page);
  try {
    await data.releaseFirst(false, false);
    await expect(data.voice).toBeEnabled();
    await expect(
      data.voice.locator('option[value="current-voice"]')
    ).toHaveCount(1);
    await data.reload(true);
    await data.releaseSecond(true);
    await expect(data.voice).toBeEnabled();
    await expect(
      data.voice.locator('option[value="current-voice"]')
    ).toHaveCount(1);
    await expect(
      page
        .getByRole('alert')
        .filter({ hasText: 'Could not refresh speech options' })
    ).toContainText('Controlled old settings failure');
    await page.screenshot({
      path: info.outputPath('drawer-speech-refresh-failure.png')
    });
    await page.setViewportSize({ width: 390, height: 844 });
    const retry = page.getByRole('button', {
      name: 'Retry speech options',
      exact: true
    });
    await expect(retry).toBeInViewport();
    await retry.focus();
    await expect(retry).toBeFocused();
    await page.screenshot({
      path: info.outputPath('drawer-speech-refresh-failure-narrow.png')
    });
    await page
      .getByRole('button', { name: 'Retry speech options', exact: true })
      .click();
    await page
      .getByRole('button', { name: 'Options for segment 1', exact: true })
      .click();
    await expect(data.voice).toBeEnabled();
    await expect(
      data.voice.locator('option[value="current-voice"]')
    ).toHaveCount(1);
    await expect(
      page.getByRole('button', { name: 'Retry speech options', exact: true })
    ).toHaveCount(0);
  } finally {
    await data.releaseFirst();
    await data.releaseSecond();
  }
});

test('an older drawer speech discovery cannot add obsolete voices after a newer refresh', async ({
  page
}) => {
  const data = await fixture(page, true);
  try {
    await data.releaseFirst(false, false);
    await data.waitForDiscovery();
    await data.reload();
    await expect(data.voice).toBeEnabled();
    await expect(data.voice.locator('option[value="fresh-voice"]')).toHaveCount(
      1
    );
    await data.releaseDiscovery();
    await expect(
      data.voice.locator('option[value="obsolete-voice"]')
    ).toHaveCount(0);
    await expect(data.voice.locator('option[value="fresh-voice"]')).toHaveCount(
      1
    );
  } finally {
    await data.releaseFirst();
    await data.releaseSecond();
    await data.releaseDiscovery();
  }
});
