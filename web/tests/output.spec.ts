import { expect, test, type Page } from '@playwright/test';

async function signIn(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const closeTour = page.getByRole('button', { name: 'Close tour' });
  if (await closeTour.isVisible()) await closeTour.click();
}

async function createSession(page: Page, workflowKind: string) {
  const authStatus = await page.request.get('/api/v1/auth/status');
  const csrfToken = (await authStatus.json()).csrf_token;
  const headers = { 'X-CSRF-Token': csrfToken };
  const response = await page.request.post('/api/v1/sessions', {
    headers,
    data: { name: `Output regression ${crypto.randomUUID()}`, workflow_kind: workflowKind }
  });
  expect(response.ok()).toBeTruthy();
  return { session: await response.json(), headers };
}

test('output tab renders the actual running export status and progress', async ({ page }) => {
  await signIn(page);
  const { session } = await createSession(page, 'voiceover');
  await page.route('**/api/v1/jobs?limit=500', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        items: [{
          id: '12345678-running-export-job',
          kind: 'export.create',
          session_id: session.id,
          status: 'running',
          progress: 0.42,
          created_at: new Date().toISOString()
        }]
      })
    });
  });

  await page.goto(`/sessions/${session.id}/output`);
  await expect(page.getByText('Export activity')).toBeVisible();
  await expect(page.getByText('Running export')).toBeVisible();
  await expect(page.getByText('42%')).toBeVisible();
});

test('completed subtitle exports can be removed from Output', async ({ page }) => {
  await signIn(page);
  const { session, headers } = await createSession(page, 'subtitles');
  const uploaded = await page.request.post('/api/v1/uploads', {
    headers,
    multipart: {
      session_id: session.id,
      purpose: 'source',
      file: {
        name: 'source.srt',
        mimeType: 'application/x-subrip',
        buffer: (globalThis as any).Buffer.from('1\n00:00:00,000 --> 00:00:01,000\nHello\n')
      }
    }
  });
  expect(uploaded.ok()).toBeTruthy();

  await page.goto(`/sessions/${session.id}/output`);
  await page.getByRole('button', { name: 'Create subtitle export' }).click();
  await expect(page.getByText('Completed export')).toBeVisible({ timeout: 20_000 });
  const remove = page.getByRole('button', { name: /Remove export/ }).first();
  await expect(remove).toBeVisible();
  page.once('dialog', (dialog) => dialog.accept());
  await remove.click();
  await expect(page.getByText('Export removed.')).toBeVisible();
  await expect(page.getByRole('button', { name: /Remove export/ })).toHaveCount(0);
});

test('Create export saves the visible burned-subtitle selection before submitting', async ({ page }) => {
  await signIn(page);
  const { session, headers } = await createSession(page, 'voiceover');
  const uploaded = await page.request.post('/api/v1/uploads', {
    headers,
    multipart: {
      session_id: session.id,
      purpose: 'source',
      file: {
        name: 'source-video.mp4',
        mimeType: 'video/mp4',
        buffer: (globalThis as any).Buffer.from('media fixture')
      }
    }
  });
  expect(uploaded.ok()).toBeTruthy();

  const requests: string[] = [];
  page.on('request', (request) => {
    if (request.method() === 'PUT' && request.url().endsWith(`/sessions/${session.id}/settings/output`)) requests.push('save');
  });
  await page.route(`**/api/v1/sessions/${session.id}/stages/export/run`, async (route) => {
    requests.push('export');
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        id: 'burned-export-job', kind: 'export.create', session_id: session.id,
        status: 'queued', progress: 0, created_at: new Date().toISOString()
      })
    });
  });

  await page.goto(`/sessions/${session.id}/output`);
  await page.getByLabel('Audio result').selectOption('preserve');
  await page.locator('label').filter({ hasText: /^Subtitles/ }).locator('select').selectOption('burned');
  await page.getByRole('button', { name: 'Create export' }).click();
  await expect(page.getByText(/Export burned-e was submitted/)).toBeVisible();

  expect(requests).toEqual(['save', 'export']);
  const saved = await page.request.get(`/api/v1/sessions/${session.id}/settings/output`);
  expect((await saved.json()).override.subtitle_mode).toBe('burned');
});

test('Create export rebuilds a completed assembly when synchronization settings changed', async ({ page }) => {
  await signIn(page);
  const { session, headers } = await createSession(page, 'voiceover');
  const uploaded = await page.request.post('/api/v1/uploads', {
    headers,
    multipart: {
      session_id: session.id,
      purpose: 'source',
      file: {
        name: 'source-video.mp4',
        mimeType: 'video/mp4',
        buffer: (globalThis as any).Buffer.from('media fixture')
      }
    }
  });
  expect(uploaded.ok()).toBeTruthy();

  let assemblyRequested = false;
  const requests: string[] = [];
  await page.route(`**/api/v1/sessions/${session.id}/generation-runs`, async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        items: [{
          id: 'completed-run', status: 'completed', label: 'Run 1: Test voice',
          assembly: {
            id: assemblyRequested ? 'fresh-assembly' : 'stale-assembly',
            status: 'completed',
            settings_hash: assemblyRequested ? 'fresh' : 'stale'
          }
        }]
      })
    });
  });
  await page.route(`**/api/v1/sessions/${session.id}/output-assemblies`, async (route) => {
    assemblyRequested = true;
    requests.push('assembly');
    await route.fulfill({ status: 202, contentType: 'application/json', body: JSON.stringify({ id: 'fresh-assembly', status: 'queued' }) });
  });
  await page.route(`**/api/v1/sessions/${session.id}/settings/resolve`, async (route) => {
    requests.push('resolve');
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ value: {}, settings_hash: 'current-settings' }) });
  });
  await page.route(`**/api/v1/sessions/${session.id}/stages/export/run`, async (route) => {
    requests.push('export');
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        id: 'rebuilt-export-job', kind: 'export.create', session_id: session.id,
        status: 'queued', progress: 0, created_at: new Date().toISOString()
      })
    });
  });

  await page.goto(`/sessions/${session.id}/output`);
  await expect(page.getByLabel('Audio version')).toHaveValue('completed-run');
  await page.getByLabel('Audio result').selectOption('mixed');
  await page.getByLabel('Maximum speed-up').fill('1.25');
  await page.getByRole('button', { name: 'Create export' }).click();
  await expect(page.getByText(/Export rebuilt- was submitted/)).toBeVisible({ timeout: 10_000 });

  expect(assemblyRequested).toBeTruthy();
  expect(requests).toEqual(['resolve', 'assembly', 'export']);
});
