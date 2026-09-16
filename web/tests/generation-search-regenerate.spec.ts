import { expect, test } from '@playwright/test';

// Deterministic drawer regression for the edit-copy/regenerate race:
// a route-controlled PATCH returns new plan/segment IDs (R1 -> R2), the first
// plan GET after the save is a delayed stale R1 response, and the next
// generation-runs POST is captured. No real TTS runs; isolated fixture only.
test('tts save adopts the edit-copy revision before first regenerate', async ({
  page
}) => {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const closeTour = page.getByRole('button', { name: 'Close tour' });
  if (await closeTour.isVisible()) await closeTour.click();

  const csrf = (await (await page.request.get('/api/v1/auth/status')).json())
    .csrf_token as string;
  const headers = { 'X-CSRF-Token': csrf };
  const session = await (
    await page.request.post('/api/v1/sessions', {
      headers,
      data: {
        name: `Regen revision ${crypto.randomUUID()}`,
        workflow_kind: 'voiceover'
      }
    })
  ).json();
  const sessionId = session.id as string;
  await page.request.post(`/api/v1/sessions/${sessionId}/generation-plan`, {
    headers,
    data: {
      segments: [
        { text: 'Cueword opens the scene.' },
        { text: 'A plain second cue.' }
      ]
    }
  });
  const listed = await page.request.get(
    `/api/v1/sessions/${sessionId}/generation-segments?limit=250`
  );
  const realBody = await listed.json();
  const r1 = realBody.plan_revision_id as string;
  const r2 = 'plan-r2-edit-copy';
  const realItems = realBody.items as Array<{
    id: string;
    ordinal: number;
    revision: number;
    text: string;
    optimized_text: string | null;
  }>;
  const realA = realItems.find((item) => item.ordinal === 0)!;
  const realB = realItems.find((item) => item.ordinal === 1)!;
  expect(r1).toBeTruthy();
  expect(r1).not.toBe(r2);

  // Seed an effective spoken value on the real R1 segment (real API,
  // in-place edit on the untouched plan, before any mock is installed).
  const seeded = await page.request.patch(
    `/api/v1/sessions/${sessionId}/generation-segments`,
    {
      headers,
      data: {
        updates: [
          {
            id: realA.id,
            revision: realA.revision,
            changes: { optimized_text: 'Spokenword covers the scene.' }
          }
        ]
      }
    }
  );
  expect(seeded.ok()).toBeTruthy();

  const copyA = {
    ...realA,
    id: 'copy-seg-a',
    plan_revision_id: r2,
    revision: 1,
    optimized_text: 'VOICEWORD covers the scene.'
  };
  const copyB = {
    ...realB,
    id: 'copy-seg-b',
    plan_revision_id: r2,
    revision: 1
  };
  const copies = [copyA, copyB];

  const literalMatches = (text: string, query: string) => {
    const matches: Array<{ start: number; end: number }> = [];
    const hay = text.toLowerCase();
    const needle = query.toLowerCase();
    let from = 0;
    while (needle && from <= hay.length) {
      const at = hay.indexOf(needle, from);
      if (at < 0) break;
      matches.push({ start: at, end: at + needle.length });
      from = at + Math.max(1, needle.length);
    }
    return matches;
  };

  let patchSeen = false;
  // The drawer retries the authoritative reload once, so the first two
  // list GETs after the save answer with the delayed stale R1 payload.
  let staleListsRemaining = 2;
  await page.route(
    '**/api/v1/sessions/*/generation-segments*',
    async (route) => {
      const url = new URL(route.request().url());
      if (route.request().method() === 'PATCH') {
        patchSeen = true;
        const body = route.request().postDataJSON() as {
          updates: Array<{ id: string; changes: Record<string, unknown> }>;
        };
        const items = body.updates.map((update) => {
          const peer = update.id === realA.id ? copyA : copyB;
          const base = realItems.find((item) => item.id === update.id)!;
          return {
            ...base,
            ...peer,
            ...update.changes,
            id: peer.id,
            plan_revision_id: r2,
            previous_segment_id: update.id
          };
        });
        await route.fulfill({
          contentType: 'application/json',
          body: JSON.stringify({ items })
        });
        return;
      }
      if (!patchSeen) {
        await route.continue();
        return;
      }
      const query = url.searchParams.get('q') ?? '';
      const field = url.searchParams.get('text_field') ?? 'text';
      const effective = (item: {
        text: string;
        optimized_text: string | null;
      }) => (field === 'spoken' ? item.optimized_text || item.text : item.text);
      if (!url.searchParams.has('fields') && staleListsRemaining > 0) {
        // Delayed stale R1 response racing the authoritative post-save reload
        // (and its single retry).
        staleListsRemaining -= 1;
        await new Promise((resolve) => setTimeout(resolve, 700));
        await route.fulfill({
          contentType: 'application/json',
          body: JSON.stringify(realBody)
        });
        return;
      }
      const items = query
        ? copies
            .map((item) => ({
              ...item,
              search_matches: literalMatches(effective(item), query)
            }))
            .filter((item) => item.search_matches.length > 0)
        : copies.map((item) => ({ ...item, search_matches: [] }));
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          items,
          total: items.length,
          next_cursor: null,
          plan_revision_id: r2
        })
      });
    }
  );

  const captured: { body: unknown } = { body: null };
  await page.route('**/api/v1/sessions/*/generation-runs*', async (route) => {
    if (route.request().method() !== 'POST') {
      await route.continue();
      return;
    }
    captured.body = route.request().postDataJSON();
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        id: 'regen-run-1',
        session_id: sessionId,
        plan_revision_id: r2,
        sequence_number: 1,
        operation: 'regenerate',
        label: 'Captured regen',
        job_id: 'regen-job-1',
        status: 'queued',
        progress: 0
      })
    });
  });

  await page.goto(`/sessions/${sessionId}`);
  await page.getByRole('button', { name: 'Generation', exact: true }).click();
  await page.getByRole('button', { name: 'Search and replace' }).click();
  await page.getByRole('radio', { name: 'TTS text' }).click();

  const find = page.getByLabel('Find in generation segments');
  await find.fill('spokenword');
  await expect(page.getByText('1 / 1', { exact: true })).toBeVisible();
  await page.getByLabel('Replace in generation segments').fill('VOICEWORD');
  await page.getByRole('button', { name: 'Replace', exact: true }).click();

  // The stale R1 reload must not silently win: the drawer surfaces the skew.
  await expect(
    page.getByText('The speech plan changed during save.').first()
  ).toBeVisible();

  // Recover on the authoritative R2 revision, then regenerate first try.
  await page.getByRole('button', { name: 'Search and replace' }).click();
  await expect(
    page.locator('tbody tr[data-segment-id="copy-seg-a"]')
  ).toBeVisible();
  await page.locator('tbody tr[data-segment-id="copy-seg-a"]').click();
  await page.getByRole('button', { name: 'Regeneration options' }).click();
  await page.getByRole('button', { name: 'Regenerate selected (1)' }).click();
  await expect
    .poll(() => captured.body !== null, { timeout: 15000 })
    .toBe(true);
  const regenBody = captured.body as Record<string, unknown> | null;
  if (!regenBody) throw new Error('Regenerate POST was not captured.');
  expect(regenBody['speech_plan_revision_id']).toBe(r2);
  expect(regenBody['segment_ids']).toEqual(['copy-seg-a']);
  await expect(
    page.getByText('The speech plan changed', { exact: false })
  ).toHaveCount(0);
});
