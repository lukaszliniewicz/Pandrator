import { expect, test, type Page } from '@playwright/test';

type SlimModel = {
  id: string;
  label: string;
  family?: string;
  voice_mode?: string;
  supported_languages?: string[];
  loaded?: boolean;
  package_availability?: { status: string; reason?: string };
};

const CATALOG: SlimModel[] = [
  {
    id: 'qwen3_tts_1_7b_base_q8_0',
    label: 'Qwen3 TTS 12Hz 1.7B Base Q8_0 GGUF',
    family: 'qwen3_tts',
    voice_mode: 'cloning',
    supported_languages: ['en']
  },
  {
    id: 'qwen3_tts_1_7b_base_bf16',
    label: 'Qwen3 TTS 12Hz 1.7B Base BF16 GGUF',
    family: 'qwen3_tts',
    voice_mode: 'cloning',
    supported_languages: ['en'],
    loaded: true
  },
  {
    id: 'qwen3_tts_1_7b_customvoice_q8_0',
    label: 'Qwen3 TTS 12Hz 1.7B CustomVoice Q8_0 GGUF',
    family: 'qwen3_tts',
    voice_mode: 'prebuilt',
    supported_languages: ['en']
  },
  {
    id: 'qwen3_tts_1_7b_voicedesign_q8_0',
    label: 'Qwen3 TTS 12Hz 1.7B VoiceDesign Q8_0 GGUF',
    family: 'qwen3_tts',
    voice_mode: 'design',
    supported_languages: ['en']
  },
  {
    id: 'pocket_tts_english_q8_0',
    label: 'PocketTTS English Q8_0 GGUF',
    family: 'pocket_tts',
    voice_mode: 'hybrid',
    supported_languages: ['en']
  },
  {
    id: 'pocket_tts_german_q8_0',
    label: 'PocketTTS German Q8_0 GGUF',
    family: 'pocket_tts',
    voice_mode: 'hybrid',
    supported_languages: ['de'],
    package_availability: {
      status: 'installable',
      reason: 'Available through Pandrator Manager'
    }
  },
  {
    id: 'pocket_tts_german_bf16',
    label: 'PocketTTS German BF16 GGUF',
    family: 'pocket_tts',
    voice_mode: 'hybrid',
    supported_languages: ['de'],
    package_availability: { status: 'installable' }
  },
  { id: 'mystery_custom', label: 'Mystery Custom' }
];

const SERVICE = {
  id: 'audio_cpp',
  name: 'audio.cpp',
  adapter: 'audio_cpp',
  available: true,
  models: [
    'qwen3_tts_1_7b_base_q8_0',
    'qwen3_tts_1_7b_customvoice_q8_0',
    'pocket_tts_english_q8_0'
  ],
  default_model: 'qwen3_tts_1_7b_base_q8_0',
  voices: [],
  model_catalog: CATALOG,
  model_voice_modes: {
    qwen3_tts_1_7b_base_q8_0: 'cloning',
    qwen3_tts_1_7b_base_bf16: 'cloning',
    qwen3_tts_1_7b_customvoice_q8_0: 'prebuilt',
    qwen3_tts_1_7b_voicedesign_q8_0: 'design',
    pocket_tts_english_q8_0: 'hybrid',
    pocket_tts_german_q8_0: 'hybrid'
  },
  voice_catalogues: {},
  voice_metadata: {}
};

async function fixture(page: Page, model: string) {
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
      name: `Local model picker ${crypto.randomUUID()}`,
      workflow_kind: 'audiobook'
    }
  });
  expect(created.ok()).toBeTruthy();
  const sessionId = (await created.json()).id as string;
  const settings = await (
    await page.request.get(`/api/v1/sessions/${sessionId}/settings/tts`)
  ).json();
  const writes: string[] = [];
  page.on('request', (request) => {
    if (
      request.method() !== 'GET' &&
      request.url().includes(`/sessions/${sessionId}/settings/tts`)
    )
      writes.push(`${request.method()} ${request.url()}`);
  });
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
            model,
            voice: '',
            language: 'en'
          },
          override: { service: 'audio_cpp', model }
        }
      });
    }
  );
  await page.route('**/api/v1/services/tts*', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/v1/services/tts/audio_cpp') {
      const selected =
        url.searchParams.get('model') ??
        url.searchParams.get('models') ??
        model;
      await route.fulfill({
        json: {
          view: 'detail',
          selected_models: [selected],
          service: {
            ...SERVICE,
            model_catalog: [
              {
                id: selected,
                label: selected,
                request_parameters: {}
              }
            ]
          }
        }
      });
      return;
    }
    if (url.pathname !== '/api/v1/services/tts') return route.continue();
    await route.fulfill({
      json: {
        view: 'compact',
        services: [SERVICE],
        default_service: 'audio_cpp'
      }
    });
  });
  await page.goto(`/sessions/${sessionId}/voice`);
  const settingsPanel = page.locator('.settings-panel').filter({
    has: page.getByRole('heading', {
      name: 'Speech generation',
      exact: true
    })
  });
  const trigger = settingsPanel.getByRole('button', { name: /^Model: / });
  await expect(trigger).toBeVisible();
  const dialog = page.getByRole('dialog', { name: 'Model', exact: true });
  return { settingsPanel, trigger, dialog, writes };
}

test('grouped picker nests quants under variants and selects an exact Qwen id', async ({
  page
}, testInfo) => {
  const { settingsPanel, trigger, dialog, writes } = await fixture(
    page,
    'qwen3_tts_1_7b_base_q8_0'
  );
  await expect(trigger).toContainText('qwen3_tts_1_7b_base_q8_0');
  await trigger.click();
  await expect(dialog).toBeVisible();
  // Base, CustomVoice and VoiceDesign stay distinct under one family.
  // The selection's family opens by default; toggling shuts it again.
  const qwen = dialog.getByRole('button', { name: /Qwen3-TTS/ });
  await expect(qwen).toHaveAttribute('aria-expanded', 'true');
  await qwen.click();
  await expect(qwen).toHaveAttribute('aria-expanded', 'false');
  await expect(dialog.getByRole('button', { name: /1\.7B Base/ })).toHaveCount(
    0
  );
  await qwen.click();
  await expect(qwen).toHaveAttribute('aria-expanded', 'true');
  await expect(
    dialog.getByRole('button', { name: /1\.7B CustomVoice/ })
  ).toBeVisible();
  await expect(
    dialog.getByRole('button', { name: /1\.7B VoiceDesign/ })
  ).toBeVisible();
  await expect(
    dialog.getByRole('button', { name: /1\.7B Base/ })
  ).toHaveAttribute('aria-expanded', 'true');
  // Both quants nest under the same variant with exact ids visible.
  await expect(
    dialog.getByRole('radio', { name: /qwen3_tts_1_7b_base_q8_0/ })
  ).toBeVisible();
  await expect(
    dialog.getByRole('radio', { name: /qwen3_tts_1_7b_base_bf16/ })
  ).toBeVisible();
  // Live evidence surfaces without claiming installed from models[].
  // Radios carry no text themselves; the badge lives in the label's
  // accessible name.
  await expect(
    dialog.getByRole('radio', { name: /qwen3_tts_1_7b_base_bf16/ })
  ).toHaveAccessibleName(/Loaded/);
  await page.screenshot({
    path: testInfo.outputPath('grouped-models-desktop.png')
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(async () => {
      const box = await dialog.boundingBox();
      return box ? box.x + box.width : Infinity;
    })
    .toBeLessThanOrEqual(390);
  const bounds = await dialog.boundingBox();
  expect(bounds).not.toBeNull();
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390);
  expect(bounds!.y).toBeGreaterThanOrEqual(0);
  expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(844);
  await page.screenshot({
    path: testInfo.outputPath('grouped-models-mobile.png')
  });
  await page.setViewportSize({ width: 1280, height: 720 });
  await dialog.getByRole('button', { name: /1\.7B CustomVoice/ }).click();
  await dialog
    .getByRole('radio', { name: /qwen3_tts_1_7b_customvoice_q8_0/ })
    .click();
  await expect(dialog).toBeHidden();
  await expect(trigger).toContainText('qwen3_tts_1_7b_customvoice_q8_0');
  await expect(trigger).toContainText('Qwen3-TTS · 1.7B CustomVoice · Q8_0');
  await expect(
    settingsPanel.getByRole('button', { name: 'Save', exact: true })
  ).toBeEnabled();
  expect(writes).toEqual([]);
});

test('search reaches Pocket languages and selects an exact installable id', async ({
  page
}) => {
  const { trigger, dialog, writes } = await fixture(
    page,
    'qwen3_tts_1_7b_base_q8_0'
  );
  await trigger.click();
  await expect(dialog).toBeVisible();
  await dialog.getByLabel('Search models').fill('german');
  await expect(dialog.getByRole('button', { name: /PocketTTS/ })).toBeVisible();
  await expect(dialog.getByRole('button', { name: /Qwen3-TTS/ })).toHaveCount(
    0
  );
  await expect(
    dialog.getByRole('radio', { name: /pocket_tts_german_q8_0/ })
  ).toHaveAccessibleName(/Installable/);
  await dialog.getByRole('radio', { name: /pocket_tts_german_bf16/ }).click();
  await expect(trigger).toContainText('PocketTTS · German · BF16');
  await expect(trigger).toContainText('pocket_tts_german_bf16');
  expect(writes).toEqual([]);
});

test('expanding and searching never change the current model', async ({
  page
}) => {
  const { trigger, dialog, writes } = await fixture(
    page,
    'qwen3_tts_1_7b_base_q8_0'
  );
  const before = await trigger.getAttribute('aria-label');
  await trigger.click();
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: /Qwen3-TTS/ }).click();
  await dialog.getByRole('button', { name: /PocketTTS/ }).click();
  await dialog.getByRole('button', { name: /Other models/ }).click();
  await dialog.getByLabel('Search models').fill('no-such-model-fixture');
  await expect(dialog.getByText('No models match.')).toBeVisible();
  await expect(
    dialog.getByText('The current selection is kept unchanged.')
  ).toBeVisible();
  await dialog.getByLabel('Search models').fill('');
  await dialog.press('Escape');
  await expect(dialog).toBeHidden();
  expect(await trigger.getAttribute('aria-label')).toBe(before);
  expect(writes).toEqual([]);
});

test('unknown current selection is preserved with an Unknown badge', async ({
  page
}) => {
  const { trigger, dialog, writes } = await fixture(page, 'retired_custom_id');
  await expect(trigger).toContainText('retired_custom_id');
  await expect(trigger).toContainText('Unknown');
  await trigger.click();
  await expect(dialog).toBeVisible();
  // The unknown selection's fallback group opens by default.
  await expect(
    dialog.getByRole('button', { name: /Other models/ })
  ).toHaveAttribute('aria-expanded', 'true');
  const option = dialog.getByRole('radio', { name: /retired_custom_id/ });
  await expect(option).toBeVisible();
  await expect(option).toHaveAccessibleName(/Unknown/);
  await expect(option).toHaveAccessibleName(/Custom/);
  await dialog.press('Escape');
  await expect(trigger).toContainText('retired_custom_id');
  expect(writes).toEqual([]);
});
