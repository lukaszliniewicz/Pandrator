import { expect, test, type Locator, type Page } from '@playwright/test';

// The cast panel compares the saved cast with current recordings through
// POST .../generation-recast/preview. Opening the panel compares at once;
// later service, model or revision changes settle for 750 ms first, so a burst
// sends one comparison for its last state. These tests run on Playwright's
// fake clock: it flows normally until a test pauses it, and while paused no
// app timer fires until the test advances time. That makes "nothing before
// 750 ms" exact instead of a race against wall-clock time.

const SETTLE_MS = 750;
const COMPARING = 'Comparing the saved cast with current recordings…';

type Selector = {
  key: string;
  label: string;
  affected_count: number;
  changed_count: number;
  missing_count: number;
  unverified_count: number;
  overridden_count: number;
};

function recastPreview(selectors: Selector[] = []) {
  return {
    speech_plan_revision_id: 'e2e-plan',
    controls_revision: 1,
    selection_hash: 'e2e-selection',
    settings_summary: { service: '', model: '' },
    selectors,
    selected_keys: selectors.map((item) => item.key),
    generate_count: 0,
    keep_count: 0,
    missing_count: 0,
    shared_segment_count: 0,
    blocked_reason: null
  };
}

async function signIn(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.locator('.app-shell')).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
}

async function createAudiobookSession(page: Page) {
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const response = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': auth.csrf_token },
    data: {
      interface_mode: 'advanced',
      name: `Cast preview settle ${crypto.randomUUID()}`,
      workflow_kind: 'audiobook'
    }
  });
  expect(response.ok()).toBeTruthy();
  return (await response.json()).id as string;
}

// Count recast previews by origin and answer them locally. The panel sends an
// empty body; the recast dialog always names its selection options.
async function countPreviews(page: Page, selectors: Selector[] = []) {
  const sent = { panel: 0, dialog: 0 };
  await page.route('**/generation-recast/preview', async (route) => {
    const body = (route.request().postDataJSON() ?? {}) as object;
    if (Object.keys(body).length) sent.dialog++;
    else sent.panel++;
    await route.fulfill({ json: recastPreview(selectors) });
  });
  return sent;
}

// Serve the real cast with its revision raised by `bump.by`, so "Refresh cast"
// hands the panel a new revision without any server-side event.
async function bumpCastRevision(page: Page, id: string) {
  const bump = { by: 0 };
  await page.route(
    `**/api/v1/sessions/${id}/generation-controls{,?*}`,
    async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      const response = await route.fetch();
      const json = await response.json();
      await route.fulfill({
        response,
        json: { ...json, revision: json.revision + bump.by }
      });
    }
  );
  return bump;
}

async function openMultiVoiceCast(page: Page, id: string) {
  await page.goto(`/sessions/${id}`);
  const card = page.getByRole('region', { name: 'Voices', exact: true });
  await expect(card).toBeVisible();
  await card.getByRole('radio', { name: /Multiple voices/ }).check();
  await expect(card.getByTestId('cast-disclosure')).toHaveAttribute('open');
  await expect(card.getByTestId('cast-summary')).toContainText('0 characters');
  return card;
}

// Freeze app timers from here on; the tests advance them explicitly.
async function pauseClock(page: Page) {
  const now = await page.evaluate(() => Date.now());
  await page.clock.pauseAt(now + 1_000);
}

async function refreshCast(page: Page, card: Locator) {
  const refresh = card.getByRole('button', {
    name: 'Refresh cast',
    exact: true
  });
  const loaded = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname.endsWith('/generation-controls') &&
      response.request().method() === 'GET'
  );
  await refresh.click();
  await loaded;
  // The button is disabled from the click until the load has been applied.
  await expect(refresh).toBeEnabled();
}

function isOpen(card: Locator) {
  return card
    .getByTestId('cast-disclosure')
    .evaluate((node) => (node as HTMLDetailsElement).open);
}

test.beforeEach(async ({ page }) => {
  await page.clock.install();
  await signIn(page);
});

test('opening the cast compares once, at once', async ({ page }) => {
  const id = await createAudiobookSession(page);
  const sent = await countPreviews(page);
  const card = await openMultiVoiceCast(page, id);
  await expect.poll(() => sent.panel).toBe(1);
  await expect(card.getByText(COMPARING)).toHaveCount(0);

  // No trailing comparison follows the opening one.
  await pauseClock(page);
  await page.clock.runFor(SETTLE_MS * 3);
  expect(sent).toEqual({ panel: 1, dialog: 0 });
});

test('a burst of cast changes sends one comparison after it settles', async ({
  page
}) => {
  const id = await createAudiobookSession(page);
  const bump = await bumpCastRevision(page, id);
  const sent = await countPreviews(page);
  const card = await openMultiVoiceCast(page, id);
  await expect.poll(() => sent.panel).toBe(1);
  await expect(card.getByText(COMPARING)).toHaveCount(0);
  await pauseClock(page);

  // Three new revisions while no fake time passes: each restarts the wait.
  for (const by of [1, 2, 3]) {
    bump.by = by;
    await refreshCast(page, card);
    await expect(card.getByText(COMPARING)).toBeVisible();
  }
  expect(sent.panel).toBe(1);

  await page.clock.runFor(SETTLE_MS - 1);
  expect(sent.panel).toBe(1);
  await expect(card.getByText(COMPARING)).toBeVisible();

  await page.clock.runFor(1);
  await expect.poll(() => sent.panel).toBe(2);
  await expect(card.getByText(COMPARING)).toHaveCount(0);
  await page.clock.runFor(SETTLE_MS * 3);
  expect(sent).toEqual({ panel: 2, dialog: 0 });
});

test('collapsing the cast drops a pending comparison', async ({ page }) => {
  const id = await createAudiobookSession(page);
  const bump = await bumpCastRevision(page, id);
  const sent = await countPreviews(page);
  const card = await openMultiVoiceCast(page, id);
  await expect.poll(() => sent.panel).toBe(1);
  await expect(card.getByText(COMPARING)).toHaveCount(0);
  await pauseClock(page);

  bump.by = 1;
  await refreshCast(page, card);
  await expect(card.getByText(COMPARING)).toBeVisible();
  await card.getByTestId('cast-summary').click();
  await expect.poll(() => isOpen(card)).toBe(false);
  await page.clock.runFor(SETTLE_MS * 3);
  expect(sent.panel).toBe(1);

  // Opening again compares at once, without waiting for fake time.
  await card.getByTestId('cast-summary').click();
  await expect.poll(() => isOpen(card)).toBe(true);
  await expect.poll(() => sent.panel).toBe(2);
  await page.clock.runFor(SETTLE_MS * 3);
  expect(sent.panel).toBe(2);
});

test('a comparison due while the recast dialog is open waits for it to close', async ({
  page
}) => {
  const id = await createAudiobookSession(page);
  const sent = await countPreviews(page, [
    {
      key: 'narrator',
      label: 'Narrator',
      affected_count: 2,
      changed_count: 2,
      missing_count: 0,
      unverified_count: 0,
      overridden_count: 0
    }
  ]);
  const card = await openMultiVoiceCast(page, id);
  await expect.poll(() => sent.panel).toBe(1);
  await expect(card.getByText(COMPARING)).toHaveCount(0);
  await card.getByRole('button', { name: 'Add speaker', exact: true }).click();
  await card
    .locator('article')
    .first()
    .getByLabel('Speaker name', { exact: true })
    .fill('Alice');
  await expect(card.getByText('Checking voice setup…')).toHaveCount(0);
  await pauseClock(page);

  // Saving raises the revision and reports a flow change; both schedule a
  // panel comparison that must wait for the dialog's own preview to finish.
  await card
    .getByRole('button', {
      name: 'Save & regenerate changed voices…',
      exact: true
    })
    .click();
  const dialog = page.getByRole('dialog', {
    name: 'Regenerate selected voices'
  });
  await expect(dialog).toBeVisible();
  await expect.poll(() => sent.dialog).toBe(1);
  await page.clock.runFor(SETTLE_MS * 3);
  expect(sent.panel).toBe(1);

  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect.poll(() => sent.panel).toBe(2);
  await page.clock.runFor(SETTLE_MS * 3);
  expect(sent).toEqual({ panel: 2, dialog: 1 });
});
