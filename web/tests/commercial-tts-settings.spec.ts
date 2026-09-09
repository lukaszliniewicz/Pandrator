import { expect, test } from '@playwright/test';

test('session TTS settings filter MAI voices by model and language and retain multilingual voices', async ({
  page
}, testInfo) => {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const csrf = (await (await page.request.get('/api/v1/auth/status')).json())
    .csrf_token;
  const headers = { 'X-CSRF-Token': csrf };
  const session = await (
    await page.request.post('/api/v1/sessions', {
      headers,
      data: {
        name: `Commercial voices ${crypto.randomUUID()}`,
        workflow_kind: 'voiceover'
      }
    })
  ).json();
  const mai = 'MAI-Voice-2';
  const flash = 'MAI-Voice-2-Flash';
  const klaus = `de-DE-Klaus:${mai}`;
  const mia = `de-DE-Mia:${mai}`;
  const ethan = `en-US-Ethan:${mai}`;
  const services = [
    {
      id: 'mai',
      name: 'MAI Voice 2',
      available: true,
      supports_prebuilt_voices: true,
      supports_parallel_synthesis: true,
      models: [mai, flash],
      default_model: mai,
      default_voice: klaus,
      voice_catalogues: {
        [mai]: [klaus, mia, ethan],
        [flash]: [`de-DE-Mia:${flash}`]
      },
      voice_metadata: {
        [`${mai}:${klaus}`]: {
          locale: 'de-DE',
          language: 'German (Germany)',
          gender: 'Male'
        },
        [`${mai}:${mia}`]: {
          locale: 'de-DE',
          language: 'German (Germany)',
          gender: 'Female'
        },
        [`${mai}:${ethan}`]: {
          locale: 'en-US',
          language: 'English (United States)',
          gender: 'Male'
        },
        [`${flash}:de-DE-Mia:${flash}`]: {
          locale: 'de-DE',
          language: 'German (Germany)',
          gender: 'Female'
        }
      }
    },
    {
      id: 'openai',
      name: 'OpenAI',
      available: true,
      supports_prebuilt_voices: false,
      supports_parallel_synthesis: true,
      models: ['gpt-4o-mini-tts'],
      default_model: 'gpt-4o-mini-tts',
      default_voice: 'alloy',
      voices: ['alloy', 'coral']
    }
  ];
  await page.route('**/api/v1/services/tts**', (route) =>
    route.fulfill({ json: { services, default_service: 'mai' } })
  );
  await page.route('**/api/v1/voices', (route) =>
    route.fulfill({ json: { items: [] } })
  );
  const settingsUrl = `/api/v1/sessions/${session.id}/settings/tts`;
  const original = await (await page.request.get(settingsUrl)).json();
  expect(
    (
      await page.request.put(settingsUrl, {
        headers: { ...headers, 'If-Match': `"${original.revision}"` },
        data: {
          value: { service: 'mai', model: mai, voice: klaus, language: 'de' }
        }
      })
    ).ok()
  ).toBeTruthy();
  await page.goto(`/sessions/${session.id}`);
  const card = page
    .getByRole('heading', { name: 'Generate audio', exact: true })
    .locator('xpath=ancestor::article');
  await card.getByRole('button', { name: 'Settings' }).click();
  const dialog = page.getByRole('dialog');
  const voice = dialog.getByRole('combobox', { name: 'Voice', exact: true });
  const language = dialog.getByLabel('Speech language');
  await expect(voice.locator('optgroup option')).toHaveText([
    'Klaus · Male · German (Germany)',
    'Mia · Female · German (Germany)'
  ]);
  await expect(voice).toHaveValue(klaus);
  await language.selectOption('en');
  await expect(voice.locator('optgroup option')).toHaveText([
    'Ethan · Male · English (United States)'
  ]);
  await expect(voice).toHaveValue(ethan);
  await language.selectOption('de');
  await dialog.getByLabel('TTS model', { exact: true }).selectOption(flash);
  await expect(voice.locator('optgroup option')).toHaveCount(1);
  await expect(voice).toHaveValue(`de-DE-Mia:${flash}`);
  await page.screenshot({
    path: testInfo.outputPath('mai-language-voices.png')
  });
  await dialog.getByLabel('TTS service').selectOption('openai');
  await language.selectOption('de');
  await expect(voice.locator('optgroup option')).toHaveText([
    'Alloy · Multilingual',
    'Coral · Multilingual'
  ]);
  await voice.selectOption('coral');
  await dialog
    .getByRole('spinbutton', { name: 'Concurrent TTS requests' })
    .fill('9');
  await expect(
    dialog.getByRole('button', { name: 'Save settings', exact: true })
  ).toBeDisabled();
  await dialog
    .getByRole('spinbutton', { name: 'Concurrent TTS requests' })
    .fill('3');
  await dialog
    .getByText('Send 1–8 segment requests at once.', { exact: false })
    .scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('cloud-concurrency.png') });
  await dialog
    .getByRole('button', { name: 'Save settings', exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const saved = await (await page.request.get(settingsUrl)).json();
  expect(saved.effective).toMatchObject({
    service: 'openai',
    model: 'gpt-4o-mini-tts',
    voice: 'coral',
    language: 'de',
    tts_concurrent_requests: 3
  });
  await card.getByRole('button', { name: 'Settings' }).click();
  await expect(voice).toHaveValue('coral');
  await expect(
    dialog.getByRole('spinbutton', { name: 'Concurrent TTS requests' })
  ).toHaveValue('3');
});
