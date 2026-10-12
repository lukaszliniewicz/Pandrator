import { expect, test } from '@playwright/test';

for (const width of [1280, 390]) {
  test(`Eleven v4 route controls and context persist at ${width}`, async ({
    page
  }, info) => {
    await page.setViewportSize({ width, height: 900 });
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.goto('/');
    await page.getByLabel('Owner password').fill('pandrator-e2e');
    await page.getByRole('button', { name: 'Sign in' }).click();
    await expect(
      page.getByRole('heading', { name: 'Create a project' })
    ).toBeVisible();
    const tour = page.getByRole('button', { name: 'Close tour' });
    if (await tour.isVisible()) await tour.click();
    const csrf = (await (await page.request.get('/api/v1/auth/status')).json())
      .csrf_token;
    const headers = { 'X-CSRF-Token': csrf };
    const session = await (
      await page.request.post('/api/v1/sessions', {
        headers,
        data: {
          name: `Eleven v4 ${crypto.randomUUID()}`,
          workflow_kind: 'voiceover',
          interface_mode: 'advanced'
        }
      })
    ).json();
    const catalogue = await (
      await page.request.get('/api/v1/services/tts?view=compact')
    ).json();
    const service = catalogue.services.find(
      (item: { id: string }) => item.id === 'elevenlabs'
    );
    // Preserve real model, language and route metadata; only account availability
    // and voice discovery are fixtures. The test makes no paid provider request.
    const voiceId = 'eleven-review-voice';
    Object.assign(service, {
      available: true,
      online: true,
      credential_configured: true,
      voices: [voiceId],
      default_voice: voiceId,
      voice_catalogues: Object.fromEntries(
        service.models.map((model: string) => [model, [voiceId]])
      ),
      default_voices: Object.fromEntries(
        service.models.map((model: string) => [model, voiceId])
      )
    });
    await page.route('**/api/v1/services/tts**', (route) =>
      route.fulfill({
        json: { services: [service], default_service: 'elevenlabs' }
      })
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
            value: {
              service: 'elevenlabs',
              model: 'eleven_v4',
              voice: voiceId,
              language: 'en'
            }
          }
        })
      ).ok()
    ).toBeTruthy();
    await page.goto(`/sessions/${session.id}`);
    const card = page
      .getByRole('heading', { name: 'Generate audio', exact: true })
      .locator('xpath=ancestor::article');
    const dialog = page.getByRole('dialog');
    const save = dialog.getByRole('button', {
      name: 'Save settings',
      exact: true
    });
    await card.getByRole('button', { name: 'Settings' }).click();
    await dialog
      .getByRole('textbox', { name: /^Speech direction/ })
      .fill('Warm, measured narration.');
    const stability = dialog.getByRole('spinbutton', {
      name: 'Stability',
      exact: true
    });
    const similarity = dialog.getByRole('spinbutton', {
      name: 'Similarity',
      exact: true
    });
    await stability.fill('1.1');
    await expect(save).toBeDisabled();
    await stability.fill('0.4');
    await similarity.fill('0.8');
    const context = dialog.getByRole('combobox', {
      name: 'Text context',
      exact: true
    });
    await context.selectOption('both');
    await dialog
      .getByRole('spinbutton', {
        name: 'Preceding context blocks',
        exact: true
      })
      .fill('2');
    await dialog
      .getByRole('spinbutton', {
        name: 'Following context blocks',
        exact: true
      })
      .fill('1');
    await dialog
      .getByRole('spinbutton', {
        name: 'Context character budget',
        exact: true
      })
      .fill('400');
    await expect(
      dialog.getByText(
        'Eleven v4 receives up to 100 characters on each side.',
        { exact: false }
      )
    ).toBeVisible();
    await stability.scrollIntoViewIfNeeded();
    await page.screenshot({ path: info.outputPath('eleven-v4-settings.png') });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth
      )
    ).toBeTruthy();
    await save.click();
    await expect(dialog).toHaveCount(0);
    const saved = await (await page.request.get(settingsUrl)).json();
    expect(saved.effective).toMatchObject({
      service: 'elevenlabs',
      model: 'eleven_v4',
      voice: voiceId,
      elevenlabs_voice_settings: { stability: 0.4, similarity_boost: 0.8 },
      generation_prompt: 'Warm, measured narration.',
      tts_context_mode: 'both',
      performance_context_before: 2,
      performance_context_after: 1,
      performance_context_max_chars: 400
    });
    await card.getByRole('button', { name: 'Settings' }).click();
    await expect(stability).toHaveValue('0.4');
    await expect(similarity).toHaveValue('0.8');
    await expect(context).toHaveValue('both');
    await dialog
      .getByLabel('TTS model', { exact: true })
      .selectOption('eleven_v4_turbo');
    await expect(similarity).toHaveCount(0);
    await expect(context).toHaveCount(0);
    await expect(dialog.getByRole('alert')).toContainText(
      'does not support similarity boost'
    );
    await expect(save).toBeDisabled();
    await dialog
      .getByRole('button', {
        name: 'Reset voice settings to provider defaults',
        exact: true
      })
      .click();
    await stability.fill('0.6');
    await expect(save).toBeEnabled();
    await stability.scrollIntoViewIfNeeded();
    await page.screenshot({
      path: info.outputPath('eleven-v4-turbo-settings.png')
    });
    await save.click();
    await expect(dialog).toHaveCount(0);
    expect(
      (await (await page.request.get(settingsUrl)).json()).effective
    ).toMatchObject({
      model: 'eleven_v4_turbo',
      elevenlabs_voice_settings: { stability: 0.6 }
    });
    await card.getByRole('button', { name: 'Settings' }).click();
    await expect(stability).toHaveValue('0.6');
    await expect(similarity).toHaveCount(0);
    await expect(context).toHaveCount(0);
    expect(errors).toEqual([]);
  });
}
