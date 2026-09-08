import { expect, test, type Page } from '@playwright/test';

async function editor(
  page: Page,
  action: 'render' | 'propose',
  failed = false
) {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const created = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': auth.csrf_token },
    data: { name: `Edit completion ${Date.now()}`, workflow_kind: 'media_edit' }
  });
  expect(created.ok()).toBeTruthy();
  const session = await created.json();
  const artifact = {
    id: 'completion-video',
    role: 'source',
    kind: 'video',
    filename: 'recording.mp4'
  };
  let revision = 1;
  let stateReads = 0;
  let jobReads = 0;
  let allowCompletion = false;
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(String(error)));
  const state = () => ({
    session_id: session.id,
    workflow_kind: 'media_edit',
    readiness: { ready: true, source_media_artifact: artifact },
    plan: {
      plan_id: 'completion-plan',
      revision_id: `revision-${revision}`,
      revision,
      source_media_artifact: artifact,
      editorial_transcript_artifact: {
        ...artifact,
        id: 'completion-captions',
        kind: 'srt'
      },
      duration_ms: 60000,
      instructions: 'Remove setup chatter.',
      keep_ranges: [{ id: 'keep-1', start_ms: 10000, end_ms: 55000 }],
      cues: [
        {
          id: 'cue-1',
          start_ms: 10000,
          end_ms: 18000,
          text:
            stateReads > 1
              ? 'Refreshed public presentation.'
              : 'Welcome to the public presentation.',
          speaker: 'Presenter',
          words: [],
          timing_source: 'captions',
          timing_confidence: null
        }
      ],
      evidence: {},
      operation: {},
      reviewed: false,
      content_hash: 'completion-hash',
      created_at: new Date().toISOString()
    }
  });
  await page.route(
    `**/api/v1/sessions/${session.id}/media-edit`,
    async (route) => {
      if (route.request().method() === 'PUT') revision++;
      else stateReads++;
      await route.fulfill({ json: state() });
    }
  );
  await page.route('**/api/v1/artifacts/completion-video/waveform**', (route) =>
    route.fulfill({
      json: { points: [0, 0.3, 0.7, 0.1], start_ms: 0, end_ms: 60000 }
    })
  );
  await page.route('**/api/v1/artifacts/completion-video/content', (route) =>
    route.fulfill({ status: 404 })
  );
  await page.route('**/api/v1/capabilities**', (route) =>
    route.fulfill({
      json: {
        ffmpeg: {
          available: true,
          burn_video_encoders: [
            { id: 'libx264', label: 'H.264', hardware: false, codec: 'h264' }
          ]
        }
      }
    })
  );
  await page.route('**/api/v1/providers', (route) =>
    route.fulfill({
      json: {
        items: [
          {
            id: 'test-provider',
            provider_key: 'openai',
            label: 'Test provider',
            enabled: true
          }
        ]
      }
    })
  );
  await page.route('**/api/v1/providers/test-provider/models', (route) =>
    route.fulfill({
      json: {
        items: [{ model_id: 'test-model', is_active: true, is_default: true }]
      }
    })
  );
  const job = {
    id: 'completion-job',
    kind: `media_edit.${action}`,
    progress: 0,
    created_at: new Date().toISOString()
  };
  await page.route(
    `**/api/v1/sessions/${session.id}/media-edit/${action}`,
    (route) =>
      route.fulfill({ status: 202, json: { ...job, status: 'queued' } })
  );
  await page.route('**/api/v1/jobs/completion-job', async (route) => {
    jobReads++;
    await route.fulfill({
      json: {
        ...job,
        status: allowCompletion
          ? failed
            ? 'failed'
            : 'succeeded'
          : jobReads === 1
            ? 'running'
            : 'cancel_requested',
        progress: allowCompletion ? 1 : 0.5,
        error_message: failed ? 'The encoder could not read the source.' : null,
        result_json: {
          media_artifact_id: 'rendered-video',
          subtitle_artifact_id: 'rendered-captions'
        }
      }
    });
  });
  await page.goto(`/sessions/${session.id}/edit`);
  await expect(
    page.getByRole('button', { name: 'Render', exact: true })
  ).toBeVisible();
  if (action === 'render') {
    await page.getByRole('button', { name: 'Render', exact: true }).click();
    await page
      .getByRole('button', { name: 'Start render', exact: true })
      .click();
  } else {
    await page.getByRole('button', { name: 'Process', exact: true }).click();
    await page.getByRole('button', { name: /Configured LLM/ }).click();
    await page
      .getByRole('button', { name: 'Generate proposal', exact: true })
      .click();
  }
  await expect.poll(() => jobReads).toBeGreaterThanOrEqual(2);
  await expect(
    page.getByRole('button', { name: 'Save', exact: true })
  ).toBeDisabled();
  expect(stateReads).toBe(1);
  allowCompletion = true;
  return { stateReads: () => stateReads, errors };
}

for (const action of ['render', 'propose'] as const) {
  test(`successful media-edit ${action} refreshes the plan and restores controls`, async ({
    page
  }, testInfo) => {
    const observed = await editor(page, action);
    await expect(
      page.getByText(
        action === 'render'
          ? /Rendered media is ready\./
          : /The agent proposal is ready\./
      )
    ).toBeVisible();
    await expect.poll(observed.stateReads).toBe(2);
    await expect(
      page.getByText('Refreshed public presentation.', { exact: true })
    ).toBeVisible();
    await expect(
      page.getByRole('button', { name: 'Save', exact: true })
    ).toBeEnabled();
    await expect(page.getByRole('alert')).toHaveCount(0);
    if (action === 'render') {
      await expect(
        page.getByRole('heading', { name: 'Latest rendered edit' })
      ).toBeVisible();
      await expect(
        page.getByRole('link', { name: 'Media', exact: true })
      ).toHaveAttribute('href', '/api/v1/artifacts/rendered-video/content');
      await expect(
        page.getByRole('link', { name: 'Subtitles', exact: true })
      ).toHaveAttribute('href', '/api/v1/artifacts/rendered-captions/content');
      await page.screenshot({
        path: testInfo.outputPath('render-desktop.png'),
        fullPage: true
      });
      await page.setViewportSize({ width: 390, height: 844 });
      await expect
        .poll(() => page.evaluate(() => document.documentElement.scrollWidth))
        .toBe(390);
      await page.screenshot({
        path: testInfo.outputPath('render-mobile.png'),
        fullPage: true
      });
    }
    expect(observed.errors).toEqual([]);
  });
}

test('failed media-edit render keeps its error and does not refresh the plan', async ({
  page
}) => {
  const observed = await editor(page, 'render', true);
  await expect(page.getByRole('alert')).toContainText(
    'The encoder could not read the source.'
  );
  await expect(
    page.getByRole('button', { name: 'Save', exact: true })
  ).toBeEnabled();
  expect(observed.stateReads()).toBe(1);
  await expect(
    page.getByRole('heading', { name: 'Latest rendered edit' })
  ).toHaveCount(0);
  expect(observed.errors).toEqual([]);
});
