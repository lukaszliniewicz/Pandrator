import { Buffer } from 'node:buffer';
import { expect, test, type Page } from '@playwright/test';
import type { GenerationRun } from '../src/lib/api-models';

async function fixture(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const closeTour = page.getByRole('button', { name: 'Close tour' });
  if (await closeTour.isVisible()) await closeTour.click();
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const headers = { 'X-CSRF-Token': auth.csrf_token };
  const response = await page.request.post('/api/v1/sessions', {
    headers,
    data: {
      name: `Repair history ${crypto.randomUUID()}`,
      workflow_kind: 'voiceover'
    }
  });
  expect(response.ok()).toBeTruthy();
  const session = await response.json();
  const endpoint = `/api/v1/sessions/${session.id}`;
  const base = {
    session_id: session.id,
    status: 'completed',
    operation: 'generate',
    progress: 1,
    created_at: new Date().toISOString()
  };
  const root: GenerationRun = {
    ...base,
    id: 'root',
    plan_revision_id: 'original-plan',
    sequence_number: 1,
    label: 'Run 1: Test voice',
    assembly: {
      id: 'original-assembly',
      status: 'completed',
      settings_hash: 'current-settings'
    } as GenerationRun['assembly'],
    timing_repair: {
      result_generation_run_id: 'repair-133',
      result_plan_revision_id: 'repair-plan-133',
      result_sequence_number: 134,
      applied_count: 133,
      attempt_count: 134,
      status: 'completed',
      versions: []
    }
  };
  const children: GenerationRun[] = Array.from({ length: 134 }, (_, index) => {
    const n = index + 1;
    return {
      ...base,
      id: `repair-${n}`,
      plan_revision_id: `repair-plan-${n}`,
      sequence_number: n + 1,
      label: `Run 1: Test voice · Timing repair ${n}`,
      early_repair_parent_run_id: root.id,
      assembly: {
        id: `assembly-${n}`,
        status: 'completed',
        settings_hash: 'current-settings'
      } as GenerationRun['assembly']
    };
  });
  root.timing_repair!.usage = {
    total_cost_usd: 1.3,
    commercial: true,
    estimated: false,
    has_unpriced_usage: false
  } as NonNullable<GenerationRun['usage']>;
  root.timing_repair!.versions = children.map((child, index) => ({
    generation_run_id: child.id,
    plan_revision_id: child.plan_revision_id,
    sequence_number: child.sequence_number,
    status: child.status,
    repair_status: index < 133 ? 'applied' : 'not_applied',
    repair_reason: index < 133 ? null : 'added_delay',
    source_block_ordinal: index
  }));
  await page.route(`**${endpoint}/generation-runs`, (route) =>
    route.fulfill({ json: { items: [...children].reverse().concat(root) } })
  );
  const segmentRequests: string[] = [];
  await page.route(`**${endpoint}/generation-segments?*`, (route) => {
    const runId =
      new URL(route.request().url()).searchParams.get('generation_run_id') ??
      'repair-133';
    segmentRequests.push(runId);
    return route.fulfill({
      json: {
        items: [
          {
            id: `segment-${runId}`,
            ordinal: 0,
            node_kind: 'speech_block',
            text: `Audio from ${runId}.`,
            speech_plan: {},
            marked: false,
            removed: false,
            status: 'completed',
            revision: 1,
            takes: [
              {
                id: `take-${runId}`,
                generation_run_id: runId,
                artifact_id: `audio-${runId}`,
                kind: 'tts',
                status: 'completed',
                is_active: true,
                revision: 1
              }
            ]
          }
        ],
        total: 1,
        next_cursor: null,
        plan_revision_id: runId === 'root' ? 'original-plan' : `plan-${runId}`
      }
    });
  });
  await page.route('**/api/v1/artifacts/audio-*/peaks*', (route) =>
    route.fulfill({ json: { peaks: [], duration: 1 } })
  );
  return { session, endpoint, headers, root, children, segmentRequests };
}

test('drawer groups repairs and selects final, original and intermediate audio', async ({
  page
}, testInfo) => {
  const { session, segmentRequests } = await fixture(page);
  await page.goto(`/sessions/${session.id}`);
  await page.getByRole('button', { name: 'Generation', exact: true }).click();
  const picker = page.getByRole('combobox', {
    name: 'Audio view',
    exact: true
  });
  await expect(picker.locator('option')).toHaveCount(2);
  await picker.selectOption('root');
  const row = page.locator('tbody tr[data-segment-id]');
  await expect(row.locator('audio')).toHaveAttribute(
    'src',
    '/api/v1/artifacts/audio-repair-133/content'
  );
  expect(segmentRequests.at(-1)).toBe('repair-133');
  await expect(page.getByText('$1.3000', { exact: true })).toBeVisible();
  const history = page.getByRole('region', { name: 'Timing repair history' });
  await expect(history).not.toBeVisible();
  await page
    .locator('summary')
    .filter({ hasText: 'Split / repair history' })
    .click();
  await expect(
    history.getByText('133 blocks split', { exact: false })
  ).toBeVisible();
  await history.getByRole('button', { name: 'View original' }).click();
  await expect(row.locator('audio')).toHaveAttribute(
    'src',
    '/api/v1/artifacts/audio-root/content'
  );
  await history.locator('summary').click();
  await expect(history.getByRole('listitem')).toHaveCount(134);
  await history
    .getByRole('button', { name: 'Inspect repair 1 audio', exact: true })
    .click();
  await expect(row.locator('audio')).toHaveAttribute(
    'src',
    '/api/v1/artifacts/audio-repair-1/content'
  );
  await history
    .getByRole('button', { name: 'Final audio', exact: true })
    .click();
  await expect(row.locator('audio')).toHaveAttribute(
    'src',
    '/api/v1/artifacts/audio-repair-133/content'
  );
  await page.screenshot({
    path: testInfo.outputPath('repair-history-desktop.png')
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(
    history.getByRole('button', { name: 'View original' })
  ).toBeVisible();
  const box = await history.boundingBox();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(391);
  await page.screenshot({
    path: testInfo.outputPath('repair-history-mobile.png')
  });
});

test('output defaults to accepted repair and can export the original', async ({
  page
}, testInfo) => {
  const { session, endpoint, headers } = await fixture(page);
  const uploaded = await page.request.post('/api/v1/uploads', {
    headers,
    multipart: {
      session_id: session.id,
      purpose: 'source',
      file: {
        name: 'source-video.mp4',
        mimeType: 'video/mp4',
        buffer: Buffer.from('media fixture')
      }
    }
  });
  expect(uploaded.ok()).toBeTruthy();
  await page.route(`**${endpoint}/settings/resolve`, (route) =>
    route.fulfill({ json: { value: {}, settings_hash: 'current-settings' } })
  );
  const exports: Record<string, unknown>[] = [];
  await page.route(`**${endpoint}/stages/export/run`, (route) => {
    exports.push(route.request().postDataJSON());
    return route.fulfill({
      json: {
        id: `export-${exports.length}`,
        kind: 'export.create',
        session_id: session.id,
        status: 'queued',
        progress: 0
      }
    });
  });
  await page.goto(`/sessions/${session.id}/output`);
  const picker = page.getByRole('combobox', {
    name: 'Audio version',
    exact: true
  });
  await expect(picker.locator('option')).toHaveCount(2);
  await expect(picker).toHaveValue('root');
  await page
    .getByRole('combobox', { name: 'Audio result', exact: true })
    .selectOption('mixed');
  await page
    .getByRole('button', { name: 'Create export', exact: true })
    .click();
  await expect.poll(() => exports.length).toBe(1);
  expect(exports[0].generation_run_id).toBe('repair-133');
  const history = page.getByRole('region', { name: 'Timing repair history' });
  await history.getByRole('button', { name: 'View original' }).click();
  await page
    .getByRole('button', { name: 'Create export', exact: true })
    .click();
  await expect.poll(() => exports.length).toBe(2);
  expect(exports[1].generation_run_id).toBe('root');
  await page.setViewportSize({ width: 390, height: 844 });
  await history.locator('summary').click();
  await history.scrollIntoViewIfNeeded();
  const box = await history.boundingBox();
  expect(box!.x + box!.width).toBeLessThanOrEqual(391);
  await page.screenshot({
    path: testInfo.outputPath('repair-output-mobile.png')
  });
});
