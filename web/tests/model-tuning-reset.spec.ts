import { expect, test } from '@playwright/test';

test('model defaults and inherited tuning survive project save and reload', async ({
  page
}, testInfo) => {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const headers = { 'X-CSRF-Token': auth.csrf_token };
  const model = 'qwen3_tts_1_7b_customvoice_q8_0';
  const defaultsUrl = '/api/v1/settings/defaults.tts';
  const originalResponse = await page.request.get(defaultsUrl);
  const original = originalResponse.ok()
    ? await originalResponse.json()
    : { revision: 0, value: {} };
  const defaults = {
    ...original.value,
    audio_cpp_model_settings: { [model]: { temperature: 0.8, seed: 17 } }
  };
  expect(
    (
      await page.request.put(defaultsUrl, {
        headers: { ...headers, 'If-Match': `"${original.revision}"` },
        data: { value: defaults }
      })
    ).ok()
  ).toBeTruthy();
  try {
    const service = {
      id: 'audio_cpp',
      name: 'audio.cpp',
      adapter: 'audio_cpp',
      available: true,
      models: [model],
      default_model: model,
      default_voice: 'Vivian',
      model_voice_modes: { [model]: 'prebuilt' },
      voice_catalogues: { [model]: ['Vivian'] },
      model_catalog: [
        {
          id: model,
          family: 'qwen3_tts',
          voice_mode: 'prebuilt',
          supported_languages: ['en'],
          request_parameters: {
            temperature: {
              type: 'number',
              default: 0.9,
              minimum: 0,
              maximum: 2
            },
            seed: { type: 'integer', minimum: 0 }
          }
        }
      ]
    };
    await page.route('**/api/v1/services/tts**', (route) =>
      route.fulfill({
        json: route.request().url().includes('/tts/audio_cpp')
          ? { service }
          : { services: [service], default_service: 'audio_cpp' }
      })
    );
    const session = await (
      await page.request.post('/api/v1/sessions', {
        headers,
        data: {
          name: `Tuning reset ${crypto.randomUUID()}`,
          interface_mode: 'advanced',
          workflow_kind: 'voiceover'
        }
      })
    ).json();
    const settingsUrl = `/api/v1/sessions/${session.id}/settings/tts`;
    const initial = await (await page.request.get(settingsUrl)).json();
    expect(
      (
        await page.request.put(settingsUrl, {
          headers: { ...headers, 'If-Match': `"${initial.revision}"` },
          data: {
            value: {
              service: 'audio_cpp',
              model,
              voice: 'Vivian',
              language: 'en'
            }
          }
        })
      ).ok()
    ).toBeTruthy();
    await page.goto(`/sessions/${session.id}/voice`);
    const panel = page
      .getByRole('heading', { name: 'Speech generation', exact: true })
      .locator('xpath=ancestor::section[1]');
    const tuning = panel.getByRole('region', { name: 'Model tuning' });
    const temperature = tuning.getByRole('spinbutton', {
      name: 'Temperature',
      exact: true
    });
    await expect(temperature).toHaveValue('0.8');
    await expect(tuning.getByRole('status')).toHaveText('Inherited tuning');
    await tuning
      .getByRole('button', { name: 'Use model defaults', exact: true })
      .click();
    await expect(temperature).toHaveValue('');
    await panel.getByRole('button', { name: 'Save', exact: true }).click();
    await expect(
      panel.getByText('Saved for this project.', { exact: true })
    ).toBeVisible();
    expect(
      (await (await page.request.get(settingsUrl)).json()).override
        .audio_cpp_model_settings[model]
    ).toBeNull();
    await page.reload();
    await expect(tuning.getByRole('status')).toHaveText('Model defaults');
    await expect(temperature).toHaveValue('');
    await tuning
      .getByRole('button', { name: 'Inherit saved tuning', exact: true })
      .click();
    await expect(temperature).toHaveValue('0.8');
    await panel.getByRole('button', { name: 'Save', exact: true }).click();
    await expect(
      panel.getByText('Saved for this project.', { exact: true })
    ).toBeVisible();
    await page.reload();
    await expect(temperature).toHaveValue('0.8');
    await expect(tuning.getByRole('status')).toHaveText('Inherited tuning');
    expect((await (await page.request.get(defaultsUrl)).json()).value).toEqual(
      defaults
    );
    await tuning.screenshot({
      path: testInfo.outputPath('model-tuning-inheritance.png')
    });
  } finally {
    const current = await (await page.request.get(defaultsUrl)).json();
    await page.request.put(defaultsUrl, {
      headers: { ...headers, 'If-Match': `"${current.revision}"` },
      data: { value: original.value }
    });
  }
});
