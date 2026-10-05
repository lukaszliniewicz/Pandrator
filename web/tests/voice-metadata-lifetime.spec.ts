import { expect, test, type Page } from '@playwright/test';
import type { VoiceRecord } from '../src/lib/api-models';
import {
  gate,
  selectVoice,
  voiceReferenceFixture
} from './helpers/voice-reference';

async function readStoredVoice(page: Page, id: string) {
  const response = await page.request.get('/api/v1/voices');
  expect(response.ok()).toBeTruthy();
  const payload = (await response.json()) as { items: VoiceRecord[] };
  const voice = payload.items.find((item) => item.id === id);
  expect(voice).toBeDefined();
  if (!voice) throw new Error(`Missing persisted voice ${id}`);
  return voice;
}

async function editProfile(page: Page, name: string) {
  await page.getByRole('button', { name: 'Edit voice', exact: true }).click();
  await page.getByRole('textbox', { name: 'Name', exact: true }).fill(name);
  await page.getByRole('button', { name: 'Save voice', exact: true }).click();
}

async function selectWhisper(page: Page) {
  await page
    .locator('summary')
    .filter({ hasText: 'Transcription settings' })
    .click();
  await page
    .getByRole('combobox', { name: 'Transcription model', exact: true })
    .selectOption('whisper');
}

for (const width of [1280, 390]) {
  test.describe(`voice metadata lifetime ${width}`, () => {
    test.use({ viewport: { width, height: 900 } });

    for (const kind of ['profile', 'language'] as const) {
      for (const failed of [false, true]) {
        test(`obsolete ${kind} ${failed ? 'failure' : 'completion'} cannot change the current voice`, async ({
          page
        }) => {
          const f = await voiceReferenceFixture(page);
          const pending = gate();
          const started = gate();
          const renamed = `${f.a.name} renamed`;
          await page.route(`**/api/v1/voices/${f.a.id}`, async (route) => {
            if (route.request().method() !== 'PATCH') return route.fallback();
            expect(route.request().headers()['if-match']).toBe(
              `"${f.a.revision}"`
            );
            const body = route.request().postDataJSON();
            expect(kind === 'profile' ? body.name : body.language).toBe(
              kind === 'profile' ? renamed : 'pl'
            );
            started.release();
            await pending.ready;
            if (failed) {
              await route.fulfill({
                status: 503,
                json: {
                  error: {
                    code: 'unavailable',
                    message: 'Old voice update unavailable'
                  }
                }
              });
            } else {
              const response = await route.fetch();
              expect(response.status()).toBe(200);
              await route.fulfill({ response });
            }
          });
          if (kind === 'profile') await editProfile(page, renamed);
          else
            await page
              .getByRole('combobox', { name: 'Voice language', exact: true })
              .selectOption('pl');
          await started.ready;
          await selectVoice(page, f.b.name);
          await f.rows.locator('summary').click();
          await f.rows.locator('textarea').fill('Current B transcript draft');
          await selectWhisper(page);
          const language = page.getByRole('combobox', {
            name: 'Voice language',
            exact: true
          });
          await expect(language).toBeDisabled();
          pending.release();
          await expect(language).toBeEnabled();
          const persisted = await readStoredVoice(page, f.a.id);
          expect(persisted.revision).toBe(f.a.revision + (failed ? 0 : 1));
          if (!failed)
            expect(
              kind === 'profile' ? persisted.name : persisted.language
            ).toBe(kind === 'profile' ? renamed : 'pl');
          await expect(
            page.getByRole('heading', { name: f.b.name, exact: true })
          ).toBeVisible({ timeout: 1000 });
          await expect(f.rows.locator('textarea')).toHaveValue(
            'Current B transcript draft',
            { timeout: 1000 }
          );
          await expect(
            page.getByRole('combobox', {
              name: 'Transcription model',
              exact: true
            })
          ).toHaveValue('whisper', { timeout: 1000 });
          await expect(language).toHaveValue('en', { timeout: 1000 });
          await expect(page.getByRole('alert')).toHaveCount(0, {
            timeout: 1000
          });
          await expect(
            page
              .getByRole('status')
              .filter({ hasText: /Voice (details|language) saved/ })
          ).toHaveCount(0, { timeout: 1000 });
        });
      }

      test(`same-voice ${kind} success keeps its sample draft and acknowledges its revision`, async ({
        page
      }) => {
        const f = await voiceReferenceFixture(page);
        await f.second.fill('Unsaved sample through metadata save');
        const renamed = `${f.a.name} renamed`;
        if (kind === 'profile') await editProfile(page, renamed);
        else
          await page
            .getByRole('combobox', { name: 'Voice language', exact: true })
            .selectOption('pl');
        await expect(page.getByRole('status')).toContainText(
          kind === 'profile' ? 'Voice details saved' : 'Voice language saved'
        );
        await expect(
          page.getByRole('combobox', { name: 'Voice language', exact: true })
        ).toBeEnabled();
        await expect(f.second).toHaveValue(
          'Unsaved sample through metadata save'
        );
        const persisted = await readStoredVoice(page, f.a.id);
        expect(persisted.revision).toBe(f.a.revision + 1);
        await expect(
          page.getByRole('heading', {
            name: kind === 'profile' ? renamed : f.a.name,
            exact: true
          })
        ).toBeVisible();
        await expect(
          page.getByRole('combobox', { name: 'Voice language', exact: true })
        ).toHaveValue(kind === 'profile' ? 'en' : 'pl');
        await expect(page.getByRole('alert')).toHaveCount(0);
      });

      test(`current ${kind} failure preserves its fields and reports the error`, async ({
        page
      }) => {
        const f = await voiceReferenceFixture(page);
        await f.second.fill('Unsaved sample through rejected metadata');
        await page.route(`**/api/v1/voices/${f.a.id}`, (route) =>
          route.request().method() === 'PATCH'
            ? route.fulfill({
                status: 503,
                json: {
                  error: {
                    code: 'unavailable',
                    message: 'Current voice update unavailable'
                  }
                }
              })
            : route.fallback()
        );
        const renamed = `${f.a.name} retry draft`;
        if (kind === 'profile') await editProfile(page, renamed);
        else
          await page
            .getByRole('combobox', { name: 'Voice language', exact: true })
            .selectOption('pl');
        await expect(page.getByRole('alert')).toContainText(
          'Current voice update unavailable'
        );
        await expect(
          page.getByRole('combobox', { name: 'Voice language', exact: true })
        ).toBeEnabled();
        await expect(f.second).toHaveValue(
          'Unsaved sample through rejected metadata'
        );
        if (kind === 'profile')
          await expect(
            page.getByRole('textbox', { name: 'Name', exact: true })
          ).toHaveValue(renamed);
        await expect(
          page.getByRole('combobox', { name: 'Voice language', exact: true })
        ).toHaveValue('en', { timeout: 1000 });
        const persisted = await readStoredVoice(page, f.a.id);
        expect(persisted.revision).toBe(f.a.revision);
      });
    }

    test('a profile acknowledgment keeps newer editor changes', async ({
      page
    }, testInfo) => {
      const f = await voiceReferenceFixture(page);
      const pending = gate();
      const started = gate();
      const submitted = `${f.a.name} submitted`;
      const newer = `${f.a.name} newer draft`;
      await page.route(`**/api/v1/voices/${f.a.id}`, async (route) => {
        if (route.request().method() !== 'PATCH') return route.fallback();
        started.release();
        await pending.ready;
        const response = await route.fetch();
        expect(response.status()).toBe(200);
        await route.fulfill({ response });
      });
      await editProfile(page, submitted);
      await started.ready;
      await page
        .getByRole('textbox', { name: 'Name', exact: true })
        .fill(newer);
      await page
        .getByRole('textbox', { name: 'Description', exact: true })
        .fill('Newer unsaved description');
      pending.release();
      await expect(
        page.getByRole('combobox', { name: 'Voice language', exact: true })
      ).toBeEnabled();
      await expect(
        page.getByRole('textbox', { name: 'Name', exact: true })
      ).toHaveValue(newer, { timeout: 1000 });
      await expect(
        page.getByRole('textbox', { name: 'Description', exact: true })
      ).toHaveValue('Newer unsaved description', { timeout: 1000 });
      const persisted = await readStoredVoice(page, f.a.id);
      expect(persisted.name).toBe(submitted);
      expect(persisted.description).not.toBe('Newer unsaved description');
      expect(persisted.revision).toBe(f.a.revision + 1);
      await expect(
        page.getByRole('button', { name: 'Save voice', exact: true })
      ).toBeEnabled();
      await page
        .getByRole('textbox', { name: 'Name', exact: true })
        .press('End');
      await page.getByRole('textbox', { name: 'Name', exact: true }).press('!');
      await expect(
        page.getByRole('textbox', { name: 'Name', exact: true })
      ).toHaveValue(`${newer}!`);
      await page
        .locator('section')
        .filter({
          has: page.getByRole('textbox', { name: 'Name', exact: true })
        })
        .last()
        .screenshot({
          path: testInfo.outputPath('retained-profile-editor.png')
        });
    });
  });
}
