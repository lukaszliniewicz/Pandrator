import { expect, test, type Page } from '@playwright/test';

async function fixture(
  page: Page,
  options: { legacy?: boolean; conflict?: boolean } = {}
) {
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
      name: `Repair batches ${crypto.randomUUID()}`,
      workflow_kind: 'voiceover'
    }
  });
  expect(created.ok()).toBeTruthy();
  const sid = (await created.json()).id as string;
  const base = `/api/v1/sessions/${sid}`;
  let activeId = 'repaired-plan';
  let conflict = false;
  let undoBody: Record<string, unknown> | undefined;
  let undoRequests = 0;
  let detailRequests = 0;
  const revision = (id: string, number: number, summary: string) => ({
    id,
    entry_id: id,
    revision_number: number,
    parent_revision_id: null,
    summary,
    origin: 'automatic',
    segment_count: 2,
    active_segment_count: 2,
    reusable_segment_count: 2,
    stale_segment_count: 0,
    audio_reuse_checked: true,
    source_artifact_id: null,
    reviewed: false,
    compatible: true
  });
  const batch = () => ({
    id: 'repair-run',
    base_revision_id: 'original-plan',
    result_revision_id: 'repaired-plan',
    attempt_count: 50,
    applied_count: 49,
    rejected_count: 1,
    status: 'completed',
    can_undo: !options.legacy && !conflict && activeId === 'repaired-plan',
    undo_disabled_reason: options.legacy
      ? 'This older batch has no verified undo snapshot. Inspect or restore the original as a separate copy.'
      : conflict
        ? 'The repaired text or selected audio changed. Restore the original as a separate copy to preserve later work.'
        : null,
    expected_revision_id: 'repaired-plan',
    expected_state_hash: options.legacy ? null : 'a'.repeat(64)
  });
  const history = () => {
    const items = [
      {
        ...revision(
          'repaired-plan',
          50,
          'Automatic timing repair · 49 accepted / 50 attempted'
        ),
        entry_id: 'repair-batch:repair-run',
        repair_batch: batch()
      },
      revision('original-plan', 1, 'Automatic speech plan')
    ];
    if (activeId === 'undo-plan')
      items.unshift(
        revision(
          'undo-plan',
          52,
          'Undo automatic timing repairs'
        ) as (typeof items)[number]
      );
    return {
      items,
      active_revision_id: activeId,
      total: items.length,
      checkpoint_total: 52,
      next_before_revision_number: null
    };
  };
  await page.route(`**${base}/generation-plan/history?*`, (route) =>
    route.fulfill({ json: history() })
  );
  await page.route(`**${base}/generation-plan/status`, (route) =>
    route.fulfill({
      json: {
        ...history(),
        session_id: sid,
        session_revision: 1,
        selected_revision_id: activeId,
        latest_revision_id: activeId,
        content_signature: 'fixture',
        can_prepare: true,
        can_generate: true,
        current_input: null,
        blocked_reason: null,
        warning: null
      }
    })
  );
  await page.route(`**${base}/generation-runs`, (route) =>
    route.fulfill({ json: { items: [] } })
  );
  await page.route(`**${base}/generation-segments?*`, (route) => {
    const target =
      new URL(route.request().url()).searchParams.get('plan_revision_id') ??
      activeId;
    return route.fulfill({
      json: {
        items: [
          {
            id: `segment-${target}`,
            ordinal: 0,
            revision: 1,
            text: `Narration from ${target}.`,
            status: 'ready',
            node_kind: 'subtitle_cue',
            paragraph_break_after: false,
            marked: false,
            removed: false,
            source_segment_ids: [1],
            takes: [],
            optimized_text: null,
            speech_plan: {}
          }
        ],
        total: 1,
        next_cursor: null,
        plan_revision_id: target
      }
    });
  });
  await page.route(
    `**${base}/generation-plan/repair-batches/repair-run?*`,
    (route) => {
      detailRequests += 1;
      const before = Number(
        new URL(route.request().url()).searchParams.get(
          'before_revision_number'
        ) ?? 52
      );
      const numbers = Array.from({ length: 50 }, (_, n) => 51 - n)
        .filter((n) => n < before)
        .slice(0, 20);
      return route.fulfill({
        json: {
          repair_batch: batch(),
          active_revision_id: activeId,
          items: numbers.map((n) => ({
            id: `checkpoint-${n}`,
            revision_number: n,
            repair_status: n === 51 ? 'not_applied' : 'applied',
            repair_reason: n === 51 ? 'added_delay' : null,
            source_block_ordinal: n - 2
          })),
          next_before_revision_number:
            numbers.length === 20 ? numbers.at(-1) : null
        }
      });
    }
  );
  await page.route(
    `**${base}/generation-plan/repair-batches/repair-run/undo`,
    (route) => {
      undoRequests += 1;
      expect(route.request().headers()['content-type']).toContain(
        'application/json'
      );
      expect(route.request().headers()['idempotency-key']).toBeTruthy();
      undoBody = route.request().postDataJSON();
      if (options.conflict) {
        conflict = true;
        return route.fulfill({
          status: 409,
          json: {
            error: {
              code: 'revision_conflict',
              message:
                'The repaired text or selected audio changed. No automatic repairs were undone.'
            }
          }
        });
      }
      activeId = 'undo-plan';
      return route.fulfill({
        status: 201,
        json: {
          plan_revision_id: activeId,
          restored_from_revision_id: 'original-plan'
        }
      });
    }
  );
  await page.goto(`/sessions/${sid}`);
  await page.getByRole('button', { name: 'Generation', exact: true }).click();
  await page.getByRole('button', { name: 'Speech plans', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Versioned speech plans' });
  await expect(dialog).toBeVisible();
  return {
    dialog,
    getUndo: () => undoBody,
    getUndoCount: () => undoRequests,
    getDetailCount: () => detailRequests
  };
}

test('fifty attempts occupy one batch card; details are optional and bounded', async ({
  page
}, info) => {
  const { dialog, getDetailCount } = await fixture(page);
  const nav = dialog.getByRole('navigation', { name: 'Plan versions' });
  await expect(nav.getByRole('button')).toHaveCount(2);
  await expect(
    nav.getByRole('button', { name: /Automatic timing repair/ })
  ).toHaveCount(1);
  await expect(
    dialog.getByRole('list', { name: 'Repair attempts' })
  ).toHaveCount(0);
  expect(getDetailCount()).toBe(0);
  await dialog
    .getByRole('button', { name: 'View repair details', exact: true })
    .click();
  await expect(dialog.getByRole('listitem')).toHaveCount(20);
  await dialog.getByRole('button', { name: 'Load earlier attempts' }).click();
  await expect(dialog.getByRole('listitem')).toHaveCount(40);
  await dialog
    .getByRole('button', { name: 'Preview repair checkpoint 51' })
    .click();
  await expect(
    dialog.getByText('Narration from checkpoint-51.', { exact: true })
  ).toBeVisible();
  await dialog
    .getByRole('button', { name: 'Preview original', exact: true })
    .click();
  await expect(
    dialog.getByText('Narration from original-plan.', { exact: true })
  ).toBeVisible();
  await dialog
    .getByRole('button', { name: 'Hide repair details', exact: true })
    .click();
  await page.screenshot({ path: info.outputPath('repair-batch-desktop.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  const panel = dialog.getByRole('region', { name: 'Automatic repair batch' });
  await panel.scrollIntoViewIfNeeded();
  const box = await panel.boundingBox();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(391);
  await page.screenshot({ path: info.outputPath('repair-batch-mobile.png') });
});

test('Undo sends both guards once and selects the returned restore revision', async ({
  page
}) => {
  const { dialog, getUndo, getUndoCount } = await fixture(page);
  await dialog
    .getByRole('button', { name: 'Undo automatic repairs', exact: true })
    .click();
  await expect.poll(getUndoCount).toBe(1);
  expect(getUndo()).toEqual({
    expected_revision_id: 'repaired-plan',
    expected_state_hash: 'a'.repeat(64)
  });
  await expect(
    dialog.getByText('Narration from undo-plan.', { exact: true })
  ).toBeVisible();
  await expect(dialog.getByRole('navigation').getByRole('button')).toHaveCount(
    3
  );
});

test('a stale Undo reports the conflict without selecting another plan', async ({
  page
}) => {
  const { dialog, getUndoCount } = await fixture(page, { conflict: true });
  await dialog
    .getByRole('button', { name: 'Undo automatic repairs', exact: true })
    .click();
  await expect(dialog.getByRole('alert')).toContainText(
    'selected audio changed'
  );
  await expect(
    dialog.getByText('Narration from repaired-plan.', { exact: true })
  ).toBeVisible();
  await expect(
    dialog.getByRole('button', { name: 'Undo automatic repairs', exact: true })
  ).toBeDisabled();
  expect(getUndoCount()).toBe(1);
});

test('legacy batches explain unavailable Undo but permit original preview and explicit restoration', async ({
  page
}) => {
  const { dialog, getUndoCount } = await fixture(page, { legacy: true });
  await expect(
    dialog.getByRole('button', { name: 'Undo automatic repairs', exact: true })
  ).toBeDisabled();
  await expect(
    dialog.getByText(/older batch has no verified undo snapshot/)
  ).toBeVisible();
  await dialog
    .getByRole('button', { name: 'Preview original', exact: true })
    .click();
  await expect(
    dialog.getByRole('button', {
      name: 'Restore as a new active revision',
      exact: true
    })
  ).toBeEnabled();
  expect(getUndoCount()).toBe(0);
});
