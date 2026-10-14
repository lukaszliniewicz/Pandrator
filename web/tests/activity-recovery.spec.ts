import { expect, test, type Page } from '@playwright/test';

async function failedActivity(
  page: Page,
  options: {
    busy?: boolean;
    foreign?: boolean;
    trashed?: boolean;
    acceptStart?: boolean;
  } = {}
) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.locator('.app-shell')).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const csrf = (await (await page.request.get('/api/v1/auth/status')).json())
    .csrf_token;
  const created = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': csrf },
    data: {
      name: `Consciousness — Japanese recovery ${crypto.randomUUID().slice(0, 8)}`,
      workflow_kind: 'audiobook'
    }
  });
  expect(created.ok(), await created.text()).toBeTruthy();
  const project = await created.json();
  const planned = await page.request.post(
    `/api/v1/sessions/${project.id}/generation-plan`,
    {
      headers: { 'X-CSRF-Token': csrf },
      data: {
        segments: Array.from({ length: 5 }, (_, index) => ({
          text: `Block ${index + 1}.`
        }))
      }
    }
  );
  expect(planned.ok(), await planned.text()).toBeTruthy();
  const segments = await (
    await page.request.get(
      `/api/v1/sessions/${project.id}/generation-segments?limit=250`
    )
  ).json();
  const planRevisionId = segments.plan_revision_id as string;
  expect(planRevisionId).toBeTruthy();
  const job = {
    id: 'activity-failed-job',
    kind: 'generation.run',
    session_id: project.id,
    generation_run_id: 'activity-failed-run',
    status: 'failed',
    progress: 0.4,
    progress_detail: 'Generated block 2 of 5',
    error_message:
      'Speech generation failed after rate-limit retries (HTTP 429).',
    created_at: '2026-10-14T00:00:00Z',
    project: {
      id: project.id,
      name: project.name,
      available: !options.trashed,
      trashed: Boolean(options.trashed)
    }
  };
  await page.route('**/api/v1/events/snapshot?view=compact', async (route) => {
    const response = await route.fetch();
    const snapshot = await response.json();
    snapshot.jobs = { items: [job] };
    await route.fulfill({ response, json: snapshot });
  });
  await page.route('**/api/v1/jobs?**', (route) =>
    route.fulfill({ json: { items: [job] } })
  );
  await page.route('**/api/v1/generation-runs/activity-failed-run', (route) =>
    route.fulfill({
      json: {
        id: 'activity-failed-run',
        session_id: options.foreign ? 'another-project' : project.id,
        status: 'failed'
      }
    })
  );
  await page.route(
    `**/api/v1/sessions/${project.id}/generation/summary`,
    (route) =>
      route.fulfill({
        json: {
          session_id: project.id,
          plan_revision_id: planRevisionId,
          total: 5,
          included_total: 5,
          active_run: options.busy
            ? { id: 'newer-run', status: 'running', progress: 0.5 }
            : null,
          assembly: null
        }
      })
  );
  const previews: Record<string, unknown>[] = [];
  await page.route(
    `**/api/v1/sessions/${project.id}/generation-runs/preview`,
    async (route) => {
      previews.push(route.request().postDataJSON());
      await route.fulfill({
        json: {
          mode: 'missing',
          speech_plan_revision_id: planRevisionId,
          selection_hash: 'safe-continuation',
          total_count: 5,
          generate_count: 1,
          preserve_count: 4,
          replace_count: 0,
          missing_count: 1,
          first_generate_ordinal: 2,
          reasons: { missing_audio: 1 },
          settings_summary: {
            service: 'Vertex AI',
            model: 'gemini-3.8-flash-tts',
            voice: 'Saved narrator'
          }
        }
      });
    }
  );
  let starts = 0;
  const admissions: Record<string, unknown>[] = [];
  await page.route(
    `**/api/v1/sessions/${project.id}/generation-admissions`,
    async (route) => {
      starts++;
      admissions.push(route.request().postDataJSON());
      await route.fulfill(
        options.acceptStart
          ? { status: 202, json: { job_id: 'activity-preparation' } }
          : {
              status: 409,
              json: {
                error: {
                  message: 'No generation is authorized by this fixture.'
                }
              }
            }
      );
    }
  );
  await page.route('**/api/v1/jobs/activity-preparation', (route) =>
    route.fulfill({
      json: {
        id: 'activity-preparation',
        status: 'succeeded',
        result_json: { generation_run_id: 'activity-continuation' }
      }
    })
  );
  await page.route('**/api/v1/generation-runs/activity-continuation', (route) =>
    route.fulfill({
      json: {
        id: 'activity-continuation',
        session_id: project.id,
        plan_revision_id: planRevisionId,
        sequence_number: 2,
        status: 'queued',
        progress: 0
      }
    })
  );
  await page.goto('/activity');
  return {
    project,
    planRevisionId,
    previews,
    admissions,
    starts: () => starts
  };
}

for (const width of [1280, 390]) {
  test(`Activity offers project navigation and a preserving continuation at ${width}`, async ({
    page
  }, info) => {
    await page.setViewportSize({ width, height: 900 });
    const fixture = await failedActivity(page);
    await expect(
      page.getByText(fixture.project.name, { exact: true })
    ).toBeVisible();
    await expect(
      page.getByRole('link', { name: 'Open project', exact: true })
    ).toHaveAttribute('href', `/sessions/${fixture.project.id}`);
    await expect(page.getByText('Generated block 2 of 5')).toBeVisible();
    if (width < 640) {
      const failure = page.getByText(
        'Speech generation failed after rate-limit retries (HTTP 429).',
        { exact: true }
      );
      expect((await failure.boundingBox())?.width).toBeGreaterThan(width * 0.6);
    }
    await page.screenshot({
      path: info.outputPath('activity-project-recovery.png'),
      fullPage: true
    });
    await page
      .getByRole('button', {
        name: `Continue unfinished audio for ${fixture.project.name}`,
        exact: true
      })
      .click();
    const dialog = page.getByTestId('generation-start-dialog');
    await expect(dialog).toBeVisible();
    await expect(
      dialog.getByTestId('generation-start-mode-continue')
    ).toBeChecked();
    await expect(dialog.getByTestId('generation-start-counts')).toContainText(
      '4 kept'
    );
    await expect(
      dialog.getByTestId('generation-start-mode-all')
    ).not.toBeChecked();
    await expect.poll(() => fixture.previews.length).toBeGreaterThan(0);
    expect(fixture.previews[0]).toMatchObject({
      settings_source_run_id: 'activity-failed-run',
      speech_plan_revision_id: fixture.planRevisionId,
      missing_only: true,
      stale_only: false
    });
    expect(fixture.starts()).toBe(0);
    await page.screenshot({
      path: info.outputPath('activity-continuation-preview.png'),
      fullPage: true
    });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth
      )
    ).toBeTruthy();
  });
}

test('Activity confirms the preserving selection before opening the active generation workspace', async ({
  page
}) => {
  const fixture = await failedActivity(page, { acceptStart: true });
  await page
    .getByRole('button', {
      name: `Continue unfinished audio for ${fixture.project.name}`,
      exact: true
    })
    .click();
  const dialog = page.getByTestId('generation-start-dialog');
  await expect(dialog.getByTestId('generation-start-counts')).toContainText(
    '4 kept'
  );
  expect(fixture.starts()).toBe(0);
  await dialog.getByTestId('generation-start-submit').click();
  await expect(page).toHaveURL(
    new RegExp(`/sessions/${fixture.project.id}/generation\\?view=active$`)
  );
  expect(fixture.starts()).toBe(1);
  expect(fixture.admissions[0]).toMatchObject({
    settings_source_run_id: 'activity-failed-run',
    speech_plan_revision_id: fixture.planRevisionId,
    missing_only: true,
    stale_only: false,
    expected_selection_hash: 'safe-continuation'
  });
  await expect(page.getByTestId('generation-start-dialog')).toHaveCount(0);
});

for (const condition of ['busy', 'foreign'] as const) {
  test(`Activity refuses a ${condition} continuation before previewing or starting`, async ({
    page
  }) => {
    const fixture = await failedActivity(page, { [condition]: true });
    await page
      .getByRole('button', {
        name: `Continue unfinished audio for ${fixture.project.name}`,
        exact: true
      })
      .click();
    await expect(page.getByRole('alert')).toContainText(
      condition === 'busy'
        ? 'already has generation in progress'
        : 'no longer a failed run in this project'
    );
    await expect(page.getByTestId('generation-start-dialog')).toHaveCount(0);
    expect(fixture.previews).toHaveLength(0);
    expect(fixture.starts()).toBe(0);
  });
}

test('Activity retains a trashed project name and offers restoration guidance', async ({
  page
}) => {
  const fixture = await failedActivity(page, { trashed: true });
  await expect(
    page.getByText(fixture.project.name, { exact: true })
  ).toBeVisible();
  await expect(
    page.getByText('In Trash — restore the project to continue.')
  ).toBeVisible();
  await expect(
    page.getByRole('button', { name: /Continue unfinished audio for/ })
  ).toHaveCount(0);
  await expect(
    page.getByRole('link', { name: 'Open project', exact: true })
  ).toHaveCount(0);
  await expect(
    page.getByRole('link', { name: 'Open projects', exact: true })
  ).toHaveAttribute('href', '/sessions');
});
