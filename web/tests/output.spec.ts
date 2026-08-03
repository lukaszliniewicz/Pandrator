import { Buffer } from 'node:buffer';
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
    data: {
      name: `Output regression ${crypto.randomUUID()}`,
      workflow_kind: workflowKind
    }
  });
  expect(response.ok()).toBeTruthy();
  return { session: await response.json(), headers };
}

test('output tab renders the actual running export status and progress', async ({
  page
}) => {
  await signIn(page);
  const { session } = await createSession(page, 'voiceover');
  await page.route('**/api/v1/jobs?limit=500', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        items: [
          {
            id: '12345678-running-export-job',
            kind: 'export.create',
            session_id: session.id,
            status: 'running',
            progress: 0.42,
            progress_detail: 'Prepared subtitle track 1 of 2',
            created_at: new Date().toISOString()
          }
        ]
      })
    });
  });

  await page.goto(`/sessions/${session.id}/output`);
  await expect(page.getByText('Export activity')).toBeVisible();
  await expect(page.getByText('Running export')).toBeVisible();
  await expect(page.getByText('42%')).toBeVisible();
  await expect(page.getByText('Prepared subtitle track 1 of 2')).toBeVisible();
});

test('completed subtitle exports can be removed from Output', async ({
  page
}) => {
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
        buffer: Buffer.from('1\n00:00:00,000 --> 00:00:01,000\nHello\n')
      }
    }
  });
  expect(uploaded.ok()).toBeTruthy();

  await page.goto(`/sessions/${session.id}/output`);
  await page.getByRole('button', { name: 'Create subtitle export' }).click();
  await expect(page.getByText('Completed export')).toBeVisible({
    timeout: 20_000
  });
  const settingsUsed = page.getByText('Settings used').first();
  await expect(settingsUsed).toBeVisible();
  await settingsUsed.click();
  await expect(page.getByText('Export Mode').first()).toBeVisible();
  const remove = page.getByRole('button', { name: /Remove export/ }).first();
  await expect(remove).toBeVisible();
  page.once('dialog', (dialog) => dialog.accept());
  await remove.click();
  await expect(page.getByText('Export removed.')).toBeVisible();
  await expect(page.getByRole('button', { name: /Remove export/ })).toHaveCount(
    0
  );
});

test('Create export saves the visible burned-subtitle selection before submitting', async ({
  page
}) => {
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
        buffer: Buffer.from('media fixture')
      }
    }
  });
  expect(uploaded.ok()).toBeTruthy();

  const requests: string[] = [];
  let exportPayload: Record<string, unknown> | null = null;
  page.on('request', (request) => {
    if (
      request.method() === 'PUT' &&
      request.url().endsWith(`/sessions/${session.id}/settings/output`)
    )
      requests.push('save');
  });
  await page.route(
    `**/api/v1/sessions/${session.id}/stages/export/run`,
    async (route) => {
      requests.push('export');
      exportPayload = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          id: 'burned-export-job',
          kind: 'export.create',
          session_id: session.id,
          status: 'queued',
          progress: 0,
          created_at: new Date().toISOString()
        })
      });
    }
  );

  await page.goto(`/sessions/${session.id}/output`);
  await page.getByLabel('Audio result').selectOption('preserve');
  await page
    .locator('label')
    .filter({ hasText: /^Subtitles/ })
    .locator('select')
    .selectOption('burned');
  await page.getByText('Advanced video encoding').click();
  await page.getByLabel('Output resolution').selectOption('720p');
  await page.getByRole('button', { name: 'Create export' }).click();
  await expect(page.getByText(/Export burned-e was submitted/)).toBeVisible();

  expect(requests).toEqual(['save', 'export']);
  const saved = await page.request.get(
    `/api/v1/sessions/${session.id}/settings/output`
  );
  const savedSettings = await saved.json();
  expect(savedSettings.override.subtitle_mode).toBe('burned');
  expect(savedSettings.override.burn_video_resolution).toBe('720p');
  expect(exportPayload).toEqual({
    output: {
      export_mode: 'media',
      audio_mode: 'preserve'
    }
  });
});

test('advanced video encoding can be enabled without subtitles', async ({
  page
}) => {
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
        buffer: Buffer.from('media fixture')
      }
    }
  });
  expect(uploaded.ok()).toBeTruthy();
  await page.route(
    `**/api/v1/sessions/${session.id}/stages/export/run`,
    async (route) => {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          id: 'advanced-video-export-job',
          kind: 'export.create',
          session_id: session.id,
          status: 'queued',
          progress: 0,
          created_at: new Date().toISOString()
        })
      });
    }
  );

  await page.goto(`/sessions/${session.id}/output`);
  await page.getByLabel('Audio result').selectOption('preserve');
  await page.getByText('Advanced video encoding').click();
  await expect(page.getByLabel('Output resolution')).toBeDisabled();
  await page.getByLabel('Transcode the video stream').check();
  await page.getByLabel('Output resolution').selectOption('1080p');
  await page.getByRole('button', { name: 'Create export' }).click();
  await expect(page.getByText(/Export advanced was submitted/)).toBeVisible();

  const saved = await page.request.get(
    `/api/v1/sessions/${session.id}/settings/output`
  );
  const savedSettings = await saved.json();
  expect(savedSettings.override.video_transcode).toBe(true);
  expect(savedSettings.override.subtitle_mode).toBeUndefined();
  expect(savedSettings.override.burn_video_resolution).toBe('1080p');
});

test('Create export keeps the selected audio version when saved effective defaults are omitted', async ({
  page
}) => {
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
        buffer: Buffer.from('media fixture')
      }
    }
  });
  expect(uploaded.ok()).toBeTruthy();

  await page.route(
    `**/api/v1/sessions/${session.id}/generation-runs`,
    async (route) => {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          items: [
            {
              id: 'selected-completed-run',
              status: 'completed',
              label: 'Run 1: Selected voice',
              assembly: {
                id: 'selected-assembly',
                status: 'completed',
                settings_hash: 'current-settings'
              }
            }
          ]
        })
      });
    }
  );
  await page.route(
    `**/api/v1/sessions/${session.id}/settings/output`,
    async (route) => {
      if (route.request().method() !== 'PUT') {
        await route.fallback();
        return;
      }
      const submitted = route.request().postDataJSON() as {
        value: Record<string, unknown>;
      };
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          revision: 2,
          effective: {},
          override: submitted.value
        })
      });
    }
  );
  await page.route(
    `**/api/v1/sessions/${session.id}/settings/resolve`,
    async (route) => {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ value: {}, settings_hash: 'current-settings' })
      });
    }
  );
  let exportPayload: Record<string, unknown> | null = null;
  await page.route(
    `**/api/v1/sessions/${session.id}/stages/export/run`,
    async (route) => {
      exportPayload = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          id: 'selected-version-export-job',
          kind: 'export.create',
          session_id: session.id,
          status: 'queued',
          progress: 0,
          created_at: new Date().toISOString()
        })
      });
    }
  );

  await page.goto(`/sessions/${session.id}/output`);
  await expect(page.getByLabel('Audio version')).toHaveValue(
    'selected-completed-run'
  );
  await page.getByLabel('Audio result').selectOption('mixed');
  await page.getByRole('button', { name: 'Create export' }).click();
  await expect(page.getByText(/Export selected was submitted/)).toBeVisible();

  expect(exportPayload).toEqual({
    output: {
      export_mode: 'media',
      audio_mode: 'mixed'
    },
    generation_run_id: 'selected-completed-run'
  });
});

test('soundtrack mix preview uses the selected version and current unsaved controls', async ({
  page
}) => {
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
        buffer: Buffer.from('media fixture')
      }
    }
  });
  expect(uploaded.ok()).toBeTruthy();

  await page.route(
    `**/api/v1/sessions/${session.id}/generation-runs`,
    async (route) => {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          items: [
            {
              id: 'preview-completed-run',
              status: 'completed',
              label: 'Run 1: Preview voice',
              assembly: {
                id: 'preview-completed-assembly',
                status: 'completed'
              }
            }
          ]
        })
      });
    }
  );
  const previewArtifact = {
    id: 'ducking-preview-artifact',
    session_id: session.id,
    kind: 'audio',
    role: 'mix_preview',
    relative_path: `sessions/${session.storage_key}/previews/soundtrack-mix-preview.wav`,
    mime_type: 'audio/wav',
    size_bytes: 4096,
    content_hash: 'preview-settings-hash',
    state: 'current',
    metadata_json: {},
    created_at: new Date().toISOString()
  };
  let previewPayload: Record<string, unknown> | null = null;
  await page.route(
    `**/api/v1/sessions/${session.id}/output-mix-preview`,
    async (route) => {
      previewPayload = route.request().postDataJSON() as Record<
        string,
        unknown
      >;
      await route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify({
          id: 'ducking-preview-job',
          kind: 'output.mix_preview',
          session_id: session.id,
          status: 'queued',
          progress: 0,
          created_at: new Date().toISOString()
        })
      });
    }
  );
  await page.route('**/api/v1/jobs/ducking-preview-job', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        id: 'ducking-preview-job',
        kind: 'output.mix_preview',
        session_id: session.id,
        status: 'succeeded',
        progress: 1,
        result_json: {
          artifact_id: previewArtifact.id,
          artifact: previewArtifact,
          start_seconds: 4.3,
          duration_seconds: 12,
          automatic_start: true
        },
        created_at: new Date().toISOString()
      })
    });
  });

  await page.goto(`/sessions/${session.id}/output`);
  await expect(page.getByLabel('Audio version')).toHaveValue(
    'preview-completed-run'
  );
  await page
    .locator('select:has(option[value="very_strong"])')
    .selectOption('very_strong');
  await page.getByLabel('Source level (dB)').fill('-3');
  await page.getByLabel('Voiceover level (dB)').fill('1.5');
  await page.getByRole('button', { name: 'Preview 12 seconds' }).click();

  await expect(
    page.getByRole('heading', { name: 'soundtrack-mix-preview.wav' })
  ).toBeVisible();
  await expect(page.locator('audio')).toHaveAttribute(
    'src',
    `/api/v1/artifacts/${previewArtifact.id}/content?v=preview-settings-hash`
  );
  expect(previewPayload).toMatchObject({
    generation_run_id: 'preview-completed-run',
    start_seconds: null,
    duration_seconds: 12,
    mix_source_gain_db: -3,
    mix_voice_gain_db: 1.5,
    mix_ducking: 'very_strong'
  });
});

test('Create export sends an explicit voiceover-only contract when no source is attached', async ({
  page
}) => {
  await signIn(page);
  const { session } = await createSession(page, 'voiceover');

  await page.route(
    `**/api/v1/sessions/${session.id}/generation-runs`,
    async (route) => {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          items: [
            {
              id: 'voiceover-only-run',
              status: 'completed',
              label: 'Run 1: Voiceover only',
              assembly: {
                id: 'voiceover-only-assembly',
                status: 'completed',
                settings_hash: 'voiceover-only-settings'
              }
            }
          ]
        })
      });
    }
  );
  await page.route(
    `**/api/v1/sessions/${session.id}/settings/output`,
    async (route) => {
      if (route.request().method() !== 'PUT') {
        await route.fallback();
        return;
      }
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ revision: 1, effective: {}, override: {} })
      });
    }
  );
  await page.route(
    `**/api/v1/sessions/${session.id}/settings/resolve`,
    async (route) => {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          value: {},
          settings_hash: 'voiceover-only-settings'
        })
      });
    }
  );
  let exportPayload: Record<string, unknown> | null = null;
  await page.route(
    `**/api/v1/sessions/${session.id}/stages/export/run`,
    async (route) => {
      exportPayload = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          id: 'voiceover-only-export-job',
          kind: 'export.create',
          session_id: session.id,
          status: 'queued',
          progress: 0,
          created_at: new Date().toISOString()
        })
      });
    }
  );

  await page.goto(`/sessions/${session.id}/output`);
  await expect(page.getByText('This source has no soundtrack')).toBeVisible();
  await page.getByRole('button', { name: 'Create export' }).click();
  await expect(page.getByText(/Export voiceove was submitted/)).toBeVisible();

  expect(exportPayload).toEqual({
    output: {
      export_mode: 'media',
      audio_mode: 'dubbing_only'
    },
    generation_run_id: 'voiceover-only-run'
  });
});

test('Create export rebuilds a completed assembly when synchronization settings changed', async ({
  page
}) => {
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
        buffer: Buffer.from('media fixture')
      }
    }
  });
  expect(uploaded.ok()).toBeTruthy();

  let assemblyRequested = false;
  let assemblyPayload: Record<string, unknown> | null = null;
  const requests: string[] = [];
  await page.route(
    `**/api/v1/sessions/${session.id}/generation-runs`,
    async (route) => {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          items: [
            {
              id: 'completed-run',
              status: 'completed',
              label: 'Run 1: Test voice',
              assembly: {
                id: assemblyRequested ? 'fresh-assembly' : 'stale-assembly',
                status: 'completed',
                settings_hash: assemblyRequested ? 'fresh' : 'stale'
              }
            }
          ]
        })
      });
    }
  );
  await page.route(
    `**/api/v1/sessions/${session.id}/output-assemblies`,
    async (route) => {
      assemblyRequested = true;
      assemblyPayload = route.request().postDataJSON() as Record<
        string,
        unknown
      >;
      requests.push('assembly');
      await route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify({ id: 'fresh-assembly', status: 'queued' })
      });
    }
  );
  await page.route(
    `**/api/v1/sessions/${session.id}/settings/resolve`,
    async (route) => {
      requests.push('resolve');
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ value: {}, settings_hash: 'current-settings' })
      });
    }
  );
  await page.route(
    `**/api/v1/sessions/${session.id}/stages/export/run`,
    async (route) => {
      requests.push('export');
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          id: 'rebuilt-export-job',
          kind: 'export.create',
          session_id: session.id,
          status: 'queued',
          progress: 0,
          created_at: new Date().toISOString()
        })
      });
    }
  );

  await page.goto(`/sessions/${session.id}/output`);
  await expect(page.getByLabel('Audio version')).toHaveValue('completed-run');
  await page.getByLabel('Audio result').selectOption('mixed');
  await page.getByLabel('Maximum speed-up').fill('1.25');
  await page.getByRole('button', { name: 'Create export' }).click();
  await expect(page.getByText(/Export rebuilt- was submitted/)).toBeVisible({
    timeout: 10_000
  });

  expect(assemblyRequested).toBeTruthy();
  expect(assemblyPayload).toEqual({
    generation_run_id: 'completed-run',
    run_override: {
      output: {
        export_mode: 'media',
        audio_mode: 'mixed'
      }
    }
  });
  expect(requests).toEqual(['resolve', 'assembly', 'export']);
});
