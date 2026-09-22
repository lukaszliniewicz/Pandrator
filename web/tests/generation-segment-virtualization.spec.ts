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
      name: `Segment virtualization ${crypto.randomUUID()}`,
      workflow_kind: 'audiobook'
    }
  });
  expect(response.ok()).toBeTruthy();
  return (await response.json()).id as string;
}

function silentWav() {
  const samples = 24000;
  const header = Buffer.alloc(44);
  header.write('RIFF', 0);
  header.writeUInt32LE(36 + samples * 2, 4);
  header.write('WAVE', 8);
  header.write('fmt ', 12);
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20);
  header.writeUInt16LE(1, 22);
  header.writeUInt32LE(8000, 24);
  header.writeUInt32LE(16000, 28);
  header.writeUInt16LE(2, 32);
  header.writeUInt16LE(16, 34);
  header.write('data', 36);
  header.writeUInt32LE(samples * 2, 40);
  return Buffer.concat([header, Buffer.alloc(samples * 2)]);
}

// Mixed heights: every fifth segment carries a long body so measured rows
// vary several-fold; the rest stay compact.
function segmentText(ordinal: number) {
  if (ordinal % 5 === 0) {
    return (
      `Virtualization segment ${ordinal + 1}. ` +
      'A long delivery paragraph exercises variable row measurement. '.repeat(12)
    );
  }
  return `Virtualization segment ${ordinal + 1}. Compact row.`;
}

async function mockLargeCorpus(page: Page, sessionId: string, total: number) {
  await page.route(
    `**/api/v1/sessions/${sessionId}/generation-segments?*`,
    async (route) => {
      const url = new URL(route.request().url());
      const cursor = Number(url.searchParams.get('cursor') ?? 0);
      const limit = Number(url.searchParams.get('limit') ?? 100);
      const end = Math.min(total, cursor + limit);
      const items = Array.from({ length: end - cursor }, (_, offset) => {
        const ordinal = cursor + offset;
        return {
          id: `virt-segment-${ordinal}`,
          ordinal,
          node_kind: 'paragraph',
          text: segmentText(ordinal),
          optimized_text: null,
          marked: false,
          removed: false,
          status: 'completed',
          revision: 1,
          created_at: new Date().toISOString(),
          takes: [
            {
              id: `virt-take-${ordinal}`,
              generation_run_id: null,
              artifact_id: `virt-artifact-${ordinal}`,
              kind: 'tts',
              status: 'completed',
              is_active: true,
              revision: 1
            }
          ]
        };
      });
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          items,
          total,
          next_cursor: end < total ? end : null,
          plan_revision_id: 'virt-plan'
        })
      });
    }
  );
  await page.route('**/api/v1/artifacts/virt-artifact-*/content', (route) =>
    route.fulfill({ contentType: 'audio/wav', body: silentWav() })
  );
}

function trackPageErrors(page: Page) {
  const errors: Error[] = [];
  page.on('pageerror', (error) => errors.push(error));
  return errors;
}

async function openGeneration(page: Page, sessionId: string) {
  await page.goto(`/sessions/${sessionId}`);
  await page.getByRole('button', { name: 'Generation', exact: true }).click();
  const table = page.getByTestId('generation-segment-table');
  await expect(table).toBeVisible();
  return table;
}

function segmentRows(page: Page) {
  return page.locator('tbody tr[data-segment-id]');
}

async function scrollHostTo(page: Page, scrollTop: number) {
  await page.evaluate((target: number) => {
    const table = document.querySelector(
      '[data-testid="generation-segment-table"]'
    ) as HTMLElement | null;
    let host: HTMLElement | null = table?.parentElement ?? null;
    while (host) {
      const overflowY = getComputedStyle(host).overflowY;
      if (overflowY === 'auto' || overflowY === 'scroll') break;
      host = host.parentElement;
    }
    if (host) host.scrollTop = target;
    else window.scrollTo(0, target);
  }, scrollTop);
}

async function scrollHostToBottom(page: Page) {
  await page.evaluate(() => {
    const table = document.querySelector(
      '[data-testid="generation-segment-table"]'
    ) as HTMLElement | null;
    let host: HTMLElement | null = table?.parentElement ?? null;
    while (host) {
      const overflowY = getComputedStyle(host).overflowY;
      if (overflowY === 'auto' || overflowY === 'scroll') break;
      host = host.parentElement;
    }
    if (host) host.scrollTop = host.scrollHeight;
    else window.scrollTo(0, document.body.scrollHeight);
  });
}

async function renderedIds(page: Page) {
  return segmentRows(page).evaluateAll((rows) =>
    rows.map((row) => row.getAttribute('data-segment-id') ?? '')
  );
}

async function loadAll(page: Page, total: number) {
  const table = page.getByTestId('generation-segment-table');
  for (let attempt = 0; attempt < 10; attempt += 1) {
    const loaded = Number(
      (await table.getAttribute('data-loaded-count')) ?? '0'
    );
    if (loaded >= total) return;
    await page.getByRole('button', { name: 'Load more' }).click();
    await expect(table).toHaveAttribute(
      'data-loaded-count',
      String(Math.min(total, loaded + 100))
    );
  }
  await expect(table).toHaveAttribute('data-loaded-count', String(total));
}

test('600 loaded rows render a bounded window with loaded counts', async ({
  page
}) => {
  const pageErrors = trackPageErrors(page);
  await signIn(page);
  const sessionId = await createAudiobookSession(page);
  await mockLargeCorpus(page, sessionId, 600);
  const table = await openGeneration(page, sessionId);

  await loadAll(page, 600);
  await expect(table).toHaveAttribute('data-loaded-count', '600');
  await expect(table).toHaveAttribute('data-virtualized', 'true');
  const rendered = await segmentRows(page).count();
  expect(rendered).toBeGreaterThan(0);
  expect(rendered).toBeLessThanOrEqual(60);
  await expect(table).toHaveAttribute('data-rendered-count', String(rendered));
  await expect(page.locator('audio')).toHaveCount(0);
  const domTotal = await page.locator('*').count();
  expect(domTotal).toBeLessThan(15000);

  // Visible accessible fallback: show-all mounts everything for
  // find-in-page, then virtualizes again without losing loaded rows.
  await page
    .getByRole('button', { name: /Show all 600 loaded rows/ })
    .click();
  await expect(table).toHaveAttribute('data-virtualized', 'false');
  await expect(segmentRows(page)).toHaveCount(600);
  await expect(table).toHaveAttribute('data-loaded-count', '600');
  await page.getByRole('button', { name: /Virtualize rows/ }).click();
  await expect(table).toHaveAttribute('data-virtualized', 'true');
  expect(await segmentRows(page).count()).toBeLessThanOrEqual(60);
  expect(pageErrors).toEqual([]);
});

test('scroll reaches the last row with correct boundary neighbors', async ({
  page
}) => {
  const pageErrors = trackPageErrors(page);
  await signIn(page);
  const sessionId = await createAudiobookSession(page);
  await mockLargeCorpus(page, sessionId, 600);
  const table = await openGeneration(page, sessionId);
  await loadAll(page, 600);

  await scrollHostToBottom(page);
  const lastRow = page.locator('tbody tr[data-segment-id="virt-segment-599"]');
  await expect(lastRow).toBeAttached({ timeout: 8000 });
  await lastRow.scrollIntoViewIfNeeded();
  // Editable script text lives in the textarea value, not innerText.
  await expect(lastRow.locator('textarea')).toHaveValue(
    /Virtualization segment 600\./
  );
  // The boundary row travels with its right-hand segment and references
  // the original neighbors even though the left one scrolled out long ago.
  const boundary = lastRow.locator('xpath=preceding-sibling::tr[1]');
  await expect(boundary).toHaveClass(/boundary-row/);
  // The boundary trigger travels with its right-hand segment; the Join
  // action lives in its manual popover until opened.
  const trigger = boundary.getByRole('button', {
    name: /Boundary before segment 600/
  });
  await expect(trigger).toBeVisible();
  await trigger.click();
  await expect(
    boundary.getByRole('button', { name: /Join blocks 599–600/ })
  ).toBeVisible();
  expect(await segmentRows(page).count()).toBeLessThanOrEqual(60);

  // Mid-corpus with tall measured rows: no duplicates, window stays bounded.
  await scrollHostTo(page, 30000);
  await expect(
    page.locator('tbody tr[data-segment-id="virt-segment-0"]')
  ).toHaveCount(0);
  const ids = await renderedIds(page);
  expect(new Set(ids).size).toBe(ids.length);
  expect(ids.length).toBeLessThanOrEqual(60);

  // Host resize re-resolves the viewport instead of freezing the window.
  await page.setViewportSize({ width: 1280, height: 1400 });
  await expect
    .poll(async () => segmentRows(page).count(), { timeout: 8000 })
    .toBeLessThanOrEqual(80);
  const resizedIds = await renderedIds(page);
  expect(new Set(resizedIds).size).toBe(resizedIds.length);
  await expect(table).toHaveAttribute('data-loaded-count', '600');
  expect(pageErrors).toEqual([]);
});

test('editing, selection, and playback survive scrolling away', async ({
  page
}) => {
  const pageErrors = trackPageErrors(page);
  await signIn(page);
  const sessionId = await createAudiobookSession(page);
  await mockLargeCorpus(page, sessionId, 600);
  await openGeneration(page, sessionId);
  await loadAll(page, 600);

  // Load-more leaves the host scrolled at the bottom: reveal the top rows
  // before focusing the editor in row 2.
  await scrollHostTo(page, 0);
  // Unsaved draft plus a live textarea selection in row 2.
  const editor = page.getByLabel('Script text for segment 3');
  await expect(editor).toBeVisible({ timeout: 8000 });
  await editor.click();
  await editor.pressSequentially(' — draft edit');
  await page.evaluate(() => {
    const field = document.querySelector(
      'tr[data-segment-id="virt-segment-2"] textarea'
    ) as HTMLTextAreaElement | null;
    if (!field) throw new Error('editing row unmounted');
    field.focus();
    field.selectionStart = 0;
    field.selectionEnd = 14;
    document.dispatchEvent(new Event('selectionchange'));
  });

  // Scroll far: the focused/selected row stays mounted with draft intact.
  await scrollHostToBottom(page);
  const pinned = page.locator('tbody tr[data-segment-id="virt-segment-2"]');
  await expect(pinned).toBeAttached({ timeout: 8000 });
  const preserved = await pinned
    .locator('textarea')
    .evaluate((field: HTMLTextAreaElement) => ({
      value: field.value,
      start: field.selectionStart,
      end: field.selectionEnd
    }));
  expect(preserved.value).toContain('— draft edit');
  expect(preserved.start).toBe(0);
  expect(preserved.end).toBe(14);

  // Playback pins the preview row: one mounted player keeps its source.
  await scrollHostTo(page, 0);
  const rowPlay = page
    .locator('tbody tr[data-segment-id="virt-segment-4"]')
    .getByRole('button', { name: /Play audio for segment/ });
  await expect(rowPlay).toBeVisible({ timeout: 8000 });
  await rowPlay.click();
  await expect(page.locator('audio')).toHaveCount(1);
  await scrollHostToBottom(page);
  await expect(page.locator('audio')).toHaveCount(1);
  await expect
    .poll(async () =>
      page.evaluate(() => document.querySelector('audio')?.currentSrc ?? '')
    )
    .toContain('virt-artifact-4/content');
  expect(pageErrors).toEqual([]);
});

test('rapid keyboard navigation settles on the newest target', async ({
  page
}) => {
  const pageErrors = trackPageErrors(page);
  await signIn(page);
  const sessionId = await createAudiobookSession(page);
  await mockLargeCorpus(page, sessionId, 600);
  await openGeneration(page, sessionId);
  await loadAll(page, 600);

  // Overlapping async reveals: only the newest may move the viewport or
  // clear the pin, so a flood of ArrowDown must land exactly N rows ahead.
  await scrollHostTo(page, 0);
  const first = page.locator('tbody tr[data-segment-id="virt-segment-0"]');
  await expect(first).toBeAttached({ timeout: 8000 });
  await first.locator('td').nth(1).click();
  for (let press = 0; press < 25; press += 1) {
    await page.keyboard.press('ArrowDown');
  }
  const target = page.locator(
    'tbody tr[data-segment-id="virt-segment-25"].selected'
  );
  await expect(target).toBeAttached({ timeout: 10000 });
  await expect(target).toBeInViewport({ timeout: 10000 });
  // Script text lives in the textarea value, not innerText.
  await expect(target.locator('textarea')).toHaveValue(
    /Virtualization segment 26\./
  );
  expect(pageErrors).toEqual([]);
});
