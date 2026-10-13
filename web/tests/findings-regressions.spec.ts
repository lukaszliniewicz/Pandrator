import { expect, test, type Page } from '@playwright/test';

async function signIn(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const closeTour = page.getByRole('button', { name: 'Close tour' });
  if (await closeTour.isVisible()) await closeTour.click();
}

test('comparison opens selected captions across separate revision-1 documents and keeps editing separate from output selection', async ({
  page
}, testInfo) => {
  await signIn(page);
  const status = await (await page.request.get('/api/v1/auth/status')).json();
  const created = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': status.csrf_token },
    data: {
      name: `Caption selection regression ${crypto.randomUUID()}`,
      workflow_kind: 'voiceover',
      interface_mode: 'advanced'
    }
  });
  expect(created.ok()).toBeTruthy();
  const session = await created.json();
  const endpoint = `/api/v1/sessions/${session.id}`;
  let selected = 'translation-v2';
  const mutations: string[] = [];
  page.on('request', (request) => {
    if (
      new URL(request.url()).pathname.startsWith(`${endpoint}/`) &&
      !['GET', 'HEAD', 'OPTIONS'].includes(request.method())
    ) {
      mutations.push(`${request.method()} ${new URL(request.url()).pathname}`);
    }
  });
  const requests: string[][] = [];
  const records = [1, 2].map((version) => ({
    artifact_id: `translation-v${version}`,
    role: 'translation',
    stage: 'translation',
    version,
    revision: 1,
    revision_id: `revision-v${version}`,
    document_id: `document-v${version}`,
    language: 'en',
    reviewed: true,
    segment_count: 1,
    state: 'current',
    created_at: `2026-10-${11 + version}T10:00:00Z`
  }));
  const artifact = (id: string) => ({
    id,
    session_id: session.id,
    kind: 'srt',
    role: 'translation',
    state: 'current',
    relative_path: `${id}.srt`
  });
  await page.route(`**${endpoint}/documents`, async (route) => {
    await route.fulfill({
      json: {
        items: records.map((item) => ({
          id: item.document_id,
          stage: item.stage,
          language: item.language,
          active_revision_id: item.revision_id,
          created_at: item.created_at,
          revisions: [
            {
              id: item.revision_id,
              revision_number: 1,
              reviewed: true,
              created_at: item.created_at,
              segment_count: 1,
              duration_ms: 1000,
              content_hash: item.artifact_id,
              artifact: artifact(item.artifact_id)
            }
          ]
        }))
      }
    });
  });
  await page.route(`**${endpoint}/workflow`, async (route) => {
    await route.fulfill({
      json: {
        session_id: session.id,
        workflow_kind: 'voiceover',
        workflow_preset: 'voiceover',
        revision: 1,
        sources: [],
        stages: [
          {
            number: 3,
            key: 'translate',
            title: 'Translate',
            explanation: 'Written captions',
            status: 'completed',
            executable: true,
            included: true,
            selected_artifact_id: selected,
            artifact: {
              ...artifact(selected),
              version: 2,
              is_selected: true,
              parent_ids: []
            }
          }
        ]
      }
    });
  });
  await page.route(`**${endpoint}/subtitles/catalog`, (route) =>
    route.fulfill({ json: { session_id: session.id, items: records } })
  );
  await page.route(`**${endpoint}/subtitle-evidence?*`, (route) =>
    route.fulfill({ json: { session_id: session.id, items: [] } })
  );
  await page.route(`**${endpoint}/subtitles/review?*`, async (route) => {
    const ids = new URL(route.request().url()).searchParams.getAll(
      'artifact_id'
    );
    requests.push(ids);
    const segment = (id: string) => ({
      id: `${id}-segment`,
      ordinal: 1,
      revision: 1,
      start_ms: 1000,
      end_ms: 2000,
      text: id === 'translation-v2' ? 'Odra 1305' : 'Odra thirteen oh five',
      speaker: '',
      review_state: 'clear',
      edit_capabilities: {
        ownership: 'exact',
        text: true,
        display_timing: true,
        speaker: true,
        split: true,
        merge: false,
        delete: true
      }
    });
    await route.fulfill({
      json: {
        session_id: session.id,
        primary_artifact_id: ids[0],
        columns: ids.map((id) => ({
          ...records.find((item) => item.artifact_id === id),
          source_media_artifact_id: null,
          source_media_error: null,
          segments: [segment(id)]
        })),
        rows: [
          {
            start_ms: 1000,
            end_ms: 2000,
            cells: Object.fromEntries(ids.map((id) => [id, [segment(id)]]))
          }
        ]
      }
    });
  });
  await page.goto(`/sessions/${session.id}/text`);
  await page
    .getByRole('button', { name: 'Compare subtitles', exact: true })
    .click();
  const review = page.getByRole('dialog', { name: 'Compare and refine' });
  await expect(
    review.getByText('Editing translation v2 · en · Selected project captions')
  ).toBeVisible();
  await expect.poll(() => requests).toEqual([['translation-v2']]);
  await review.getByText('Find, filter & compare', { exact: true }).click();
  await review.getByLabel('Add a comparison').selectOption('translation-v1');
  await review.getByRole('button', { name: 'Add', exact: true }).click();
  await review
    .getByLabel('Subtitle artifact to edit')
    .selectOption('translation-v1');
  await expect(
    review.getByText(
      'Editing translation v1 · en · Different from selected project captions'
    )
  ).toBeVisible();
  expect(selected).toBe('translation-v2');
  expect(mutations).toEqual([]);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: testInfo.outputPath('caption-revision-context.png')
  });
  await review.getByRole('button', { name: 'Close subtitle review' }).click();
  selected = 'translation-v1';
  await page
    .getByRole('button', { name: 'Compare subtitles', exact: true })
    .click();
  await expect(
    review.getByText('Editing translation v1 · en · Selected project captions')
  ).toBeVisible();
  await review.getByRole('button', { name: 'Close subtitle review' }).click();
  selected = 'translation-v2';
  await page.goto(`/sessions/${session.id}/text?review=translation-v1`);
  await expect(
    review.getByText(
      'Editing translation v1 · en · Different from selected project captions'
    )
  ).toBeVisible();
  expect(mutations).toEqual([]);
});

test('background work distinguishes queue occupancy from healthy, stopped and unknown workers', async ({
  page
}, testInfo) => {
  const now = new Date().toISOString();
  const queuedJob = {
    id: 'queued-export',
    kind: 'export.variant',
    status: 'queued',
    progress: 0,
    created_at: now
  };
  const runningJob = {
    id: 'running-transcription',
    kind: 'transcription.quick',
    status: 'running',
    progress: 0.2,
    created_at: now
  };
  let jobs = [runningJob, queuedJob];
  let services: {
    id: string;
    process: { pid: number } | null;
    health: { state: string };
  }[] = [
    {
      id: 'pandrator.worker',
      process: { pid: 123 },
      health: { state: 'healthy' }
    }
  ];
  await page.route('**/api/v1/events/snapshot?*', async (route) => {
    const response = await route.fetch();
    await route.fulfill({
      response,
      json: { ...(await response.json()), jobs: { items: jobs } }
    });
  });
  await page.route('**/api/v1/jobs?*', (route) =>
    route.fulfill({ json: { items: jobs } })
  );
  await page.route('**/api/v1/manager/services', (route) =>
    route.fulfill({ json: { items: services } })
  );
  await signIn(page);
  const work = page.getByLabel('Background work status');
  await expect(work.getByText('1 running · 1 queued')).toBeVisible();
  await expect(work.getByText(/Running: Transcription/)).toBeVisible();
  await expect(work.getByText(/Worker is running/)).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await work.scrollIntoViewIfNeeded();
  await page.screenshot({
    path: testInfo.outputPath('background-work-busy.png')
  });
  services = [
    { id: 'pandrator.worker', process: null, health: { state: 'unknown' } }
  ];
  jobs = [queuedJob];
  await page.reload();
  await expect(work.getByText('0 running · 1 queued')).toBeVisible();
  await expect(work.getByText(/Worker is stopped/)).toBeVisible();
  services = [];
  await page.reload();
  await expect(work.getByText(/Worker health is unknown/)).toBeVisible();
  services = [
    {
      id: 'pandrator.worker',
      process: { pid: 123 },
      health: { state: 'healthy' }
    }
  ];
  jobs = [];
  await page.reload();
  await expect(work.getByText('0 running · 0 queued')).toBeVisible();
  await expect(work).toContainText('Job counts do not indicate free capacity');
});

test('provider retirement notice follows the selected model without migrating saved settings', async ({
  page
}, testInfo) => {
  await signIn(page);
  const csrf = (await (await page.request.get('/api/v1/auth/status')).json())
    .csrf_token;
  const created = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': csrf },
    data: {
      name: `Provider lifecycle regression ${crypto.randomUUID()}`,
      workflow_kind: 'voiceover',
      interface_mode: 'advanced'
    }
  });
  expect(created.ok()).toBeTruthy();
  const session = await created.json();
  const oldModel = 'gemini-3.1-flash-tts-preview';
  const currentModel = 'gemini-3.8-flash-tts';
  const catalog = await (
    await page.request.get('/api/v1/services/tts?view=compact')
  ).json();
  const service = catalog.services.find(
    (item: { id: string }) => item.id === 'gemini'
  );
  expect(service).toBeTruthy();
  service.available = true;
  service.online = true;
  service.credential_configured = true;
  service.models = [oldModel, currentModel];
  service.default_model = oldModel;
  service.voices = ['Kore'];
  service.default_voice = 'Kore';
  service.voice_catalogues = { [oldModel]: ['Kore'], [currentModel]: ['Kore'] };
  await page.route('**/api/v1/services/tts**', async (route) => {
    if (new URL(route.request().url()).pathname.endsWith('/gemini')) {
      const response = await route.fetch();
      const detail = await response.json();
      await route.fulfill({
        response,
        json: { ...detail, service: { ...detail.service, available: true } }
      });
    } else {
      await route.fulfill({
        json: { ...catalog, services: [service], default_service: 'gemini' }
      });
    }
  });
  const settingsUrl = `/api/v1/sessions/${session.id}/settings/tts`;
  const before = await (await page.request.get(settingsUrl)).json();
  const saved = await page.request.put(settingsUrl, {
    headers: { 'X-CSRF-Token': csrf, 'If-Match': `"${before.revision}"` },
    data: {
      value: {
        service: 'gemini',
        model: oldModel,
        voice: 'Kore',
        language: 'en'
      }
    }
  });
  expect(saved.ok()).toBeTruthy();
  await page.goto(`/sessions/${session.id}/voice`);
  const notice = page.getByLabel('Model lifecycle').first();
  await expect(notice).toContainText('17 November 2026');
  await expect(
    notice.getByRole('link', { name: 'Provider lifecycle information' })
  ).toHaveAttribute(
    'href',
    'https://ai.google.dev/gemini-api/docs/deprecations'
  );
  const readback = await (await page.request.get(settingsUrl)).json();
  expect(readback.override.model).toBe(oldModel);
  expect(readback.revision).toBe((await saved.json()).revision);
  await page.setViewportSize({ width: 390, height: 844 });
  await notice.scrollIntoViewIfNeeded();
  await page.screenshot({
    path: testInfo.outputPath('provider-retirement-notice.png')
  });
});
