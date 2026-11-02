import { expect, test, type Page } from '@playwright/test';

async function editor(
  page: Page,
  action: 'render' | 'propose',
  failed = false,
  options: { timingWarnings?: boolean; startAction?: boolean } = {}
) {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(
    page.getByRole('button', { name: 'Sign in', exact: true })
  ).toBeHidden();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const created = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': auth.csrf_token },
    data: {
      interface_mode: 'advanced',
      name: `Edit completion ${Date.now()}`,
      workflow_kind: 'media_edit'
    }
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
          timing_confidence: null,
          timing_flags: options.timingWarnings
            ? ['cue_overlap', 'word_overlap', 'word_order_conflict']
            : []
        },
        ...(options.timingWarnings
          ? [
              {
                id: 'cue-2',
                start_ms: 17000,
                end_ms: 24000,
                text: 'The second speaker overlaps briefly.',
                speaker: 'Guest',
                words: [],
                timing_source: 'captions',
                timing_flags: ['cue_overlap', 'word_overlap']
              },
              {
                id: 'cue-3',
                start_ms: 30000,
                end_ms: 33000,
                text: 'This cue has clear timing.',
                words: [],
                timing_source: 'captions',
                timing_flags: []
              }
            ]
          : [])
      ],
      timing_diagnostics: options.timingWarnings
        ? {
            cue_overlap_events: 1,
            word_overlap_events: 1,
            word_order_conflicts: 1,
            affected_cue_count: 2
          }
        : undefined,
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
  ).toBeVisible({ timeout: 20_000 });
  if (options.startAction === false)
    return { stateReads: () => stateReads, errors };
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

for (const width of [1280, 390]) {
  test(`timing warnings identify and filter affected recording cues at ${width}px`, async ({
    page
  }, info) => {
    await page.setViewportSize({ width, height: 844 });
    const observed = await editor(page, 'render', false, {
      timingWarnings: true,
      startAction: false
    });
    const warnings = page.getByRole('region', {
      name: 'Timing warnings',
      exact: true
    });
    await expect(warnings).toContainText('Timing warnings in 2 cues');
    await warnings
      .getByRole('button', { name: 'Review timing warnings' })
      .click();
    const filter = page.getByLabel('Only timing warnings (2)', { exact: true });
    await expect(filter).toBeChecked();
    await expect(page.locator('.transcript .cue')).toHaveCount(2);
    await expect(
      page.getByText('Word times run backwards', { exact: true })
    ).toBeVisible();
    await page.screenshot({
      path: info.outputPath(`timing-warnings-${width}.png`)
    });
    const geometry = await page.evaluate(() => ({
      width: window.innerWidth,
      scroll: document.documentElement.scrollWidth
    }));
    expect(geometry.scroll).toBeLessThanOrEqual(geometry.width + 1);
    if (width === 390) {
      const drawer = page.getByRole('dialog', {
        name: 'Transcript & details',
        exact: true
      });
      await expect(drawer).toBeVisible();
      await page.keyboard.press('Escape');
      await expect(drawer).toBeHidden();
      const trigger = warnings.getByRole('button', {
        name: 'Review timing warnings'
      });
      await expect(trigger).toBeFocused();
      await trigger.click();
      await expect(filter).toBeChecked();
    }
    await filter.uncheck();
    await expect(page.locator('.transcript .cue')).toHaveCount(3);
    await page
      .getByRole('button', { name: /The second speaker overlaps briefly/ })
      .click();
    await expect
      .poll(() =>
        page
          .locator('video')
          .evaluate((video) => (video as HTMLVideoElement).currentTime)
      )
      .toBe(17);
    expect(observed.errors).toEqual([]);
  });
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
      const videoBox = await page.locator('video').first().boundingBox();
      const transcriptBox = await page
        .locator('[data-workspace-tools]')
        .boundingBox();
      expect(transcriptBox!.x).toBeGreaterThanOrEqual(
        videoBox!.x + videoBox!.width - 1
      );
      await page.screenshot({
        path: testInfo.outputPath('render-desktop.png'),
        fullPage: true
      });
      await page.setViewportSize({ width: 390, height: 844 });
      await expect
        .poll(() => page.evaluate(() => document.documentElement.scrollWidth))
        .toBe(390);
      const drawer = page.getByRole('dialog', {
        name: 'Transcript & details',
        exact: true
      });
      await expect(drawer).toBeVisible();
      await page.keyboard.press('Escape');
      await expect(drawer).toBeHidden();
      await expect(
        page.getByRole('button', {
          name: 'Show transcript & details',
          exact: true
        })
      ).toBeVisible();
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
