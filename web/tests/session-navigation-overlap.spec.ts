import { readFileSync } from 'node:fs';
import { expect, test, type Page } from '@playwright/test';

// Regression tests for session A -> B navigation while requests overlap.
//
// SvelteKit reuses `routes/sessions/[id]/+layout.svelte` across [id]
// navigations, so SessionStore/WorkflowStore must retarget and ResourceState
// must discard late completions for the previous id. All session endpoints
// are mocked: these tests create no sessions and write nothing.
//
// The navigation test uses injected same-origin anchors (SvelteKit
// intercepts them into client-side navigations) rather than page.goto,
// which would be a full document load and would NOT exercise layout reuse.
// A performance.timeOrigin marker proves the document (and layout instance)
// survived the whole scenario.

const source = (path: string) =>
  readFileSync(new URL(`../src/${path}`, import.meta.url), 'utf8');

const now = () => new Date().toISOString();

function sessionRecord(id: string, name: string) {
  return {
    id,
    name,
    storage_key: id,
    workflow_kind: 'audiobook',
    source_language: 'en',
    target_language: null,
    workflow_preset: 'default',
    included_stages_json: [],
    status: 'draft',
    revision: 1,
    created_at: now(),
    updated_at: now()
  };
}

function outcomePlan() {
  return {
    revision: 1,
    value: { focus: 'custom', inputs: {} },
    pipeline: []
  };
}

function workflowSnapshot(id: string) {
  return {
    session_id: id,
    workflow_kind: 'audiobook',
    workflow_preset: 'default',
    revision: 1,
    stages: [],
    sources: []
  };
}

function settingsPayload() {
  return {
    revision: 1,
    effective: {},
    override: {},
    context: { source_profile: 'none' }
  };
}

async function signIn(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const closeTour = page.getByRole('button', { name: 'Close tour' });
  if (await closeTour.isVisible()) await closeTour.click();
}

test('layout TDZ guard: request counter precedes the initial reload', () => {
  const layout = source('routes/sessions/[id]/+layout.svelte');
  const declaration = layout.indexOf('let sourceProfileRequest');
  expect(declaration).toBeGreaterThanOrEqual(0);
  // loadSourceProfile() bumps the counter synchronously, so the declaration
  // must precede every call that can reach it during component init.
  expect(declaration).toBeLessThan(layout.indexOf('reload();'));
});

test('resource-state records invalidations that arrive mid-flight', () => {
  const state = source('lib/resource-state.svelte.ts');
  const markStale = state.slice(state.indexOf('markStale()'));
  // The pending flag must be set regardless of current status: an external
  // replace() (e.g. live-progress patching) can flip status back to ready
  // while a request is still pending, which would otherwise lose the event.
  expect(markStale).toContain('if (this.pending)');
  expect(markStale).not.toContain('} else if (this.pending)');
  // Orphaned completions (reset or forced reload) never touch state.
  expect(state).toContain('if (epoch !== this.epoch) return this.value;');
});

test('catalogue sharing is inflight-only with guarded cleanup', () => {
  const dedupe = source('lib/inflight-dedupe.ts');
  const cache = source('lib/tts-catalogue-cache.ts');
  // No TTL reuse anywhere: sequential callers always refetch.
  expect(cache).not.toContain('setTimeout');
  expect(cache).not.toMatch(/TTL|Date\.now/);
  expect(cache).not.toContain('clearTtsCatalogueCache');
  // Cleanup consumes both outcomes (no detached rejection) and only clears
  // the slot it still owns (late settlers cannot clobber newer requests).
  expect(dedupe).toContain('void request.then(cleanup, cleanup);');
  expect(dedupe).toContain('if (slots.get(key) === request) slots.delete(key);');
});

test('late responses for the previous session never overwrite the current one', async ({
  page
}) => {
  const pageErrors: Error[] = [];
  page.on('pageerror', (error) => pageErrors.push(error));
  await signIn(page);
  // Per-session artificial latency, mutated between phases.
  const delayMs: Record<string, number> = {
    'session-alpha': 0,
    'session-beta': 0
  };
  for (const id of ['session-alpha', 'session-beta'] as const) {
    const label = id === 'session-alpha' ? 'Session Alpha' : 'Session Beta';
    await page.route(`**/api/v1/sessions/${id}`, async (route) => {
      await new Promise((resolve) => setTimeout(resolve, delayMs[id] ?? 0));
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify(sessionRecord(id, label))
      });
    });
    await page.route(
      `**/api/v1/sessions/${id}/outcome-plan`,
      async (route) => {
        await new Promise((resolve) => setTimeout(resolve, delayMs[id] ?? 0));
        await route.fulfill({
          contentType: 'application/json',
          body: JSON.stringify(outcomePlan())
        });
      }
    );
    await page.route(`**/api/v1/sessions/${id}/workflow`, async (route) => {
      await new Promise((resolve) => setTimeout(resolve, delayMs[id] ?? 0));
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify(workflowSnapshot(id))
      });
    });
    await page.route(
      `**/api/v1/sessions/${id}/settings/**`,
      async (route) => {
        await route.fulfill({
          contentType: 'application/json',
          body: JSON.stringify(settingsPayload())
        });
      }
    );
  }

  const title = page.locator('.session-shell h1');

  // Phase 1: fully load Alpha so the layout (and its stores) is constructed
  // with the Alpha id. Client-side navigation to Beta must then reuse it.
  await page.goto('/sessions/session-alpha');
  await expect(title).toContainText('Session Alpha');
  const documentMarker = await page.evaluate(() => performance.timeOrigin);
  // Fixture links: SvelteKit intercepts same-origin anchor clicks. Pinned
  // above the fixed app sidebar so a real click reaches them.
  await page.evaluate(() => {
    document.body.insertAdjacentHTML(
      'beforeend',
      '<a href="/sessions/session-beta" data-testid="fixture-go-beta" style="position:fixed;top:8px;right:8px;z-index:200;background:#fff;padding:8px">beta</a>' +
        '<a href="/sessions/session-alpha" data-testid="fixture-go-alpha" style="position:fixed;top:48px;right:8px;z-index:200;background:#fff;padding:8px">alpha</a>'
    );
  });

  // Phase 2: Beta is slow; navigate back to fast Alpha before Beta resolves.
  // Beta's late session/outcome/workflow responses must be discarded, with
  // no document reload (layout reuse) and no page errors (TDZ guard).
  delayMs['session-beta'] = 600;
  delayMs['session-alpha'] = 0;
  await page.getByTestId('fixture-go-beta').click();
  await expect
    .poll(async () => page.url(), { timeout: 8000 })
    .toContain('/sessions/session-beta');
  await expect(title).toContainText('Session Beta');
  await page.getByTestId('fixture-go-alpha').click();
  await expect(title).toContainText('Session Alpha');
  await expect(page.getByRole('link', { name: 'Sources' })).toHaveAttribute(
    'href',
    '/sessions/session-alpha/sources'
  );

  // Let every delayed Beta response land, then re-assert: no flash of Beta,
  // the same document throughout, and a clean console.
  await page.waitForTimeout(900);
  await expect(title).toContainText('Session Alpha');
  await expect(title).not.toContainText('Session Beta');
  await expect(page.getByRole('link', { name: 'Sources' })).toHaveAttribute(
    'href',
    '/sessions/session-alpha/sources'
  );
  await expect
    .poll(async () => page.evaluate(() => performance.timeOrigin))
    .toBe(documentMarker);
  expect(pageErrors).toEqual([]);
});
