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

async function createAudiobookSession(page: Page) {
  const authStatus = await page.request.get('/api/v1/auth/status');
  const csrfToken = (await authStatus.json()).csrf_token;
  const response = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': csrfToken },
    data: {
      name: `Generation performance ${crypto.randomUUID()}`,
      workflow_kind: 'audiobook'
    }
  });
  expect(response.ok()).toBeTruthy();
  return (await response.json()).id as string;
}

async function mockGenerationSegments(
  page: Page,
  sessionId: string,
  getTotal: () => number,
  withAudio: boolean
) {
  const requestedCursors: number[] = [];
  await page.route(
    `**/api/v1/sessions/${sessionId}/generation-segments?*`,
    async (route) => {
      const url = new URL(route.request().url());
      const total = getTotal();
      const cursor = Number(url.searchParams.get('cursor') ?? 0);
      requestedCursors.push(cursor);
      const limit = Number(url.searchParams.get('limit') ?? 100);
      const end = Math.min(total, cursor + limit);
      const items = Array.from({ length: end - cursor }, (_, offset) => {
        const ordinal = cursor + offset;
        return {
          id: `performance-segment-${ordinal}`,
          ordinal,
          node_kind: 'paragraph',
          paragraph_break_after: ordinal % 5 === 4,
          text: `Performance segment ${ordinal + 1}. The quick brown fox checks whether a large generation plan remains responsive.`,
          optimized_text: null,
          speech_plan: {},
          optimization_status: 'not_requested',
          optimization_reviewed: false,
          marked: false,
          removed: false,
          status: withAudio ? 'completed' : 'ready',
          revision: 1,
          takes: withAudio
            ? [
                {
                  id: `performance-take-${ordinal}`,
                  generation_run_id: null,
                  artifact_id: `performance-artifact-${ordinal}`,
                  kind: 'tts',
                  status: 'completed',
                  is_active: true,
                  revision: 1
                }
              ]
            : []
        };
      });
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          items,
          total,
          next_cursor: end < total ? end : null,
          plan_revision_id: 'performance-plan'
        })
      });
    }
  );
  return () => [...requestedCursors];
}

test('generation search queries the complete plan only after input', async ({
  page
}) => {
  await signIn(page);
  const sessionId = await createAudiobookSession(page);
  const csrf = (await (await page.request.get('/api/v1/auth/status')).json())
    .csrf_token;
  const plan = await page.request.post(
    `/api/v1/sessions/${sessionId}/generation-plan`,
    {
      headers: { 'X-CSRF-Token': csrf },
      data: {
        segments: Array.from({ length: 101 }, (_, index) => ({
          text: `Performance segment ${index + 1}.`
        }))
      }
    }
  );
  expect(plan.ok()).toBeTruthy();
  const requests: URL[] = [];
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (
      request.method() === 'GET' &&
      url.pathname.endsWith('/generation-segments')
    )
      requests.push(url);
  });
  await page.goto(`/sessions/${sessionId}`);
  await page.getByRole('button', { name: 'Generation', exact: true }).click();
  const table = page.getByTestId('generation-segment-table');
  await expect(table).toHaveAttribute('data-loaded-count', '100');
  await expect(table).toHaveAttribute('data-virtualized', 'true');
  await page.getByRole('button', { name: 'Search and replace' }).click();
  const find = page.getByLabel('Find in generation segments');
  await expect(find).toBeFocused();
  expect(requests.filter((url) => url.searchParams.has('q'))).toHaveLength(0);
  await find.fill('Performance segment 101.');
  await expect(
    page.getByText('1 matching segments across the complete plan · script text')
  ).toBeVisible();
  await expect(
    page.locator('textarea[data-generation-search-index]')
  ).toHaveValue('Performance segment 101.');
  expect(
    requests.some(
      (url) => url.searchParams.get('q') === 'Performance segment 101.'
    )
  ).toBeTruthy();
  expect(
    requests.some(
      (url) =>
        Number(url.searchParams.get('cursor')) > 0 && !url.searchParams.has('q')
    )
  ).toBeFalsy();
});

test('records generation drawer costs at representative corpus sizes', async ({
  page
}, testInfo) => {
  test.skip(
    process.env.PANDRATOR_UI_PERF !== '1',
    'Set PANDRATOR_UI_PERF=1 to run the opt-in UI performance probe.'
  );
  test.slow();
  await signIn(page);
  const sessionId = await createAudiobookSession(page);
  let total = 100;
  const requests = await mockGenerationSegments(
    page,
    sessionId,
    () => total,
    true
  );
  const measurements: Array<Record<string, number>> = [];

  for (const size of [100, 500, 1000]) {
    total = size;
    const requestsBeforeScenario = requests().length;
    const startedAt = performance.now();
    await page.goto(`/sessions/${sessionId}`);
    await page.getByRole('button', { name: 'Generation', exact: true }).click();
    const table = page.getByTestId('generation-segment-table');
    await expect(table).toHaveAttribute(
      'data-loaded-count',
      String(Math.min(100, size))
    );
    const initialRenderMs = performance.now() - startedAt;
    const requestsAfterInitialRender = requests().length;

    let explicitFullLoadMs = 0;
    if (size > 100) {
      const fullLoadStartedAt = performance.now();
      for (let loaded = 100; loaded < size; loaded += 100) {
        await page
          .getByRole('button', { name: 'Load more', exact: true })
          .click();
        await expect(table).toHaveAttribute(
          'data-loaded-count',
          String(Math.min(size, loaded + 100))
        );
      }
      explicitFullLoadMs = performance.now() - fullLoadStartedAt;
    }

    measurements.push({
      segments: size,
      initialRenderMs: Math.round(initialRenderMs),
      explicitFullLoadMs: Math.round(explicitFullLoadMs),
      initialSegmentRequests:
        requestsAfterInitialRender - requestsBeforeScenario,
      totalSegmentRequests: requests().length - requestsBeforeScenario,
      domElements: await page.locator('*').count(),
      audioElements: await page.locator('audio').count()
    });
  }

  await testInfo.attach('generation-drawer-performance.json', {
    body: Buffer.from(JSON.stringify(measurements, null, 2)),
    contentType: 'application/json'
  });
});
