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
