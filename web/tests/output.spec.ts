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

for (const kind of ['export.create', 'export.variant']) {
  test(`output retains ${kind} status and progress after reload`, async ({
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
              kind,
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
    await expect(
      page.getByRole('progressbar', { name: 'Export 12345678 progress' })
    ).toHaveAttribute('aria-valuenow', '42');
    await page.reload();
    await expect(page.getByText('Running export')).toBeVisible();
    await expect(
      page.getByText('Prepared subtitle track 1 of 2')
    ).toBeVisible();
    await expect(
      page.getByRole('progressbar', { name: 'Export 12345678 progress' })
    ).toHaveAttribute('aria-valuenow', '42');
  });
}

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

  // Hold an old artifact snapshot across deletion, as an in-flight refresh can.
  let releaseSnapshot!: () => void;
  let snapshotCaptured!: () => void;
  const snapshotGate = new Promise<void>((resolve) => {
    releaseSnapshot = resolve;
  });
  const captured = new Promise<void>((resolve) => {
    snapshotCaptured = resolve;
  });
  await page.route('**/api/v1/artifacts?**', async (route) => {
    const url = new URL(route.request().url());
    if (
      url.searchParams.get('session_id') !== session.id ||
      url.searchParams.get('limit') !== '300'
    ) {
      await route.fallback();
      return;
    }
    expect(url.searchParams.get('output_only')).toBe('true');
    const response = await route.fetch();
    snapshotCaptured();
    await snapshotGate;
    await route.fulfill({ response });
  });
  await page.route(
    `**/api/v1/sessions/${session.id}/stages/export/run`,
    (route) =>
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          id: 'refresh-test-job',
          kind: 'export.create',
          session_id: session.id,
          status: 'queued',
          progress: 0,
          created_at: new Date().toISOString()
        })
      })
  );
  const createExport = page.getByRole('button', {
    name: 'Create subtitle export'
  });
  try {
    await createExport.click();
    await captured;
    page.once('dialog', (dialog) => dialog.accept());
    await remove.click();
    await expect(page.getByText('Export removed.')).toBeVisible();
    await expect(
      page.getByRole('button', { name: /Remove export/ })
    ).toHaveCount(0);
  } finally {
    releaseSnapshot();
  }
  await expect(createExport).toBeEnabled();
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
  let assemblyCalls = 0;
  await page.route(
    `**/api/v1/sessions/${session.id}/output-assemblies`,
    async (route) => {
      assemblyCalls += 1;
      await route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify({ id: 'unexpected-assembly', status: 'queued' })
      });
    }
  );
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
  expect(assemblyCalls).toBe(0);
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
  let advancedAssemblyCalls = 0;
  let advancedExportCalls = 0;
  await page.route(
    `**/api/v1/sessions/${session.id}/output-assemblies`,
    async (route) => {
      advancedAssemblyCalls += 1;
      await route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify({ id: 'unexpected-assembly', status: 'queued' })
      });
    }
  );
  await page.route(
    `**/api/v1/sessions/${session.id}/stages/export/run`,
    async (route) => {
      advancedExportCalls += 1;
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
  expect(advancedAssemblyCalls).toBe(0);
  expect(advancedExportCalls).toBe(1);
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
              id: 'internal-regeneration-task',
              output_generation_run_id: 'selected-completed-run',
              status: 'completed',
              label: 'Run 1: Selected voice'
            },
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
  let selectedAssemblyCalls = 0;
  let selectedExportCalls = 0;
  await page.route(
    `**/api/v1/sessions/${session.id}/output-assemblies`,
    async (route) => {
      selectedAssemblyCalls += 1;
      await route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify({ id: 'unexpected-assembly', status: 'queued' })
      });
    }
  );
  await page.route(
    `**/api/v1/sessions/${session.id}/stages/export/run`,
    async (route) => {
      selectedExportCalls += 1;
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
  await expect(page.getByLabel('Audio version').locator('option')).toHaveCount(
    2
  );
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
  expect(selectedAssemblyCalls).toBe(0);
  expect(selectedExportCalls).toBe(1);
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
  let voiceoverAssemblyCalls = 0;
  let voiceoverExportCalls = 0;
  await page.route(
    `**/api/v1/sessions/${session.id}/output-assemblies`,
    async (route) => {
      voiceoverAssemblyCalls += 1;
      await route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify({ id: 'unexpected-assembly', status: 'queued' })
      });
    }
  );
  await page.route(
    `**/api/v1/sessions/${session.id}/stages/export/run`,
    async (route) => {
      voiceoverExportCalls += 1;
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
  expect(voiceoverAssemblyCalls).toBe(0);
  expect(voiceoverExportCalls).toBe(1);
});

test('Create export submits ONE durable export request with the pinned run', async ({
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

  let assemblyCalls = 0;
  let exportCalls = 0;
  let exportPayload: Record<string, unknown> | null = null;
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
                id: 'stale-assembly',
                status: 'completed',
                settings_hash: 'stale'
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
      assemblyCalls += 1;
      await route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify({ id: 'unexpected-assembly', status: 'queued' })
      });
    }
  );
  await page.route(
    `**/api/v1/sessions/${session.id}/stages/export/run`,
    async (route) => {
      exportCalls += 1;
      exportPayload = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          id: 'single-export-job',
          kind: 'export.variant',
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
  await expect(page.getByText(/Export single-e was submitted/)).toBeVisible({
    timeout: 10_000
  });

  // The export stage owns assembly server-side: exactly one export POST and
  // zero separate frontend assembly requests, with the pinned run attached.
  expect(exportCalls).toBe(1);
  expect(assemblyCalls).toBe(0);
  expect(exportPayload).toEqual({
    output: {
      export_mode: 'media',
      audio_mode: 'mixed'
    },
    generation_run_id: 'completed-run'
  });
});

test('Navigation after an accepted export job does not resubmit the export', async ({
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

  let assemblyCalls = 0;
  let exportCalls = 0;
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
                id: 'finished-assembly',
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
    `**/api/v1/sessions/${session.id}/output-assemblies`,
    async (route) => {
      assemblyCalls += 1;
      await route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify({ id: 'finished-assembly', status: 'queued' })
      });
    }
  );
  await page.route(
    `**/api/v1/sessions/${session.id}/stages/export/run`,
    async (route) => {
      exportCalls += 1;
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          id: 'durable-export-job',
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
  await page.getByRole('button', { name: 'Create export' }).click();
  await expect(page.getByText(/Export durable- was submitted/)).toBeVisible({
    timeout: 10_000
  });
  expect(exportCalls).toBe(1);

  // Leaving the page after the job was accepted must not repeat the request:
  // the durable server job continues on its own (mock contract here; the real
  // durable behavior is covered backend-side).
  await page.goto('/');
  await page.goto(`/sessions/${session.id}/output`);
  await expect(
    page.getByRole('button', { name: 'Create export' })
  ).toBeVisible();
  expect(exportCalls).toBe(1);
  expect(assemblyCalls).toBe(0);
});

test('Create export requires a completed audio version for a mixed media export', async ({
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

  let exportCalls = 0;
  let assemblyCalls = 0;
  await page.route(
    `**/api/v1/sessions/${session.id}/generation-runs`,
    async (route) => {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ items: [] })
      });
    }
  );
  await page.route(
    `**/api/v1/sessions/${session.id}/output-assemblies`,
    async (route) => {
      assemblyCalls += 1;
      await route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify({ id: 'unexpected-assembly', status: 'queued' })
      });
    }
  );
  await page.route(
    `**/api/v1/sessions/${session.id}/stages/export/run`,
    async (route) => {
      exportCalls += 1;
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          id: 'guard-export-job',
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
  await page.getByLabel('Audio result').selectOption('mixed');
  await page.getByRole('button', { name: 'Create export' }).click();
  await expect(
    page.getByText('Select a completed audio version for this media export.')
  ).toBeVisible({ timeout: 10_000 });

  expect(exportCalls).toBe(0);
  expect(assemblyCalls).toBe(0);
});

test('Frozen-tail limit is saved with the video output profile', async ({
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
              id: 'completed-run',
              status: 'completed',
              label: 'Run 1: Test voice',
              assembly: {
                id: 'finished-assembly',
                status: 'completed',
                settings_hash: 'tail-settings'
              }
            }
          ]
        })
      });
    }
  );
  await page.route(
    `**/api/v1/sessions/${session.id}/settings/resolve`,
    async (route) => {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ value: {}, settings_hash: 'tail-settings' })
      });
    }
  );
  let tailAssemblyCalls = 0;
  let tailExportCalls = 0;
  await page.route(
    `**/api/v1/sessions/${session.id}/output-assemblies`,
    async (route) => {
      tailAssemblyCalls += 1;
      await route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify({ id: 'unexpected-assembly', status: 'queued' })
      });
    }
  );
  await page.route(
    `**/api/v1/sessions/${session.id}/stages/export/run`,
    async (route) => {
      tailExportCalls += 1;
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          id: 'tail-export-job',
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
  await page.getByLabel('Audio result').selectOption('mixed');
  await page.getByText('Advanced video encoding').click();
  await page.getByLabel('Frozen-tail limit (ms)').fill('1500');
  await page.getByRole('button', { name: 'Create export' }).click();
  await expect(page.getByText(/Export tail-exp was submitted/)).toBeVisible({
    timeout: 10_000
  });

  const saved = await page.request.get(
    `/api/v1/sessions/${session.id}/settings/output`
  );
  const savedSettings = await saved.json();
  expect(savedSettings.override.video_tail_extension_max_ms).toBe(1500);
  expect(tailAssemblyCalls).toBe(0);
  expect(tailExportCalls).toBe(1);
});

test('Run 8 displays merged groups and preserves original/final audio choices', async ({
  page
}) => {
  await signIn(page);
  const { session } = await createSession(page, 'voiceover');
  const versions = Array.from({ length: 19 }, (_, index) => ({
    generation_run_id: `group-${index + 1}`,
    plan_revision_id: `plan-${index + 1}`,
    sequence_number: index + 2,
    status: 'completed',
    reason: 'passage_regroup',
    repair_status: index < 9 ? 'applied' : 'not_applied',
    repair_reason: index < 9 ? null : 'duration_misfit'
  }));
  await page.route(
    `**/api/v1/sessions/${session.id}/generation-runs`,
    (route) =>
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          items: [
            {
              id: 'run-8',
              session_id: session.id,
              plan_revision_id: 'original-plan',
              status: 'completed',
              progress: 1,
              operation: 'generate',
              sequence_number: 1,
              label: 'Run 8: German Voiceover',
              second_pass: 'regroup',
              timing_repair: {
                kind: 'regroup',
                result_generation_run_id: 'group-9',
                result_plan_revision_id: 'plan-9',
                result_sequence_number: 10,
                applied_count: 9,
                attempt_count: 19,
                status: 'completed',
                versions
              }
            },
            {
              id: 'group-9',
              session_id: session.id,
              plan_revision_id: 'plan-9',
              status: 'completed',
              progress: 1,
              operation: 'generate',
              sequence_number: 10,
              label: 'Accepted group 9',
              early_repair_parent_run_id: 'run-8'
            }
          ]
        })
      })
  );
  await page.goto(`/sessions/${session.id}/output`);
  const history = page.getByRole('region', { name: 'Passage regroup history' });
  await expect(history).toBeVisible();
  await expect(history.getByText(/9 groups merged/)).toBeVisible();
  await expect(
    history.getByText('19 groups regenerated: 9 accepted, 10 rejected.')
  ).toBeVisible();
  await expect(page.getByText(/blocks split/)).toHaveCount(0);
  await expect(
    history.getByRole('button', { name: 'Final audio', exact: true })
  ).toHaveAttribute('aria-pressed', 'true');
  await history.getByRole('button', { name: 'View original' }).click();
  await expect(
    history.getByRole('button', { name: 'View original' })
  ).toHaveAttribute('aria-pressed', 'true');
  await history.getByText(/Regroup details/).click();
  await expect(history.getByText(/Viewing: Original audio/)).toBeVisible();
  await expect(
    history.getByRole('button', { name: 'Inspect group 1 audio', exact: true })
  ).toBeVisible();
  await expect(history.getByText('Merge applied', { exact: true })).toHaveCount(
    9
  );
  await expect(
    history.getByText(/The regenerated group did not fit/)
  ).toHaveCount(10);
  await history
    .getByRole('button', { name: 'Final audio', exact: true })
    .click();
  await expect(
    history.getByRole('button', { name: 'Final audio', exact: true })
  ).toHaveAttribute('aria-pressed', 'true');
});
