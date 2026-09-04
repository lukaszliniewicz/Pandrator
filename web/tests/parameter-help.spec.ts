import { expect, test, type Page } from '@playwright/test';

async function signIn(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const closeTour = page.getByRole('button', { name: 'Close tour' });
  if (await closeTour.isVisible()) await closeTour.click();
}

test('technical setting names expose pipeline help on hover and keyboard focus', async ({
  page
}) => {
  await signIn(page);
  await page.goto('/settings');

  const sttSection = page
    .locator('details')
    .filter({
      has: page.locator('summary').filter({ hasText: /^STT$/ })
    })
    .last();
  await sttSection.locator('summary').click();

  const help = sttSection.getByRole('button', { name: 'About STT Engine' });
  const tooltip = help.getByRole('tooltip');
  await expect(help).toBeVisible();

  await help.hover();
  await expect(tooltip).toBeVisible();
  await expect(tooltip).toContainText(/transcription|recognition|engine/i);
  await expect(tooltip).toContainText(/Choices:/);

  await page.mouse.move(0, 0);
  await expect(tooltip).toBeHidden();
  await help.focus();
  await expect(tooltip).toBeVisible();
});

test('global STT defaults disclose only controls used by the selected engine', async ({
  page
}) => {
  await signIn(page);
  await page.goto('/settings');

  const sttSection = page
    .locator('details')
    .filter({
      has: page.locator('summary').filter({ hasText: /^STT$/ })
    })
    .last();
  await sttSection.locator('summary').click();
  const field = (name: string) => sttSection.getByLabel(name, { exact: true });

  await expect(sttSection.locator('label').first()).toHaveText('STT Engine');
  await expect(field('Use VAD for Whisper and Parakeet')).toBeVisible();
  await expect(field('VAD speech threshold')).toBeVisible();
  await expect(field('MOSS CTC aligner model')).toHaveCount(0);
  await expect(field('STT Transcribe Style')).toHaveCount(0);
  await expect(field('STT Compute Device')).toHaveCount(0);
  const selectBox = await field('STT Engine').boundingBox();
  const checkboxBox = await field('Use VAD for Whisper and Parakeet')
    .locator('..')
    .boundingBox();
  expect(selectBox?.height).toBe(44);
  expect(checkboxBox?.height).toBe(44);

  await field('Use VAD for Whisper and Parakeet').uncheck();
  await expect(field('VAD speech threshold')).toHaveCount(0);

  await field('STT Engine').selectOption('moss');
  await expect(field('Use VAD before MOSS diarization')).toBeVisible();
  await expect(field('MOSS CTC aligner model')).toBeVisible();
  await expect(field('Whisper Prompt')).toHaveCount(0);

  await field('Align each MOSS turn to words with CTC').uncheck();
  await expect(field('MOSS CTC aligner model')).toHaveCount(0);

  await field('STT Engine').selectOption('azure_mai_transcribe_1_5');
  await expect(field('STT Transcribe Style')).toBeVisible();
  await expect(field('STT Compute Backend')).toHaveCount(0);
  await expect(field('Use VAD for Whisper and Parakeet')).toHaveCount(0);
});

test('global defaults retain but hide inactive branch parameters', async ({
  page
}) => {
  await signIn(page);
  await page.goto('/settings');

  const section = (name: string) =>
    page
      .locator('details')
      .filter({
        has: page
          .locator('summary')
          .filter({ hasText: new RegExp(`^${name}$`) })
      })
      .last();
  const field = (panel: ReturnType<typeof section>, name: string) =>
    panel.getByLabel(name, { exact: true });

  const correction = section('Correction');
  await correction.locator('summary').click();
  await expect(field(correction, 'Model Name')).toHaveCount(0);
  await field(correction, 'Enabled').check();
  await expect(field(correction, 'Model Name')).toBeVisible();
  await expect(field(correction, 'Researcher model')).toHaveCount(0);
  await field(correction, 'Ground uncertain terms with web research').check();
  await expect(field(correction, 'Researcher model')).toBeVisible();

  const audio = section('Audio');
  await audio.locator('summary').click();
  await expect(field(audio, 'Fade-in duration (ms)')).toHaveCount(0);
  await field(audio, 'Fade generated audio edges').check();
  await expect(field(audio, 'Fade-in duration (ms)')).toBeVisible();

  const rvc = section('RVC');
  await rvc.locator('summary').click();
  await expect(field(rvc, 'Model')).toHaveCount(0);
  await field(rvc, 'Enabled').check();
  await expect(field(rvc, 'Model')).toBeVisible();

  const cleaning = section('Source Cleaning');
  await cleaning.locator('summary').click();
  await expect(
    field(cleaning, 'Maximum source-cleaning agent turns')
  ).toHaveCount(0);
  await field(cleaning, 'Use LLM-assisted source cleaning').check();
  await expect(
    field(cleaning, 'Maximum source-cleaning agent turns')
  ).toBeVisible();
  await field(cleaning, 'PDF OCR Mode').selectOption('off');
  await expect(field(cleaning, 'PDF OCR Language')).toHaveCount(0);

  const output = section('Output');
  await output.locator('summary').click();
  await expect(field(output, 'Subtitle Selection')).toHaveCount(0);
  await field(output, 'Subtitle Mode').selectOption('soft');
  await expect(field(output, 'Subtitle Selection')).toBeVisible();
  await expect(field(output, 'Burn Video Encoder')).toHaveCount(0);
  await field(output, 'Video Transcode').check();
  await expect(field(output, 'Burn Video Encoder')).toBeVisible();
  await expect(field(output, 'Burn Audio Bitrate')).toHaveCount(0);
  await field(output, 'Burn Audio Codec').selectOption('aac');
  await expect(field(output, 'Burn Audio Bitrate')).toBeVisible();
  await expect(field(output, 'Bitrate')).toHaveCount(0);
  await field(output, 'Format').selectOption('mp3');
  await expect(field(output, 'Bitrate')).toBeVisible();
});
