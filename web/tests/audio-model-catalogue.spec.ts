import { expect, test } from '@playwright/test';

async function signIn(page: import('@playwright/test').Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(
    page.getByRole('heading', { name: 'What shall we make?' })
  ).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  await page.goto('/models');
}

test('model catalogue exposes reviewed choices, licence filters and all-package pagination', async ({
  page
}, info) => {
  await signIn(page);
  await expect(
    page.getByRole('heading', { name: 'Find a model for the job' })
  ).toBeVisible();
  await expect(page.locator('article')).toHaveCount(10);
  const breeze = page
    .locator('article')
    .filter({ has: page.getByRole('heading', { name: /Breeze/ }) });
  await breeze
    .getByText('Languages, licence and controls', { exact: true })
    .click();
  await expect(
    breeze.getByText('BreezeBlue Research and Non-Commercial License 1.1')
  ).toBeVisible();
  await expect(
    breeze.getByText('Not implemented', { exact: true })
  ).toHaveCount(4);
  await page.screenshot({
    path: info.outputPath('catalogue-desktop.png'),
    fullPage: true
  });
  await page
    .getByRole('combobox', { name: 'Commercial use', exact: true })
    .selectOption('permitted');
  await page.getByRole('button', { name: 'Apply filters' }).click();
  await expect(breeze).toHaveCount(0);
  await page.getByLabel('Search models').fill('no-such-model-fixture');
  await page.getByRole('button', { name: 'Apply filters' }).click();
  await expect(
    page.getByText('No models match.', { exact: false })
  ).toBeVisible();
  await page.getByLabel('Search models').fill('');
  await page
    .getByRole('combobox', { name: 'Commercial use', exact: true })
    .selectOption('');
  await page.getByLabel('Recommended starting points').uncheck();
  await page.getByRole('button', { name: 'Apply filters' }).click();
  await expect(
    page.getByText('256 matching packages', { exact: false })
  ).toBeVisible();
  await expect(page.locator('article')).toHaveCount(20);
  await page.getByRole('button', { name: 'Next', exact: true }).click();
  await expect(page.getByText('21–40 of 256', { exact: true })).toBeVisible();
  await page.getByLabel('Task family').selectOption('asr');
  await page.getByRole('button', { name: 'Apply filters' }).click();
  await expect(
    page
      .locator('article')
      .first()
      .getByText('Catalogue only:', { exact: true })
  ).toBeVisible();
});

test('model catalogue stays usable on a narrow screen and recovers from an API error', async ({
  page
}, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page);
  await expect(page.locator('article')).toHaveCount(10);
  await page
    .locator('article')
    .first()
    .getByText('Languages, licence and controls', { exact: true })
    .click();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth
    )
  ).toBeTruthy();
  await page.screenshot({
    path: info.outputPath('catalogue-mobile.png'),
    fullPage: true
  });
  await page.route('**/api/v1/services/audio-cpp/catalogue?**', (route) =>
    route.fulfill({
      status: 503,
      json: { error: { message: 'Catalogue temporarily unavailable' } }
    })
  );
  await page.getByRole('button', { name: 'Apply filters' }).click();
  await expect(page.getByRole('alert')).toBeVisible();
  await page.unroute('**/api/v1/services/audio-cpp/catalogue?**');
  await page.getByRole('button', { name: 'Retry', exact: true }).click();
  await expect(page.getByRole('alert')).toHaveCount(0);
  await expect(page.locator('article')).toHaveCount(10);
});

test('voice design uses catalogue models, languages and their own licence', async ({
  page
}, info) => {
  await signIn(page);
  const ids = ['voxcpm2_q8_0', 'omnivoice_q8_0', 'chatterbox_turbo_q8_0'];
  const responses = await Promise.all(
    ids.map(async (id) => {
      const data = await (
        await page.request.get(
          `/api/v1/services/audio-cpp/catalogue?query=${id}`
        )
      ).json();
      const model = data.items.find((item: { id: string }) => item.id === id);
      return { ...model, catalogue_info: model };
    })
  );
  await page.route('**/api/v1/services/tts**', (route) =>
    route.fulfill({
      json: {
        default_service: 'audio_cpp',
        services: [
          {
            id: 'audio_cpp',
            adapter: 'audio_cpp',
            name: 'audio.cpp',
            available: true,
            models: ids,
            model_catalog: responses,
            supports_voice_cloning: true
          }
        ]
      }
    })
  );
  await page.goto('/voices');
  await page.getByRole('button', { name: 'Design voice', exact: true }).click();
  const dialog = page.getByRole('dialog');
  const model = dialog.getByRole('combobox', {
    name: 'Design model',
    exact: true
  });
  await expect(model.locator('option')).toHaveCount(2);
  await model.selectOption('voxcpm2_q8_0');
  await dialog
    .getByRole('combobox', { name: 'Language', exact: true })
    .selectOption('pl');
  await expect(
    dialog.getByText('Model licence: Apache-2.0.', { exact: false })
  ).toBeVisible();
  await model.selectOption('omnivoice_q8_0');
  await expect(
    dialog.getByLabel('Language code', { exact: true })
  ).toBeVisible();
  await expect(
    dialog.getByText('Model licence: CC-BY-NC-4.0 (weights).', { exact: false })
  ).toBeVisible();
  await page.screenshot({
    path: info.outputPath('catalogue-voice-design.png'),
    fullPage: true
  });
});
