import { Buffer } from 'node:buffer';
import { expect, test, type Page } from '@playwright/test';
import {
  qwenTimingExplanation,
  qwenTimedLanguageProblem,
  sttLanguageProblem
} from '../src/lib/stt-language-policy';

const coverage = {
  stt: {
    models: {
      qwen3: {
        supported_languages: ['en', 'pl', 'ar', 'yue', 'fil'],
        timed_supported_languages: ['en', 'pl', 'yue'],
        requires_explicit_language_for_timestamps: true
      }
    }
  }
};

test('recognition coverage and alignment coverage remain distinct', () => {
  expect(sttLanguageProblem(coverage, 'qwen3', 'ar')).toBe('');
  expect(qwenTimedLanguageProblem(coverage, 'ar')).toContain('word alignment');
  expect(qwenTimingExplanation(coverage, 'pl')).toContain('Canary CTC');
  expect(qwenTimingExplanation(coverage, 'en')).toContain(
    'Qwen3 Forced Aligner'
  );
  expect(qwenTimedLanguageProblem(coverage, 'auto')).toContain(
    'source language'
  );
});

async function fixture(page: Page) {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.locator('.app-shell')).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const csrf = (await (await page.request.get('/api/v1/auth/status')).json())
    .csrf_token;
  const headers = { 'X-CSRF-Token': csrf };
  const created = await page.request.post('/api/v1/sessions', {
    headers,
    data: {
      name: `Local transcription ${crypto.randomUUID()}`,
      workflow_kind: 'voiceover',
      source_language: 'en'
    }
  });
  expect(created.ok()).toBeTruthy();
  const id = (await created.json()).id as string;
  const wav = Buffer.alloc(44 + 32000);
  wav.write('RIFF');
  wav.writeUInt32LE(wav.length - 8, 4);
  wav.write('WAVEfmt ', 8);
  wav.writeUInt32LE(16, 16);
  wav.writeUInt16LE(1, 20);
  wav.writeUInt16LE(1, 22);
  wav.writeUInt32LE(16000, 24);
  wav.writeUInt32LE(32000, 28);
  wav.writeUInt16LE(2, 32);
  wav.writeUInt16LE(16, 34);
  wav.write('data', 36);
  wav.writeUInt32LE(32000, 40);
  const uploaded = await page.request.post('/api/v1/uploads', {
    headers,
    multipart: {
      session_id: id,
      file: { name: 'synthetic.wav', mimeType: 'audio/wav', buffer: wav }
    }
  });
  expect(uploaded.ok(), await uploaded.text()).toBeTruthy();
  await page.goto(`/sessions/${id}`);
  const card = page
    .locator('article')
    .filter({ has: page.getByRole('heading', { name: /^Transcribe/ }) })
    .first();
  await card.getByRole('button', { name: 'Settings', exact: true }).click();
  const dialog = page.getByRole('dialog').filter({
    has: page.getByRole('combobox', {
      name: 'Recognition model',
      exact: true
    })
  });
  await expect(dialog).toBeVisible();
  return { id, dialog, errors };
}

test('Qwen model and optional vocal isolation save without starting transcription', async ({
  page
}) => {
  const { id, dialog, errors } = await fixture(page);
  const started: string[] = [];
  page.on('request', (request) => {
    if (
      request.method() === 'POST' &&
      /\/run(?:\?|$)|\/transcriptions(?:\?|$)/.test(request.url())
    )
      started.push(request.url());
  });
  await dialog
    .getByRole('combobox', { name: 'Recognition model', exact: true })
    .selectOption('qwen3');
  const qwen = dialog.getByRole('region', {
    name: 'Qwen3 transcription options'
  });
  await expect(qwen).toBeVisible();
  await qwen
    .getByRole('combobox', { name: 'Source language', exact: true })
    .selectOption('en');
  await expect(qwen.getByTestId('qwen-timing-explanation')).toContainText(
    'Qwen3 Forced Aligner'
  );
  await qwen
    .getByRole('combobox', { name: 'Qwen model size' })
    .selectOption('qwen3_asr_1_7b');
  const preprocessing = dialog.getByTestId('transcription-preprocessing');
  await preprocessing.locator('summary').click();
  await expect(
    preprocessing.getByRole('combobox', {
      name: 'Vocal isolation',
      exact: true
    })
  ).toHaveValue('off');
  await preprocessing
    .getByRole('combobox', { name: 'Vocal isolation', exact: true })
    .selectOption('mel_band_roformer');
  await expect(preprocessing).toContainText('original media stays unchanged');
  await dialog
    .getByRole('button', { name: 'Save settings', exact: true })
    .click();
  await expect(dialog).toBeHidden();
  const saved = await (
    await page.request.get(`/api/v1/sessions/${id}/settings/stt`)
  ).json();
  expect(saved.effective).toMatchObject({
    stt_engine: 'qwen3',
    qwen_asr_model: 'qwen3_asr_1_7b',
    transcription_vocal_isolation: 'mel_band_roformer'
  });
  expect(started).toEqual([]);
  expect(errors).toEqual([]);
});

test('Qwen accepts Polish recognition with fallback but explains unsupported timed Arabic', async ({
  page
}) => {
  const { dialog, errors } = await fixture(page);
  await dialog
    .getByRole('combobox', { name: 'Recognition model', exact: true })
    .selectOption('qwen3');
  const qwen = dialog.getByRole('region', {
    name: 'Qwen3 transcription options'
  });
  await qwen
    .getByRole('combobox', { name: 'Source language', exact: true })
    .selectOption('pl');
  await expect(qwen.getByTestId('qwen-timing-explanation')).toContainText(
    'Canary CTC'
  );
  await qwen
    .getByRole('combobox', { name: 'Source language', exact: true })
    .selectOption('ar');
  await expect(
    dialog.getByRole('alert').filter({ hasText: 'word alignment' })
  ).toBeVisible();
  await dialog
    .getByRole('button', { name: 'Save settings', exact: true })
    .click();
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText('Timed transcription for ar');
  expect(errors).toEqual([]);
});
