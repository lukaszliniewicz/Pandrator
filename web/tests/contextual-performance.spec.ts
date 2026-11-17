import { expect, test } from '@playwright/test';

test('contextual performance opens lazily, saves a draft and previews without synthesizing', async ({
  page
}, testInfo) => {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const csrf = (await (await page.request.get('/api/v1/auth/status')).json())
    .csrf_token;
  const created = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': csrf },
    data: {
      interface_mode: 'advanced',
      name: `Contextual performance ${crypto.randomUUID()}`,
      workflow_kind: 'audiobook'
    }
  });
  expect(created.ok()).toBeTruthy();
  const sessionId = (await created.json()).id as string;
  const revisionId = 'accepted-speech-revision';
  const speech = {
    id: revisionId,
    revision_number: 1,
    summary: 'Accepted text',
    origin: 'manual',
    reviewed: true,
    compatible: true,
    segment_count: 1,
    active_segment_count: 1,
    reusable_segment_count: 0,
    stale_segment_count: 1,
    source_artifact_id: null
  };
  await page.route(
    `**/api/v1/sessions/${sessionId}/generation-plan/status*`,
    (route) =>
      route.fulfill({
        json: {
          session_id: sessionId,
          session_revision: 1,
          items: [speech],
          selected_revision_id: revisionId,
          latest_revision_id: revisionId,
          content_signature: 'accepted-hash',
          can_prepare: false,
          can_generate: false,
          current_input: null,
          blocked_reason: null,
          warning: null
        }
      })
  );
  const capability = {
    model: 'fish_audio_s2_pro_q8_0',
    instructions: 'inline',
    dialect: 'fish_s2',
    status: 'documented',
    semantic_context: 'none'
  };
  let plan: Record<string, unknown> | null = null;
  let annotation: Record<string, unknown> | null = null;
  let version = 1;
  let reads = 0;
  let writes = 0;
  let synthesized = false;
  page.on('request', (request) => {
    if (request.url().includes('/audio/speech')) synthesized = true;
  });
  await page.route(
    `**/api/v1/sessions/${sessionId}/performance-plans**`,
    async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const suffix = url.pathname.split('/performance-plans')[1];
      if (request.method() === 'GET') {
        reads += 1;
        if (suffix) {
          await route.fulfill({
            json: {
              ...plan,
              version,
              analysed_count: annotation ? 1 : 0,
              steered_count: annotation ? 1 : 0,
              locked_count: annotation ? 1 : 0,
              items: [
                {
                  id: 'block-1',
                  ordinal: 0,
                  text: 'Yet this defeat was temporary.',
                  spoken_text: 'Yet this defeat was temporary.',
                  annotation
                }
              ]
            }
          });
        } else {
          await route.fulfill({
            json: {
              items: plan ? [{ ...plan, version }] : [],
              capabilities: capability
            }
          });
        }
        return;
      }
      const body = request.postDataJSON();
      expect(request.headers()['idempotency-key']).toBeTruthy();
      expect(request.headers()['x-csrf-token']).toBeTruthy();
      if (!suffix) {
        expect(body.expected_plan_revision_id).toBe(revisionId);
        expect(body.mode).toBe('manual');
        plan = {
          id: 'performance-1',
          plan_revision_id: revisionId,
          status: 'draft',
          version,
          total: 1,
          analysed_count: 0,
          steered_count: 0,
          locked_count: 0,
          stale: false,
          settings: { mode: 'manual' }
        };
        await route.fulfill({ status: 201, json: plan });
      } else if (request.method() === 'PATCH') {
        expect(body.expected_version).toBe(version);
        expect(body.items[0].segment_id).toBe('block-1');
        expect(body.items[0].annotation.delivery.instruction).toBe(
          'Introduce a restrained contrast.'
        );
        expect(body.items[0].annotation.locked).toBe(true);
        annotation = body.items[0].annotation;
        writes += 1;
        version += 1;
        await route.fulfill({
          json: { id: 'performance-1', version, updated: 1 }
        });
      } else if (suffix.endsWith('/preview')) {
        expect(body.segment_id).toBe('block-1');
        expect(body.annotation.delivery.instruction).toBe(
          'Introduce a restrained contrast.'
        );
        await route.fulfill({
          json: {
            capabilities: capability,
            transcript: 'Yet this defeat was temporary.',
            input:
              '[Introduce a restrained contrast.]Yet this defeat was temporary.',
            instructions: '',
            parts: [
              {
                text: 'Yet this defeat ',
                input: 'Yet this defeat ',
                instructions: 'Introduce a restrained contrast.'
              },
              {
                text: 'was temporary.',
                input: 'was temporary.',
                instructions: 'Become more assured.',
                request_options: { previous_text: 'Yet this defeat ' }
              }
            ],
            report: [
              {
                status: 'applied',
                control: 'direction',
                message: 'Compiled as an inline Fish direction.'
              }
            ]
          }
        });
      } else {
        throw new Error(
          `Unexpected performance request: ${request.method()} ${suffix}`
        );
      }
    }
  );
  await page.goto(`/sessions/${sessionId}`);
  const card = page.getByRole('region', { name: 'Speech plan', exact: true });
  await expect(card).toBeVisible();
  expect(reads).toBe(0);
  await card
    .getByRole('link', { name: 'Annotate & review', exact: true })
    .click();
  const panel = page.getByRole('region', {
    name: 'Annotation workspace',
    exact: true
  });
  await expect(panel.getByText('Model:', { exact: false })).toContainText(
    'fish_audio_s2_pro_q8_0'
  );
  await panel
    .getByRole('button', { name: 'Create manual draft', exact: true })
    .click();
  await expect(
    panel.getByText('Editable speech-direction draft created.', {
      exact: false
    })
  ).toBeVisible();
  await panel
    .getByLabel('Block delivery direction')
    .fill('Introduce a restrained contrast.');
  await panel
    .getByRole('button', { name: 'Save block annotations', exact: true })
    .click();
  await expect(
    panel.getByText('Block directions and speaker assignments saved', {
      exact: false
    })
  ).toBeVisible();
  expect(writes).toBe(1);
  await panel
    .getByRole('button', {
      name: 'Preview model request',
      exact: true
    })
    .click();
  await expect(
    panel.getByRole('region', { name: 'Compiled model request' })
  ).toBeVisible();
  await expect(
    panel.getByText('Compiled as an inline Fish direction.')
  ).toBeVisible();
  await panel.getByText('Exact provider input', { exact: true }).click();
  await expect(
    panel.getByText('Become more assured.', { exact: true })
  ).toBeVisible();
  await expect(
    panel.getByText('"previous_text"', { exact: false })
  ).toBeVisible();
  await panel
    .getByLabel('Block delivery direction')
    .fill('Unsaved direction stays here.');
  await page.setViewportSize({ width: 390, height: 844 });
  const drawer = panel.getByRole('dialog', {
    name: 'Blocks & filters',
    exact: true
  });
  await expect(drawer).toBeVisible();
  await drawer
    .getByRole('navigation', { name: 'Speech blocks' })
    .getByRole('button')
    .first()
    .click();
  await expect(drawer).toBeHidden();
  const warning = page.getByRole('dialog', {
    name: 'Save this block before leaving?',
    exact: true
  });
  await expect(warning).toBeVisible();
  await warning.getByRole('button', { name: 'Stay here', exact: true }).click();
  await expect(panel.getByLabel('Block delivery direction')).toHaveValue(
    'Unsaved direction stays here.'
  );
  await panel
    .getByRole('button', { name: 'Show blocks & filters', exact: true })
    .click();
  await expect(drawer).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(drawer).toBeHidden();
  await expect(panel.getByLabel('Block delivery direction')).toHaveValue(
    'Unsaved direction stays here.'
  );
  expect(writes).toBe(1);
  await page.screenshot({
    path: testInfo.outputPath('annotations-phone.png'),
    fullPage: true
  });
  expect(synthesized).toBe(false);
});

test('a lost or stale speaker plan can be copied, keeping unchanged blocks', async ({
  page
}, testInfo) => {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const csrf = (await (await page.request.get('/api/v1/auth/status')).json())
    .csrf_token;
  const created = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': csrf },
    data: {
      interface_mode: 'advanced',
      name: `Speaker plan continuity ${crypto.randomUUID()}`,
      workflow_kind: 'audiobook'
    }
  });
  expect(created.ok()).toBeTruthy();
  const sessionId = (await created.json()).id as string;
  const revisionId = 'split-speech-revision';
  await page.route(
    `**/api/v1/sessions/${sessionId}/generation-plan/status*`,
    (route) =>
      route.fulfill({
        json: {
          session_id: sessionId,
          session_revision: 1,
          items: [
            {
              id: revisionId,
              revision_number: 9,
              summary: 'Split block',
              origin: 'manual',
              reviewed: true,
              compatible: true,
              segment_count: 3,
              active_segment_count: 3,
              reusable_segment_count: 0,
              stale_segment_count: 3,
              source_artifact_id: null
            }
          ],
          selected_revision_id: revisionId,
          latest_revision_id: revisionId,
          content_signature: 'split-hash',
          can_prepare: false,
          can_generate: false,
          current_input: null,
          blocked_reason: null,
          warning: null
        }
      })
  );
  const units = ['First line.', '"Second line," said Mary.', 'Third line.'].map(
    (text, ordinal) => ({
      id: `block-${ordinal}`,
      ordinal,
      text,
      spoken_text: text,
      annotation: null
    })
  );
  const plan = (id: string, values: Record<string, unknown>) => ({
    id,
    plan_revision_id: revisionId,
    status: 'draft',
    version: 1,
    total: 3,
    analysed_count: 0,
    steered_count: 0,
    locked_count: 0,
    stale: false,
    settings: { purpose: 'speakers', annotation_format: 'xml', mode: 'manual' },
    ...values
  });
  let plans: Record<string, unknown>[] = [];
  let ancestor: Record<string, unknown> | null = {
    plan_id: 'plan-revision-7',
    plan_revision_id: 'revision-7',
    revision_number: 7,
    purpose: 'speakers',
    annotation_format: 'xml'
  };
  const posted: Record<string, unknown>[] = [];
  await page.route(
    `**/api/v1/sessions/${sessionId}/performance-plans**`,
    async (route) => {
      const request = route.request();
      const suffix = new URL(request.url()).pathname.split(
        '/performance-plans'
      )[1];
      if (request.method() === 'GET') {
        if (suffix) {
          const id = decodeURIComponent(suffix.slice(1));
          const found = plans.find((item) => item.id === id);
          await route.fulfill({ json: { ...found, items: units } });
        } else
          await route.fulfill({
            json: {
              items: plans,
              capabilities: {
                model: 'fish_audio_s2_pro_q8_0',
                instructions: 'inline',
                status: 'documented',
                semantic_context: 'none'
              },
              ancestor_adopted_plan: ancestor
            }
          });
        return;
      }
      const body = request.postDataJSON() as Record<string, unknown>;
      posted.push(body);
      const copy = plan(`copy-${posted.length}`, {
        analysed_count: 2,
        copied_from_id: body.copy_from_id,
        copied_count: 2,
        skipped_count: 1
      });
      plans = [copy, ...plans];
      ancestor = null;
      await route.fulfill({ status: 201, json: copy });
    }
  );
  await page.goto(`/sessions/${sessionId}/speech`);
  const panel = page.getByRole('region', {
    name: 'Annotation workspace',
    exact: true
  });
  const notice = panel.getByTestId('performance-ancestor-plan');
  await expect(notice).toContainText('revision 7 had one');
  await page.screenshot({
    path: testInfo.outputPath('ancestor-plan.png'),
    fullPage: true
  });
  await notice
    .getByRole('button', { name: 'Copy earlier plan', exact: true })
    .click();
  await expect(
    panel.getByText('Copied 2 of 3 blocks; 1 needs review.', { exact: false })
  ).toBeVisible();
  expect(posted[0]).toMatchObject({
    expected_plan_revision_id: revisionId,
    mode: 'manual',
    purpose: 'speakers',
    annotation_format: 'xml',
    copy_from_id: 'plan-revision-7'
  });
  expect(posted[0]).not.toHaveProperty('keep_unchanged');
  await expect(notice).toBeHidden();

  // The copy is adopted, then an edit on this revision makes it stale. A plan
  // read judges an adopted plan on the selected revision block by block.
  plans = [
    plan('adopted-stale', {
      status: 'adopted',
      stale: true,
      analysed_count: 3,
      changed_segment_count: 1,
      review_segment_count: 0,
      review_segments: []
    })
  ];
  await panel.getByRole('button', { name: 'Refresh', exact: true }).click();
  await panel.getByLabel('Annotation draft').selectOption('adopted-stale');
  const stale = panel.getByTestId('performance-stale-plan');
  await expect(stale).toContainText('1 block changed since adoption.');
  await expect(stale).toContainText('copy the plan to review it again');
  await page.screenshot({
    path: testInfo.outputPath('stale-plan.png'),
    fullPage: true
  });
  await stale
    .getByRole('button', { name: 'Copy and analyse the rest', exact: true })
    .click();
  await expect.poll(() => posted.length).toBe(2);
  expect(posted[1]).toMatchObject({
    mode: 'llm',
    purpose: 'speakers',
    annotation_format: 'xml',
    copy_from_id: 'adopted-stale',
    keep_unchanged: true
  });
});

test('an adopted speaker plan shows the blocks changed since adoption', async ({
  page
}) => {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const csrf = (await (await page.request.get('/api/v1/auth/status')).json())
    .csrf_token;
  const created = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': csrf },
    data: {
      interface_mode: 'advanced',
      name: `Adopted plan changes ${crypto.randomUUID()}`,
      workflow_kind: 'audiobook'
    }
  });
  expect(created.ok()).toBeTruthy();
  const sessionId = (await created.json()).id as string;
  const revisionId = 'edited-speech-revision';
  await page.route(
    `**/api/v1/sessions/${sessionId}/generation-plan/status*`,
    (route) =>
      route.fulfill({
        json: {
          session_id: sessionId,
          session_revision: 1,
          items: [
            {
              id: revisionId,
              revision_number: 1,
              summary: 'Edited text',
              origin: 'manual',
              reviewed: true,
              compatible: true,
              segment_count: 3,
              active_segment_count: 3,
              reusable_segment_count: 1,
              stale_segment_count: 2,
              source_artifact_id: null
            }
          ],
          selected_revision_id: revisionId,
          latest_revision_id: revisionId,
          content_signature: 'edited-hash',
          can_prepare: false,
          can_generate: false,
          current_input: null,
          blocked_reason: null,
          warning: null
        }
      })
  );
  // The plan read's per-block state; the second read is not judged (null).
  let counts: Record<string, unknown> = {
    changed_segment_count: 2,
    review_segment_count: 1,
    review_segments: [{ ordinal: 0, id: 'block-1' }]
  };
  const plan = () => ({
    id: 'performance-adopted',
    plan_revision_id: revisionId,
    status: 'adopted',
    version: 2,
    total: 3,
    analysed_count: 3,
    steered_count: 1,
    locked_count: 0,
    stale: true,
    settings: { mode: 'manual' },
    ...counts
  });
  await page.route(
    `**/api/v1/sessions/${sessionId}/performance-plans**`,
    async (route) => {
      const request = route.request();
      expect(request.method()).toBe('GET');
      const suffix = new URL(request.url()).pathname.split(
        '/performance-plans'
      )[1];
      await route.fulfill({
        json: suffix
          ? {
              ...plan(),
              items: [
                {
                  id: 'block-1',
                  ordinal: 0,
                  text: 'He said, “Bah,” then left.',
                  spoken_text: 'He said, “Bah,” then left.',
                  annotation: null
                }
              ]
            }
          : {
              items: [plan()],
              capabilities: {
                model: 'fish_audio_s2_pro_q8_0',
                instructions: 'inline',
                dialect: 'fish_s2',
                status: 'documented',
                semantic_context: 'none'
              }
            }
      });
    }
  );
  await page.goto(`/sessions/${sessionId}`);
  await page
    .getByRole('region', { name: 'Speech plan', exact: true })
    .getByRole('link', { name: 'Annotate & review', exact: true })
    .click();
  const panel = page.getByRole('region', {
    name: 'Annotation workspace',
    exact: true
  });
  const status = panel.getByTestId('performance-stale-plan');
  // Blocks are numbered from 1 here, as the run refusal names them.
  await expect(status).toContainText(
    '2 blocks changed since adoption. 1 needs a new speaker plan review before generation: block #1. Every other block keeps its entry.'
  );
  // Copying stays the way to re-review the neighbours of an edited block.
  await expect(
    status.getByRole('button', { name: 'Copy unchanged blocks', exact: true })
  ).toBeVisible();
  await expect(
    status.getByRole('button', {
      name: 'Copy and analyse the rest',
      exact: true
    })
  ).toBeVisible();
  // Stale only through block settings the per-block judgement ignores.
  counts = {
    changed_segment_count: 0,
    review_segment_count: 0,
    review_segments: []
  };
  await panel.getByRole('button', { name: 'Refresh', exact: true }).click();
  await expect(status).toContainText(
    'Block settings such as voice or pauses changed since adoption. Every entry still holds'
  );
  await expect(status).not.toContainText('can no longer be adopted');
  counts = {
    changed_segment_count: null,
    review_segment_count: null,
    review_segments: null
  };
  // Null counts: the adopted plan is not judged, as it is not on the
  // revision in use. It is still an adopted plan, not a draft.
  await panel.getByRole('button', { name: 'Refresh', exact: true }).click();
  await expect(status).toContainText(
    'This plan was adopted for another version of the speech, not the one now in use, so generation does not use it.'
  );
  await expect(status).not.toContainText('can no longer be adopted');
  await expect(status).not.toContainText('changed since adoption');
  await expect(
    status.getByRole('button', { name: 'Copy unchanged blocks', exact: true })
  ).toBeVisible();
});
