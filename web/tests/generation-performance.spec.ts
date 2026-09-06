import { Buffer } from 'node:buffer';
import { readFileSync } from 'node:fs';
import { expect, test, type Page } from '@playwright/test';

const source = (path: string) =>
  readFileSync(new URL(`../src/${path}`, import.meta.url), 'utf8');

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

test('generation search loads the full corpus only on explicit request', async ({
  page
}) => {
  await signIn(page);
  const sessionId = await createAudiobookSession(page);
  const requests = await mockGenerationSegments(
    page,
    sessionId,
    () => 101,
    false
  );

  await page.goto(`/sessions/${sessionId}`);
  await page.getByRole('button', { name: 'Generation', exact: true }).click();
  const rows = page.locator('tbody tr[data-segment-id]');
  await expect(rows).toHaveCount(100);

  const pagedRequestsBeforeFocus = requests().filter(
    (cursor) => cursor > 0
  ).length;
  await page.getByLabel('Find in generation segments').focus();
  await page.waitForTimeout(200);
  expect(requests().filter((cursor) => cursor > 0)).toHaveLength(
    pagedRequestsBeforeFocus
  );
  await expect(rows).toHaveCount(100);
  await expect(
    page.getByText('Searching 100 loaded generation segments out of 101.')
  ).toBeVisible();

  await page.getByRole('button', { name: 'Load all 101 for search' }).click();
  await expect(rows).toHaveCount(101);
  await expect(
    page.getByRole('button', { name: 'Load all 101 for search' })
  ).toBeHidden();
  expect(requests()).toContain(100);
});

test('expensive preview work is cached or lifecycle-cancelled', () => {
  const pdfEditor = source('lib/PdfEditor.svelte');
  const waveform = source('lib/WaveformPeaks.svelte');
  const outputSettings = source('lib/OutputSettingsPanel.svelte');
  const outputPage = source('routes/sessions/[id]/output/+page.svelte');

  expect(pdfEditor).toContain('function compositeRenderedPages()');
  expect(pdfEditor).toContain(
    'renderedStackPages = [...renderedStackPages, offscreen]'
  );
  expect(waveform).toContain('onDestroy(() => controller?.abort())');
  expect(outputSettings).toContain('previewController?.abort()');
  expect(outputPage).toContain('onDestroy(() => assemblyController?.abort())');
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
    const rows = page.locator('tbody tr[data-segment-id]');
    await expect(rows).toHaveCount(Math.min(100, size));
    const initialRenderMs = performance.now() - startedAt;
    const requestsAfterInitialRender = requests().length;

    let explicitFullLoadMs = 0;
    if (size > 100) {
      const fullLoadStartedAt = performance.now();
      await page
        .getByRole('button', { name: `Load all ${size} for search` })
        .click();
      await expect(rows).toHaveCount(size);
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
