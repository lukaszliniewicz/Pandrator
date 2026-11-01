import { Buffer } from 'node:buffer';
import AxeBuilder from '@axe-core/playwright';
import { writeFile } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import { expect, test, type Page } from '@playwright/test';

async function setup(
  page: Page,
  workflowKind: 'audiobook' | 'voiceover' = 'audiobook'
) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const headers = { 'X-CSRF-Token': auth.csrf_token };
  const response = await page.request.post('/api/v1/sessions', {
    headers,
    data: {
      name: `Review panes ${crypto.randomUUID()}`,
      workflow_kind: workflowKind,
      interface_mode: 'advanced'
    }
  });
  expect(response.ok()).toBeTruthy();
  const session = await response.json();
  return { session, headers };
}

async function upload(
  page: Page,
  sessionId: string,
  headers: Record<string, string>,
  name: string,
  buffer: Buffer,
  mimeType = 'text/plain'
) {
  const response = await page.request.post('/api/v1/uploads', {
    headers,
    multipart: {
      session_id: sessionId,
      purpose: 'source',
      file: { name, buffer, mimeType }
    }
  });
  expect(response.ok(), await response.text()).toBeTruthy();
  return response.json();
}

test('artifact comparisons retain their direction and load state across panes, maximize and mobile focus', async ({
  page
}, info) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  const { session, headers } = await setup(page);
  const earlier = await upload(
    page,
    session.id,
    headers,
    'earlier.txt',
    Buffer.from('Alpha original.')
  );
  const later = await upload(
    page,
    session.id,
    headers,
    'later.txt',
    Buffer.from('Alpha revised.')
  );
  await page.route(
    `**/api/v1/artifacts/${earlier.artifact_id}/context`,
    (route) =>
      route.fulfill({
        json: {
          parents: [],
          children: [
            {
              id: later.artifact_id,
              role: 'source',
              kind: 'text',
              relative_path: 'later.txt',
              mime_type: 'text/plain'
            }
          ],
          usage: null
        }
      })
  );
  const requests: string[] = [];
  page.on('request', (request) => {
    if (/\/artifacts\/[^/]+\/content/.test(request.url()))
      requests.push(request.url());
  });
  await page.goto(`/sessions/${session.id}/sources`);
  const card = page
    .locator('article')
    .filter({ hasText: 'earlier.txt' })
    .first();
  await card.getByRole('button', { name: 'Preview', exact: true }).click();
  const preview = page.getByRole('dialog', {
    name: 'earlier.txt',
    exact: true
  });
  await expect(
    preview.getByText('Alpha original.', { exact: true })
  ).toBeVisible();
  await preview.getByRole('button', { name: /^Details & compare/ }).click();
  await preview
    .getByLabel('Compare with a related asset')
    .selectOption(later.artifact_id);
  await preview.getByRole('button', { name: 'Load comparison' }).click();
  await expect(
    preview.getByRole('heading', { name: 'Compared file · later.txt' })
  ).toBeVisible();
  await expect(
    preview.getByRole('heading', { name: 'Current file · earlier.txt' })
  ).toBeVisible();
  await expect(
    preview.getByText('Alpha revised.', { exact: true })
  ).toBeVisible();
  const accessibility = await new AxeBuilder({ page })
    .include('[data-review-workspace]')
    .analyze();
  expect(accessibility.violations).toEqual([]);
  const loaded = requests.length;
  await preview
    .getByRole('button', { name: 'Close details & compare', exact: true })
    .click();
  await preview.getByRole('button', { name: 'Maximize workspace' }).click();
  await preview.getByRole('button', { name: 'Restore workspace size' }).click();
  await expect(
    preview.getByText('Alpha revised.', { exact: true })
  ).toBeVisible();
  expect(requests.length).toBe(loaded);
  await page.screenshot({ path: info.outputPath('artifact-wide.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await preview.getByRole('button', { name: /^Details & compare/ }).click();
  const drawer = page.getByRole('dialog', {
    name: 'Details & compare',
    exact: true
  });
  await expect(drawer).toBeVisible();
  for (let i = 0; i < 12; i++) await page.keyboard.press('Tab');
  expect(
    await drawer.evaluate((node) => node.contains(document.activeElement))
  ).toBe(true);
  await page.screenshot({ path: info.outputPath('artifact-phone-drawer.png') });
  await page.keyboard.press('Escape');
  await expect(drawer).toBeHidden();
  await expect(preview).toBeVisible();
  await expect(
    preview.getByRole('button', { name: /^Details & compare/ })
  ).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(preview).toBeHidden();
  await page.route(
    `**/api/v1/artifacts/${earlier.artifact_id}/content*`,
    (route) =>
      route.fulfill({
        contentType: 'text/plain; charset=utf-8',
        body: '字幕の比較。'.repeat(180000)
      })
  );
  await card.getByRole('button', { name: 'Preview', exact: true }).click();
  await expect(preview.getByText(/Preview limited to 2 MiB/)).toBeVisible();
  expect(
    await preview.locator('[data-preview-content]').textContent()
  ).not.toContain('\uFFFD');
});

test('optimization review keeps draft and search state and protects failed and pending saves', async ({
  page
}, info) => {
  test.setTimeout(90_000);
  await page.setViewportSize({ width: 1280, height: 720 });
  const { session } = await setup(page);
  const id = `optimization-${session.id}`;
  const rows = Array.from({ length: 75 }, (_, i) => ({
    source_text: `Original item ${i + 1}`,
    text: `Optimized item ${i + 1}`
  }));
  await page.route(`**/api/v1/artifacts/${id}/content*`, (route) =>
    route.fulfill({ json: rows })
  );
  await page.route(`**/api/v1/sessions/${session.id}/workflow`, (route) =>
    route.fulfill({
      json: {
        session_id: session.id,
        workflow_kind: 'audiobook',
        workflow_preset: 'custom',
        revision: 1,
        sources: [],
        stages: [
          {
            number: 4,
            key: 'optimize_tts',
            title: 'Optimize narration',
            explanation: 'Review spoken text.',
            status: 'completed',
            executable: true,
            included: true,
            selected_artifact_id: id,
            artifact: {
              id,
              kind: 'json',
              role: 'tts_optimized',
              path: 'optimized.json',
              relative_path: 'optimized.json',
              state: 'current',
              metadata_json: {},
              parent_ids: [],
              version: 1
            }
          }
        ]
      }
    })
  );
  let fail = true;
  let release: (() => void) | undefined;
  let saves = 0;
  let submitted: { items: { index: number; text: string }[] } | undefined;
  await page.route(
    `**/api/v1/artifacts/${id}/optimization-review`,
    async (route) => {
      saves++;
      submitted = route.request().postDataJSON();
      if (fail)
        return route.fulfill({
          status: 503,
          json: {
            error: { message: 'Synthetic save failure', code: 'unavailable' }
          }
        });
      await new Promise<void>((resolve) => {
        release = resolve;
      });
      await route.fulfill({
        status: 201,
        json: { id: 'reviewed-child', role: 'tts_optimized' }
      });
    }
  );
  // Enter through the app so Back exercises its in-app navigation guard.
  await page.goto('/');
  await page.locator(`a[href="/sessions/${session.id}"]`).first().click();
  await page.getByTestId('stage-disclosure-toggle').click();
  await expect(
    page.getByRole('button', { name: /Preview latest: Speech-optimized text/ })
  ).toBeVisible();
  await page
    .getByRole('button', { name: /Preview latest: Speech-optimized text/ })
    .click();
  const review = page.getByRole('dialog', {
    name: 'Review narration optimization'
  });
  const first = review.getByLabel('Optimized narration item 1', {
    exact: true
  });
  await first.fill('Reviewed Japanese 字幕');
  await review.getByRole('button', { name: /^Find & compare/ }).click();
  const search = review.getByLabel('Find in optimized narration segments');
  await search.fill('75');
  await review
    .getByRole('button', { name: 'Close find & compare', exact: true })
    .click();
  await review.getByRole('button', { name: /^Find & compare/ }).click();
  await expect(search).toHaveValue('75');
  await review
    .getByRole('button', { name: 'Close find & compare', exact: true })
    .click();
  await expect(first).toHaveValue('Reviewed Japanese 字幕');
  await review
    .getByRole('button', { name: 'Close review', exact: true })
    .click();
  const warning = page.getByRole('alertdialog', { name: 'Save your changes?' });
  await warning.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(warning.getByRole('alert')).toContainText(
    'Synthetic save failure'
  );
  await warning.getByRole('button', { name: 'Keep editing' }).click();
  await expect(first).toHaveValue('Reviewed Japanese 字幕');
  const editorUrl = page.url();
  await page.goBack();
  await expect(warning).toBeVisible();
  await warning.getByRole('button', { name: 'Keep editing' }).click();
  await expect(page).toHaveURL(editorUrl);
  await expect(first).toHaveValue('Reviewed Japanese 字幕');
  await page.mouse.click(3, 3);
  await expect(warning).toBeVisible();
  await warning.getByRole('button', { name: 'Keep editing' }).click();
  await page.screenshot({ path: info.outputPath('optimization-draft.png') });
  fail = false;
  await review
    .getByRole('button', { name: 'Save revision', exact: true })
    .click();
  await expect.poll(() => typeof release).toBe('function');
  await expect(
    review.getByRole('button', { name: 'Close review', exact: true })
  ).toBeDisabled();
  await page.keyboard.press('Escape');
  await expect(review).toBeVisible();
  expect(saves).toBe(2);
  release!();
  await expect(review).toBeHidden();
  expect(submitted?.items).toHaveLength(75);
  expect(submitted?.items[0].text).toBe('Reviewed Japanese 字幕');
});

test('PDF tools load on demand and unapplied edits have save and discard protection', async ({
  page
}, info) => {
  const { session, headers } = await setup(page);
  const pdf = execFileSync('python', [
    '-c',
    'import pymupdf,sys; d=pymupdf.open(); p=d.new_page(width=400,height=500); p.insert_text((30,50),"Synthetic page"); sys.stdout.buffer.write(d.tobytes())'
  ]);
  await upload(page, session.id, headers, 'review.pdf', pdf, 'application/pdf');
  const workers: string[] = [];
  page.on('request', (request) => {
    if (request.url().includes('pdf.worker')) workers.push(request.url());
  });
  await page.goto(`/sessions/${session.id}/sources`);
  expect(workers).toEqual([]);
  await page.getByRole('button', { name: 'Preprocess PDF' }).click();
  const editor = page.getByRole('dialog', { name: 'review.pdf', exact: true });
  const canvas = editor.locator('canvas[data-pdf-ready]');
  await expect(canvas).toBeVisible();
  await expect(canvas).toHaveAttribute('data-pdf-ready', 'true');
  await expect
    .poll(() => canvas.evaluate((node) => (node as HTMLCanvasElement).width))
    .toBeGreaterThan(100);
  await expect
    .poll(
      () =>
        page.workers().filter((worker) => worker.url().includes('pdf.worker'))
          .length
    )
    .toBe(1);
  const box = (await canvas.boundingBox())!;
  await page.mouse.move(box.x + 30, box.y + 40);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width - 30, box.y + box.height - 40);
  await page.mouse.up();
  await expect(
    editor.getByRole('button', { name: 'Undo PDF edit' })
  ).toBeEnabled();
  await editor
    .getByRole('button', { name: 'Close PDF editor', exact: true })
    .click();
  const warning = page.getByRole('alertdialog', { name: 'Save your changes?' });
  await expect(warning).toBeVisible();
  await warning.getByRole('button', { name: 'Keep editing' }).click();
  await editor
    .getByRole('button', { name: 'Pages & tools', exact: true })
    .click();
  await expect(editor.getByText('Page and stack settings')).toBeVisible();
  await page.screenshot({ path: info.outputPath('pdf-tools.png') });
  await editor
    .getByRole('button', { name: 'Close pages & tools', exact: true })
    .click();
  await editor
    .getByRole('button', { name: 'Close PDF editor', exact: true })
    .click();
  await warning.getByRole('button', { name: 'Discard changes' }).click();
  await expect(editor).toBeHidden();
  expect(workers.length).toBe(1);
  await expect
    .poll(
      () =>
        page.workers().filter((worker) => worker.url().includes('pdf.worker'))
          .length
    )
    .toBe(0);
});

test('five thousand subtitle cues keep the editor tall and search state across tools and phone layout', async ({
  page
}, info) => {
  test.setTimeout(90_000);
  await page.setViewportSize({ width: 1280, height: 720 });
  const { session } = await setup(page, 'voiceover');
  const id = `captions-${session.id}`;
  const record = {
    artifact_id: id,
    role: 'transcription',
    stage: 'transcription',
    version: 1,
    revision: 1,
    revision_id: 'revision-cues',
    document_id: 'document-cues',
    language: 'ja',
    reviewed: true,
    segment_count: 5000,
    state: 'current',
    created_at: '2026-10-31T10:00:00Z'
  };
  const artifact = {
    id,
    session_id: session.id,
    kind: 'srt',
    role: 'transcription',
    state: 'current',
    relative_path: 'captions.srt'
  };
  await page.route(`**/api/v1/sessions/${session.id}/documents`, (route) =>
    route.fulfill({
      json: {
        items: [
          {
            id: record.document_id,
            stage: record.stage,
            language: 'ja',
            active_revision_id: record.revision_id,
            created_at: record.created_at,
            revisions: [
              {
                id: record.revision_id,
                revision_number: 1,
                reviewed: true,
                created_at: record.created_at,
                segment_count: 5000,
                duration_ms: 5000000,
                content_hash: id,
                artifact
              }
            ]
          }
        ]
      }
    })
  );
  await page.route(`**/api/v1/sessions/${session.id}/workflow`, (route) =>
    route.fulfill({
      json: {
        session_id: session.id,
        workflow_kind: 'voiceover',
        workflow_preset: 'voiceover',
        revision: 1,
        sources: [],
        stages: [
          {
            number: 1,
            key: 'transcribe',
            title: 'Transcribe',
            status: 'completed',
            included: true,
            executable: true,
            selected_artifact_id: id,
            artifact
          }
        ]
      }
    })
  );
  await page.route(
    `**/api/v1/sessions/${session.id}/subtitles/catalog`,
    (route) =>
      route.fulfill({ json: { session_id: session.id, items: [record] } })
  );
  await page.route(
    `**/api/v1/sessions/${session.id}/subtitle-evidence?*`,
    (route) => route.fulfill({ json: { items: [] } })
  );
  const segments = Array.from({ length: 5000 }, (_, ordinal) => ({
    id: `cue-${ordinal}`,
    ordinal,
    revision: 1,
    start_ms: ordinal * 1000,
    end_ms: ordinal * 1000 + 900,
    text: `字幕 ${ordinal + 1} の言葉を正確に保ちます。`,
    speaker: '',
    review_state: 'clear',
    edit_capabilities: {
      ownership: 'exact',
      text: true,
      display_timing: true,
      speaker: true,
      split: true,
      merge: true,
      delete: true
    }
  }));
  let loads = 0;
  await page.route(
    `**/api/v1/sessions/${session.id}/subtitles/review?*`,
    (route) => {
      loads++;
      return route.fulfill({
        json: {
          session_id: session.id,
          primary_artifact_id: id,
          columns: [
            {
              ...record,
              source_media_artifact_id: null,
              source_media_error: null,
              segments
            }
          ],
          rows: segments.map((segment) => ({
            root_id: segment.id,
            start_ms: segment.start_ms,
            end_ms: segment.end_ms,
            changed: false,
            cells: { [id]: [segment] }
          }))
        }
      });
    }
  );
  const start = Date.now();
  await page.goto(`/sessions/${session.id}/text?review=${id}`);
  const review = page.getByRole('dialog', { name: 'Compare and refine' });
  await expect(review.locator('tbody tr')).toHaveCount(50);
  const openedMs = Date.now() - start;
  const geometry = await review.evaluate((node) => ({
    frame: node.getBoundingClientRect().height,
    rows: node.querySelector('[data-review-rows]')!.getBoundingClientRect()
      .height
  }));
  expect(geometry.rows / geometry.frame).toBeGreaterThanOrEqual(0.7);
  await review.getByRole('button', { name: /^Tools/ }).click();
  await review.getByLabel('Find in transcription segments').fill('字幕 5000');
  await review.getByRole('button', { name: 'Next match', exact: true }).click();
  await expect(
    review.getByText('Page 100 of 100', { exact: true })
  ).toBeVisible();
  const last = review.locator('[data-subtitle-search-index="4999"]');
  await expect(last).toBeFocused();
  await expect(last).toBeInViewport({ ratio: 1 });
  await expect(review).not.toContainText('NaN');
  await review.getByRole('button', { name: /^Tools/ }).click();
  await expect(review.getByLabel('Find in transcription segments')).toHaveValue(
    '字幕 5000'
  );
  await review
    .getByRole('button', { name: 'Close tools', exact: true })
    .click();
  await expect(review.locator('tbody tr')).toHaveCount(50);
  expect(loads).toBe(1);
  await page.screenshot({ path: info.outputPath('subtitle-5000-wide.png') });
  await writeFile(
    info.outputPath('subtitle-workspace-geometry.json'),
    JSON.stringify(
      {
        ...geometry,
        openedMs,
        renderedRows: 50,
        totalCues: 5000,
        reviewRequests: loads
      },
      null,
      2
    )
  );
  await info.attach('subtitle-workspace-geometry.json', {
    body: JSON.stringify({
      ...geometry,
      openedMs,
      renderedRows: 50,
      totalCues: 5000,
      reviewRequests: loads
    }),
    contentType: 'application/json'
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await review.getByRole('button', { name: /^Tools/ }).click();
  const drawer = page.getByRole('dialog', { name: 'Tools', exact: true });
  await expect(drawer).toBeVisible();
  await expect(drawer.getByLabel('Find in transcription segments')).toHaveValue(
    '字幕 5000'
  );
  await page.keyboard.press('Escape');
  await expect(drawer).toBeHidden();
  await expect(review).toBeVisible();
  await expect(
    review.getByText('Page 100 of 100', { exact: true })
  ).toBeVisible();
});
