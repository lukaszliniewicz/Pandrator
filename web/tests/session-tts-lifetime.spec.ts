import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page, type Route } from '@playwright/test';

const services = ['a', 'b'].map((suffix) => ({
  id: `provider_${suffix}`,
  name: `Provider ${suffix.toUpperCase()}`,
  api_base: `http://provider-${suffix}.invalid`,
  available: true,
  online: true,
  supports_prebuilt_voices: true,
  models: [`model-${suffix}`, `model-${suffix}-2`],
  default_model: `model-${suffix}`,
  default_voice: `voice-${suffix}`,
  default_voices: {
    [`model-${suffix}`]: `voice-${suffix}`,
    [`model-${suffix}-2`]: `voice-${suffix}-2`
  },
  voice_catalogues: {
    [`model-${suffix}`]: [`voice-${suffix}`, `custom-${suffix}`],
    [`model-${suffix}-2`]: [`voice-${suffix}-2`, `custom-${suffix}`]
  }
}));

async function fixture(page: Page, publication = false) {
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
      name: `TTS lifetime ${crypto.randomUUID()}`,
      workflow_kind: 'voiceover'
    }
  });
  expect(created.ok()).toBeTruthy();
  const sessionId = (await created.json()).id as string;
  const settingsUrl = `/api/v1/sessions/${sessionId}/settings/tts`;
  const settings = await (await page.request.get(settingsUrl)).json();
  await page.route(`**${settingsUrl}`, async (route) => {
    if (route.request().method() !== 'GET') return route.continue();
    await route.fulfill({
      json: {
        ...settings,
        effective: {
          ...settings.effective,
          service: 'provider_b',
          tts_service: 'provider_b',
          model: 'model-b',
          xtts_model: 'model-b',
          voice: 'voice-b',
          speaker: 'voice-b',
          language: 'en'
        },
        override: {
          service: 'provider_b',
          tts_service: 'provider_b',
          model: 'model-b',
          xtts_model: 'model-b',
          voice: 'voice-b',
          speaker: 'voice-b'
        }
      }
    });
  });
  let delayedDiscovery: Route | undefined;
  let delayedCatalogue: Route | undefined;
  let holdDiscovery = false;
  let holdCatalogue = false;
  const body = {
    view: 'compact',
    services: publication
      ? services.map((service) => ({
          ...service,
          supports_voice_cloning: true,
          model_voice_modes: Object.fromEntries(
            service.models.map((model) => [model, 'hybrid'])
          )
        }))
      : services,
    default_service: 'provider_b'
  };
  const localVoice = {
    id: 'local-publication-voice',
    name: 'Publication narrator',
    language: 'en',
    revision: 1,
    available_sample_count: 1,
    metadata_json: { providers: {} }
  };
  let library: unknown[] = publication ? [localVoice] : [];
  let libraryFailure = false;
  let holdReadback = false;
  let delayedReadback: Route | undefined;
  let delayedJob: Route | undefined;
  let submittedService = '';
  if (publication) {
    await page.route(
      '**/api/v1/voices/local-publication-voice/providers/*',
      async (route) => {
        expect(route.request().method()).toBe('POST');
        expect(route.request().headers()['if-match']).toBe('"1"');
        submittedService = route.request().url().split('/').at(-1) ?? '';
        await route.fulfill({
          json: {
            id: 'controlled-publication-job',
            kind: 'voice.publish',
            status: 'queued'
          }
        });
      }
    );
    await page.route('**/api/v1/jobs/controlled-publication-job', (route) => {
      delayedJob = route;
    });
  }
  await page.route('**/api/v1/services/tts?*', async (route) => {
    if (holdCatalogue) {
      delayedCatalogue = route;
      return;
    }
    await route.fulfill({ json: body });
  });
  await page.route('**/api/v1/voices', async (route) => {
    if (holdReadback) {
      delayedReadback = route;
      return;
    }
    await route.fulfill(
      libraryFailure
        ? {
            status: 503,
            json: { error: { message: 'Controlled voice readback outage' } }
          }
        : { json: { items: library } }
    );
  });
  await page.route('**/api/v1/services/tts/discover', async (route) => {
    if (
      holdDiscovery &&
      route.request().postDataJSON().service_id === 'provider_a'
    ) {
      delayedDiscovery = route;
      return;
    }
    await route.fulfill({ json: { success: true, models: [], voices: [] } });
  });
  await page.goto(`/sessions/${sessionId}`);
  const card = page
    .getByRole('heading', { name: 'Generate audio', exact: true })
    .locator('xpath=ancestor::article');
  const open = async () => {
    await card.getByRole('button', { name: 'Settings' }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog).toHaveAttribute('aria-busy', 'false');
    return dialog;
  };
  const dialog = await open();
  await expect(dialog.getByLabel('TTS service')).toHaveValue('provider_b');
  const settleResponse = async (
    route: Route,
    options: Parameters<Route['fulfill']>[0]
  ) => {
    const response = page.waitForResponse(
      (candidate) => candidate.request() === route.request()
    );
    await route.fulfill(options);
    await (await response).finished();
    // Run after the response's promise continuations and Svelte DOM update.
    await page.evaluate(
      () =>
        new Promise<void>((resolve) => {
          requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
        })
    );
  };
  return {
    dialog,
    open,
    delayVoiceReadback() {
      holdReadback = true;
    },
    async waitForVoiceReadback() {
      await expect.poll(() => Boolean(delayedReadback)).toBe(true);
    },
    async releaseVoiceReadback() {
      holdReadback = false;
      if (delayedReadback) {
        const route = delayedReadback;
        delayedReadback = undefined;
        await settleResponse(route, { json: { items: library } });
      }
    },
    async publishVoice() {
      await dialog
        .locator('summary')
        .filter({ hasText: 'Available from your Voice Library' })
        .click();
      await dialog
        .getByRole('button', { name: 'Upload & use', exact: true })
        .click();
      await expect.poll(() => Boolean(delayedJob)).toBe(true);
      expect(submittedService).toBe('provider_b');
    },
    async releasePublication(
      status: 'ready' | 'stale' | 'canceled' | 'readback-failure' = 'ready'
    ) {
      if (!delayedJob) return;
      libraryFailure = status === 'readback-failure';
      library = [
        {
          ...localVoice,
          revision: 7,
          metadata_json: {
            providers: {
              provider_b: {
                voice_id: 'published-b',
                status: status === 'stale' ? 'stale' : 'ready',
                managed_by: 'pandrator',
                protocol: 'pandrator-voices-v1',
                resource_kind: 'uploaded_reference',
                stale_reason:
                  status === 'stale'
                    ? 'Reference changed during upload.'
                    : undefined
              }
            }
          }
        }
      ];
      const route = delayedJob;
      delayedJob = undefined;
      await settleResponse(route, {
        json: {
          id: 'controlled-publication-job',
          kind: 'voice.publish',
          status: status === 'canceled' ? 'canceled' : 'succeeded',
          error_message:
            status === 'canceled'
              ? 'Controlled cancellation after upload.'
              : undefined,
          result_json:
            status === 'canceled'
              ? null
              : {
                  voice_id: localVoice.id,
                  service_id: 'provider_b',
                  provider_voice_id: 'published-b',
                  voice_revision: 2
                }
        }
      });
    },
    async delayDiscovery() {
      holdDiscovery = true;
      await dialog.getByLabel('TTS service').selectOption('provider_a');
      await expect.poll(() => Boolean(delayedDiscovery)).toBe(true);
    },
    async releaseDiscovery() {
      holdDiscovery = false;
      if (delayedDiscovery) {
        const route = delayedDiscovery;
        delayedDiscovery = undefined;
        await settleResponse(route, {
          json: { success: true, models: [], voices: [] }
        });
      }
    },
    async delayRefresh() {
      holdCatalogue = true;
      await dialog
        .getByRole('button', { name: 'Refresh service availability' })
        .click();
      await expect.poll(() => Boolean(delayedCatalogue)).toBe(true);
    },
    async releaseRefresh(fail = false) {
      holdCatalogue = false;
      if (delayedCatalogue) {
        const route = delayedCatalogue;
        delayedCatalogue = undefined;
        await settleResponse(
          route,
          fail
            ? {
                status: 503,
                json: { error: { message: 'Controlled catalogue outage' } }
              }
            : {
                json: {
                  ...body,
                  services: body.services.map((service) => ({
                    ...service,
                    models: [...service.models, `fresh-${service.id}`]
                  }))
                }
              }
        );
      }
    }
  };
}

for (const narrow of [false, true]) {
  test(`late provider discovery cannot replace the voice for a newer provider${narrow ? ' on a narrow screen' : ''}`, async ({
    page
  }, info) => {
    const data = await fixture(page);
    if (narrow) await page.setViewportSize({ width: 390, height: 844 });
    try {
      await data.delayDiscovery();
      await data.dialog.getByLabel('TTS service').selectOption('provider_b');
      await expect(
        data.dialog.getByRole('combobox', { name: 'Voice', exact: true })
      ).toHaveValue('voice-b');
      const voice = data.dialog.getByRole('combobox', {
        name: 'Voice',
        exact: true
      });
      await voice.focus();
      await expect(voice).toBeFocused();
      await page.keyboard.press('Tab');
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth
        )
      ).toBe(true);
      const accessibility = await new AxeBuilder({ page })
        .include('[role="dialog"]')
        .withTags(['wcag2a', 'wcag2aa'])
        .analyze();
      expect(
        accessibility.violations.filter((violation) =>
          ['serious', 'critical'].includes(violation.impact ?? '')
        )
      ).toEqual([]);
      await data.dialog.screenshot({
        path: info.outputPath(
          narrow ? 'tts-selection-narrow.png' : 'tts-selection-desktop.png'
        )
      });
      await data.releaseDiscovery();
      await expect(data.dialog.getByLabel('TTS service')).toHaveValue(
        'provider_b'
      );
      await expect(data.dialog.getByLabel('TTS model')).toHaveValue('model-b');
      await expect(
        data.dialog.getByRole('combobox', { name: 'Voice', exact: true })
      ).toHaveValue('voice-b');
    } finally {
      await data.releaseDiscovery();
    }
  });
}

for (const changeModel of [false, true]) {
  test(`late provider discovery preserves a voice edit${changeModel ? ' after changing model' : ''}`, async ({
    page
  }) => {
    const data = await fixture(page);
    try {
      await data.delayDiscovery();
      if (changeModel)
        await data.dialog.getByLabel('TTS model').selectOption('model-a-2');
      const voice = data.dialog.getByRole('combobox', {
        name: 'Voice',
        exact: true
      });
      await voice.selectOption('custom-a');
      await data.releaseDiscovery();
      await expect(data.dialog.getByLabel('TTS model')).toHaveValue(
        changeModel ? 'model-a-2' : 'model-a'
      );
      await expect(voice).toHaveValue('custom-a');
    } finally {
      await data.releaseDiscovery();
    }
  });
}

for (const fail of [false, true]) {
  test(`a late catalogue refresh ${fail ? 'failure' : 'response'} preserves a newer provider choice`, async ({
    page
  }) => {
    const data = await fixture(page);
    try {
      await data.delayRefresh();
      await data.dialog.getByLabel('TTS service').selectOption('provider_a');
      await expect(
        data.dialog.getByRole('combobox', { name: 'Voice', exact: true })
      ).toHaveValue('voice-a');
      await data.releaseRefresh(fail);
      await expect(data.dialog.getByLabel('TTS service')).toHaveValue(
        'provider_a'
      );
      await expect(data.dialog.getByLabel('TTS model')).toHaveValue('model-a');
      if (fail) {
        await expect(data.dialog.getByRole('alert')).toContainText(
          'Controlled catalogue outage'
        );
      } else {
        await expect(
          data.dialog
            .getByLabel('TTS model')
            .locator('option[value="fresh-provider_a"]')
        ).toHaveCount(1);
      }
      await expect(
        data.dialog.getByRole('combobox', { name: 'Voice', exact: true })
      ).toHaveValue('voice-a');
    } finally {
      await data.releaseRefresh();
    }
  });
}

test('a discovery response from a closed settings dialog cannot modify its replacement', async ({
  page
}) => {
  const data = await fixture(page);
  try {
    await data.delayDiscovery();
    await data.dialog
      .getByRole('button', { name: 'Close stage settings' })
      .click();
    const reopened = await data.open();
    await expect(reopened.getByLabel('TTS service')).toHaveValue('provider_b');
    await data.releaseDiscovery();
    await expect(
      reopened.getByRole('combobox', { name: 'Voice', exact: true })
    ).toHaveValue('voice-b');
  } finally {
    await data.releaseDiscovery();
  }
});

for (const edit of ['provider', 'model', 'voice'] as const) {
  test(`a late voice publication preserves a newer ${edit} choice`, async ({
    page
  }) => {
    const data = await fixture(page, true);
    try {
      await data.publishVoice();
      if (edit === 'provider')
        await data.dialog.getByLabel('TTS service').selectOption('provider_a');
      if (edit === 'model')
        await data.dialog.getByLabel('TTS model').selectOption('model-b-2');
      if (edit === 'voice')
        await data.dialog
          .getByRole('combobox', { name: 'Voice', exact: true })
          .selectOption('custom-b');
      await data.releasePublication();
      await expect(
        data.dialog.getByRole('button', { name: /Uploading…/ })
      ).toHaveCount(0);
      await expect(data.dialog.getByLabel('TTS service')).toHaveValue(
        edit === 'provider' ? 'provider_a' : 'provider_b'
      );
      await expect(data.dialog.getByLabel('TTS model')).toHaveValue(
        edit === 'model'
          ? 'model-b-2'
          : edit === 'provider'
            ? 'model-a'
            : 'model-b'
      );
      await expect(
        data.dialog.getByRole('combobox', { name: 'Voice', exact: true })
      ).toHaveValue(
        edit === 'voice'
          ? 'custom-b'
          : edit === 'provider'
            ? 'voice-a'
            : 'voice-b-2'
      );
      if (edit === 'provider') {
        // Provider B's upload must not create a registration for A.
        await expect(
          data.dialog.getByRole('button', { name: 'Upload & use', exact: true })
        ).toBeVisible();
        await data.dialog.getByLabel('TTS service').selectOption('provider_b');
        await expect(
          data.dialog.getByRole('button', { name: 'Use', exact: true })
        ).toBeVisible();
      }
    } finally {
      await data.releasePublication();
    }
  });
}

test('a completed voice publication selects its current ready registration', async ({
  page
}) => {
  const data = await fixture(page, true);
  try {
    await data.publishVoice();
    await data.releasePublication();
    await expect(
      data.dialog.getByRole('button', { name: 'Selected', exact: true })
    ).toBeVisible();
    await expect(
      data.dialog.getByRole('combobox', { name: 'Voice', exact: true })
    ).toHaveValue('published-b');
  } finally {
    await data.releasePublication();
  }
});

for (const result of ['stale', 'canceled', 'readback-failure'] as const) {
  test(`a ${result} voice publication does not invent a ready selection`, async ({
    page
  }, info) => {
    const data = await fixture(page, true);
    try {
      await data.publishVoice();
      await data.releasePublication(result);
      await expect(
        data.dialog.getByRole('button', { name: /Uploading…/ })
      ).toHaveCount(0);
      await expect(
        data.dialog.getByRole('combobox', { name: 'Voice', exact: true })
      ).toHaveValue('voice-b');
      if (result === 'stale') {
        await expect(
          data.dialog.getByRole('button', { name: 'Update & use', exact: true })
        ).toBeVisible();
        await expect(
          data.dialog
            .getByRole('status')
            .filter({ hasText: 'Publication narrator' })
        ).toContainText('needs');
        await page.setViewportSize({ width: 390, height: 844 });
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth
          )
        ).toBe(true);
        await data.dialog.screenshot({
          path: info.outputPath('voice-publication-stale-narrow.png')
        });
      }
      if (result === 'canceled')
        await expect(
          data.dialog.getByRole('button', { name: 'Use', exact: true })
        ).toBeVisible();
      if (result === 'readback-failure')
        await expect(data.dialog).toContainText(
          'Controlled voice readback outage'
        );
    } finally {
      await data.releasePublication();
    }
  });
}

test('a voice publication cannot change a reopened settings draft', async ({
  page
}) => {
  const data = await fixture(page, true);
  try {
    await data.publishVoice();
    await data.dialog
      .getByRole('button', { name: 'Close stage settings' })
      .click();
    const reopened = await data.open();
    await data.releasePublication();
    await expect(
      reopened.getByRole('button', { name: /Uploading…/ })
    ).toHaveCount(0);
    await expect(
      reopened.getByRole('combobox', { name: 'Voice', exact: true })
    ).toHaveValue('voice-b');
  } finally {
    await data.releasePublication();
  }
});

test('a catalogue refresh cannot replace a newer publication registration', async ({
  page
}, info) => {
  const data = await fixture(page, true);
  try {
    await data.publishVoice();
    await data.delayRefresh();
    await data.releasePublication();
    await expect(
      data.dialog.getByRole('button', { name: 'Selected', exact: true })
    ).toBeVisible();
    await data.releaseRefresh();
    await expect(
      data.dialog
        .getByLabel('TTS model')
        .locator('option[value="fresh-provider_b"]')
    ).toHaveCount(1);
    await expect(
      data.dialog.getByRole('button', { name: 'Selected', exact: true })
    ).toBeVisible();
    await expect(
      data.dialog.getByRole('combobox', { name: 'Voice', exact: true })
    ).toHaveValue('published-b');
    await data.dialog.screenshot({
      path: info.outputPath('voice-publication-refreshed.png')
    });
  } finally {
    await data.releasePublication();
    await data.releaseRefresh();
  }
});

test('a voice readback from a closed settings dialog cannot update its replacement', async ({
  page
}) => {
  const data = await fixture(page, true);
  try {
    await data.publishVoice();
    data.delayVoiceReadback();
    await data.releasePublication();
    await data.waitForVoiceReadback();
    await data.dialog
      .getByRole('button', { name: 'Close stage settings' })
      .click();
    const reopened = await data.open();
    await data.releaseVoiceReadback();
    await expect(
      reopened.getByRole('combobox', { name: 'Voice', exact: true })
    ).toHaveValue('voice-b');
    await reopened
      .locator('summary')
      .filter({ hasText: 'Available from your Voice Library' })
      .click();
    await expect(
      reopened.getByRole('button', { name: 'Upload & use', exact: true })
    ).toBeVisible();
  } finally {
    await data.releasePublication();
    await data.releaseVoiceReadback();
  }
});
