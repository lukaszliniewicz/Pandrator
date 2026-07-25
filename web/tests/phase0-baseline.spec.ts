import { expect, test, type Page, type Request } from '@playwright/test';

declare global {
  interface Window {
    __pandratorEventSources?: Array<{
      emit: (type: string, payload: Record<string, unknown>) => void;
    }>;
    __emitPandratorEvent?: (type: string, payload: Record<string, unknown>) => number;
  }
}

type RequestSummary = {
  total: number;
  byPath: Record<string, number>;
};

const phase0BaselineEnabled = Boolean(
  (globalThis as typeof globalThis & {
    process?: { env?: Record<string, string | undefined> };
  }).process?.env?.PANDRATOR_PHASE0_BASELINE
);

function summarize(requests: Request[]): RequestSummary {
  const byPath: Record<string, number> = {};
  for (const request of requests) {
    const url = new URL(request.url());
    const path = `${request.method()} ${url.pathname}${url.search}`;
    byPath[path] = (byPath[path] ?? 0) + 1;
  }
  return { total: requests.length, byPath };
}

function difference(observed: RequestSummary, baseline: RequestSummary): RequestSummary {
  const paths = new Set([...Object.keys(observed.byPath), ...Object.keys(baseline.byPath)]);
  return {
    total: observed.total - baseline.total,
    byPath: Object.fromEntries(
      [...paths]
        .map((path) => [path, (observed.byPath[path] ?? 0) - (baseline.byPath[path] ?? 0)])
        .filter(([, count]) => count !== 0)
    )
  };
}

async function installControllableEventSource(page: Page) {
  await page.addInitScript(() => {
    class BaselineEventSource {
      private listeners = new Map<string, Array<(event: MessageEvent) => void>>();

      constructor(_url: string) {
        window.__pandratorEventSources = window.__pandratorEventSources ?? [];
        window.__pandratorEventSources.push(this);
      }

      addEventListener(type: string, listener: EventListenerOrEventListenerObject) {
        const callback = typeof listener === 'function'
          ? listener
          : (event: Event) => listener.handleEvent(event);
        const listeners = this.listeners.get(type) ?? [];
        listeners.push(callback as (event: MessageEvent) => void);
        this.listeners.set(type, listeners);
      }

      close() {}

      emit(type: string, payload: Record<string, unknown>) {
        const event = new MessageEvent(type, { data: JSON.stringify(payload) });
        for (const listener of this.listeners.get(type) ?? []) listener(event);
      }
    }

    Object.defineProperty(window, 'EventSource', {
      configurable: true,
      value: BaselineEventSource
    });
    window.__emitPandratorEvent = (type, payload) => {
      const sources = window.__pandratorEventSources ?? [];
      for (const source of sources) source.emit(type, payload);
      return sources.length;
    };
  });
}

async function signIn(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const closeTour = page.getByRole('button', { name: 'Close tour' });
  if (await closeTour.isVisible()) await closeTour.click();
}

test('records API request fan-out caused by one job event', async ({ page, browserName }) => {
  test.skip(
    !phase0BaselineEnabled,
    'Run through scripts/phase0_baseline.py --include-browser.'
  );
  test.skip(browserName !== 'chromium', 'One browser is sufficient for this diagnostic.');

  await installControllableEventSource(page);
  await signIn(page);

  const requests: Request[] = [];
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (
      request.method() === 'GET'
      && url.pathname.startsWith('/api/v1/')
      && url.pathname !== '/api/v1/events'
    ) {
      requests.push(request);
    }
  });

  await page.waitForTimeout(800);
  requests.length = 0;
  const globallyDelivered = await page.evaluate(() => (
    window.__emitPandratorEvent?.('job.progress', {
      job_id: 'phase0-global-event-job',
      job_kind: 'noop',
      progress: 0.5
    }) ?? 0
  ));
  expect(globallyDelivered).toBeGreaterThan(0);
  await page.waitForTimeout(800);
  const globalEventWindow = summarize(requests);
  requests.length = 0;

  const authStatus = await page.request.get('/api/v1/auth/status');
  const csrfToken = (await authStatus.json()).csrf_token;
  const created = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': csrfToken },
    data: {
      name: `Phase 0 event baseline ${crypto.randomUUID()}`,
      workflow_kind: 'audiobook'
    }
  });
  expect(created.ok()).toBeTruthy();
  const session = await created.json();
  await page.goto(`/sessions/${session.id}`);
  await expect(page.getByRole('heading', { name: session.name })).toBeVisible();

  requests.length = 0;
  await page.waitForTimeout(800);
  const ambient = summarize(requests);
  requests.length = 0;

  const delivered = await page.evaluate((sessionId) => (
    window.__emitPandratorEvent?.('job.progress', {
      job_id: 'phase0-event-job',
      job_kind: 'audiobook.generate_audio',
      session_id: sessionId,
      progress: 0.5
    }) ?? 0
  ), session.id);
  expect(delivered).toBeGreaterThan(0);

  await page.waitForTimeout(800);
  const eventWindow = summarize(requests);
  console.log(`PHASE0_EVENT_FANOUT=${JSON.stringify({
    windowMs: 800,
    globalEventWindow,
    sessionAmbient: ambient,
    sessionAmbientRequestsPerSecond: ambient.total / 0.8,
    sessionEventWindow: eventWindow,
    sessionEventDelta: difference(eventWindow, ambient),
    sessionEventRequestsPerSecond: eventWindow.total / 0.8
  })}`);
});
