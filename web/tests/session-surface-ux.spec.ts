import { expect, test, type Page } from '@playwright/test';

async function fixture(page: Page, reviewed = false) {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const headers = { 'X-CSRF-Token': auth.csrf_token };
  const response = await page.request.post('/api/v1/sessions', {
    headers,
    data: {
      name: `Session clarity ${crypto.randomUUID()}`,
      workflow_kind: 'audiobook'
    }
  });
  expect(response.ok()).toBeTruthy();
  const id = (await response.json()).id as string;
  const created = await page.request.post(
    `/api/v1/sessions/${id}/generation-plan`,
    {
      headers,
      data: { segments: [{ text: 'First block.' }, { text: 'Second block.' }] }
    }
  );
  expect(created.ok()).toBeTruthy();
  const status = await (
    await page.request.get(
      `/api/v1/sessions/${id}/generation-plan/status?summary=true`
    )
  ).json();
  await page.route(
    `**/api/v1/sessions/${id}/generation-plan/status*`,
    (route) =>
      route.fulfill({
        json: {
          ...status,
          can_generate: true,
          warning: 'The input text changed. Review the selected version.',
          blocked_reason: null,
          items: status.items.map((item: Record<string, unknown>) => ({
            ...item,
            reviewed,
            audio_reuse_checked: false,
            reusable_segment_count: null,
            stale_segment_count: null,
            audio_settings_stale_segment_count: null,
            audio_identity_unknown_segment_count: null
          }))
        }
      })
  );
  await page.route(`**/api/v1/sessions/${id}/workflow`, (route) =>
    route.fulfill({
      json: {
        session_id: id,
        workflow_kind: 'audiobook',
        workflow_preset: 'default',
        revision: 1,
        sources: [],
        stages: [
          {
            number: 1,
            key: 'generate_audio',
            title: 'Generate audio',
            explanation: 'Record the selected speech plan.',
            status: 'ready',
            executable: true,
            included: true,
            artifacts: [],
            artifact: null,
            selected_artifact_id: null,
            job_id: null,
            progress: null,
            detail: null,
            usage: {
              input_tokens: 1200,
              output_tokens: 300,
              cached_input_tokens: 200,
              total_tokens: 1500,
              cost_usd: 0.01,
              model_id: 'example-model',
              model_ids: ['example-model'],
              event_count: 1
            }
          }
        ]
      }
    })
  );
  return { id, errors };
}

test('workflow mode labels describe selection and do not start generation', async ({
  page
}) => {
  const { id, errors } = await fixture(page);
  let starts = 0;
  page.on('request', (request) => {
    if (
      request.method() === 'POST' &&
      /\/(generation-runs|jobs)$/.test(new URL(request.url()).pathname)
    )
      starts++;
  });
  await page.goto(`/sessions/${id}`);
  const help = page.getByTestId('session-workflow-help');
  await expect(help).not.toHaveAttribute('open');
  const manual = page.getByRole('button', {
    name: 'Review each stage',
    exact: true
  });
  const automatic = page.getByRole('button', {
    name: 'Automatic workflow',
    exact: true
  });
  await expect(manual).toHaveAttribute('aria-pressed', 'true');
  await expect(automatic).toHaveAttribute('aria-pressed', 'false');
  await automatic.click();
  await expect(automatic).toHaveAttribute('aria-pressed', 'true');
  await help.locator('summary').click();
  await expect(help).toContainText('Choosing this mode does not start a job');
  await manual.click();
  await expect(manual).toHaveAttribute('aria-pressed', 'true');
  await expect(help).toHaveAttribute('open');
  expect(starts).toBe(0);
  expect(errors).toEqual([]);
});

test('plan summary avoids false audio counts and keeps warnings visible', async ({
  page
}) => {
  const { id, errors } = await fixture(page);
  await page.goto(`/sessions/${id}`);
  const card = page.getByRole('region', { name: 'Speech plan', exact: true });
  await expect(card.getByTestId('speech-plan-summary')).toContainText(
    '2 included blocks'
  );
  await expect(card.getByTestId('speech-plan-next-action')).toContainText(
    'Open Review plan'
  );
  const details = card.getByTestId('speech-plan-details');
  await expect(details).not.toHaveAttribute('open');
  await expect(
    card.getByText('The input text changed. Review the selected version.')
  ).toBeVisible();
  await expect(
    card.getByRole('button', { name: 'Mark reviewed', exact: true })
  ).toBeVisible();
  await details.locator('summary').click();
  await expect(details).toContainText(
    'Recording compatibility has not been checked yet'
  );
  await expect(details).not.toContainText('0 recordings match');
  expect(errors).toEqual([]);
});

test('reviewed plan avoids a redundant disabled Reviewed button', async ({
  page
}) => {
  const { id, errors } = await fixture(page, true);
  await page.goto(`/sessions/${id}`);
  const card = page.getByRole('region', { name: 'Speech plan', exact: true });
  await expect(card.getByTestId('speech-plan-summary')).toContainText(
    'Reviewed'
  );
  await expect(card.getByTestId('speech-plan-next-action')).toContainText(
    'Use Generate audio'
  );
  await expect(
    card.getByRole('button', { name: 'Reviewed', exact: true })
  ).toHaveCount(0);
  await expect(
    card.getByRole('button', { name: 'Review plan', exact: true })
  ).toBeVisible();
  expect(errors).toEqual([]);
});

test('generation has one primary opener and diagnostics do not crowd the card', async ({
  page
}) => {
  const { id, errors } = await fixture(page);
  await page.goto(`/sessions/${id}`);
  const card = page.locator('article').filter({
    has: page.getByRole('heading', { name: 'Generate audio', exact: true })
  });
  await expect(
    card.getByRole('button', { name: 'Generate audio…', exact: true })
  ).toHaveCount(1);
  await expect(
    card.getByRole('button', { name: 'Refresh changed audio…', exact: true })
  ).toBeVisible();
  const metrics = card.getByLabel('Generate audio run metrics');
  await expect(metrics).toContainText('Cost');
  const details = card.getByTestId('stage-usage-details');
  await expect(details).not.toHaveAttribute('open');
  await expect(details.getByText('Input tokens', { exact: true })).toBeHidden();
  await details.locator('summary').click();
  await expect(
    details.getByText('Input tokens', { exact: true })
  ).toBeVisible();
  await expect(details).toContainText('1,200');
  await expect(details.getByLabel('Model example-model')).toBeVisible();
  expect(errors).toEqual([]);
});
