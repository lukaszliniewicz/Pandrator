import { expect, test, type Page, type Locator } from '@playwright/test';
import { openVoicePage } from './helpers/voice-page';

const models = [
  ['qwen3_tts_1_7b_base_q8_0', 'qwen3_tts', '350'],
  ['breeze_tts_2_q8_0', 'breeze_tts', '450'],
  ['voxcpm2_q8_0', 'voxcpm2', '350']
];

async function project(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const { csrf_token } = await (
    await page.request.get('/api/v1/auth/status')
  ).json();
  const headers = { 'X-CSRF-Token': csrf_token };
  const created = await page.request.post('/api/v1/sessions', {
    headers,
    data: {
      name: `Narration model length ${crypto.randomUUID()}`,
      workflow_kind: 'audiobook',
      interface_mode: 'advanced',
      source_language: 'en'
    }
  });
  expect(created.ok()).toBeTruthy();
  const { id } = await created.json();
  const tts = await (
    await page.request.get(`/api/v1/sessions/${id}/settings/tts`)
  ).json();
  const saved = await page.request.put(`/api/v1/sessions/${id}/settings/tts`, {
    headers: { ...headers, 'If-Match': `"${tts.revision}"` },
    data: {
      value: { service: 'audio_cpp', model: models[0][0], language: 'en' }
    }
  });
  expect(saved.ok()).toBeTruthy();
  const service = {
    id: 'audio_cpp',
    name: 'audio.cpp',
    adapter: 'audio_cpp',
    available: true,
    models: models.map(([id]) => id),
    default_model: models[0][0],
    model_voice_modes: Object.fromEntries(
      models.map(([id]) => [id, 'optional_cloning'])
    ),
    model_catalog: models.map(([id, family]) => ({
      id,
      family,
      supported_languages: ['en'],
      voice_mode: 'optional_cloning',
      request_parameters: {}
    }))
  };
  await page.route('**/api/v1/services/tts**', (route) =>
    route.fulfill({
      json: route.request().url().includes('/tts/audio_cpp')
        ? { service }
        : { services: [service], default_service: 'audio_cpp' }
    })
  );
  return id as string;
}

async function chooseModel(
  page: Page,
  root: Locator,
  label: string,
  model: string
) {
  await root.getByRole('button', { name: new RegExp(`^${label}:`) }).click();
  await page
    .getByRole('searchbox', { name: 'Search models', exact: true })
    .fill(model);
  await page.locator(`input[type="radio"][value="${model}"]`).check();
}

test('model changes update active narration length in the generation settings modal before saving', async ({
  page
}) => {
  const id = await project(page);
  await page.goto(`/sessions/${id}`);
  const card = page
    .getByRole('heading', { name: 'Generate audio', exact: true })
    .locator('xpath=ancestor::article');
  await card.getByRole('button', { name: 'Settings', exact: true }).click();
  const dialog = page.getByRole('dialog', {
    name: 'Generate audio',
    exact: true
  });
  const length = dialog.getByRole('spinbutton', {
    name: 'Model default (characters)',
    exact: true
  });
  await expect(length).toHaveValue('350');
  for (const [model, , target] of models.slice(1)) {
    await chooseModel(page, dialog, 'TTS model', model);
    await expect(length).toHaveValue(target);
  }
  await dialog
    .getByRole('combobox', { name: /^Length policy/ })
    .selectOption('manual');
  await dialog
    .getByRole('spinbutton', { name: 'Maximum characters', exact: true })
    .fill('74');
  await dialog
    .getByRole('button', { name: 'Save settings', exact: true })
    .click();
  await expect(dialog).toBeHidden();
  const text = await (
    await page.request.get(`/api/v1/sessions/${id}/settings/text`)
  ).json();
  expect(text.effective.audiobook_chunking).toBe('manual');
  expect(text.effective.max_sentence_length).toBe(74);
  await openVoicePage(page, id);
  const panel = page
    .getByRole('heading', { name: 'Speech generation', exact: true })
    .locator('xpath=ancestor::section[1]');
  await expect(
    panel.getByRole('spinbutton', { name: 'Maximum characters', exact: true })
  ).toHaveValue('74');
  await chooseModel(page, panel, 'Model', models[0][0]);
  await expect(
    panel.getByRole('spinbutton', { name: 'Maximum characters', exact: true })
  ).toHaveValue('74');
  await panel
    .getByRole('combobox', { name: /^Length policy/ })
    .selectOption('model');
  await expect(
    panel.getByRole('spinbutton', {
      name: 'Model default (characters)',
      exact: true
    })
  ).toHaveValue('350');
  await panel.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(
    panel.getByText('Saved for this project.', { exact: true })
  ).toBeVisible();
  await page.reload();
  await expect(
    panel.getByRole('spinbutton', {
      name: 'Model default (characters)',
      exact: true
    })
  ).toHaveValue('350');
  await page.setViewportSize({ width: 390, height: 844 });
  await panel
    .getByRole('group', { name: 'Narration length', exact: true })
    .scrollIntoViewIfNeeded();
  await page.screenshot({
    path: test.info().outputPath('narration-length-mobile.png')
  });
});
