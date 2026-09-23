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
      name: `Audio preview ${crypto.randomUUID()}`,
      workflow_kind: 'audiobook'
    }
  });
  expect(response.ok()).toBeTruthy();
  return (await response.json()).id as string;
}

// Minimal valid WAV (8000 Hz mono 16-bit, 3 s of silence) so the mounted
// preview is still playing when the test observes it.
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

function trackPageErrors(page: Page) {
  const errors: Error[] = [];
  page.on('pageerror', (error) => errors.push(error));
  return errors;
}

async function revealSegment(page: Page, segmentId: string, ordinal: number) {
  // Virtualized rows outside the window are not mounted: scroll the drawer
  // host near the target estimate, wait for the row to mount, then settle
  // it into view before interacting.
  const row = page.locator(`tbody tr[data-segment-id="${segmentId}"]`);
  if (await row.count()) {
    await row.scrollIntoViewIfNeeded();
    return row;
  }
  await page.evaluate((targetOrdinal: number) => {
    const table = document.querySelector(
      '[data-testid="generation-segment-table"]'
    ) as HTMLElement | null;
    let host: HTMLElement | null = table?.parentElement ?? null;
    while (host) {
      const overflowY = getComputedStyle(host).overflowY;
      if (overflowY === 'auto' || overflowY === 'scroll') break;
      host = host.parentElement;
    }
    if (host) host.scrollTop = Math.max(0, targetOrdinal * 300 - 300);
  }, ordinal);
  await expect(row).toBeAttached({ timeout: 8000 });
  await row.scrollIntoViewIfNeeded();
  return row;
}

async function mockCompletedCorpus(
  page: Page,
  sessionId: string,
  total: number
) {
  // Stateful selection: production persists the chosen take server-side, so
  // the mocked select POST must be reflected by subsequent GETs. A stateless
  // mock that always returns take-a active would unfaithfully revert the mix
  // after every selectTake() reload.
  const activeTakeBySegment = new Map<number, string>();
  await page.route(
    `**/api/v1/sessions/${sessionId}/generation-segments?*`,
    async (route) => {
      const url = new URL(route.request().url());
      const cursor = Number(url.searchParams.get('cursor') ?? 0);
      const limit = Number(url.searchParams.get('limit') ?? 100);
      const end = Math.min(total, cursor + limit);
      const items = Array.from({ length: end - cursor }, (_, offset) => {
        const ordinal = cursor + offset;
        const activeTakeId =
          activeTakeBySegment.get(ordinal) ??
          (ordinal === 5
            ? `preview-take-${ordinal}-a`
            : `preview-take-${ordinal}`);
        const take = (id: string, artifact: string, created: string) => ({
          id,
          generation_run_id: null,
          artifact_id: artifact,
          kind: 'tts',
          status: 'completed',
          is_active: id === activeTakeId,
          revision: 1,
          created_at: created
        });
        return {
          id: `preview-segment-${ordinal}`,
          ordinal,
          node_kind: 'paragraph',
          text: `Preview segment ${ordinal + 1}. Every completed row offers audio without mounting hundreds of players.`,
          optimized_text: null,
          marked: false,
          removed: false,
          status: 'completed',
          revision: 1,
          created_at: new Date().toISOString(),
          takes:
            ordinal === 5
              ? [
                  take(
                    `preview-take-${ordinal}-a`,
                    `preview-artifact-${ordinal}a`,
                    '2026-01-01T00:00:00Z'
                  ),
                  take(
                    `preview-take-${ordinal}-b`,
                    `preview-artifact-${ordinal}b`,
                    '2026-01-02T00:00:00Z'
                  )
                ]
              : [
                  take(
                    `preview-take-${ordinal}`,
                    `preview-artifact-${ordinal}`,
                    new Date().toISOString()
                  )
                ]
        };
      });
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          items,
          total,
          next_cursor: end < total ? end : null,
          plan_revision_id: 'preview-plan'
        })
      });
    }
  );
  const wav = silentWav();
  await page.route('**/api/v1/artifacts/preview-artifact-*/content', (route) =>
    route.fulfill({ contentType: 'audio/wav', body: wav })
  );
  await page.route(
    '**/api/v1/generation-segments/*/takes/*/select',
    async (route) => {
      expect(route.request().method()).toBe('POST');
      // Persist like production: later GETs keep the selected take active.
      const match = route
        .request()
        .url()
        .match(/generation-segments\/([^/]+)\/takes\/([^/]+)\/select/);
      const ordinal = Number(match?.[1]?.split('-').pop());
      if (Number.isInteger(ordinal))
        activeTakeBySegment.set(ordinal, match?.[2] ?? '');
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ revision: 2 })
      });
    }
  );
}

test('per-row audio defers to one mounted player', () => {
  const table = source('lib/GenerationSegmentTable.svelte');
  const preview = source('lib/SegmentAudioPreview.svelte');
  // The table never mounts players itself and holds no per-row audio refs.
  expect(table).not.toContain('AudioPlayer');
  expect(table).not.toContain('bind:element');
  expect(table).toContain('SegmentAudioPreview');
  // The deferred preview mounts the single player only when active,
  // pauses the previous element via effect cleanup (the outer component
  // persists across row switches, so onDestroy alone would miss them),
  // keys the player by take, and moves focus to its transport on activation.
  expect(preview).toContain('{#if active}');
  expect(preview).toContain('bind:element={audioElement}');
  expect(preview).toContain('return () => current?.pause();');
  expect(preview).toContain('{#key take.artifact_id}');
  expect(preview).toContain("querySelector('button')");
  // Second-pass virtualization: bounded DOM with loaded-data semantics.
  expect(table).toContain('data-loaded-count={items.length}');
  expect(table).toContain('data-rendered-count={visibleIndexes.length}');
  expect(table).toContain('scrollToSegment');
  expect(table).toContain('generation-virtual-window');
});

test('hundreds of playable rows mount a single audio element on demand', async ({
  page
}) => {
  const pageErrors = trackPageErrors(page);
  await signIn(page);
  const sessionId = await createAudiobookSession(page);
  await mockCompletedCorpus(page, sessionId, 150);

  await page.goto(`/sessions/${sessionId}`);
  await page.getByRole('button', { name: 'Generation', exact: true }).click();
  // Loaded-data semantics: 100 rows loaded, only the viewport window
  // mounted. DOM row count no longer equals loaded rows.
  const table = page.getByTestId('generation-segment-table');
  await expect(table).toHaveAttribute('data-loaded-count', '100');
  await expect(table).toHaveAttribute('data-virtualized', 'true');
  const rows = page.locator('tbody tr[data-segment-id]');
  const rendered = await rows.count();
  expect(rendered).toBeGreaterThan(0);
  expect(rendered).toBeLessThan(100);
  await expect(table).toHaveAttribute('data-rendered-count', String(rendered));
  const previewButtons = page.getByRole('button', {
    name: /Play audio for segment/
  });
  await expect(previewButtons).toHaveCount(rendered);
  // No eager media elements despite playable rows.
  await expect(page.locator('audio')).toHaveCount(0);

  // Pointer: clicking a row's Play mounts and plays exactly that take.
  const rowPlay = async (index: number) => {
    const row = await revealSegment(page, `preview-segment-${index}`, index);
    return row.getByRole('button', { name: /Play audio for segment/ });
  };
  await (await rowPlay(2)).click();
  const players = page.locator('audio');
  await expect(players).toHaveCount(1);
  await expect
    .poll(async () =>
      page.evaluate(() => {
        const el = document.querySelector('audio');
        return el ? { src: el.currentSrc, paused: el.paused } : null;
      })
    )
    .toEqual({
      src: expect.stringContaining('preview-artifact-2/content'),
      paused: false
    });
  await expect(
    page.getByRole('button', { name: 'Pause', exact: true })
  ).toBeVisible();

  // Pointer on another row: the previous player unmounts, one remains,
  // and the retained previous element is paused (not left playing).
  const firstAudio = await page.evaluateHandle(() =>
    document.querySelector('audio')
  );
  await (await rowPlay(5)).click();
  await expect(players).toHaveCount(1);
  await expect
    .poll(async () =>
      page.evaluate(() => document.querySelector('audio')?.currentSrc ?? '')
    )
    .toContain('preview-artifact-5a/content');
  await expect
    .poll(async () =>
      firstAudio.evaluate(
        (el: Element | null) => (el as HTMLAudioElement | null)?.paused ?? true
      )
    )
    .toBe(true);

  // Wrong-take race: switching takes on the mounted row resets the player
  // to the new take; the old element stays paused and no second player
  // ever mounts.
  const row5 = await revealSegment(page, 'preview-segment-5', 5);
  const takeSelect = row5.locator('td').nth(3).getByRole('combobox');
  await takeSelect.selectOption('preview-take-5-b');
  await expect(takeSelect).toHaveValue('preview-take-5-b');
  await expect(players).toHaveCount(1);
  await expect
    .poll(async () =>
      page.evaluate(() => document.querySelector('audio')?.currentSrc ?? '')
    )
    .toContain('preview-artifact-5b/content');

  // Keyboard: activating another row's Play mounts that take and moves
  // focus to its Pause transport.
  await (await rowPlay(7)).focus();
  await page.keyboard.press('Enter');
  await expect(players).toHaveCount(1);
  await expect
    .poll(async () =>
      page.evaluate(() => document.querySelector('audio')?.currentSrc ?? '')
    )
    .toContain('preview-artifact-7/content');
  const focused = await page.evaluate(() => {
    const active = document.activeElement;
    return {
      label: active?.getAttribute('aria-label') ?? '',
      row:
        active
          ?.closest('tr[data-segment-id]')
          ?.getAttribute('data-segment-id') ?? ''
    };
  });
  expect(focused).toEqual({ label: 'Pause', row: 'preview-segment-7' });

  // Mounted player keeps native keyboard behavior: Enter pauses and Space
  // resumes without the drawer shortcut taking over or unmounting.
  await page.keyboard.press('Enter');
  await expect
    .poll(async () =>
      page.evaluate(() => {
        const el = document.querySelector('audio');
        return el
          ? {
              count: document.querySelectorAll('audio').length,
              src: el.currentSrc,
              paused: el.paused
            }
          : null;
      })
    )
    .toEqual({
      count: 1,
      src: expect.stringContaining('preview-artifact-7/content'),
      paused: true
    });
  await page.keyboard.press('Space');
  await expect
    .poll(async () =>
      page.evaluate(() => {
        const el = document.querySelector('audio');
        return el ? { paused: el.paused } : null;
      })
    )
    .toEqual({ paused: false });

  // Drawer shortcut: selecting another row, then Enter, plays via the
  // playlist controller and unmounts the row preview (no stacked audio).
  const row10 = await revealSegment(page, 'preview-segment-10', 10);
  await row10.locator('td').nth(1).click();
  await page.keyboard.press('Enter');
  await expect(players).toHaveCount(0);
  expect(pageErrors).toEqual([]);
});
