import { expect, test } from '@playwright/test';

test('transcription settings check the selected files and preserve an existing VAD path', async ({
  page
}, testInfo) => {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const created = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': auth.csrf_token },
    data: {
      name: `Model files ${crypto.randomUUID()}`,
      workflow_kind: 'subtitles',
      interface_mode: 'advanced',
      source_language: 'en'
    }
  });
  expect(created.ok()).toBeTruthy();
  const { id } = await created.json();
  const endpoint = `/api/v1/sessions/${id}`;
  const stored = await (
    await page.request.get(`${endpoint}/settings/stt`)
  ).json();
  const saved = await page.request.put(`${endpoint}/settings/stt`, {
    headers: {
      'X-CSRF-Token': auth.csrf_token,
      'If-Match': `"${stored.revision}"`
    },
    data: {
      value: {
        stt_engine: 'parakeet',
        stt_language: 'en',
        stt_model_quantization: 'f16',
        crispasr_vad_enabled: true,
        crispasr_vad_model: 'silero'
      }
    }
  });
  expect(saved.ok()).toBeTruthy();
  await page.route(`**${endpoint}/workflow*`, (route) =>
    route.fulfill({
      json: {
        session_id: id,
        workflow_kind: 'subtitles',
        workflow_preset: 'subtitles',
        revision: 1,
        sources: [],
        stages: [
          {
            number: 1,
            key: 'transcribe',
            title: 'Transcribe',
            explanation: 'Create captions',
            status: 'ready',
            executable: true,
            included: true,
            artifacts: []
          }
        ]
      }
    })
  );
  const selections: Record<string, unknown>[] = [];
  await page.route(`**${endpoint}/transcription-readiness`, async (route) => {
    const settings = route.request().postDataJSON().settings;
    selections.push(settings);
    const missing = settings.crispasr_vad_model === 'silero';
    await route.fulfill({
      json: {
        schema_version: 1,
        scope: 'local_model_files',
        engine: settings.stt_engine,
        status: missing ? 'missing' : 'cached',
        ready: !missing,
        runtime_check_required: true,
        assets: [
          {
            kind: 'model',
            id: 'parakeet',
            status: 'cached',
            verification: 'nonempty'
          },
          {
            kind: 'vad',
            id: settings.crispasr_vad_model,
            status: missing ? 'missing' : 'not_required',
            verification: 'nonempty'
          }
        ]
      }
    });
  });
  await page.goto(`/sessions/${id}`);
  const card = page
    .getByRole('heading', { name: 'Transcribe', exact: true })
    .locator('xpath=ancestor::article');
  const disclosure = card.getByTestId('stage-disclosure-toggle');
  if (
    (await disclosure.count()) &&
    (await disclosure.getAttribute('aria-expanded')) === 'false'
  )
    await disclosure.click();
  await card.getByRole('button', { name: 'Settings', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Transcribe', exact: true });
  const files = dialog.getByRole('region', {
    name: 'Transcription model files'
  });
  await expect(
    files.getByText('Missing or empty', { exact: true })
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath('model-files.png'),
    fullPage: true
  });
  await dialog
    .getByRole('combobox', { name: 'Speech detector', exact: true })
    .selectOption('webrtc');
  await expect(files.getByText('Not required', { exact: true })).toBeVisible();
  expect(selections.at(-1)?.crispasr_vad_model).toBe('webrtc');
  await dialog
    .getByRole('combobox', { name: 'Speech detector', exact: true })
    .selectOption('custom');
  await dialog
    .getByRole('textbox', {
      name: /^Existing model file on the Pandrator computer/
    })
    .fill('/synthetic/existing-silero.bin');
  await expect(
    files.getByText(/Save this existing-file selection/)
  ).toBeVisible();
  expect(
    selections.some(
      (selection) =>
        selection.crispasr_vad_model === '/synthetic/existing-silero.bin'
    )
  ).toBe(false);
  await dialog
    .getByRole('button', { name: 'Save settings', exact: true })
    .click();
  await expect(dialog).not.toBeVisible();
  const current = await (
    await page.request.get(`${endpoint}/settings/stt`)
  ).json();
  expect(current.effective.crispasr_vad_model).toBe(
    '/synthetic/existing-silero.bin'
  );
  if (testInfo.project.name === 'chromium')
    await page.screenshot({
      path: testInfo.outputPath('model-files.png'),
      fullPage: true
    });
});
