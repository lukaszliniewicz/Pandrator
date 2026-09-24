import { expect, test, type Page } from '@playwright/test';

async function fixture(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const created = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': auth.csrf_token },
    data: {
      name: `Independent generation ${crypto.randomUUID()}`,
      workflow_kind: 'voiceover'
    }
  });
  const sessionId = (await created.json()).id;
  let edited = false;
  let patch: Record<string, unknown> | undefined;
  let started: Record<string, unknown> | undefined;
  const old = {
    id: 'history',
    session_id: sessionId,
    plan_revision_id: 'r1',
    sequence_number: 1,
    label: 'Run 1: Saved voice',
    operation: 'generate',
    status: 'completed',
    progress: 1,
    settings_snapshot: {
      tts: { service: 'openai', model: 'tts-1', voice: 'nova', speed: 1 }
    }
  };
  const row = () => ({
    id: edited ? 'edited-row' : 'old-row',
    ordinal: 0,
    revision: 1,
    plan_revision_id: edited ? 'r2' : 'r1',
    text: edited ? 'New subtitles.' : 'Old subtitles.',
    optimized_text: 'Original spoken wording.',
    node_kind: 'subtitle_cue',
    source_segment_ids: [1],
    status: 'stale',
    marked: false,
    removed: false,
    takes: []
  });
  await page.route(
    `**/api/v1/sessions/${sessionId}/generation-runs`,
    async (route) => {
      if (route.request().method() === 'POST') {
        started = route.request().postDataJSON();
        await route.fulfill({
          json: {
            ...old,
            id: 'replacement',
            plan_revision_id: 'r2',
            status: 'queued'
          },
          status: 202
        });
      } else await route.fulfill({ json: { items: [old] } });
    }
  );
  await page.route(
    `**/api/v1/sessions/${sessionId}/generation-segments?*`,
    (route) =>
      route.fulfill({
        json: {
          items: [row()],
          total: 1,
          next_cursor: null,
          plan_revision_id: edited ? 'r2' : 'r1'
        }
      })
  );
  await page.route('**/api/v1/generation-segments/old-row', async (route) => {
    patch = route.request().postDataJSON();
    edited = true;
    await route.fulfill({ json: { ...row(), previous_segment_id: 'old-row' } });
  });
  await page.goto(`/sessions/${sessionId}`);
  await page.getByRole('button', { name: 'Generation', exact: true }).click();
  await page
    .getByRole('combobox', { name: 'Audio view', exact: true })
    .selectOption('history');
  return { sessionId, patch: () => patch, started: () => started };
}

test('display edit retains spoken wording and historical settings after plan copy', async ({
  page
}) => {
  const state = await fixture(page);
  await expect(
    page.getByRole('combobox', { name: 'Text to edit', exact: true })
  ).toHaveValue('display');
  const field = page.getByLabel('Script text for segment 1');
  await field.fill('New subtitles.');
  await field.press('Tab');
  await expect.poll(state.patch).toEqual({
    text: 'New subtitles.',
    optimized_text: 'Original spoken wording.'
  });
  await expect(
    page.getByRole('combobox', { name: 'Audio view', exact: true })
  ).toHaveValue('');
  await expect(page.locator('[data-generation-settings-source]')).toContainText(
    'Run 1'
  );
  await page
    .getByRole('combobox', { name: 'Text to edit', exact: true })
    .selectOption('speech');
  await expect(page.getByLabel('Spoken override for segment 1')).toHaveValue(
    'Original spoken wording.'
  );
  await page
    .getByRole('button', { name: 'Regenerate segment 1', exact: true })
    .click();
  await page.getByRole('menuitem', { name: 'Regenerate', exact: true }).click();
  await expect.poll(state.started).toMatchObject({
    operation: 'regenerate',
    segment_ids: ['edited-row'],
    speech_plan_revision_id: 'r2',
    settings_source_run_id: 'history',
    generation_run_id: null
  });
});

test('full generation preview retains historical settings and queued rows explain export wait', async ({
  page
}) => {
  const state = await fixture(page);
  let previewBody: Record<string, unknown> | undefined;
  await page.route(
    `**/api/v1/sessions/${state.sessionId}/generation-runs/preview`,
    async (route) => {
      previewBody = route.request().postDataJSON();
      await route.fulfill({
        json: {
          mode: 'missing',
          speech_plan_revision_id: 'r1',
          selection_hash: 'hash',
          total_count: 1,
          generate_count: 1,
          preserve_count: 0,
          replace_count: 0,
          missing_count: 1,
          reasons: { missing_audio: 1 },
          settings_summary: { service: 'openai', model: 'tts-1', voice: 'nova' }
        }
      });
    }
  );
  await page
    .getByRole('button', { name: 'Generate audio…', exact: true })
    .click();
  await expect
    .poll(() => previewBody)
    .toMatchObject({
      settings_source_run_id: 'history',
      speech_plan_revision_id: 'r1'
    });
  await page
    .getByRole('button', { name: 'Generate 1 block', exact: true })
    .click();
  await expect.poll(state.started).toMatchObject({
    settings_source_run_id: 'history',
    expected_selection_hash: 'hash'
  });
  await page.route(
    `**/api/v1/sessions/${state.sessionId}/generation-runs`,
    (route) =>
      route.fulfill({
        json: {
          items: [
            {
              id: 'queued',
              session_id: state.sessionId,
              plan_revision_id: 'r1',
              sequence_number: 2,
              label: 'Replacement',
              operation: 'regenerate',
              status: 'queued',
              progress: 0,
              queued_segment_ids: ['old-row'],
              waiting_for_job: {
                id: 'export',
                kind: 'export.variant',
                progress_detail: 'Extending final frame'
              }
            }
          ]
        }
      })
  );
  await page.reload();
  await page.getByRole('button', { name: 'Generation', exact: true }).click();
  await expect(
    page.getByText('Regeneration queued — waiting for export', { exact: true })
  ).toBeVisible();
});
