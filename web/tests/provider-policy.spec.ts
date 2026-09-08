import { expect, test, type Page } from '@playwright/test';

async function signIn(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const closeTour = page.getByRole('button', { name: 'Close tour' });
  if (await closeTour.isVisible()) await closeTour.click();
}

const targetModel = 'qwen3_tts_1_7b_customvoice_q8_0';
const services = [
  {
    id: 'audio_cpp',
    name: 'audio.cpp',
    catalogue_role: 'primary',
    available: true,
    models: [targetModel],
    default_model: targetModel,
    default_voice: 'Ryan',
    supports_prebuilt_voices: true,
    supports_voice_cloning: true,
    model_voice_modes: { [targetModel]: 'prebuilt' },
    model_catalog: [
      { id: targetModel, family: 'qwen3_tts', voice_mode: 'prebuilt' }
    ],
    voice_catalogues: { [targetModel]: ['Ryan', 'Serena'] }
  },
  {
    id: 'kobold_qwen',
    name: 'Qwen3 TTS',
    catalogue_role: 'compatibility',
    available: true,
    replacement_service_id: 'audio_cpp',
    replacement_model_family: 'qwen3_tts',
    models: ['Prebuilt Voices'],
    default_model: 'Prebuilt Voices',
    default_voice: 'Ryan',
    supports_prebuilt_voices: true,
    model_voice_modes: { 'Prebuilt Voices': 'prebuilt' },
    voice_catalogues: { 'Prebuilt Voices': ['Ryan'] }
  },
  {
    id: 'fishs2',
    name: 'Fish S2 Pro',
    catalogue_role: 'compatibility',
    available: true
  },
  { id: 'kokoro', name: 'Kokoro', catalogue_role: 'primary', available: true }
];

for (const saveAsDefaults of [false, true]) {
  test(`saved compatibility provider switches with reviewed native voice${saveAsDefaults ? ' as defaults' : ''}`, async ({
    page
  }, testInfo) => {
    await signIn(page);
    const csrf = (await (await page.request.get('/api/v1/auth/status')).json())
      .csrf_token;
    const headers = { 'X-CSRF-Token': csrf };
    const session = await (
      await page.request.post('/api/v1/sessions', {
        headers,
        data: {
          name: `Provider switch ${crypto.randomUUID()}`,
          workflow_kind: 'voiceover'
        }
      })
    ).json();
    const settingsUrl = `/api/v1/sessions/${session.id}/settings/tts`;
    const current = await (await page.request.get(settingsUrl)).json();
    const oldSettings = {
      service: 'kobold_qwen',
      tts_service: 'kobold_qwen',
      model: 'Prebuilt Voices',
      xtts_model: 'Prebuilt Voices',
      voice: 'Ryan',
      speaker: 'Ryan',
      options: { temperature: 0.9 },
      kobold_qwen_temperature: 0.9
    };
    expect(
      (
        await page.request.put(settingsUrl, {
          headers: { ...headers, 'If-Match': `"${current.revision}"` },
          data: { value: oldSettings }
        })
      ).ok()
    ).toBeTruthy();
    await page.route('**/api/v1/services/tts**', (route) =>
      route.fulfill({ json: { services, default_service: 'audio_cpp' } })
    );
    await page.route('**/api/v1/voices', (route) =>
      route.fulfill({ json: { items: [] } })
    );
    await page.goto(`/sessions/${session.id}`);
    const card = page
      .getByRole('heading', { name: 'Generate audio', exact: true })
      .locator('xpath=ancestor::article');
    await card.getByRole('button', { name: 'Settings' }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByLabel('TTS service')).toHaveValue('kobold_qwen');
    await expect(
      dialog.getByLabel('TTS service').locator('option[value="fishs2"]')
    ).toHaveCount(0);
    await expect(
      dialog.getByText('Compatibility provider', { exact: true })
    ).toBeVisible();
    await dialog.getByRole('button', { name: 'Switch to audio.cpp' }).click();
    await expect(dialog.getByLabel('TTS service')).toHaveValue('audio_cpp');
    await expect(dialog.getByLabel('TTS model', { exact: true })).toHaveValue(
      targetModel
    );
    await expect(
      dialog.getByRole('combobox', { name: 'Voice', exact: true })
    ).toHaveValue('Ryan');
    await expect(
      dialog.getByRole('button', { name: 'Save settings', exact: true })
    ).toBeDisabled();
    expect(
      (await (await page.request.get(settingsUrl)).json()).override
    ).toEqual(oldSettings);
    await dialog
      .getByLabel('I reviewed the target model, voice, and settings reset.')
      .check();
    await page.screenshot({
      path: testInfo.outputPath('provider-switch-review.png'),
      fullPage: false
    });
    await dialog
      .getByRole('button', {
        name: saveAsDefaults ? 'Save as defaults' : 'Save settings',
        exact: true
      })
      .click();
    await expect
      .poll(
        async () =>
          (await (await page.request.get(settingsUrl)).json()).effective.service
      )
      .toBe('audio_cpp');
    const saved = await (await page.request.get(settingsUrl)).json();
    expect(saved.effective).toMatchObject({
      service: 'audio_cpp',
      tts_service: 'audio_cpp',
      model: targetModel,
      xtts_model: targetModel,
      voice: 'Ryan',
      speaker: 'Ryan',
      options: {}
    });
    expect(saved.override).not.toHaveProperty('provider_switch_reviewed');
    expect(saved.override).not.toHaveProperty('kobold_qwen_temperature');
  });
}

test('global service picker uses catalogue policy and clears an old selection on switch', async ({
  page
}, testInfo) => {
  await signIn(page);
  await page.route('**/api/v1/services/tts**', (route) =>
    route.fulfill({ json: { services, default_service: 'audio_cpp' } })
  );
  await page.route('**/api/v1/defaults/tts', (route) =>
    route.fulfill({
      json: {
        section: 'tts',
        revision: 1,
        builtin: { service: 'audio_cpp', model: '', voice: '' },
        value: {
          service: 'kobold_qwen',
          model: 'Prebuilt Voices',
          voice: 'Ryan'
        },
        effective: {
          service: 'kobold_qwen',
          model: 'Prebuilt Voices',
          voice: 'Ryan'
        }
      }
    })
  );
  let saved: Record<string, unknown> | null = null;
  await page.route('**/api/v1/settings/defaults.tts', (route) => {
    saved = route.request().postDataJSON().value;
    return route.fulfill({ json: { value: saved, revision: 2 } });
  });
  await page.goto('/settings');
  await page.locator('summary').filter({ hasText: /^TTS$/ }).click();
  const panel = page
    .locator('details')
    .filter({ has: page.locator('summary').filter({ hasText: /^TTS$/ }) });
  const select = panel.getByRole('combobox', { name: 'Service', exact: true });
  await expect(select).toHaveValue('kobold_qwen');
  await expect(select.locator('option')).toHaveText([
    'audio.cpp',
    'Qwen3 TTS · compatibility',
    'Kokoro'
  ]);
  await select.selectOption('audio_cpp');
  await panel.getByRole('button', { name: 'Save global defaults' }).click();
  await expect
    .poll(() => saved)
    .toMatchObject({
      service: 'audio_cpp',
      model: '',
      voice: '',
      xtts_model: '',
      speaker: ''
    });
  await expect(select.locator('option')).toHaveText(['audio.cpp', 'Kokoro']);
  await page.screenshot({
    path: testInfo.outputPath('global-provider-picker.png')
  });
});
