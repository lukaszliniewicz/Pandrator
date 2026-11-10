import { expect, test, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const spoken = 'Mara smiled. “Hello 😀 there!” Then they left.';

async function project(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.locator('.app-shell')).toBeVisible();
  expect((await page.context().cookies()).map(({ name }) => name)).toContain(
    'pandrator_e2e_session'
  );
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const headers = {
    'X-CSRF-Token': auth.csrf_token,
    'Idempotency-Key': crypto.randomUUID()
  };
  const created = await page.request.post('/api/v1/sessions', {
    headers,
    data: {
      name: `Project UX ${crypto.randomUUID()}`,
      workflow_kind: 'audiobook',
      interface_mode: 'advanced',
      source_language: 'en'
    }
  });
  expect(created.ok(), await created.text()).toBeTruthy();
  const { id } = await created.json();
  const prepared = await page.request.post(
    `/api/v1/sessions/${id}/generation-plan`,
    {
      headers: { ...headers, 'Idempotency-Key': crypto.randomUUID() },
      data: {
        segments: [
          { text: spoken },
          { text: 'The road was quiet.' },
          { text: 'They walked on.' }
        ]
      }
    }
  );
  expect(prepared.ok(), await prepared.text()).toBeTruthy();
  return { id: id as string, headers };
}

async function selectWords(page: Page, words: string) {
  const text = page.getByRole('textbox', { name: /^Accepted spoken text/ });
  await text.evaluate((node: HTMLTextAreaElement, selected: string) => {
    const start = node.value.indexOf(selected);
    if (start < 0) throw new Error('Selection missing from accepted text');
    node.focus();
    node.setSelectionRange(start, start + selected.length);
    node.dispatchEvent(new Event('select', { bubbles: true }));
  }, words);
}

test('overview tools and narration help are discoverable; both views show run metrics', async ({
  page
}) => {
  const { id } = await project(page);
  const workflow = await (
    await page.request.get(`/api/v1/sessions/${id}/workflow`)
  ).json();
  const started = new Date(Date.now() - 120_000).toISOString();
  await page.route(`**/api/v1/sessions/${id}/workflow`, (route) =>
    route.fulfill({
      json: {
        ...workflow,
        stages: [
          {
            number: 4,
            key: 'generate_audio',
            title: 'Generate audio',
            explanation: 'Record the selected plan.',
            status: 'running',
            executable: true,
            included: true,
            job_id: 'timing-fixture',
            progress: 0.5,
            progress_basis: 'job',
            detail: 'Generating segment 2 of 4',
            run_metrics: {
              started_at: started,
              finished_at: null,
              duration_seconds: 120
            },
            usage: null,
            artifacts: []
          }
        ]
      }
    })
  );
  await page.goto(`/sessions/${id}`);
  await expect(
    page.getByRole('navigation', { name: 'Project location' })
  ).toHaveCount(0);
  const tools = page.getByLabel('Project tools', { exact: true });
  await expect(
    tools.getByRole('button', { name: 'Folders', exact: true })
  ).toBeVisible();
  const sizes = await tools
    .getByRole('button')
    .evaluateAll((buttons) =>
      buttons.map((button) => button.getBoundingClientRect().height)
    );
  expect(sizes).toHaveLength(3);
  expect(Math.max(...sizes) - Math.min(...sizes)).toBeLessThan(1);
  const interfaceHeight = await page
    .getByRole('group', { name: 'Project interface', exact: true })
    .evaluate((node) => node.getBoundingClientRect().height);
  expect(Math.abs(interfaceHeight - sizes[0])).toBeLessThan(1);
  const workflowHeight = await page
    .getByLabel('Workflow execution', { exact: true })
    .evaluate((node) => node.getBoundingClientRect().height);
  expect(Math.abs(workflowHeight - sizes[0])).toBeLessThan(1);
  const settings = page.getByTestId('narration-settings');
  await expect(settings).not.toHaveAttribute('open');
  // Use an idle workflow for editable settings; the timing fixture remains read-only.
  const metrics = page.getByLabel('Generate audio run metrics');
  await expect(metrics).toContainText('Elapsed');
  await expect(metrics).toContainText('Estimated time left');
  await expect(metrics).toContainText('About 2m');
  await expect(metrics).toContainText('not metered');
  await page.screenshot({
    path: test.info().outputPath('overview-desktop.png'),
    fullPage: true
  });
  await page.getByRole('button', { name: /^simple$/i, exact: true }).click();
  await expect(
    page.locator('[data-simple-stage="generate_audio"]')
  ).toBeVisible();
  await expect(metrics).toContainText('Estimated time left');
  await expect(metrics).toContainText('not metered');
  await page.unroute(`**/api/v1/sessions/${id}/workflow`);
  await page.getByRole('button', { name: /^advanced$/i, exact: true }).click();
  await settings.locator('summary').click();
  const dialogue = settings.getByRole('combobox', { name: /^Dialogue flow/ });
  await expect(dialogue).toHaveValue('auto');
  const help = settings.getByRole('button', {
    name: 'About Whole-sentence threshold (characters)',
    exact: true
  });
  await help.focus();
  await page.keyboard.press('Enter');
  await expect(
    page.getByRole('tooltip').filter({ hasText: /separate from wtpsplit/ })
  ).toBeVisible();
  await page.keyboard.press('Escape');
  await dialogue.selectOption('off');
  await page.getByRole('button', { name: /^simple$/i, exact: true }).click();
  await expect(
    page
      .getByRole('alert')
      .filter({ hasText: 'Finish or close the open editor' })
  ).toBeVisible();
  await expect(dialogue).toHaveValue('off');
  await settings
    .getByRole('button', { name: 'Save narration settings' })
    .click();
  await expect(
    page
      .getByRole('status')
      .filter({ hasText: 'Saved. Generation updates untouched narration' })
  ).toBeVisible();
  await page.reload();
  await settings.locator('summary').click();
  await expect(dialogue).toHaveValue('off');
});

test('manual annotations preserve words, support phrase directions and protect against concurrent saves', async ({
  page
}) => {
  const { id, headers } = await project(page);
  const controlsUrl = `/api/v1/sessions/${id}/generation-controls`;
  const controls = await (await page.request.get(controlsUrl)).json();
  const character = crypto.randomUUID();
  const cast = await page.request.put(controlsUrl, {
    headers: { ...headers, 'Idempotency-Key': crypto.randomUUID() },
    data: {
      expected_revision: controls.revision,
      characters: [
        { id: character, display_name: 'Mara', voice_category: 'female' }
      ]
    }
  });
  expect(cast.ok(), await cast.text()).toBeTruthy();
  const unexpected: string[] = [];
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('request', (request) => {
    if (
      request.method() === 'POST' &&
      /\/(audio\/speech|generation-runs|jobs)$/.test(
        new URL(request.url()).pathname
      )
    )
      unexpected.push(request.url());
  });
  await page.goto(`/sessions/${id}/speech`);
  const workspace = page.getByRole('region', {
    name: 'Annotation workspace',
    exact: true
  });
  await expect(workspace).toBeVisible();
  await workspace
    .getByRole('button', { name: 'Create manual draft', exact: true })
    .click();
  await expect(
    workspace.getByRole('textbox', { name: /^Accepted spoken text/ })
  ).toHaveValue(spoken);
  await workspace
    .getByRole('checkbox', {
      name: 'Protect this block from analysis',
      exact: true
    })
    .uncheck();
  await selectWords(page, '“Hello 😀 there!”');
  await workspace
    .getByRole('combobox', { name: 'Selected words belong to', exact: true })
    .selectOption(character);
  await workspace
    .getByRole('button', { name: 'Mark selected text', exact: true })
    .click();
  await selectWords(page, 'Hello 😀 there!');
  await workspace
    .getByRole('textbox', { name: 'Direction for selected words', exact: true })
    .fill('Warmly, as a greeting.');
  await workspace
    .getByRole('button', { name: 'Direct selected words', exact: true })
    .click();
  await expect(
    workspace.getByRole('region', { name: 'Annotated text preview' })
  ).toContainText(spoken);
  // A block edit must not be lost by undoing an earlier selection action.
  await workspace.getByLabel('Block delivery direction').fill('Level voice.');
  await expect(
    workspace.getByRole('button', {
      name: 'Undo selection change',
      exact: true
    })
  ).toHaveCount(0);
  await workspace.getByLabel('Block delivery direction').fill('');
  await workspace
    .getByRole('button', { name: 'Save block annotations', exact: true })
    .click();
  await expect(workspace.getByRole('status')).toContainText(
    'Block directions and speaker assignments saved'
  );
  let listing = await (
    await page.request.get(`/api/v1/sessions/${id}/performance-plans`)
  ).json();
  const planId = listing.items[0].id;
  const planUrl = `/api/v1/sessions/${id}/performance-plans/${planId}`;
  let saved = await (await page.request.get(planUrl)).json();
  expect(saved.items[0].speech_xml).toContain(`<speaker ref="${character}">`);
  expect(saved.items[0].speech_xml).toContain(
    '<ins>Warmly, as a greeting.</ins>Hello 😀 there!'
  );
  expect(saved.items[0].spoken_text).toBe(spoken);
  await page.reload();
  await expect(
    workspace.getByRole('region', { name: 'Annotated text preview' })
  ).toContainText(spoken);
  // Clicking the annotated phrase must sync its existing speaker, even if the
  // text selection already points to the same offsets.
  const speakerPicker = workspace.getByRole('combobox', {
    name: 'Selected words belong to',
    exact: true
  });
  await speakerPicker.selectOption('__narrator');
  await workspace
    .getByRole('region', { name: 'Annotated text preview' })
    .locator('button[title^="Mara"]')
    .filter({ hasText: 'Hello 😀 there!' })
    .click();
  await expect(speakerPicker).toHaveValue(character);
  // A complete marked turn can be reassigned while nested phrase directions survive.
  await selectWords(page, '“Hello 😀 there!”');
  await workspace
    .getByRole('combobox', { name: 'Selected words belong to', exact: true })
    .selectOption('__narrator');
  await workspace
    .getByRole('button', { name: 'Mark selected text', exact: true })
    .click();
  // Do not let a failed client-side mark be mistaken for a successful save.
  await expect(
    workspace
      .getByRole('region', { name: 'Annotated text preview' })
      .locator('button[title^="Narrator"]')
      .filter({ hasText: 'Hello 😀 there!' })
  ).toContainText('Hello 😀 there!');
  await workspace
    .getByRole('button', { name: 'Save block annotations', exact: true })
    .click();
  await expect(workspace.getByRole('status')).toContainText(
    'Block directions and speaker assignments saved'
  );
  saved = await (await page.request.get(planUrl)).json();
  expect(saved.items[0].speech_xml).toContain('<narrator>');
  expect(saved.items[0].speech_xml).toContain('Warmly, as a greeting.');
  // Another client saves after our local edit: the UI must preserve the local draft and conflict.
  await workspace
    .getByLabel('Block delivery direction')
    .fill('My unsaved direction.');
  const remoteXml = saved.items[0].speech_xml.replace(
    '>',
    '><ins>Other client direction.</ins>'
  );
  const remote = await page.request.patch(planUrl, {
    headers: { ...headers, 'Idempotency-Key': crypto.randomUUID() },
    data: {
      expected_version: saved.version,
      items: [
        { segment_id: saved.items[0].id, speech_xml: remoteXml, locked: false }
      ]
    }
  });
  expect(remote.ok(), await remote.text()).toBeTruthy();
  await workspace
    .getByRole('button', { name: 'Save block annotations', exact: true })
    .click();
  await expect(workspace.getByRole('alert').first()).toContainText(
    /changed|version|reload/i
  );
  await expect(workspace.getByLabel('Block delivery direction')).toHaveValue(
    'My unsaved direction.'
  );
  await workspace
    .getByRole('link', { name: 'Speech plan', exact: true })
    .click();
  const guard = page.getByRole('dialog', { name: /^Save your changes before/ });
  await expect(guard).toBeVisible();
  await guard.getByRole('button', { name: 'Stay here', exact: true }).click();
  await workspace
    .getByRole('button', { name: 'Discard block changes', exact: true })
    .click();
  await workspace.getByRole('button', { name: 'Refresh', exact: true }).click();
  await expect(workspace.getByLabel('Block delivery direction')).toHaveValue(
    'Other client direction.'
  );
  await workspace
    .getByRole('checkbox', { name: /^Accept .* unanalysed blocks/ })
    .check();
  const blocks = workspace.getByRole('navigation', { name: 'Speech blocks' });
  let releaseAdoption!: () => void;
  const adoptionGate = new Promise<void>((resolve) => {
    releaseAdoption = resolve;
  });
  await page.route(`**${planUrl}/adopt{,?*}`, async (route) => {
    const response = await route.fetch();
    await adoptionGate;
    await route.fulfill({ response });
  });
  try {
    await workspace
      .getByRole('button', {
        name: 'Adopt speakers and directions',
        exact: true
      })
      .click();
    await expect(blocks.getByRole('button').first()).toBeDisabled();
    await expect(blocks).toHaveAttribute('tabindex', '0');
    await blocks.focus();
    await expect(blocks).toBeFocused();
  } finally {
    releaseAdoption();
  }
  await expect(
    workspace.getByText(/adopted for future generation\./)
  ).toBeVisible();
  await expect(
    workspace.getByRole('textbox', { name: /^Accepted spoken text/ })
  ).toHaveValue(spoken);
  listing = await (
    await page.request.get(`/api/v1/sessions/${id}/performance-plans`)
  ).json();
  expect(listing.items[0].status).toBe('adopted');
  await expect(blocks.getByRole('button').first()).toBeEnabled();
  expect(unexpected).toEqual([]);
  expect(errors).toEqual([]);
  const audit = await new AxeBuilder({ page })
    .include('[aria-label="Annotation workspace"]')
    .withTags(['wcag2a', 'wcag2aa', 'wcag21aa'])
    .analyze();
  expect(
    audit.violations.filter((item) =>
      ['serious', 'critical'].includes(item.impact ?? '')
    )
  ).toEqual([]);
  await page.screenshot({
    path: test.info().outputPath('annotation-desktop.png'),
    fullPage: true
  });
});

test('mobile project tools and the annotation workspace remain usable at 390 and 320 pixels', async ({
  page
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const { id } = await project(page);
  await page.goto(`/sessions/${id}`);
  const tools = page
    .locator('details')
    .filter({ has: page.locator('summary[aria-label="Project tools"]') });
  await tools.locator('summary').click();
  await expect(
    tools.getByRole('button', { name: 'Folders', exact: true })
  ).toBeVisible();
  await expect(
    tools.getByRole('button', { name: 'Customize workflow', exact: true })
  ).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(tools).not.toHaveAttribute('open');
  await expect(tools.locator('summary')).toBeFocused();
  await page
    .getByRole('combobox', { name: 'Project section', exact: true })
    .selectOption(`/sessions/${id}/speech`);
  const workspace = page.getByRole('region', {
    name: 'Annotation workspace',
    exact: true
  });
  await workspace
    .getByRole('button', { name: 'Create manual draft', exact: true })
    .click();
  await selectWords(page, '“Hello 😀 there!”');
  await workspace
    .getByRole('button', { name: 'Mark selected text', exact: true })
    .click();
  await expect(
    workspace.getByRole('region', { name: 'Annotated text preview' })
  ).toContainText(spoken);
  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 844 });
    await expect
      .poll(() =>
        page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)
      )
      .toBe(true);
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await workspace
    .getByRole('textbox', { name: /^Accepted spoken text/ })
    .scrollIntoViewIfNeeded();
  await page.screenshot({
    path: test.info().outputPath('annotation-mobile.png'),
    fullPage: true
  });
});
