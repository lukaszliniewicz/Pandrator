import { expect, test, type Page, type Route } from '@playwright/test';

function gate() {
  let release!: () => void;
  const ready = new Promise<void>((resolve) => (release = resolve));
  return { ready, release };
}

async function selectVoice(page: Page, name: string) {
  const button = page.getByRole('button', { name });
  if (!(await button.isVisible())) {
    const selector = page.getByRole('button', {
      name: 'Choose another voice',
      exact: true
    });
    if (await selector.isVisible()) await selector.click();
    await page
      .locator('summary')
      .filter({ hasText: 'Add a sample to an existing voice' })
      .click();
  }
  await button.click();
}

async function fixture(page: Page, reviewed = true) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(
    page.getByRole('button', { name: 'New session', exact: true })
  ).toBeVisible();
  const closeTour = page.getByRole('button', { name: 'Close tour' });
  if (await closeTour.isVisible()) await closeTour.click();
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const voices = [];
  for (const prefix of ['Draft voice A', 'Draft voice B']) {
    const response = await page.request.post('/api/v1/voices', {
      headers: { 'X-CSRF-Token': auth.csrf_token },
      data: { name: `${prefix} ${crypto.randomUUID()}`, language: 'en' }
    });
    expect(response.ok()).toBeTruthy();
    voices.push((await response.json()) as { id: string; name: string });
  }
  const [a, b] = voices;
  let samples = [
    {
      id: 'first',
      artifact_id: 'first-audio',
      transcript: 'First saved',
      transcript_reviewed: reviewed,
      available: true
    },
    {
      id: 'second',
      artifact_id: 'second-audio',
      transcript: 'Second saved',
      transcript_reviewed: reviewed,
      available: true
    }
  ];
  await page.route('**/api/v1/services/tts**', (route) =>
    route.fulfill({ json: { default_service: '', services: [] } })
  );
  await page.route(`**/api/v1/voices/${a.id}/samples`, (route) =>
    route.fulfill({ json: { items: samples } })
  );
  await page.route(`**/api/v1/voices/${b.id}/samples`, (route) =>
    route.fulfill({
      json: {
        items: [
          {
            id: 'other',
            artifact_id: 'other-audio',
            transcript: 'Other voice saved',
            transcript_reviewed: true,
            available: true
          }
        ]
      }
    })
  );
  await page.route('**/api/v1/capabilities', (route) =>
    route.fulfill({
      json: {
        stt: {
          crispasr: true,
          models: { parakeet: { installed: true, supported_languages: ['en'] } }
        },
        ffmpeg: { available: true }
      }
    })
  );
  await page.goto('/voices');
  await page
    .getByRole('button', { name: 'Add reference', exact: true })
    .click();
  await selectVoice(page, a.name);
  await expect(
    page.locator(
      'textarea[placeholder="Transcript will remain unsaved until you review it."]'
    )
  ).toHaveCount(2);
  const rows = page
    .locator('article')
    .filter({ has: page.getByRole('button', { name: 'Delete voice sample' }) });
  await rows.nth(1).locator('summary').click();
  const second = rows.nth(1).locator('textarea');
  return {
    a,
    b,
    rows,
    second,
    samples,
    setSamples: (items: typeof samples) => (samples = items)
  };
}

for (const width of [1280, 390]) {
  test.describe(`sample draft ownership ${width}`, () => {
    test.use({ viewport: { width, height: 900 } });

    test('deleting a preceding sample retains the surviving draft, disclosure and focus', async ({
      page
    }, testInfo) => {
      const f = await fixture(page);
      await f.second.fill('Unsaved second draft');
      const pending = gate();
      const started = gate();
      await page.route(
        `**/api/v1/voices/${f.a.id}/samples/first`,
        async (route) => {
          started.release();
          await pending.ready;
          f.setSamples(f.samples.slice(1));
          await route.fulfill({ status: 204 });
        }
      );
      page.on('dialog', (dialog) => dialog.accept());
      await f.rows
        .nth(0)
        .getByRole('button', { name: 'Delete voice sample' })
        .click();
      await started.ready;
      await f.second.focus();
      const refreshed = page.waitForResponse(
        (response) =>
          response.url().endsWith(`/voices/${f.a.id}/samples`) &&
          response.request().method() === 'GET'
      );
      pending.release();
      await refreshed;
      await expect(f.rows).toHaveCount(1);
      const surviving = f.rows.locator('textarea');
      await expect(surviving).toHaveValue('Unsaved second draft');
      await expect(f.rows.locator('details')).toHaveAttribute('open', '');
      await expect(surviving).toBeFocused();
      await f.rows.screenshot({
        path: testInfo.outputPath('surviving-sample.png')
      });
    });

    test('a transcript save preserves other drafts and edits made while the save is pending', async ({
      page
    }) => {
      const f = await fixture(page);
      await f.rows.nth(0).locator('summary').click();
      const first = f.rows.nth(0).locator('textarea');
      await first.fill('Submitted first');
      await f.second.fill('Unsaved second draft');
      const pending = gate();
      const started = gate();
      await page.route(
        `**/api/v1/voices/${f.a.id}/samples/first/transcript`,
        async (route: Route) => {
          expect(route.request().postDataJSON().transcript).toBe(
            'Submitted first'
          );
          started.release();
          await pending.ready;
          f.samples[0] = { ...f.samples[0], transcript: 'Submitted first' };
          await route.fulfill({ json: f.samples[0] });
        }
      );
      await f.rows
        .nth(0)
        .getByRole('button', { name: 'Save reviewed transcript' })
        .click();
      await started.ready;
      await first.fill('Newer first draft');
      const refreshed = page.waitForResponse(
        (response) =>
          response.url().endsWith(`/voices/${f.a.id}/samples`) &&
          response.request().method() === 'GET'
      );
      pending.release();
      await refreshed;
      await expect(f.rows.nth(0).locator('textarea')).toHaveValue(
        'Newer first draft'
      );
      await expect(f.rows.nth(1).locator('textarea')).toHaveValue(
        'Unsaved second draft'
      );
    });

    test('an acknowledged save normalizes the clean draft while an empty draft survives', async ({
      page
    }) => {
      const f = await fixture(page);
      await f.rows.nth(0).locator('summary').click();
      await f.rows.nth(0).locator('textarea').fill('  Saved after trim  ');
      await f.second.fill('');
      await page.route(
        `**/api/v1/voices/${f.a.id}/samples/first/transcript`,
        async (route) => {
          const transcript = route.request().postDataJSON().transcript;
          expect(transcript).toBe('Saved after trim');
          f.samples[0] = { ...f.samples[0], transcript };
          await route.fulfill({ json: f.samples[0] });
        }
      );
      const refreshed = page.waitForResponse((response) =>
        response.url().endsWith(`/voices/${f.a.id}/samples`)
      );
      await f.rows
        .nth(0)
        .getByRole('button', { name: 'Save reviewed transcript' })
        .click();
      await refreshed;
      await expect(f.rows.nth(0).locator('textarea')).toHaveValue(
        'Saved after trim'
      );
      await expect(f.rows.nth(1).locator('textarea')).toHaveValue('');
      await expect(
        f.rows.nth(1).getByRole('button', { name: 'Save reviewed transcript' })
      ).toBeDisabled();
    });

    test('changed audio resets its transcript while unchanged audio retains a draft', async ({
      page
    }) => {
      const f = await fixture(page);
      await f.rows.nth(0).locator('summary').click();
      await f.rows.nth(0).locator('textarea').fill('Draft for old audio');
      await f.second.fill('Draft for unchanged audio');
      f.setSamples([
        {
          ...f.samples[0],
          artifact_id: 'replacement-audio',
          transcript: 'Replacement transcript'
        },
        f.samples[1]
      ]);
      await selectVoice(page, f.a.name);
      await expect(f.rows.nth(0).locator('textarea')).toHaveValue(
        'Replacement transcript'
      );
      await expect(f.rows.nth(1).locator('textarea')).toHaveValue(
        'Draft for unchanged audio'
      );
    });

    test('a failed refresh retains a usable draft and a retry accepts clean server values', async ({
      page
    }) => {
      const f = await fixture(page);
      const pageErrors: string[] = [];
      page.on('pageerror', (error) => pageErrors.push(error.message));
      await f.second.fill('Draft through failed refresh');
      let failed = true;
      await page.route(`**/api/v1/voices/${f.a.id}/samples`, (route) =>
        failed
          ? route.fulfill({
              status: 503,
              json: {
                error: {
                  code: 'unavailable',
                  message: 'Sample refresh unavailable'
                }
              }
            })
          : route.fallback()
      );
      await selectVoice(page, f.a.name);
      await expect(page.getByRole('alert')).toContainText(
        'Sample refresh unavailable'
      );
      await expect(f.second).toHaveValue('Draft through failed refresh');
      await f.second.press('End');
      await f.second.press('!');
      f.setSamples([
        { ...f.samples[0], transcript: 'Updated on server' },
        f.samples[1]
      ]);
      failed = false;
      await selectVoice(page, f.a.name);
      await expect(f.rows.nth(0).locator('textarea')).toHaveValue(
        'Updated on server'
      );
      await expect(f.second).toHaveValue('Draft through failed refresh!');
      await expect(page.getByRole('alert')).toHaveCount(0);
      expect(pageErrors).toEqual([]);
    });

    test('an upload refresh failure is reported instead of a false success', async ({
      page
    }) => {
      const f = await fixture(page);
      await page.route(`**/api/v1/voices/${f.a.id}/samples`, (route) =>
        route.request().method() === 'POST'
          ? route.fulfill({
              status: 202,
              json: { id: 'upload-job', status: 'queued' }
            })
          : route.fulfill({
              status: 503,
              json: {
                error: {
                  code: 'unavailable',
                  message: 'Upload sample refresh unavailable'
                }
              }
            })
      );
      await page.route('**/api/v1/jobs/upload-job', (route) =>
        route.fulfill({ json: { id: 'upload-job', status: 'succeeded' } })
      );
      const chooser = page.waitForEvent('filechooser');
      await page
        .getByRole('button', { name: 'Upload sample', exact: true })
        .click();
      await (
        await chooser
      ).setFiles({
        name: 'reference.wav',
        mimeType: 'audio/wav',
        buffer: Buffer.from('controlled upload fixture')
      });
      await expect(page.getByRole('alert')).toContainText(
        'Upload sample refresh unavailable'
      );
      await expect(
        page.getByRole('status').filter({ hasText: 'Voice sample saved' })
      ).toHaveCount(0);
      await expect(f.second).toHaveValue('Second saved');
    });

    for (const edited of [false, true]) {
      test(`transcription ${edited ? 'keeps newer edits' : 'applies an unchanged draft result'}`, async ({
        page
      }) => {
        const f = await fixture(page);
        const pending = gate();
        const started = gate();
        await page.route(
          `**/api/v1/voices/${f.a.id}/samples/second/transcribe`,
          (route) =>
            route.fulfill({
              status: 202,
              json: { id: 'transcription-job', status: 'queued' }
            })
        );
        await page.route('**/api/v1/jobs/transcription-job', async (route) => {
          started.release();
          await pending.ready;
          await route.fulfill({
            json: {
              id: 'transcription-job',
              status: 'succeeded',
              result_json: { transcript: 'Recognized sample text' }
            }
          });
        });
        await f.rows
          .nth(1)
          .getByRole('button', { name: 'Transcribe', exact: true })
          .click();
        await started.ready;
        if (edited) await f.second.fill('Newer manual transcript');
        pending.release();
        await expect(
          f.rows.nth(1).getByRole('button', { name: 'Transcribe', exact: true })
        ).toBeEnabled();
        await expect(f.second).toHaveValue(
          edited ? 'Newer manual transcript' : 'Recognized sample text'
        );
        await expect(page.getByRole('status')).toContainText(
          edited
            ? 'newer transcript edits were kept'
            : 'Transcript ready for review'
        );
      });
    }

    test('a transcription batch stops after switching voices', async ({
      page
    }) => {
      const f = await fixture(page, false);
      const pending = gate();
      const started = gate();
      const submitted: string[] = [];
      await page.route('**/api/v1/voices/*/samples/*/transcribe', (route) => {
        submitted.push(route.request().url());
        return route.fulfill({
          status: 202,
          json: { id: 'batch-job', status: 'queued' }
        });
      });
      await page.route('**/api/v1/jobs/batch-job', async (route) => {
        started.release();
        await pending.ready;
        await route.fulfill({
          json: {
            id: 'batch-job',
            status: 'succeeded',
            result_json: { transcript: 'Previous voice result' }
          }
        });
      });
      await page
        .getByRole('button', { name: 'Transcribe missing', exact: true })
        .click();
      await started.ready;
      await selectVoice(page, f.b.name);
      await expect(f.rows.locator('textarea')).toHaveValue('Other voice saved');
      pending.release();
      await expect(
        page.getByRole('button', { name: 'Transcribe missing', exact: true })
      ).toBeVisible();
      expect(submitted).toHaveLength(1);
      expect(submitted[0]).toContain(
        `/voices/${f.a.id}/samples/first/transcribe`
      );
      await expect(f.rows.locator('textarea')).toHaveValue('Other voice saved');
      await expect(
        page
          .getByRole('status')
          .filter({ hasText: 'Transcript ready for review' })
      ).toHaveCount(0);
    });

    test('a late sample response cannot replace the newly selected voice', async ({
      page
    }) => {
      const f = await fixture(page);
      await selectVoice(page, f.b.name);
      await expect(f.rows.locator('textarea')).toHaveValue('Other voice saved');
      const pending = gate();
      const started = gate();
      await page.route(`**/api/v1/voices/${f.a.id}/samples`, async (route) => {
        started.release();
        await pending.ready;
        await route.fulfill({ json: { items: f.samples } });
      });
      await selectVoice(page, f.a.name);
      await started.ready;
      await selectVoice(page, f.b.name);
      await expect(f.rows.locator('textarea')).toHaveValue('Other voice saved');
      await f.rows.locator('summary').click();
      await f.rows.locator('textarea').fill('Current voice draft');
      const late = page.waitForResponse((response) =>
        response.url().endsWith(`/voices/${f.a.id}/samples`)
      );
      pending.release();
      await late;
      await expect(
        page.getByRole('heading', { name: f.b.name, exact: true })
      ).toBeVisible();
      await expect(f.rows).toHaveCount(1);
      await expect(f.rows.locator('textarea')).toHaveValue(
        'Current voice draft'
      );
    });
  });
}
