import { expect, test, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

async function setup(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.locator('.app-shell')).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const csrf = (await (await page.request.get('/api/v1/auth/status')).json())
    .csrf_token;
  const created = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': csrf },
    data: {
      name: `Cast test ${crypto.randomUUID()}`,
      workflow_kind: 'audiobook'
    }
  });
  expect(created.ok(), await created.text()).toBeTruthy();
  const sid = (await created.json()).id;
  const revision = 'accepted-dialogue';
  await page.route(
    `**/api/v1/sessions/${sid}/generation-plan/status`,
    (route) =>
      route.fulfill({
        json: {
          session_id: sid,
          session_revision: 1,
          items: [
            {
              id: revision,
              revision_number: 1,
              summary: 'Accepted dialogue',
              origin: 'manual',
              reviewed: true,
              compatible: true,
              segment_count: 2,
              active_segment_count: 2,
              reusable_segment_count: 0,
              stale_segment_count: 2,
              source_artifact_id: null
            }
          ],
          selected_revision_id: revision,
          latest_revision_id: revision,
          content_signature: 'test',
          can_prepare: false,
          can_generate: false,
          current_input: null,
          blocked_reason: null,
          warning: null
        }
      })
  );
  const capability = {
    model: 'gemini-2.5-flash-tts',
    instructions: 'inline',
    semantic_context: 'prompt',
    status: 'documented'
  };
  let exists = false,
    version = 1,
    status = 'draft',
    writes = 0;
  const units = [
    {
      id: 'block-1',
      ordinal: 0,
      text: 'He said, “Bah!” Then left.',
      spoken_text: 'He said, “Bah!” Then left.',
      annotation: {
        decision: 'steer',
        delivery: { emotion: 'reserved' },
        locked: false
      },
      speech_xml:
        '<segment id="block-1"><em>reserved</em>He said, <dialogue><speaker g="male">“Bah!”</speaker></dialogue> Then left.</segment>'
    },
    {
      id: 'block-2',
      ordinal: 1,
      text: 'The night was cold.',
      spoken_text: 'The night was cold.',
      annotation: { decision: 'none', delivery: {}, locked: false },
      speech_xml: '<segment id="block-2">The night was cold.</segment>'
    }
  ];
  const plan = () => ({
    id: 'directions-1',
    plan_revision_id: revision,
    status,
    version,
    total: 2,
    filtered_total: 2,
    analysed_count: 2,
    steered_count: 1,
    locked_count: 0,
    stale: false,
    settings: {
      workflow_kind: 'audiobook',
      mode: 'manual',
      annotation_format: 'xml'
    },
    items: units
  });
  await page.route(
    `**/api/v1/sessions/${sid}/performance-plans**`,
    async (route) => {
      const request = route.request(),
        suffix = new URL(request.url()).pathname.split('/performance-plans')[1];
      if (request.method() === 'GET')
        return route.fulfill({
          json: suffix
            ? plan()
            : { items: exists ? [plan()] : [], capabilities: capability }
        });
      const body = request.postDataJSON();
      if (!suffix) {
        exists = true;
        expect(body.annotation_format).toBe('xml');
        return route.fulfill({ status: 201, json: plan() });
      }
      if (request.method() === 'PATCH') {
        expect(body.expected_version).toBe(version);
        const unit = units.find(
          (item) => item.id === body.items[0].segment_id
        )!;
        unit.speech_xml = body.items[0].speech_xml;
        writes++;
        version++;
        return route.fulfill({
          json: { id: 'directions-1', version, updated: 1 }
        });
      }
      if (suffix.endsWith('/preview'))
        return route.fulfill({
          json: {
            transcript: units[0].text,
            input: 'Natural narration.',
            instructions: '',
            report: [
              {
                status: 'approximated',
                control: 'emotion',
                message: 'Emotion is interpreted by the model.'
              }
            ],
            capabilities: capability,
            parts: [
              {
                text: units[0].text,
                voice: 'Kore',
                voice_source: 'narrator',
                fallback: false
              }
            ]
          }
        });
      if (suffix.endsWith('/adopt')) {
        status = 'adopted';
        version++;
        return route.fulfill({ json: plan() });
      }
      throw new Error(`Unexpected request ${suffix}`);
    }
  );
  await page.goto(`/sessions/${sid}`);
  const panel = page
    .getByRole('region', { name: 'Speech plan', exact: true })
    .locator('details')
    .filter({
      has: page.locator('summary').filter({ hasText: /^Speech direction/ })
    })
    .first();
  await panel.locator('summary').first().click();
  await panel
    .getByRole('button', { name: 'Create manual draft', exact: true })
    .click();
  await expect(panel.getByLabel('Block delivery direction')).toBeVisible();
  return { panel, writes: () => writes, sid, csrf };
}

test('one XML draft survives refresh, view changes, navigation and save; preview becomes stale', async ({
  page
}) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const { panel, writes } = await setup(page);
  await expect(panel.getByLabel('Emotion', { exact: true })).toHaveValue(
    'reserved'
  );
  await panel
    .getByLabel('Block delivery direction')
    .fill('Quietly, with restraint.');
  await panel
    .getByLabel('General speech direction')
    .fill('Warm, steady narration.');
  await panel.getByRole('button', { name: 'Refresh', exact: true }).click();
  await expect(panel.getByLabel('Block delivery direction')).toHaveValue(
    'Quietly, with restraint.'
  );
  await expect(panel.getByLabel('General speech direction')).toHaveValue(
    'Warm, steady narration.'
  );
  await panel
    .locator('summary')
    .filter({ hasText: /^XML annotation/ })
    .click();
  const xml = panel.getByLabel('XML annotation', { exact: true });
  await expect(xml).toHaveValue(/<ins>Quietly, with restraint.<\/ins>/);
  await xml.fill(
    (await xml.inputValue()).replace('<em>reserved</em>', '<em>happy</em>')
  );
  await expect(panel.getByLabel('Emotion', { exact: true })).toHaveValue(
    'happy'
  );
  await panel
    .getByRole('button', { name: 'Preview model request', exact: true })
    .click();
  await expect(
    panel.getByRole('region', { name: 'Compiled model request' })
  ).toContainText('no audio generated');
  await panel.getByLabel('Emotion', { exact: true }).fill('curious');
  await expect(
    panel.getByRole('region', { name: 'Compiled model request' })
  ).toContainText('out of date');
  await panel
    .getByRole('navigation', { name: 'Speech blocks' })
    .getByRole('button')
    .nth(1)
    .click();
  await expect(
    page.getByRole('dialog', { name: 'Save this block before leaving?' })
  ).toBeVisible();
  await page.getByRole('button', { name: 'Stay here', exact: true }).click();
  await panel
    .getByRole('button', { name: 'Save block directions', exact: true })
    .click();
  expect(writes()).toBe(1);
  await expect(
    panel.getByLabel('Accepted spoken text', { exact: false })
  ).toHaveValue('He said, “Bah!” Then left.');
  await panel
    .getByRole('button', { name: 'Clear block directions', exact: true })
    .click();
  await expect(panel.getByLabel('Emotion', { exact: true })).toHaveValue('');
  // Saving retains the selected block; reopen the advanced view after the reload.
  if (!(await xml.isVisible()))
    await panel
      .locator('summary')
      .filter({ hasText: /^XML annotation/ })
      .click();
  await expect(xml).toHaveValue(/<dialogue><speaker g="male">/);
  await expect(panel.getByLabel('General speech direction')).toHaveValue(
    'Warm, steady narration.'
  );
  expect(errors).toEqual([]);
  await page.screenshot({
    path: '/tmp/pandrator-generation-controls-desktop.png',
    fullPage: false
  });
});

test('recognition settings and managed voice categories are editable without generation', async ({
  page
}) => {
  test.setTimeout(60_000);
  const { csrf } = await setup(page);
  const stage = page.locator('article').filter({
    has: page.getByRole('heading', {
      name: 'Optimize text for speech',
      exact: true
    })
  });
  await stage.getByRole('button', { name: 'Timing & settings' }).click();
  const dialog = page.getByRole('dialog', { name: 'Optimize text for speech' });
  await dialog
    .getByRole('combobox', { name: 'Annotation level' })
    .selectOption('speakers');
  await expect(
    dialog.getByRole('checkbox', { name: /Annotate only/ })
  ).toBeVisible();
  await dialog.getByRole('checkbox', { name: /Annotate only/ }).check();
  await expect(
    dialog.getByRole('radio', { name: /During generation/ })
  ).toBeDisabled();
  await dialog
    .getByRole('group', { name: 'Dialogue and character recognition' })
    .scrollIntoViewIfNeeded();
  await page.screenshot({
    path: '/tmp/pandrator-speech-optimization-settings.png'
  });
  await page.getByRole('button', { name: 'Close stage settings' }).click();

  const created = await page.request.post('/api/v1/voices', {
    headers: { 'X-CSRF-Token': csrf },
    data: { name: 'Character reference', voice_category: 'unspecified' }
  });
  expect(created.ok(), await created.text()).toBeTruthy();
  const voice = await created.json();
  await page.goto('/voices');
  await page.getByLabel('Search voices').fill('Character reference');
  await page
    .getByRole('button', { name: 'Character reference', exact: true })
    .click();
  await page.getByRole('button', { name: 'Edit profile', exact: true }).click();
  await page
    .getByRole('combobox', { name: 'Voice presentation', exact: true })
    .selectOption('androgynous');
  await page.screenshot({ path: '/tmp/pandrator-voice-category.png' });
  const saved = page.waitForResponse(
    (response) =>
      response.url().endsWith(`/api/v1/voices/${voice.id}`) &&
      response.request().method() === 'PATCH'
  );
  await page.getByRole('button', { name: 'Save profile', exact: true }).click();
  expect((await saved).ok()).toBeTruthy();
  const catalogue = await (await page.request.get('/api/v1/voices')).json();
  expect(
    catalogue.items.find((item: { id: string }) => item.id === voice.id)
      .voice_category
  ).toBe('androgynous');
});

test('catalog selection assigns a managed reference to a character', async ({
  page
}) => {
  const { panel, sid, csrf } = await setup(page);
  const name = `Cast reference ${crypto.randomUUID()}`;
  const created = await page.request.post('/api/v1/voices', {
    headers: { 'X-CSRF-Token': csrf },
    data: { name, language: 'en' }
  });
  expect(created.ok()).toBeTruthy();
  const voice = await created.json();
  await panel
    .locator('summary')
    .filter({ hasText: /^Characters and cast/ })
    .click();
  await panel
    .getByRole('button', { name: 'Add character', exact: true })
    .click();
  const character = panel.locator('article');
  await character.getByLabel('Name', { exact: true }).fill('Scrooge');
  await character
    .getByRole('button', { name: 'Browse voice library', exact: true })
    .click();
  const library = page.getByRole('dialog', {
    name: 'Voice Library',
    exact: true
  });
  await library.getByLabel('Search voices').fill(name);
  await expect(library.getByRole('button', { name, exact: true })).toBeVisible();
  await library.getByRole('button', { name: 'Use voice', exact: true }).click();
  await expect(library).toBeHidden();
  await expect(
    character.getByText(`Reference: ${name}`, { exact: true })
  ).toBeVisible();
  await panel
    .getByRole('button', { name: 'Save characters and cast', exact: true })
    .click();
  await expect(
    panel.getByText('Characters and cast saved.', { exact: false })
  ).toBeVisible();
  const saved = await (
    await page.request.get(`/api/v1/sessions/${sid}/generation-controls`)
  ).json();
  expect(saved.cast.characters[saved.characters[0].id].voice_id).toBe(voice.id);
});

test('character dictionary and cast remain usable on mobile without synthesis', async ({
  page
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const { panel, sid } = await setup(page);
  await panel
    .locator('summary')
    .filter({ hasText: /^Characters and cast/ })
    .click();
  await panel
    .getByRole('button', { name: 'Add character', exact: true })
    .click();
  const character = panel.locator('article');
  await character.getByLabel('Name', { exact: true }).fill('Scrooge');
  await character
    .getByRole('combobox', { name: 'Voice category', exact: true })
    .selectOption('male');
  await character
    .getByLabel('Aliases', { exact: false })
    .fill('Ebenezer, Mr Scrooge');
  await character
    .getByLabel('Identity notes')
    .fill('Same character throughout the book.');
  await character
    .getByLabel('Voice for Scrooge', { exact: true })
    .fill('Charon');
  await panel
    .getByRole('button', { name: 'Save characters and cast', exact: true })
    .click();
  await expect(
    panel.getByText('Characters and cast saved.', { exact: false })
  ).toBeVisible();
  const saved = await (
    await page.request.get(`/api/v1/sessions/${sid}/generation-controls`)
  ).json();
  expect(saved.characters[0].voice_category).toBe('male');
  expect(saved.cast.characters[saved.characters[0].id].voice).toBe('Charon');
  const fieldWidth = await character
    .getByLabel('Name', { exact: true })
    .evaluate((node) => node.getBoundingClientRect().width);
  expect(fieldWidth).toBeGreaterThan(290);
  await character.scrollIntoViewIfNeeded();
  const header = page.locator('.mobile-app-header');
  await expect(header).toBeVisible();
  expect((await header.boundingBox())?.y).toBe(0);
  await expect(
    header.getByRole('combobox', { name: 'Session section' })
  ).toHaveValue(`/sessions/${sid}`);
  await page
    .getByRole('button', { name: 'Open navigation', exact: true })
    .click();
  await expect(
    page.getByRole('dialog', { name: 'Main navigation' })
  ).toBeVisible();
  const drawerBox = await page
    .getByRole('dialog', { name: 'Main navigation' })
    .boundingBox();
  expect(drawerBox?.width).toBeLessThan(390);
  await expect
    .poll(
      async () =>
        (
          await page
            .getByRole('dialog', { name: 'Main navigation' })
            .boundingBox()
        )?.x
    )
    .toBe(0);
  await page.screenshot({ path: '/tmp/pandrator-mobile-navigation.png' });
  await page.keyboard.press('Escape');
  await expect(
    page.getByRole('button', { name: 'Open navigation', exact: true })
  ).toBeFocused();
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth
      )
    )
    .toBe(true);
  expect(errors).toEqual([]);
  const accessibility = await new AxeBuilder({ page })
    .include('[aria-label="Speech plan"]')
    .withTags(['wcag2a', 'wcag2aa', 'wcag21aa'])
    .analyze();
  expect(
    accessibility.violations.filter((issue) =>
      ['serious', 'critical'].includes(issue.impact ?? '')
    )
  ).toEqual([]);
  await page.screenshot({
    path: '/tmp/pandrator-generation-controls-mobile.png',
    fullPage: false
  });
  await character.screenshot({
    path: '/tmp/pandrator-character-editor-mobile.png'
  });
  await page.setViewportSize({ width: 320, height: 740 });
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth
      )
    )
    .toBe(true);
  await page.screenshot({
    path: '/tmp/pandrator-generation-controls-narrow.png'
  });
  await character.getByLabel('Identity notes').fill('An unsaved change.');
  await header
    .getByRole('combobox', { name: 'Session section' })
    .selectOption(`/sessions/${sid}/text`);
  const guard = page.getByRole('dialog', {
    name: 'Save your changes before leaving?'
  });
  await expect(guard).toBeVisible();
  await guard.getByRole('button', { name: 'Stay here', exact: true }).click();
  await expect(page).toHaveURL(`/sessions/${sid}`);
  await expect(
    header.getByRole('combobox', { name: 'Session section' })
  ).toHaveValue(`/sessions/${sid}`);
  await expect(character.getByLabel('Identity notes')).toHaveValue(
    'An unsaved change.'
  );
  await header
    .getByRole('combobox', { name: 'Session section' })
    .selectOption(`/sessions/${sid}/text`);
  await guard
    .getByRole('button', { name: 'Discard and continue', exact: true })
    .click();
  await expect(page).toHaveURL(`/sessions/${sid}/text`);
  const unchanged = await (
    await page.request.get(`/api/v1/sessions/${sid}/generation-controls`)
  ).json();
  expect(unchanged.characters[0].notes).toBe(
    'Same character throughout the book.'
  );
});

test('leaving a section can save character, direction and generation-default drafts together', async ({
  page
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const { panel, sid, writes } = await setup(page);
  await panel
    .locator('summary')
    .filter({ hasText: /^Characters and cast/ })
    .click();
  await panel
    .getByRole('button', { name: 'Add character', exact: true })
    .click();
  await panel
    .locator('article')
    .getByLabel('Name', { exact: true })
    .fill('Alice');
  await panel
    .getByLabel('General speech direction', { exact: true })
    .fill('Read with warmth.');
  await panel
    .getByLabel('Block delivery direction', { exact: true })
    .fill('A quiet aside.');
  await page
    .getByRole('combobox', { name: 'Session section' })
    .selectOption(`/sessions/${sid}/text`);
  const guard = page.getByRole('dialog', {
    name: 'Save your changes before leaving?'
  });
  await expect(guard).toBeVisible();
  expect((await guard.boundingBox())?.height).toBeLessThan(500);
  await page.screenshot({ path: '/tmp/pandrator-unsaved-controls-mobile.png' });
  await guard
    .getByRole('button', { name: 'Save and continue', exact: true })
    .click();
  await expect(page).toHaveURL(`/sessions/${sid}/text`);
  expect(writes()).toBe(1);
  const controls = await (
    await page.request.get(`/api/v1/sessions/${sid}/generation-controls`)
  ).json();
  expect(controls.characters[0].display_name).toBe('Alice');
  const settings = await (
    await page.request.get(`/api/v1/sessions/${sid}/settings/tts`)
  ).json();
  expect(settings.effective.generation_prompt).toBe('Read with warmth.');
});
