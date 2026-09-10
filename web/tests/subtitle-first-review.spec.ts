import { Buffer } from 'node:buffer';
import { expect, test, type Page } from '@playwright/test';

async function setup(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const headers = { 'X-CSRF-Token': auth.csrf_token };
  const created = await page.request.post('/api/v1/sessions', {
    headers,
    data: {
      name: `Subtitle review ${crypto.randomUUID()}`,
      workflow_kind: 'voiceover'
    }
  });
  expect(created.ok()).toBeTruthy();
  return { session: await created.json(), headers };
}

test('subtitle-first UI attaches independent media and keeps its timed text after reopening', async ({
  page
}) => {
  const { session, headers } = await setup(page);
  const uploaded = await page.request.post('/api/v1/uploads', {
    headers,
    multipart: {
      session_id: session.id,
      file: {
        name: 'authoritative.srt',
        mimeType: 'application/x-subrip',
        buffer: Buffer.from('1\n00:00:00,000 --> 00:00:01,000\nHello, world.\n')
      }
    }
  });
  expect(uploaded.ok()).toBeTruthy();
  const original = await uploaded.json();
  await page.goto(`/sessions/${session.id}`);
  const panel = page.getByRole('region', {
    name: 'Subtitle source and media target'
  });
  await expect(panel).toBeVisible();
  await expect(panel.getByText('Not attached', { exact: true })).toBeVisible();
  await expect(
    panel.getByRole('button', { name: 'Align existing words' })
  ).toBeDisabled();
  await panel.locator('input[type="file"]').setInputFiles({
    name: 'recording.wav',
    mimeType: 'audio/wav',
    buffer: Buffer.from('disposable media attachment fixture')
  });
  await expect(panel.getByText('recording.wav', { exact: true })).toBeVisible();
  await expect(
    panel.getByRole('button', { name: 'Align existing words' })
  ).toBeEnabled();
  await page.reload();
  await expect(
    panel.getByText('authoritative.srt', { exact: true })
  ).toBeVisible();
  await expect(panel.getByText('recording.wav', { exact: true })).toBeVisible();
  const status = await (
    await page.request.get(
      `/api/v1/sessions/${session.id}/sources/subtitle-status`
    )
  ).json();
  expect(status.subtitle_revision_id).toBe(
    original.attachment.subtitle_revision.revision_id
  );
  expect(status.adoption_required).toBe(false);
});

test('history previews and restores immutable speech revisions and pins stale-only generation', async ({
  page
}) => {
  const { session, headers } = await setup(page);
  const endpoint = `/api/v1/sessions/${session.id}`;
  const created = await page.request.post(`${endpoint}/generation-plan`, {
    headers,
    data: {
      segments: [
        { text: 'Hello world. Another complete thought.' },
        { text: 'A final thought.' }
      ]
    }
  });
  expect(created.ok()).toBeTruthy();
  const initial = await created.json();
  const segments = await (
    await page.request.get(`${endpoint}/generation-segments`)
  ).json();
  const edited = await page.request.post(
    `${endpoint}/generation-plan/topology/batch`,
    {
      headers: {
        ...headers,
        'If-Match': `"${initial.active_revision_id}"`,
        'Idempotency-Key': crypto.randomUUID()
      },
      data: {
        expected_revision_id: initial.active_revision_id,
        operations: [
          {
            action: 'split',
            segment_id: segments.items[0].id,
            boundary: { after_sentence: 1 }
          }
        ]
      }
    }
  );
  expect(edited.ok()).toBeTruthy();
  await page.goto(`/sessions/${session.id}`);
  await page.getByRole('button', { name: 'Generation', exact: true }).click();
  await page.getByRole('button', { name: 'Speech plans', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Versioned speech plans' });
  await expect(dialog.getByText('Hello world.', { exact: true })).toBeVisible();
  await dialog
    .getByLabel('Speech-plan revision')
    .selectOption(initial.active_revision_id);
  await expect(
    dialog.getByText('Hello world. Another complete thought.', { exact: true })
  ).toBeVisible();
  await dialog
    .getByRole('button', { name: 'Restore as a new active revision' })
    .click();
  await expect(
    dialog.getByRole('button', { name: 'Generate missing / stale only' })
  ).toBeVisible();
  const history = await (
    await page.request.get(`${endpoint}/generation-plan/revisions`)
  ).json();
  expect(history.items[0].action).toBe('restore');
  expect(history.active_revision_id).not.toBe(initial.active_revision_id);
  // Exercise request binding without calling a real speech provider.
  await page.route(`**${endpoint}/generation-runs`, async (route) => {
    if (route.request().method() !== 'POST') return route.continue();
    await route.fulfill({
      status: 422,
      contentType: 'application/json',
      body: JSON.stringify({
        error: {
          code: 'test_only',
          message: 'No inference in browser smoke test.'
        }
      })
    });
  });
  const queued = page.waitForRequest(
    (request) =>
      request.method() === 'POST' &&
      request.url().endsWith(`${endpoint}/generation-runs`)
  );
  await dialog
    .getByRole('button', { name: 'Generate missing / stale only' })
    .click();
  const payload = (await queued).postDataJSON();
  expect(payload.speech_plan_revision_id).toBe(history.active_revision_id);
  expect(payload.stale_only).toBe(true);
});
