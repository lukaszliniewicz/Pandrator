import { expect, test, type Page, type Request } from '@playwright/test';

declare global {
  interface Window {
    __pandratorEventSourceUrls?: string[];
    __pandratorEventSources?: Array<{
      emit: (type: string, payload: Record<string, unknown>) => void;
    }>;
    __emitPandratorEvent?: (type: string, payload: Record<string, unknown>) => number;
  }
}

async function installControllableEventSource(page: Page) {
  await page.addInitScript(() => {
    class Phase2EventSource {
      onopen: ((event: Event) => void) | null = null;
      onerror: ((event: Event) => void) | null = null;
      private listeners = new Map<string, Array<(event: MessageEvent) => void>>();
      private cursor = 0;

      constructor(url: string) {
        window.__pandratorEventSourceUrls = window.__pandratorEventSourceUrls ?? [];
        window.__pandratorEventSourceUrls.push(String(url));
        window.__pandratorEventSources = window.__pandratorEventSources ?? [];
        window.__pandratorEventSources.push(this);
        queueMicrotask(() => this.onopen?.(new Event('open')));
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
        this.cursor += 1;
        const event = new MessageEvent(type, {
          data: JSON.stringify(payload),
          lastEventId: String(this.cursor)
        });
        for (const listener of this.listeners.get(type) ?? []) listener(event);
      }
    }

    Object.defineProperty(window, 'EventSource', {
      configurable: true,
      value: Phase2EventSource
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

function countRequests(requests: Request[], predicate: (url: URL) => boolean) {
  return requests.filter((request) => (
    request.method() === 'GET' && predicate(new URL(request.url()))
  )).length;
}

test('event cursor and batched invalidation avoid progress fan-out', async ({ page, browserName }) => {
  test.skip(browserName !== 'chromium', 'One browser covers the event fan-out contract.');
  await installControllableEventSource(page);
  await signIn(page);

  await expect.poll(() => page.evaluate(
    () => window.__pandratorEventSourceUrls?.at(-1) ?? ''
  )).toContain('/api/v1/events?after=');
  const eventSourceUrl = await page.evaluate(
    () => window.__pandratorEventSourceUrls?.at(-1) ?? ''
  );
  expect(new URL(eventSourceUrl, 'http://127.0.0.1').searchParams.get('after')).toMatch(/^\d+$/);

  const requests: Request[] = [];
  page.on('request', (request) => requests.push(request));
  requests.length = 0;
  const globalDeliveryCount = await page.evaluate(() => (
    window.__emitPandratorEvent?.('job.progress', {
      job_id: 'phase2-global-progress',
      job_kind: 'noop',
      status: 'running',
      progress: 0.5,
      changed_entities: ['jobs']
    }) ?? 0
  ));
  expect(globalDeliveryCount).toBeGreaterThan(0);
  await page.waitForTimeout(400);
  expect(countRequests(requests, (url) => [
    '/api/v1/sessions',
    '/api/v1/jobs',
    '/api/v1/capabilities'
  ].includes(url.pathname))).toBe(0);

  const authStatus = await page.request.get('/api/v1/auth/status');
  const csrfToken = (await authStatus.json()).csrf_token;
  const created = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': csrfToken },
    data: {
      name: `Phase 2 event regression ${crypto.randomUUID()}`,
      workflow_kind: 'audiobook'
    }
  });
  expect(created.ok()).toBeTruthy();
  const session = await created.json();
  await page.goto(`/sessions/${session.id}`);
  await expect(page.getByRole('heading', { name: session.name })).toBeVisible();
  await page.waitForTimeout(800);
  requests.length = 0;

  await page.evaluate((sessionId) => {
    for (let index = 0; index < 5; index += 1) {
      window.__emitPandratorEvent?.('job.succeeded', {
        job_id: `phase2-burst-${index}`,
        job_kind: 'audiobook.generate_audio',
        session_id: sessionId,
        status: 'succeeded',
        progress: 1,
        generation_run_id: `phase2-run-${index}`,
        changed_entities: ['jobs', 'sessions', 'workflow', 'generation']
      });
    }
  }, session.id);
  await page.waitForTimeout(1200);

  const exactPath = (path: string) => countRequests(requests, (url) => url.pathname === path);
  expect(exactPath('/api/v1/jobs')).toBeLessThanOrEqual(1);
  expect(exactPath('/api/v1/sessions')).toBeLessThanOrEqual(1);
  expect(exactPath(`/api/v1/sessions/${session.id}/workflow`)).toBeLessThanOrEqual(1);
  expect(exactPath(`/api/v1/sessions/${session.id}/outcome-plan`)).toBeLessThanOrEqual(1);
  expect(exactPath(`/api/v1/sessions/${session.id}/generation-runs`)).toBeLessThanOrEqual(1);
  expect(exactPath(`/api/v1/sessions/${session.id}/output-assemblies/latest`)).toBeLessThanOrEqual(1);
  expect(exactPath(`/api/v1/sessions/${session.id}/generation-segments`)).toBeLessThanOrEqual(1);
  expect(exactPath('/api/v1/capabilities')).toBe(0);
});

test('four live event-stream tabs leave request capacity available', async ({ page, context, browserName }) => {
  test.skip(browserName !== 'chromium', 'One browser covers the multi-tab server-capacity contract.');
  await signIn(page);
  const tabs = [page];
  for (let index = 0; index < 3; index += 1) {
    const tab = await context.newPage();
    tabs.push(tab);
    await tab.goto('/');
    await expect(tab.getByRole('button', { name: 'Sign out' })).toBeVisible();
  }

  const responses = await Promise.all(
    Array.from({ length: 8 }, () => page.request.get('/api/v1/auth/status'))
  );
  expect(responses.every((response) => response.ok())).toBeTruthy();
  await Promise.all(tabs.slice(1).map((tab) => tab.close()));
});
