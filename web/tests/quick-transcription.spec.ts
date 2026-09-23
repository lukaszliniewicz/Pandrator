import { expect, test, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

async function signIn(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  await page
    .getByRole('link', { name: 'Quick Transcribe', exact: true })
    .click();
  await expect(
    page.getByRole('heading', { name: 'Quick Transcribe' })
  ).toBeVisible();
}

async function mockTranscription(page: Page) {
  let starts = 0;
  let removed = false;
  const requestedFormats: string[] = [];
  const uploadedBodies: string[] = [];
  const texts: Record<string, string> = {
    txt: 'Hello from a temporary transcription.',
    srt: '1\n00:00:00,000 --> 00:00:02,000\nHello from a temporary transcription.\n',
    json: JSON.stringify(
      {
        schema: 'pandrator.transcript.v1',
        text: 'Hello from a temporary transcription.',
        segments: []
      },
      null,
      2
    )
  };
  const status = {
    id: 'quick-ui-test',
    job_id: 'quick-job',
    status: 'succeeded',
    progress: 1,
    progress_detail: 'Transcript ready',
    expires_at: new Date(Date.now() + 3600000).toISOString(),
    format: 'txt',
    result_available: true,
    inline_result: true,
    result_url: '/api/v1/transcriptions/quick-ui-test/result?format=txt'
  };
  await page.route('**/api/v1/transcriptions**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === 'DELETE') {
      removed = true;
      return route.fulfill({ json: { ...status, status: 'deleted' } });
    }
    if (request.method() === 'POST') {
      starts += 1;
      uploadedBodies.push(request.postData() || '');
      expect(request.headers()['idempotency-key']).toBeTruthy();
      expect(request.headers()['content-type']).toContain(
        'multipart/form-data'
      );
      expect(request.postDataBuffer()?.byteLength).toBeGreaterThan(0);
      return route.fulfill({
        status: 202,
        json: { ...status, status: 'queued', result_available: false }
      });
    }
    if (url.pathname.endsWith('/result')) {
      const format = url.searchParams.get('format') || 'txt';
      requestedFormats.push(format);
      return route.fulfill({
        json: {
          format,
          content: texts[format],
          offset: 0,
          total_chars: texts[format].length,
          next_offset: null
        }
      });
    }
    return route.fulfill({ json: status });
  });
  return {
    starts: () => starts,
    removed: () => removed,
    requestedFormats,
    uploadedBodies
  };
}

test('Qwen quick transcription explains timing limits and submits local model and isolation choices', async ({
  page
}, testInfo) => {
  const mock = await mockTranscription(page);
  await signIn(page);
  await page.getByLabel('Audio or video file', { exact: true }).setInputFiles({
    name: 'qwen-sample.wav',
    mimeType: 'audio/wav',
    buffer: Buffer.from('fixture audio')
  });
  await page
    .getByRole('combobox', { name: 'Transcription service', exact: true })
    .selectOption('qwen3');
  const qwen = page.getByRole('region', {
    name: 'Qwen3 transcription options'
  });
  await expect(qwen).toBeVisible();
  await expect(
    page.getByRole('button', { name: 'Transcribe', exact: true })
  ).toBeDisabled();
  await expect(page.getByRole('alert')).toContainText('source language');
  await qwen
    .getByRole('combobox', { name: 'Source language', exact: true })
    .selectOption('ar');
  await expect(page.getByRole('alert')).toContainText('word alignment');
  await expect(
    page.getByRole('button', { name: 'Transcribe', exact: true })
  ).toBeDisabled();
  await qwen
    .getByRole('combobox', { name: 'Source language', exact: true })
    .selectOption('pl');
  await expect(qwen.getByTestId('qwen-timing-explanation')).toContainText(
    'Canary CTC'
  );
  await qwen.getByLabel('Qwen model size').selectOption('qwen3_asr_1_7b');
  await qwen.getByText('Local processing settings', { exact: true }).click();
  await qwen.getByLabel('Compute backend').selectOption('cpu');
  await page
    .getByText('Audio preprocessing · optional', { exact: true })
    .click();
  await page
    .getByRole('combobox', { name: 'Vocal isolation', exact: true })
    .selectOption('mel_band_roformer');
  expect(mock.starts()).toBe(0);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth
    )
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath('quick-qwen-mobile.png'),
    fullPage: true
  });
  await page.getByRole('button', { name: 'Transcribe', exact: true }).click();
  await expect(page.getByLabel('Transcript', { exact: true })).toHaveValue(
    'Hello from a temporary transcription.'
  );
  expect(mock.starts()).toBe(1);
  expect(mock.uploadedBodies[0]).toContain('"engine":"qwen3"');
  expect(mock.uploadedBodies[0]).toContain('"language":"pl"');
  expect(mock.uploadedBodies[0]).toContain('"qwen_asr_model":"qwen3_asr_1_7b"');
  expect(mock.uploadedBodies[0]).toContain('"compute_backend":"cpu"');
  expect(mock.uploadedBodies[0]).toContain(
    '"transcription_vocal_isolation":"mel_band_roformer"'
  );
});

test('upload, switch formats, reload result and delete without a session', async ({
  page
}) => {
  const mock = await mockTranscription(page);
  await signIn(page);
  await page.getByLabel('Audio or video file', { exact: true }).setInputFiles({
    name: 'sample.wav',
    mimeType: 'audio/wav',
    buffer: Buffer.from('fixture audio')
  });
  await page.getByRole('button', { name: 'Transcribe', exact: true }).click();
  await expect(page.getByLabel('Transcript', { exact: true })).toHaveValue(
    'Hello from a temporary transcription.'
  );
  await expect(page).toHaveURL(/id=quick-ui-test/);
  await page.getByLabel('Output format').selectOption('srt');
  await expect(page.getByLabel('Transcript', { exact: true })).toHaveValue(
    /00:00:00,000/
  );
  await expect(
    page.getByRole('link', { name: 'Download .srt' })
  ).toHaveAttribute('href', /format=srt/);
  await page.getByLabel('Output format').selectOption('json');
  await expect(page.getByLabel('Transcript', { exact: true })).toHaveValue(
    /pandrator.transcript.v1/
  );
  expect(mock.starts()).toBe(1);
  expect(mock.requestedFormats).toEqual(['txt', 'srt', 'json']);
  const scan = await new AxeBuilder({ page }).include('main').analyze();
  expect(scan.violations).toEqual([]);
  await page.screenshot({
    path: '/tmp/pandrator-quick-transcribe-desktop.png',
    fullPage: true
  });
  await page.reload();
  await expect(page.getByLabel('Transcript', { exact: true })).toHaveValue(
    'Hello from a temporary transcription.'
  );
  expect(mock.starts()).toBe(1);
  await page.getByRole('button', { name: 'Delete now' }).click();
  await expect(
    page.getByText('Temporary transcription deleted.')
  ).toBeVisible();
  expect(mock.removed()).toBe(true);
  await expect(page).not.toHaveURL(/id=/);
});

test('record, preview and transcribe on a narrow screen', async ({ page }) => {
  const mock = await mockTranscription(page);
  await signIn(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: 'Record audio', exact: true }).click();
  await page
    .getByRole('button', { name: 'Record audio', exact: true })
    .last()
    .click();
  await expect(
    page.getByRole('button', { name: 'Stop recording' })
  ).toBeVisible();
  await page.waitForTimeout(600);
  await page.getByRole('button', { name: 'Stop recording' }).click();
  await expect(page.getByLabel('Preview source recording')).toBeVisible();
  expect(mock.starts()).toBe(0);
  await page.getByRole('button', { name: 'Transcribe', exact: true }).click();
  await expect(page.getByLabel('Transcript', { exact: true })).toHaveValue(
    'Hello from a temporary transcription.'
  );
  expect(mock.starts()).toBe(1);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth
    )
  ).toBe(true);
  await page.screenshot({
    path: '/tmp/pandrator-quick-transcribe-mobile.png',
    fullPage: true
  });
});

test('microphone denial leaves file upload usable', async ({ page }) => {
  await signIn(page);
  await page.evaluate(() => {
    navigator.mediaDevices.getUserMedia = async () => {
      throw new DOMException('Microphone permission denied', 'NotAllowedError');
    };
  });
  await page.getByRole('button', { name: 'Record audio', exact: true }).click();
  await page
    .getByRole('button', { name: 'Record audio', exact: true })
    .last()
    .click();
  await expect(page.getByRole('alert')).toContainText(
    'Microphone permission denied'
  );
  await page.getByRole('button', { name: 'Upload a file' }).click();
  await expect(
    page.getByLabel('Audio or video file', { exact: true })
  ).toBeEnabled();
});
