import { readFileSync } from 'node:fs';
import { expect, test, type Page } from '@playwright/test';

const source = (path: string) =>
  readFileSync(new URL(`../src/lib/${path}`, import.meta.url), 'utf8');

function uniqueName(prefix: string) {
  return `${prefix} ${crypto.randomUUID()}`;
}

async function signIn(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const closeTour = page.getByRole('button', { name: 'Close tour' });
  if (await closeTour.isVisible()) await closeTour.click();
}

async function createVoice(page: Page, name: string) {
  const authStatus = await page.request.get('/api/v1/auth/status');
  const csrfToken = (await authStatus.json()).csrf_token;
  const created = await page.request.post('/api/v1/voices', {
    headers: { 'X-CSRF-Token': csrfToken },
    data: { name, language: 'en' }
  });
  expect(created.ok()).toBeTruthy();
  return (await created.json()) as { id: string; revision: number };
}

async function mockEmptyTtsCatalogue(page: Page) {
  await page.route('**/api/v1/services/tts**', (route) =>
    route.fulfill({
      contentType: 'application/json',
      json: { default_service: '', services: [] }
    })
  );
}

async function recordSample(page: Page) {
  await page
    .getByRole('button', { name: 'Samples & setup', exact: true })
    .click();
  await page.getByRole('button', { name: 'Enable microphone' }).click();
  await expect(
    page.getByRole('button', { name: 'Record', exact: true })
  ).toBeEnabled();
  await page.getByRole('button', { name: 'Record', exact: true }).click();
  await page.waitForTimeout(1_000);
  await page.getByRole('button', { name: /Stop ·/ }).click();
  await expect(
    page.getByRole('button', { name: 'Play recording' })
  ).toBeVisible();
}

test('microphone cleanup is an explicit opt-in server job', () => {
  const voiceManager = source('VoiceManager.svelte');

  // Opt-in, default off, next to the microphone preview/save controls.
  expect(voiceManager).toContain('cleanupNoise = $state(false)');
  expect(voiceManager).toContain('Clean background noise with DeepFilterNet2');
  expect(voiceManager).toContain('Optional and off by default');

  // The flag is only sent when chosen; the raw upload stays unchanged.
  expect(voiceManager).toContain(
    "body.set('noise_reduction', 'deepfilternet2')"
  );
  expect(voiceManager).toContain('if (denoise)');

  // No live/instant denoise claims: cleanup runs on the job worker.
  expect(voiceManager).toContain('not live in the browser');
  expect(voiceManager).toContain('Cleaning background noise on the job worker');
  expect(voiceManager).not.toContain('live denois');
  expect(voiceManager).not.toContain('instant');

  // Failure and cancellation retain the local recording for retry.
  expect(voiceManager).toContain('cancelSavingRecording');
  expect(voiceManager).toContain('jobApi.cancel(recordingJobId)');
  expect(voiceManager).toContain(
    'Save canceled. Your recording is still available locally.'
  );

  // The original voice revision guards the save across the async job.
  expect(voiceManager).toContain('const expectedRevision = selected.revision');
  expect(voiceManager).toContain('selected?.id === voiceId');
});

test('microphone cleanup sends the flag with the original voice revision', async ({
  page,
  browserName
}) => {
  test.skip(
    browserName !== 'chromium',
    'Chromium provides a deterministic fake microphone for this media integration test.'
  );
  const voiceName = uniqueName('Cleanup opt-in');
  await signIn(page);
  const voice = await createVoice(page, voiceName);
  await mockEmptyTtsCatalogue(page);

  let saved = false;
  await page.route(`**/api/v1/voices/${voice.id}/samples`, (route) =>
    route.fulfill({
      contentType: 'application/json',
      json: {
        items: saved
          ? [
              {
                id: 'sample-1',
                artifact_id: 'artifact-1',
                transcript_reviewed: false,
                available: true
              }
            ]
          : []
      }
    })
  );
  let uploadUrl = '';
  let ifMatch = '';
  let uploadBody = '';
  await page.route('**/api/v1/voices/*/samples', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    uploadUrl = route.request().url();
    ifMatch = route.request().headers()['if-match'] ?? '';
    const buffer = route.request().postDataBuffer();
    uploadBody = buffer ? buffer.toString('utf-8') : '';
    await route.fulfill({
      contentType: 'application/json',
      status: 202,
      json: {
        id: 'cleanup-job',
        kind: 'voice.normalize_recording',
        status: 'queued'
      }
    });
  });
  let polls = 0;
  await page.route('**/api/v1/jobs/cleanup-job', (route) =>
    route.fulfill({
      contentType: 'application/json',
      json: {
        id: 'cleanup-job',
        status: polls++ < 1 ? 'queued' : 'succeeded'
      }
    })
  );

  await page.goto('/voices');
  await page.getByRole('button', { name: voiceName }).click();
  await expect(page.getByRole('heading', { name: voiceName })).toBeVisible();
  await recordSample(page);

  const cleanup = page.getByRole('checkbox', {
    name: /Clean background noise/
  });
  await expect(cleanup).not.toBeChecked();
  await cleanup.check();
  saved = true;
  await page.getByRole('button', { name: 'Save sample' }).click();

  await expect(page.getByRole('button', { name: 'Play sample' })).toBeVisible({
    timeout: 20_000
  });
  expect(uploadUrl).toContain(`/api/v1/voices/${voice.id}/samples`);
  expect(ifMatch).toBe(`"${voice.revision}"`);
  expect(uploadBody).toContain('noise_reduction');
  expect(uploadBody).toContain('deepfilternet2');
  expect(uploadBody).toContain('expected_revision');
  await expect(page.getByRole('status')).toContainText('cleaned');
  await expect(page.getByRole('alert')).toHaveCount(0);
});

test('failed cleanup keeps the local recording for retry', async ({
  page,
  browserName
}) => {
  test.skip(
    browserName !== 'chromium',
    'Chromium provides a deterministic fake microphone for this media integration test.'
  );
  const voiceName = uniqueName('Cleanup failure');
  await signIn(page);
  const voice = await createVoice(page, voiceName);
  await mockEmptyTtsCatalogue(page);

  await page.route(`**/api/v1/voices/${voice.id}/samples`, (route) =>
    route.fulfill({
      contentType: 'application/json',
      json: { items: [] }
    })
  );
  await page.route('**/api/v1/voices/*/samples', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    await route.fulfill({
      contentType: 'application/json',
      status: 202,
      json: {
        id: 'failing-job',
        kind: 'voice.normalize_recording',
        status: 'queued'
      }
    });
  });
  await page.route('**/api/v1/jobs/failing-job', (route) =>
    route.fulfill({
      contentType: 'application/json',
      json: {
        id: 'failing-job',
        status: 'failed',
        error_message: 'DeepFilterNet2 cleanup failed'
      }
    })
  );

  await page.goto('/voices');
  await page.getByRole('button', { name: voiceName }).click();
  await expect(page.getByRole('heading', { name: voiceName })).toBeVisible();
  await recordSample(page);

  await page.getByRole('checkbox', { name: /Clean background noise/ }).check();
  await page.getByRole('button', { name: 'Save sample' }).click();

  await expect(page.getByRole('alert')).toContainText(
    'DeepFilterNet2 cleanup failed'
  );
  await expect(
    page.getByRole('button', { name: 'Play recording' })
  ).toBeVisible();
  await expect(page.getByRole('button', { name: 'Save sample' })).toBeEnabled();
  await expect(
    page.getByRole('checkbox', { name: /Clean background noise/ })
  ).toBeChecked();
});

test('save cancellation is honest about intent, outcome, and uncertain jobs', () => {
  const voiceManager = source('VoiceManager.svelte');

  // Cancel records intent; the server result decides what the UI may claim.
  expect(voiceManager).toContain('saveCancelError');
  expect(voiceManager).toContain('pendingSaveVoiceId');
  expect(voiceManager).toContain('jobApi.cancel(recordingJobId)');
  expect(voiceManager).not.toContain('() => saveRecordingAborted');

  // A cancel that lands mid-upload cancels the exact returned job.
  expect(voiceManager).toContain('Cancel arrived while the upload was');
  expect(voiceManager).toContain('await jobApi.cancel(jobId)');

  // Cancellation is only announced after the server confirms it.
  expect(voiceManager).toContain(
    'Save canceled. Your recording is still available locally.'
  );

  // A failed cancel call is reported, never smoothed into a clean cancel.
  expect(voiceManager).toContain('Cancel failed:');

  // A save that wins the race against Cancel says so honestly.
  expect(voiceManager).toContain(
    'The save finished before the cancellation could stop it.'
  );

  // An uncertain outcome keeps the job ID, keeps the raw recording for
  // retry, and re-checks instead of starting a duplicate upload.
  expect(voiceManager).toContain('still running as job');
  expect(voiceManager).toContain('No duplicate upload was started.');
  expect(voiceManager).toContain('Check Activity & logs before retrying.');
});

test('cancel during a slow upload cancels the exact returned job', async ({
  page,
  browserName
}) => {
  test.skip(
    browserName !== 'chromium',
    'Chromium provides a deterministic fake microphone for this media integration test.'
  );
  const voiceName = uniqueName('Delayed upload cancel');
  await signIn(page);
  const voice = await createVoice(page, voiceName);
  await mockEmptyTtsCatalogue(page);

  await page.route(`**/api/v1/voices/${voice.id}/samples`, (route) =>
    route.fulfill({
      contentType: 'application/json',
      json: { items: [] }
    })
  );
  await page.route('**/api/v1/voices/*/samples', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    await new Promise((resolve) => setTimeout(resolve, 2_000));
    await route.fulfill({
      contentType: 'application/json',
      status: 202,
      json: {
        id: 'slow-upload-job',
        kind: 'voice.normalize_recording',
        status: 'queued'
      }
    });
  });
  let canceled = false;
  const cancelRequestedFor: string[] = [];
  await page.route('**/api/v1/jobs/slow-upload-job/cancel', (route) =>
    route.fulfill({
      contentType: 'application/json',
      json: (() => {
        cancelRequestedFor.push('slow-upload-job');
        canceled = true;
        return {
          id: 'slow-upload-job',
          status: 'canceled',
          error_message: 'Canceled by user'
        };
      })()
    })
  );
  await page.route('**/api/v1/jobs/slow-upload-job', (route) =>
    route.fulfill({
      contentType: 'application/json',
      json: canceled
        ? {
            id: 'slow-upload-job',
            status: 'canceled',
            error_message: 'Canceled by user'
          }
        : { id: 'slow-upload-job', status: 'running' }
    })
  );

  await page.goto('/voices');
  await page.getByRole('button', { name: voiceName }).click();
  await expect(page.getByRole('heading', { name: voiceName })).toBeVisible();
  await recordSample(page);

  // Cancel while the upload is still in flight, before any job ID exists.
  await page.getByRole('button', { name: 'Save sample' }).click();
  await expect(page.getByRole('button', { name: 'Cancel save' })).toBeVisible();
  await page.getByRole('button', { name: 'Cancel save' }).click();
  await expect(page.getByRole('button', { name: /Canceling/ })).toBeVisible();

  await expect(page.getByRole('status')).toContainText('Save canceled', {
    timeout: 20_000
  });
  expect(cancelRequestedFor).toContain('slow-upload-job');
  await expect(
    page.getByRole('button', { name: 'Play recording' })
  ).toBeVisible();
  await expect(page.getByRole('button', { name: 'Save sample' })).toBeEnabled();
});

test('a failed cancel call is reported instead of a false cancellation', async ({
  page,
  browserName
}) => {
  test.skip(
    browserName !== 'chromium',
    'Chromium provides a deterministic fake microphone for this media integration test.'
  );
  const voiceName = uniqueName('Cancel failure truth');
  await signIn(page);
  const voice = await createVoice(page, voiceName);
  await mockEmptyTtsCatalogue(page);

  await page.route(`**/api/v1/voices/${voice.id}/samples`, (route) =>
    route.fulfill({
      contentType: 'application/json',
      json: { items: [] }
    })
  );
  await page.route('**/api/v1/voices/*/samples', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    await route.fulfill({
      contentType: 'application/json',
      status: 202,
      json: {
        id: 'cancel-fail-job',
        kind: 'voice.normalize_recording',
        status: 'queued'
      }
    });
  });
  let polls = 0;
  await page.route('**/api/v1/jobs/cancel-fail-job', (route) =>
    route.fulfill({
      contentType: 'application/json',
      json:
        polls++ < 4
          ? { id: 'cancel-fail-job', status: 'running' }
          : {
              id: 'cancel-fail-job',
              status: 'failed',
              error_message: 'DeepFilterNet2 cleanup failed'
            }
    })
  );
  await page.route('**/api/v1/jobs/cancel-fail-job/cancel', (route) =>
    route.fulfill({
      contentType: 'application/json',
      status: 500,
      json: { message: 'cancel endpoint down' }
    })
  );

  await page.goto('/voices');
  await page.getByRole('button', { name: voiceName }).click();
  await expect(page.getByRole('heading', { name: voiceName })).toBeVisible();
  await recordSample(page);

  await page.getByRole('button', { name: 'Save sample' }).click();
  await expect(page.getByRole('button', { name: 'Cancel save' })).toBeVisible();
  await page.getByRole('button', { name: 'Cancel save' }).click();

  await expect(page.getByRole('alert')).toContainText('Cancel failed', {
    timeout: 20_000
  });
  await expect(page.getByText('Save canceled')).toHaveCount(0);
  await expect(
    page.getByRole('button', { name: 'Play recording' })
  ).toBeVisible();
  await expect(page.getByRole('button', { name: 'Save sample' })).toBeEnabled();
});

test('confirmed server cancellation keeps the recording for retry', async ({
  page,
  browserName
}) => {
  test.skip(
    browserName !== 'chromium',
    'Chromium provides a deterministic fake microphone for this media integration test.'
  );
  const voiceName = uniqueName('Confirmed cancellation');
  await signIn(page);
  const voice = await createVoice(page, voiceName);
  await mockEmptyTtsCatalogue(page);

  await page.route(`**/api/v1/voices/${voice.id}/samples`, (route) =>
    route.fulfill({
      contentType: 'application/json',
      json: { items: [] }
    })
  );
  await page.route('**/api/v1/voices/*/samples', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    await route.fulfill({
      contentType: 'application/json',
      status: 202,
      json: {
        id: 'confirm-cancel-job',
        kind: 'voice.normalize_recording',
        status: 'queued'
      }
    });
  });
  let canceled = false;
  await page.route('**/api/v1/jobs/confirm-cancel-job/cancel', (route) =>
    route.fulfill({
      contentType: 'application/json',
      json: (() => {
        canceled = true;
        return {
          id: 'confirm-cancel-job',
          status: 'canceled',
          error_message: 'Canceled by user'
        };
      })()
    })
  );
  await page.route('**/api/v1/jobs/confirm-cancel-job', (route) =>
    route.fulfill({
      contentType: 'application/json',
      json: canceled
        ? {
            id: 'confirm-cancel-job',
            status: 'canceled',
            error_message: 'Canceled by user'
          }
        : { id: 'confirm-cancel-job', status: 'running' }
    })
  );

  await page.goto('/voices');
  await page.getByRole('button', { name: voiceName }).click();
  await expect(page.getByRole('heading', { name: voiceName })).toBeVisible();
  await recordSample(page);

  await page.getByRole('button', { name: 'Save sample' }).click();
  await expect(page.getByRole('button', { name: 'Cancel save' })).toBeVisible();
  await page.getByRole('button', { name: 'Cancel save' }).click();

  await expect(page.getByRole('status')).toContainText('Save canceled', {
    timeout: 20_000
  });
  await expect(
    page.getByRole('button', { name: 'Play recording' })
  ).toBeVisible();
  await expect(page.getByRole('button', { name: 'Save sample' })).toBeEnabled();
  await expect(page.getByRole('alert')).toHaveCount(0);
});
