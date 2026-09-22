import { expect, test, type Page } from '@playwright/test';

// Regression: the generation drawer must not grow horizontally after
// search/replace (playback controls must stay inside the viewport), and
// Ctrl+K must show + focus search while the drawer is open.

async function signIn(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const closeTour = page.getByRole('button', { name: 'Close tour' });
  if (await closeTour.isVisible()) await closeTour.click();
}

async function csrf(page: Page) {
  const status = await page.request.get('/api/v1/auth/status');
  expect(status.ok()).toBeTruthy();
  return (await status.json()).csrf_token as string;
}

async function createSession(
  page: Page,
  kind: 'audiobook' | 'voiceover',
  prefix: string
) {
  const token = await csrf(page);
  const response = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': token },
    data: { name: `${prefix} ${crypto.randomUUID()}`, workflow_kind: kind }
  });
  expect(response.ok()).toBeTruthy();
  return (await response.json()).id as string;
}

async function createPlan(
  page: Page,
  sessionId: string,
  segments: Array<{ text: string }>
) {
  const token = await csrf(page);
  const response = await page.request.post(
    `/api/v1/sessions/${sessionId}/generation-plan`,
    { headers: { 'X-CSRF-Token': token }, data: { segments } }
  );
  expect(response.ok()).toBeTruthy();
}

async function openDrawer(page: Page, sessionId: string) {
  await page.goto(`/sessions/${sessionId}`);
  await page.getByRole('button', { name: 'Generation', exact: true }).click();
  await expect(
    page.getByRole('button', { name: 'Search and replace' })
  ).toBeVisible();
}

async function openSearch(page: Page) {
  const toggle = page.getByRole('button', { name: 'Search and replace' });
  if ((await toggle.getAttribute('aria-expanded')) !== 'true') {
    await toggle.click();
  }
  await expect(page.locator('#generation-search-panel')).toBeVisible();
}

type Box = {
  viewportWidth: number;
  drawerClient: number;
  drawerScroll: number;
  headerClient: number;
  headerScroll: number;
  tableClient: number | null;
  tableScroll: number | null;
  searchClient: number | null;
  searchScroll: number | null;
  playRight: number | null;
  toggleRight: number | null;
  newRunRight: number | null;
};

async function measure(page: Page): Promise<Box> {
  const box = await page.evaluate(() => {
    const drawer = document.querySelector('aside.generation-drawer');
    const header = document.querySelector('.generation-header');
    const table = document.querySelector('aside.generation-drawer table');
    const search = document.querySelector(
      '#generation-search-panel .search-replace'
    );
    const edge = (selector: string) => {
      const el = document.querySelector(selector);
      if (!el) return null;
      return el.getBoundingClientRect().right;
    };
    const play =
      edge('button[aria-label="Play playlist"]') ??
      edge('button[aria-label="Pause playlist"]') ??
      edge('button[aria-label="Resume playlist"]');
    return {
      viewportWidth: window.innerWidth,
      drawerClient: drawer?.clientWidth ?? -1,
      drawerScroll: drawer?.scrollWidth ?? -1,
      headerClient: header?.clientWidth ?? -1,
      headerScroll: header?.scrollWidth ?? -1,
      tableClient: table?.parentElement?.clientWidth ?? null,
      tableScroll: table?.parentElement?.scrollWidth ?? null,
      searchClient: search?.clientWidth ?? null,
      searchScroll: search?.scrollWidth ?? null,
      playRight: play,
      toggleRight: edge('[data-generation-search-toggle]'),
      newRunRight: null as number | null
    };
  });
  const newRun = await page
    .getByRole('button', { name: 'Generate audio…' })
    .boundingBox()
    .catch(() => null);
  return { ...box, newRunRight: newRun ? newRun.x + newRun.width : null };
}

function expectContained(box: Box) {
  expect(box.playRight, 'Playback control must exist').not.toBeNull();
  expect(box.toggleRight, 'Search control must exist').not.toBeNull();
  expect(
    box.drawerScroll,
    `drawer scroll ${box.drawerScroll} <= client ${box.drawerClient}`
  ).toBeLessThanOrEqual(box.drawerClient + 1);
  expect(
    box.headerScroll,
    `header scroll ${box.headerScroll} <= client ${box.headerClient}`
  ).toBeLessThanOrEqual(box.headerClient + 1);
  if (box.tableClient != null && box.tableScroll != null) {
    expect(
      box.tableScroll,
      `table scroll ${box.tableScroll} <= client ${box.tableClient}`
    ).toBeLessThanOrEqual(box.tableClient + 1);
  }
  if (box.searchClient != null && box.searchScroll != null) {
    expect(
      box.searchScroll,
      `search scroll ${box.searchScroll} <= client ${box.searchClient}`
    ).toBeLessThanOrEqual(box.searchClient + 1);
  }
  for (const [name, right] of [
    ['play', box.playRight],
    ['search toggle', box.toggleRight],
    ['new run', box.newRunRight]
  ] as const) {
    if (right != null) {
      expect(
        right,
        `${name} right ${right} within viewport ${box.viewportWidth}`
      ).toBeLessThanOrEqual(box.viewportWidth + 1);
    }
  }
}

const LONG_UNBROKEN = `dog${'x'.repeat(220)}end`;

test.describe('drawer search layout @ narrow desktop', () => {
  test.use({ viewport: { width: 1100, height: 800 } });

  test('audiobook stays contained before search, after long replace, after close', async ({
    page
  }) => {
    await signIn(page);
    const sessionId = await createSession(page, 'audiobook', 'Drawer layout');
    await createPlan(page, sessionId, [
      { text: 'A cat naps quietly.' },
      { text: 'A catfish and cat wander.' },
      { text: 'A calm cat stretches.' }
    ]);
    await openDrawer(page, sessionId);

    let box = await measure(page);
    console.log('BEFORE-SEARCH', JSON.stringify(box));
    expectContained(box);

    await openSearch(page);
    const find = page.getByLabel('Find in generation segments');
    await expect(find).toBeFocused();
    await find.fill('cat');
    await page.getByLabel('Replace in generation segments').fill(LONG_UNBROKEN);
    await page.getByRole('button', { name: 'Replace all' }).click();
    const fields = page.locator('textarea[data-generation-search-index]');
    // "cat" also matches inside "catfish": replace-all rewrites every hit.
    await expect(fields.nth(0)).toHaveValue(`A ${LONG_UNBROKEN} naps quietly.`);
    await expect(fields.nth(1)).toHaveValue(
      `A ${LONG_UNBROKEN}fish and ${LONG_UNBROKEN} wander.`
    );
    await expect(fields.nth(2)).toHaveValue(
      `A calm ${LONG_UNBROKEN} stretches.`
    );

    box = await measure(page);
    console.log('AFTER-LONG-REPLACE', JSON.stringify(box));
    expectContained(box);
    await page.screenshot({
      path: test.info().outputPath('drawer-layout-narrow-after-replace.png')
    });

    await page.getByRole('button', { name: 'Search and replace' }).click();
    await expect(page.locator('#generation-search-panel')).toBeHidden();
    await expect(fields).toHaveCount(3);
    box = await measure(page);
    console.log('AFTER-CLOSE', JSON.stringify(box));
    expectContained(box);
    await page.screenshot({
      path: test.info().outputPath('drawer-layout-visible-replaced-rows.png')
    });
  });
});

test.describe('drawer search layout @ laptop', () => {
  test.use({ viewport: { width: 1366, height: 768 } });

  test('voiceover cue/TTS scopes stay contained after long TTS replace', async ({
    page
  }) => {
    await signIn(page);
    const sessionId = await createSession(
      page,
      'voiceover',
      'Drawer layout VO'
    );
    await createPlan(page, sessionId, [
      { text: 'Cueword opens the scene.' },
      { text: 'A plain second cue.' }
    ]);
    await openDrawer(page, sessionId);
    await openSearch(page);

    const ttsRadio = page.getByRole('radio', { name: 'TTS text' });
    await expect(ttsRadio).toBeVisible();
    await ttsRadio.click();
    const find = page.getByLabel('Find in generation segments');
    await find.fill('opens');
    await expect(page.getByText('1 / 1', { exact: true })).toBeVisible();
    await page.getByLabel('Replace in generation segments').fill(LONG_UNBROKEN);
    await page.getByRole('button', { name: 'Replace', exact: true }).click();
    await expect(page.getByText('No matches')).toBeVisible();

    const box = await measure(page);
    console.log('VOICEOVER-AFTER-TTS-REPLACE', JSON.stringify(box));
    expectContained(box);
    await page.getByRole('button', { name: 'Search and replace' }).click();
    await expect(
      page.locator('textarea[data-generation-search-index]')
    ).toHaveCount(2);
    await expect(page.getByLabel('Script text for segment 1')).toHaveValue(
      'Cueword opens the scene.'
    );
    await expect(
      page.locator('.narrative-cell p[title^="Spoken:"]').first()
    ).toHaveAttribute('title', `Spoken: Cueword ${LONG_UNBROKEN} the scene.`);
    expectContained(await measure(page));
    await page.screenshot({
      path: test.info().outputPath('drawer-layout-laptop-voiceover.png')
    });
  });
});

test.describe('drawer search Ctrl+K', () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  async function armProbe(page: Page) {
    await page.evaluate(() => {
      (window as unknown as { __ck: boolean[] }).__ck = [];
      window.addEventListener('keydown', (event: KeyboardEvent) => {
        if (
          (event.ctrlKey || event.metaKey) &&
          !event.altKey &&
          event.key.toLowerCase() === 'k'
        ) {
          (window as unknown as { __ck: boolean[] }).__ck.push(
            event.defaultPrevented
          );
        }
      });
    });
  }

  async function lastPrevented(page: Page) {
    return page.evaluate(
      () => (window as unknown as { __ck: boolean[] }).__ck.at(-1) ?? null
    );
  }

  test('Ctrl+K shows and focuses search, repeat keeps it open, closed drawer stays native', async ({
    page
  }) => {
    await signIn(page);
    const sessionId = await createSession(page, 'audiobook', 'Drawer CtrlK');
    await createPlan(page, sessionId, [
      { text: 'A cat naps.' },
      { text: 'A dog runs.' }
    ]);
    await page.goto(`/sessions/${sessionId}`);
    await armProbe(page);

    // Closed drawer: the keystroke must stay native (not prevented, no panel).
    await page.keyboard.press('Control+k');
    expect(await lastPrevented(page)).toBe(false);
    await expect(page.locator('#generation-search-panel')).toHaveCount(0);

    await openDrawer(page, sessionId);
    await armProbe(page);
    const toggle = page.getByRole('button', { name: 'Search and replace' });
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await expect(toggle).toHaveAttribute('aria-keyshortcuts', /Control\+k/i);

    await page.keyboard.press('Control+k');
    expect(await lastPrevented(page)).toBe(true);
    await expect(page.locator('#generation-search-panel')).toBeVisible();
    const find = page.getByLabel('Find in generation segments');
    await expect(find).toBeFocused();

    // Repeat must focus/select, never toggle closed.
    await find.fill('cat');
    await page.keyboard.press('Control+k');
    await expect(page.locator('#generation-search-panel')).toBeVisible();
    await expect(find).toBeFocused();
    expect(await find.inputValue()).toBe('cat');

    // Meta+K matches the same shortcut pattern on macOS.
    await page.keyboard.press('Meta+k');
    await expect(page.locator('#generation-search-panel')).toBeVisible();
    await expect(find).toBeFocused();

    // Escape close and existing semantics are preserved.
    await find.press('Escape');
    await expect(page.locator('#generation-search-panel')).toBeHidden();
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  });

  test('Ctrl+K works from cue/TTS editor inputs and keeps query/scope', async ({
    page
  }) => {
    await signIn(page);
    const sessionId = await createSession(page, 'voiceover', 'Drawer CtrlK VO');
    await createPlan(page, sessionId, [
      { text: 'Cueword opens the scene.' },
      { text: 'A plain second cue.' }
    ]);
    await openDrawer(page, sessionId);
    await openSearch(page);
    await page.getByRole('radio', { name: 'TTS text' }).click();
    const find = page.getByLabel('Find in generation segments');
    await find.fill('scene');
    await expect(page.getByText('1 / 1', { exact: true })).toBeVisible();

    // From a cue editor textarea the shortcut must still reach search.
    await page
      .locator('textarea[data-generation-search-index]')
      .first()
      .click();
    await page.keyboard.press('Control+k');
    await expect(find).toBeFocused();
    expect(await find.inputValue()).toBe('scene');
    await expect(page.getByRole('radio', { name: 'TTS text' })).toHaveAttribute(
      'aria-checked',
      'true'
    );

    // Exercise a real modal without requiring an available synthesis provider
    // merely to open the alternate-generation form in this isolated fixture.
    await page.evaluate(() => {
      const dialog = document.createElement('dialog');
      dialog.id = 'shortcut-isolation-dialog';
      dialog.setAttribute('aria-label', 'Shortcut isolation');
      const input = document.createElement('input');
      input.setAttribute('aria-label', 'Modal input');
      dialog.append(input);
      document.body.append(dialog);
      dialog.showModal();
      input.focus();
    });
    const dialog = page.getByRole('dialog', { name: 'Shortcut isolation' });
    await expect(dialog).toBeVisible();
    await armProbe(page);
    await page.keyboard.press('Control+k');
    await expect(page.locator('#generation-search-panel')).toBeVisible();
    expect(await lastPrevented(page)).toBe(false);
    await page.evaluate(() => {
      const dialog = document.querySelector<HTMLDialogElement>(
        '#shortcut-isolation-dialog'
      );
      dialog?.close();
      dialog?.remove();
    });
    await expect(dialog).toBeHidden();
  });
});

for (const workflowKind of ['audiobook', 'voiceover'] as const) {
  test(`segment actions share the delivery row in ${workflowKind} mode`, async ({
    page
  }, testInfo) => {
    await page.setViewportSize({ width: 1600, height: 900 });
    await signIn(page);
    const sessionId = await createSession(page, workflowKind, 'Inline actions');
    await createPlan(page, sessionId, [
      { text: 'A first passage with room for editing.' },
      { text: 'A second passage keeps its own controls.' }
    ]);
    await openDrawer(page, sessionId);

    const table = page.locator('aside.generation-drawer table');
    const row = table.locator('tr[data-segment-id]').first();
    const narrative = row.locator('td.narrative-cell');
    const actions = narrative.getByRole('group', {
      name: 'Actions for segment 1'
    });
    await expect(table.locator('thead th')).toHaveCount(5);
    await expect(row.locator(':scope > td')).toHaveCount(5);
    await expect(table.locator('tr.boundary-row td').first()).toHaveAttribute(
      'colspan',
      '5'
    );
    await expect(actions).toBeVisible();
    await expect(
      actions.getByRole('button', { name: 'Remove segment', exact: true })
    ).toBeVisible();
    const split = actions.getByRole('button', {
      name: 'Split segment 1 at text cursor'
    });
    const regenerate = actions.getByRole('button', {
      name: 'Regenerate segment 1',
      exact: true
    });
    await expect(split).toBeDisabled();
    await expect(regenerate).toBeEnabled();

    // The three delivery dropdowns and the action group are siblings in one
    // flexible row. At desktop width they share a vertical centre line.
    const alignment = await actions.evaluate((group) => {
      const rect = group.getBoundingClientRect();
      return Array.from(
        group.parentElement!.querySelectorAll(':scope > select')
      ).map((select) => {
        const box = select.getBoundingClientRect();
        return Math.abs(box.y + box.height / 2 - rect.y - rect.height / 2);
      });
    });
    expect(alignment).toHaveLength(3);
    for (const offset of alignment) expect(offset).toBeLessThanOrEqual(2);

    // Moving the controls must preserve the text-cursor guard and menu access.
    const text = narrative.locator('textarea').first();
    await text.focus();
    await text.evaluate((node: HTMLTextAreaElement) => {
      node.setSelectionRange(8, 8);
      node.dispatchEvent(new Event('select', { bubbles: true }));
    });
    await expect(split).toBeEnabled();
    await regenerate.click();
    const menu = page.getByRole('menu', {
      name: 'Regeneration options for segment 1'
    });
    await expect(menu).toBeVisible();
    await expect(
      menu.getByRole('menuitem', { name: 'Regenerate', exact: true })
    ).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(menu).toBeHidden();
    await expect(regenerate).toBeFocused();

    await page.screenshot({
      path: testInfo.outputPath('inline-actions-desktop.png')
    });
    await page.setViewportSize({ width: 1100, height: 800 });
    await expect(actions).toBeVisible();
    expectContained(await measure(page));
    const contained = await actions.evaluate((group) => {
      const cell = group.closest('td')!.getBoundingClientRect();
      const rect = group.getBoundingClientRect();
      return rect.left >= cell.left && rect.right <= cell.right;
    });
    expect(contained).toBe(true);
    await page.screenshot({
      path: testInfo.outputPath('inline-actions-narrow.png')
    });
  });
}
