import { expect, test } from '@playwright/test';

for (const provider of ['gemini', 'vertex_ai']) {
  for (const width of [1280, 390]) {
    test(`Google custom voices and text context persist for ${provider} at ${width}`, async ({
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
      const csrf = (
        await (await page.request.get('/api/v1/auth/status')).json()
      ).csrf_token;
      const headers = { 'X-CSRF-Token': csrf };
      const session = await (
        await page.request.post('/api/v1/sessions', {
          headers,
          data: {
            name: `Google TTS ${crypto.randomUUID()}`,
            workflow_kind: 'voiceover',
            interface_mode: 'advanced'
          }
        })
      ).json();
      // Keep the real model/language catalogue. Only account voice discovery
      // and availability are fixtures; no Google request is made by this test.
      const catalogue = await (
        await page.request.get('/api/v1/services/tts?view=compact')
      ).json();
      const service = catalogue.services.find(
        (item: { id: string }) => item.id === provider
      );
      const models = ['gemini-3.8-flash-tts', 'gemini-3.8-flash-lite-tts'];
      const customVoice = 'voice_test_Custom_Case';
      service.available = true;
      service.online = true;
      service.credential_configured = true;
      service.supports_dynamic_catalog = true;
      service.voices = ['Kore'];
      service.voice_catalogues = Object.fromEntries(
        service.models.map((model: string) => [
          model,
          models.includes(model) ? ['Kore', customVoice] : ['Kore']
        ])
      );
      service.voice_metadata = Object.fromEntries(
        models.map((model) => [
          `${model}:${customVoice}`,
          {
            id: customVoice,
            display_name: 'Warm narrator',
            language_code: 'pl-PL',
            accent: 'Polish',
            gender: 'female',
            type: 'prompted'
          }
        ])
      );
      await page.route('**/api/v1/services/tts**', (route) =>
        route.fulfill({
          json: { services: [service], default_service: provider }
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
                service: provider,
                model: models[0],
                voice: 'Kore',
                language: 'de'
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
      const voice = dialog.getByRole('combobox', {
        name: 'Voice',
        exact: true
      });
      const context = dialog.getByRole('combobox', {
        name: 'Text context',
        exact: true
      });
      const save = dialog.getByRole('button', {
        name: 'Save settings',
        exact: true
      });
      for (const [index, model] of models.entries()) {
        await card.getByRole('button', { name: 'Settings' }).click();
        if (index === 0) {
          // Custom voices have one native language. Confirm that German
          // excludes this Polish voice, then use it with Polish speech.
          await expect(
            voice.locator(`option[value="${customVoice}"]`)
          ).toHaveCount(0);
          await dialog
            .getByRole('combobox', { name: 'Speech language', exact: true })
            .selectOption('pl');
        }
        await dialog
          .getByLabel('TTS model', { exact: true })
          .selectOption(model);
        await expect(
          voice.locator(`option[value="${customVoice}"]`)
        ).toHaveText(/Warm narrator[\s\S]*Polish/);
        await voice.selectOption(customVoice);
        await dialog
          .getByRole('textbox', { name: /^Speech direction/ })
          .fill('Warm narration with measured pacing.');
        await context.selectOption(index === 0 ? 'both' : 'before');
        await dialog
          .getByRole('spinbutton', {
            name: 'Preceding context blocks',
            exact: true
          })
          .fill('3');
        if (index === 0) {
          await dialog
            .getByRole('spinbutton', {
              name: 'Following context blocks',
              exact: true
            })
            .fill('2');
        } else {
          await expect(
            dialog.getByRole('spinbutton', {
              name: 'Following context blocks',
              exact: true
            })
          ).toHaveCount(0);
        }
        const budget = dialog.getByRole('spinbutton', {
          name: 'Context character budget',
          exact: true
        });
        await budget.fill('16001');
        await expect(save).toBeDisabled();
        await budget.fill('1200');
        const attempts = dialog.getByRole('spinbutton', {
          name: 'Maximum attempts per request'
        });
        const retryTime = dialog.getByRole('spinbutton', {
          name: 'Retry time limit (seconds)'
        });
        await attempts.fill('21');
        await expect(save).toBeDisabled();
        await attempts.fill('12');
        await retryTime.fill('0');
        await expect(save).toBeDisabled();
        await retryTime.fill('600');
        await expect(save).toBeEnabled();
        await context.scrollIntoViewIfNeeded();
        await page.screenshot({
          path: info.outputPath(`google-${index}-context.png`)
        });
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth
          )
        ).toBeTruthy();
        await save.click();
        await expect(dialog).toHaveCount(0);
        const saved = await (await page.request.get(settingsUrl)).json();
        expect(saved.effective).toMatchObject({
          service: provider,
          model,
          voice: customVoice,
          language: 'pl',
          generation_prompt: 'Warm narration with measured pacing.',
          tts_context_mode: index === 0 ? 'both' : 'before',
          performance_context_before: 3,
          performance_context_after: 2,
          performance_context_max_chars: 1200,
          max_attempts: 12,
          retry_timeout_seconds: 600
        });
      }
      await card.getByRole('button', { name: 'Settings' }).click();
      await expect(voice).toHaveValue(customVoice);
      await expect(context).toHaveValue('before');
      await expect(
        dialog.getByRole('spinbutton', {
          name: 'Context character budget',
          exact: true
        })
      ).toHaveValue('1200');
      const legacy = service.models.find((model: string) =>
        model.startsWith('gemini-3.1-')
      );
      await dialog
        .getByLabel('TTS model', { exact: true })
        .selectOption(legacy);
      await expect(voice.locator(`option[value="${customVoice}"]`)).toHaveCount(
        0
      );
      expect(errors).toEqual([]);
    });
  }
}
