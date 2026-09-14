import { expect, test, type Page } from '@playwright/test';
import {
  GenerationEditQueue,
  collectStaleSegmentIds
} from '../src/lib/generation-edit-queue';
import type {
  GenerationSegment,
  GenerationSegmentPage
} from '../src/lib/api-models';

function row(id: string, ordinal: number, status = 'stale'): GenerationSegment {
  return {
    id,
    ordinal,
    revision: 1,
    text: `Sentence ${ordinal + 1}.`,
    status,
    node_kind: 'paragraph',
    paragraph_break_after: false,
    source_segment_ids: [ordinal],
    optimized_text: null,
    speech_plan: {},
    optimization_status: 'not_requested',
    optimization_reviewed: false,
    marked: false,
    removed: false,
    takes: []
  } as GenerationSegment;
}

test('regenerate waits for a pending save and follows changed segment IDs', async () => {
  const queue = new GenerationEditQueue();
  let finish!: (value: { id: string }) => void;
  const saving = queue.save(
    'old',
    () =>
      new Promise<{ id: string }>((resolve) => {
        finish = resolve;
      })
  );
  let resolved = false;
  const ids = queue.settledIds(['old']).then((value) => {
    resolved = true;
    return value;
  });
  await Promise.resolve();
  expect(resolved).toBe(false);
  finish({ id: 'edited' });
  await saving;
  expect(await ids).toEqual(['edited']);
  await queue.save('edited', async () => ({ id: 'edited-again' }));
  expect(await queue.settledIds(['old', 'edited'])).toEqual(['edited-again']);
});

test('a failed save prevents the waiting regeneration', async () => {
  const queue = new GenerationEditQueue();
  let fail!: (reason: Error) => void;
  const saving = queue.save(
    'old',
    () =>
      new Promise<{ id: string }>((_resolve, reject) => {
        fail = reject;
      })
  );
  const saved = expect(saving).rejects.toThrow('conflict');
  const ready = expect(queue.settledIds(['old'])).rejects.toThrow('conflict');
  await Promise.resolve();
  fail(new Error('conflict'));
  await Promise.all([saved, ready]);
});

test('all-stale selection is paged, pinned, and never includes ready or removed rows', async () => {
  const queries: URLSearchParams[] = [];
  const ids = await collectStaleSegmentIds('plan-a', async (query) => {
    queries.push(query);
    return {
      plan_revision_id: 'plan-a',
      total: 3,
      items: query.has('cursor')
        ? [row('late', 300)]
        : [
            row('stale', 0),
            row('never-generated', 1, 'ready'),
            { ...row('removed', 2), removed: true }
          ],
      next_cursor: query.has('cursor') ? null : 250
    };
  });
  expect(ids).toEqual(['stale', 'late']);
  expect(queries).toHaveLength(2);
  for (const query of queries) {
    expect(query.get('status')).toBe('stale');
    expect(query.get('plan_revision_id')).toBe('plan-a');
    expect(query.has('q')).toBe(false);
  }
});

test('a changed plan or repeating cursor fails closed', async () => {
  await expect(
    collectStaleSegmentIds('a', async () => ({
      items: [],
      total: 0,
      next_cursor: null,
      plan_revision_id: 'b'
    }))
  ).rejects.toThrow('changed');
  await expect(
    collectStaleSegmentIds('a', async () => ({
      items: [],
      total: 0,
      next_cursor: 250,
      plan_revision_id: 'a'
    }))
  ).rejects.toThrow('paged safely');
});

async function setupDrawer(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const created = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': auth.csrf_token },
    data: {
      name: `Regeneration menu ${crypto.randomUUID()}`,
      workflow_kind: 'audiobook'
    }
  });
  expect(created.ok()).toBeTruthy();
  return (await created.json()).id as string;
}

test('drawer header regenerates all stale without a selection or full-plan regeneration', async ({
  page
}) => {
  const sessionId = await setupDrawer(page);
  let requestBody: Record<string, unknown> | undefined;
  await page.route(
    `**/api/v1/sessions/${sessionId}/generation-runs`,
    async (route) => {
      if (route.request().method() === 'POST') {
        requestBody = route.request().postDataJSON();
        await route.fulfill({
          contentType: 'application/json',
          status: 202,
          body: JSON.stringify({
            id: 'test-replacement',
            status: 'queued',
            operation: 'regenerate',
            plan_revision_id: 'plan-a',
            progress: 0,
            resume_source_on_completion: false
          })
        });
      } else
        await route.fulfill({
          contentType: 'application/json',
          body: JSON.stringify({ items: [] })
        });
    }
  );
  const staleQueries: string[] = [];
  await page.route(
    `**/api/v1/sessions/${sessionId}/generation-segments?*`,
    async (route) => {
      const query = new URL(route.request().url()).searchParams;
      let payload: GenerationSegmentPage;
      if (query.get('status') === 'stale') {
        staleQueries.push(query.toString());
        payload = {
          items: query.has('cursor') ? [row('late', 300)] : [row('stale', 0)],
          total: 2,
          next_cursor: query.has('cursor') ? null : 250,
          plan_revision_id: 'plan-a'
        };
      } else
        payload = {
          items: [row('stale', 0), row('never-generated', 1, 'ready')],
          total: 302,
          next_cursor: 2,
          plan_revision_id: 'plan-a'
        };
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify(payload)
      });
    }
  );
  await page.goto(`/sessions/${sessionId}`);
  await page.getByRole('button', { name: 'Generation', exact: true }).click();
  const options = page.getByRole('button', { name: 'Regeneration options' });
  await expect(options).toBeEnabled();
  await options.click();
  const allStale = page.getByRole('button', {
    name: 'Regenerate all stale',
    exact: true
  });
  await expect(allStale).toBeVisible();
  await page.screenshot({
    path: '../tmp/regeneration-all-stale-menu.png',
    fullPage: true
  });
  await allStale.click();
  await expect.poll(() => requestBody).toBeTruthy();
  expect(requestBody?.operation).toBe('regenerate');
  expect(requestBody?.segment_ids).toEqual(['stale', 'late']);
  expect(requestBody?.speech_plan_revision_id).toBe('plan-a');
  expect(requestBody?.stale_only).toBe(false);
  expect(staleQueries).toHaveLength(2);
});

test('finished save errors still block regeneration until corrected', async () => {
  const queue = new GenerationEditQueue();
  await expect(
    queue.save('old', async () => {
      throw new Error('save conflict');
    })
  ).rejects.toThrow('save conflict');
  await expect(queue.settledIds(['old'])).rejects.toThrow('save conflict');
  await queue.save('old', async () => ({ id: 'fixed' }));
  expect(await queue.settledIds(['old'])).toEqual(['fixed']);
});

test('serial saves follow all known rows across an editorial copy', async () => {
  const queue = new GenerationEditQueue();
  let finish!: () => void;
  const observed: string[] = [];
  const first = queue.save('a', async (id) => {
    observed.push(id);
    await new Promise<void>((resolve) => {
      finish = resolve;
    });
    queue.recordReplacements([
      ['a', 'new-a'],
      ['b', 'new-b']
    ]);
    return { id: 'new-a' };
  });
  const second = queue.save('b', async (id) => {
    observed.push(id);
    return { id };
  });
  await Promise.resolve();
  expect(observed).toEqual(['a']);
  finish();
  await Promise.all([first, second]);
  expect(observed).toEqual(['a', 'new-b']);
  expect(await queue.settledIds(['a', 'b'])).toEqual(['new-a', 'new-b']);
});
