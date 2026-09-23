import { expect, test, type Page } from '@playwright/test';

declare global {
  interface Window {
    __emitExportTestEvent?: (
      type: string,
      payload: Record<string, unknown>
    ) => number;
  }
}

async function installEvents(page: Page) {
  await page.addInitScript(() => {
    const sources = new Set<TestEventSource>();
    class TestEventSource extends EventTarget {
      onopen: ((event: Event) => void) | null = null;
      onerror: ((event: Event) => void) | null = null;
      private cursor = 0;
      constructor() {
        super();
        sources.add(this);
        queueMicrotask(() => this.onopen?.(new Event('open')));
      }
      close() {
        sources.delete(this);
      }
      emit(type: string, payload: Record<string, unknown>) {
        this.dispatchEvent(
          new MessageEvent(type, {
            data: JSON.stringify(payload),
            lastEventId: String(++this.cursor)
          })
        );
      }
    }
    Object.defineProperty(window, 'EventSource', {
      configurable: true,
      value: TestEventSource
    });
    window.__emitExportTestEvent = (type, payload) => {
      for (const source of sources) source.emit(type, payload);
      return sources.size;
    };
  });
}

async function sessionFixture(page: Page) {
  await installEvents(page);
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const closeTour = page.getByRole('button', { name: 'Close tour' });
  if (await closeTour.isVisible()) await closeTour.click();
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const response = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': auth.csrf_token },
    data: {
      name: `Export visibility ${crypto.randomUUID()}`,
      workflow_kind: 'voiceover'
    }
  });
  expect(response.ok()).toBeTruthy();
  return response.json();
}

for (const kind of ['export.create', 'export.variant']) {
  test(`${kind} appears from live events and keeps failure details after reload`, async ({
    page
  }) => {
    const session = await sessionFixture(page);
    const job: Record<string, unknown> = {
      id: 'live-job-export',
      kind,
      session_id: session.id,
      status: 'queued',
      progress: 0,
      created_at: new Date().toISOString()
    };
    let items: Record<string, unknown>[] = [];
    await page.route('**/api/v1/jobs?limit=500', (route) =>
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ items })
      })
    );
    await page.goto(`/sessions/${session.id}/output`);
    await expect(
      page.getByRole('button', { name: 'Create export' })
    ).toBeVisible();
    await expect(page.getByText('Export activity')).toHaveCount(0);
    // Keep HTTP snapshots consistent with the persisted state behind the event.
    Object.assign(job, {
      status: 'running',
      progress: 0.82,
      progress_detail: 'Muxing the selected soundtrack'
    });
    items = [job];
    // Progress must be processed directly, even when only jobs are invalidated.
    await page.evaluate(
      ({ sid, jobKind }) => {
        window.__emitExportTestEvent?.('job.progress', {
          job_id: 'live-job-export',
          job_kind: jobKind,
          session_id: sid,
          status: 'running',
          progress: 0.82,
          detail: 'Muxing the selected soundtrack',
          changed_entities: ['jobs']
        });
      },
      { sid: session.id, jobKind: kind }
    );
    await expect(page.getByText('Running export')).toBeVisible();
    await expect(
      page.getByText('Muxing the selected soundtrack')
    ).toBeVisible();
    await expect(
      page.getByRole('progressbar', { name: 'Export live-job progress' })
    ).toHaveAttribute('aria-valuenow', '82');
    Object.assign(job, {
      progress: 0.9,
      progress_detail: 'Attaching subtitle tracks'
    });
    await page.evaluate(
      ({ sid, jobKind }) => {
        window.__emitExportTestEvent?.('job.progress', {
          job_id: 'wrong-session-job',
          job_kind: jobKind,
          session_id: 'another-session',
          status: 'running',
          detail: 'Do not display other sessions',
          changed_entities: ['jobs']
        });
        window.__emitExportTestEvent?.('job.progress', {
          job_id: 'not-an-export-job',
          job_kind: 'generation.assemble',
          session_id: sid,
          status: 'running',
          detail: 'Do not display assemblies as exports',
          changed_entities: ['jobs']
        });
        window.__emitExportTestEvent?.('job.progress', {
          job_id: 'live-job-export',
          job_kind: jobKind,
          session_id: sid,
          status: 'running',
          progress: 0.9,
          detail: 'Attaching subtitle tracks',
          changed_entities: ['jobs']
        });
      },
      { sid: session.id, jobKind: kind }
    );
    await expect(page.getByText('Attaching subtitle tracks')).toBeVisible();
    await expect(page.getByText('Do not display other sessions')).toHaveCount(
      0
    );
    await expect(
      page.getByText('Do not display assemblies as exports')
    ).toHaveCount(0);
    Object.assign(job, {
      status: 'failed',
      progress: 0.9,
      error_message: 'The output drive is full.'
    });
    items = [job];
    await page.evaluate(
      ({ sid, jobKind }) => {
        window.__emitExportTestEvent?.('job.failed', {
          job_id: 'live-job-export',
          job_kind: jobKind,
          session_id: sid,
          status: 'failed',
          progress: 0.9,
          changed_entities: ['jobs', 'output']
        });
      },
      { sid: session.id, jobKind: kind }
    );
    await expect(page.getByText('Failed export')).toBeVisible();
    await expect(page.getByText('The output drive is full.')).toBeVisible();
    await page.reload();
    await expect(page.getByText('Failed export')).toBeVisible();
    await expect(page.getByText('The output drive is full.')).toBeVisible();
  });
}

test('durable export completion remains visible and exposes its finished video', async ({
  page
}) => {
  const session = await sessionFixture(page);
  const job: Record<string, unknown> = {
    id: 'finished-video-job',
    kind: 'export.variant',
    session_id: session.id,
    status: 'running',
    progress: 0.82,
    progress_detail: 'Using the selected mixed soundtrack',
    created_at: new Date().toISOString()
  };
  let artifacts: Record<string, unknown>[] = [];
  await page.route('**/api/v1/jobs?limit=500', (route) =>
    route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ items: [job] })
    })
  );
  await page.route('**/api/v1/artifacts?**', async (route) => {
    const query = new URL(route.request().url()).searchParams;
    if (query.get('session_id') !== session.id) {
      await route.continue();
      return;
    }
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ items: artifacts })
    });
  });
  await page.goto(`/sessions/${session.id}/output`);
  await expect(page.getByText('Running export')).toBeVisible();
  artifacts = [
    {
      id: 'finished-video',
      session_id: session.id,
      kind: 'export',
      role: 'export',
      state: 'current',
      relative_path: 'exports/finished-voiceover.mp4',
      mime_type: 'video/mp4',
      size_bytes: 12345,
      created_at: new Date().toISOString(),
      metadata_json: {}
    }
  ];
  Object.assign(job, { status: 'succeeded', progress: 1 });
  await page.evaluate((sid) => {
    window.__emitExportTestEvent?.('job.succeeded', {
      job_id: 'finished-video-job',
      job_kind: 'export.variant',
      session_id: sid,
      status: 'succeeded',
      progress: 1,
      changed_entities: ['jobs', 'output']
    });
  }, session.id);
  await expect(page.getByText(/^Completed export\s+finished$/)).toBeVisible();
  await expect(
    page.getByRole('link', { name: 'Download finished-voiceover.mp4' })
  ).toBeVisible();
  await page.reload();
  await expect(page.getByText(/^Completed export\s+finished$/)).toBeVisible();
  await expect(
    page.getByRole('link', { name: 'Download finished-voiceover.mp4' })
  ).toBeVisible();
});
