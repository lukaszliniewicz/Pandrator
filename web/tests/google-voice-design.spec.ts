import { expect, test } from '@playwright/test';

const models = ['gemini-3.8-flash-tts', 'gemini-3.8-flash-lite-tts'];

for (const provider of ['gemini', 'vertex_ai']) {
  for (const [index, model] of models.entries()) {
    for (const width of [1280, 390]) {
      test(`Google voice design saves ${provider} ${model} at ${width}`, async ({
        page
      }, info) => {
        test.skip(
          process.env.PANDRATOR_GOOGLE_TTS_E2E_FIXTURE !== '1',
          'Requires the disposable Google upstream fixture.'
        );
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
        await page.goto('/voices');
        await page
          .getByRole('button', { name: 'Design voice', exact: true })
          .click();
        const dialog = page.getByRole('dialog', {
          name: 'Design a reusable voice'
        });
        await dialog.getByLabel('Voice design provider').selectOption(provider);
        const name = `Google ${provider} ${index} ${width} ${crypto.randomUUID()}`;
        const prompt =
          'A warm, thoughtful storyteller with a gentle Polish accent and a softly textured voice.';
        await dialog.getByLabel('Voice name', { exact: true }).fill(name);
        await dialog
          .getByRole('combobox', {
            name: provider === 'vertex_ai' ? 'Use with model' : 'Design model',
            exact: true
          })
          .selectOption(model);
        await dialog
          .getByRole('combobox', { name: 'Voice language', exact: true })
          .selectOption('pl');
        await dialog
          .getByRole('textbox', { name: /^Voice description/ })
          .fill(prompt);
        await expect(
          dialog.getByLabel('Sample text', { exact: true })
        ).toHaveCount(0);
        await expect(
          dialog.getByLabel('Variation seed', { exact: true })
        ).toHaveCount(0);
        await page.screenshot({
          path: info.outputPath('google-design-brief.png'),
          fullPage: true
        });
        const queuedResponse = page.waitForResponse(
          (response) =>
            response.request().method() === 'POST' &&
            response.url().endsWith('/api/v1/voices/design/google')
        );
        await dialog
          .getByRole('button', { name: 'Create & save voice', exact: true })
          .click();
        const queued = await queuedResponse;
        expect(queued.status()).toBe(202);
        const request = await queued.json();
        await expect(
          dialog.getByRole('heading', { name: `${name} is saved` })
        ).toBeVisible();
        const voices = await (await page.request.get('/api/v1/voices')).json();
        const saved = voices.items.find(
          (voice: { id: string }) => voice.id === request.voice_id
        );
        expect(saved.description).toBe(prompt);
        expect(saved.language).toBe('pl');
        expect(saved.sample_count).toBe(0);
        expect(saved.origin).toBe('designed');
        const registration = saved.metadata_json.providers[provider];
        expect(registration.voice_id).toMatch(/^voice_e2e_/);
        expect(registration.resource_kind).toBe('designed_voice');
        expect(registration.scope_fingerprint).toHaveLength(64);
        expect(saved.preview_artifact_id).toBeTruthy();
        const previewResponse = await page.request.get(
          `/api/v1/artifacts/${saved.preview_artifact_id}/content`
        );
        expect(previewResponse.ok()).toBeTruthy();
        expect((await previewResponse.body()).subarray(0, 4).toString()).toBe(
          'RIFF'
        );
        await expect(
          dialog.getByText(registration.voice_id, { exact: true })
        ).toBeVisible();
        await dialog.getByRole('button', { name: 'Play', exact: true }).click();
        await expect(
          dialog.getByRole('button', { name: 'Pause', exact: true })
        ).toBeVisible();
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth
          )
        ).toBeTruthy();
        await page.screenshot({
          path: info.outputPath('google-design-saved.png'),
          fullPage: true
        });
        await dialog
          .getByRole('button', { name: 'Open saved voice', exact: true })
          .click();
        await expect(dialog).toHaveCount(0);
        await expect(
          page.getByRole('heading', { name, exact: true })
        ).toBeVisible();
        await expect(
          page.getByRole('heading', {
            name: 'Google designed voice',
            exact: true
          })
        ).toBeVisible();
        await expect(
          page.getByText(registration.voice_id, { exact: true })
        ).toBeVisible();
        if (provider === 'vertex_ai')
          await expect(
            page.getByText(/Project: pandrator-browser-fixture/)
          ).toBeVisible();
        await page.screenshot({
          path: info.outputPath('google-library-saved.png'),
          fullPage: true
        });
        await page.reload();
        const savedCard = page
          .getByRole('button', { name, exact: true })
          .locator('xpath=ancestor::article');
        await expect(savedCard).toBeVisible();
        await expect(savedCard).not.toContainText('Add a reference sample');
        await savedCard.scrollIntoViewIfNeeded();
        await page.screenshot({
          path: info.outputPath('google-library-card.png')
        });
        const catalogue = await (
          await page.request.get(
            `/api/v1/services/tts?view=compact&service_id=${provider}`
          )
        ).json();
        const service = catalogue.services.find(
          (item: { id: string }) => item.id === provider
        );
        for (const id of models)
          expect(service.voice_catalogues[id]).toContain(registration.voice_id);
        expect(
          service.voice_catalogues['gemini-3.1-flash-tts-preview']
        ).not.toContain(registration.voice_id);
        expect(service.managed_google_voice_ids).toContain(
          registration.voice_id
        );
        const readback = await (
          await page.request.get(
            `/api/v1/voice-catalog?query=${request.voice_id}&kind=managed`
          )
        ).json();
        expect(readback.items).toHaveLength(1);
        expect(readback.items[0].preview_artifact_id).toBe(
          saved.preview_artifact_id
        );
        expect(
          readback.items[0].compatibility
            .filter(
              (item: { service_id: string }) => item.service_id === provider
            )
            .map((item: { model: string }) => item.model)
            .sort()
        ).toEqual([...models].sort());
        expect(errors).toEqual([]);
      });
    }
  }
}
