import { expect, test, type Page } from '@playwright/test';

async function signIn(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.locator('.app-shell')).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
}

async function createAudiobookSession(page: Page) {
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const response = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': auth.csrf_token },
    data: {
      interface_mode: 'advanced',
      name: `Cast disclosure ${crypto.randomUUID()}`,
      workflow_kind: 'audiobook'
    }
  });
  expect(response.ok()).toBeTruthy();
  return (await response.json()).id as string;
}

async function openSessionMultiVoice(page: Page, id: string) {
  await page.goto(`/sessions/${id}`);
  const card = page.getByRole('region', { name: 'Voices', exact: true });
  await expect(card).toBeVisible();
  await card.getByRole('radio', { name: /Multiple voices/ }).check();
  return card;
}

test('multi-voice cast can be collapsed and keeps drafts across refresh', async ({
  page
}) => {
  await signIn(page);
  const id = await createAudiobookSession(page);
  const card = await openSessionMultiVoice(page, id);
  const disclosure = card.getByTestId('cast-disclosure');
  const summary = card.getByTestId('cast-summary');

  await expect(summary).toContainText('Speakers & cast voices');
  await expect(disclosure).toHaveAttribute('open');
  await summary.click();
  await expect
    .poll(() =>
      disclosure.evaluate((node) => (node as HTMLDetailsElement).open)
    )
    .toBe(false);
  await expect(
    card.getByRole('button', { name: 'Add speaker', exact: true })
  ).toBeHidden();
  await expect
    .poll(() =>
      card
        .getByTestId('cast-help')
        .evaluate((node) => (node as HTMLDetailsElement).open)
    )
    .toBe(false);

  await summary.click();
  await expect
    .poll(() =>
      disclosure.evaluate((node) => (node as HTMLDetailsElement).open)
    )
    .toBe(true);
  const add = card.getByRole('button', { name: 'Add speaker', exact: true });
  await expect(add).toBeVisible();
  await add.click();
  const character = card.locator('article').first();
  await character.getByLabel('Speaker name', { exact: true }).fill('Alice');
  await expect(summary).toContainText('unsaved changes');
  await expect(summary).toContainText('1 uses a default voice');
  await expect(
    card.getByText(
      'Save or discard the cast changes before switching voice mode.'
    )
  ).toBeVisible();

  await card.getByRole('button', { name: 'Refresh cast', exact: true }).click();
  await expect
    .poll(() =>
      disclosure.evaluate((node) => (node as HTMLDetailsElement).open)
    )
    .toBe(true);
  await expect(
    character.getByLabel('Speaker name', { exact: true })
  ).toHaveValue('Alice');

  await summary.click();
  await expect
    .poll(() =>
      disclosure.evaluate((node) => (node as HTMLDetailsElement).open)
    )
    .toBe(false);
  const hint = card.getByTestId('cast-collapsed-hint');
  await expect(hint).toContainText('Unsaved character or cast changes');
  await expect(
    hint.getByRole('button', { name: 'Review cast changes' })
  ).toBeVisible();
  await expect(
    card.getByText(
      'Save or discard the cast changes before switching voice mode.'
    )
  ).toBeVisible();

  await hint.getByRole('button', { name: 'Review cast changes' }).click();
  await expect
    .poll(() =>
      disclosure.evaluate((node) => (node as HTMLDetailsElement).open)
    )
    .toBe(true);
  // A first voice check that is still running can add a setup link above the
  // Save button when it finishes. Let the cast settle before clicking.
  await expect(card.getByText('Checking voice setup…')).toHaveCount(0);
  await card
    .getByRole('button', { name: 'Save characters and cast', exact: true })
    .click();
  await expect(
    card.getByText('Characters and cast saved.', { exact: false })
  ).toBeVisible();
  await expect(summary).not.toContainText('unsaved changes');

  await summary.click();
  await expect
    .poll(() =>
      disclosure.evaluate((node) => (node as HTMLDetailsElement).open)
    )
    .toBe(false);
  await expect(card.getByTestId('cast-collapsed-hint')).toHaveCount(0);
  await expect(summary).toContainText('1 uses a default voice');
  await page.reload();
  await expect(summary).toContainText('1 uses a default voice');
  await expect(disclosure).toHaveAttribute('open');
  // Multiple-voice projects open the cast on each page; collapsing it is local.
  await page.goto(`/sessions/${id}/voice`);
  await expect(page.getByTestId('cast-disclosure')).toHaveAttribute('open');
  await page.goto(`/sessions/${id}`);
  await expect(disclosure).toHaveAttribute('open');
});

test('refreshing the cast does not restart settled voice checks', async ({
  page
}) => {
  await signIn(page);
  const id = await createAudiobookSession(page);
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const settingsUrl = `/api/v1/sessions/${id}/settings/tts`;
  const current = await (await page.request.get(settingsUrl)).json();
  const model = 'qwen3_tts_1_7b_customvoice_q8_0';
  const saved = await page.request.put(settingsUrl, {
    headers: {
      'X-CSRF-Token': auth.csrf_token,
      'If-Match': `"${current.revision}"`
    },
    data: {
      value: {
        service: 'audio_cpp',
        tts_service: 'audio_cpp',
        model,
        xtts_model: model,
        voice: 'Ryan',
        speaker: 'Ryan'
      }
    }
  });
  expect(saved.ok(), await saved.text()).toBeTruthy();
  const card = await openSessionMultiVoice(page, id);
  await expect(
    card.getByRole('button', { name: 'Choose Narrator voice', exact: true })
  ).toBeVisible();
  await expect(card.getByText('Checking voice setup…')).toHaveCount(0);
  // Record any return to the checking state: it collapses the voice cards and
  // moves the Save button while the follow-up session refresh lands.
  await page.evaluate(() => {
    const probe = window as Window & { voiceRechecks?: number };
    probe.voiceRechecks = 0;
    new MutationObserver(() => {
      if (
        Array.from(document.querySelectorAll('[role="status"]')).some((node) =>
          node.textContent?.includes('Checking voice setup')
        )
      )
        probe.voiceRechecks = (probe.voiceRechecks ?? 0) + 1;
    }).observe(document.body, {
      subtree: true,
      childList: true,
      characterData: true
    });
  });
  const responses = { outcome: 0, workflow: 0 };
  page.on('response', (response) => {
    const path = new URL(response.url()).pathname;
    if (path === `/api/v1/sessions/${id}/outcome-plan`) responses.outcome++;
    if (path === `/api/v1/sessions/${id}/workflow`) responses.workflow++;
  });
  const refresh = card.getByRole('button', {
    name: 'Refresh cast',
    exact: true
  });
  await refresh.click();
  // The cast load reports to the workspace, which refetches the outcome
  // twice (its own read and the session context reload) and the workflow.
  await expect
    .poll(() => responses.outcome >= 2 && responses.workflow >= 1)
    .toBe(true);
  await expect(refresh).toBeEnabled();
  await page.evaluate(
    () =>
      new Promise((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(resolve))
      )
  );
  expect(
    await page.evaluate(
      () => (window as Window & { voiceRechecks?: number }).voiceRechecks
    )
  ).toBe(0);
});

test('cast load errors stay visible while collapsed and retry succeeds', async ({
  page
}) => {
  await signIn(page);
  const id = await createAudiobookSession(page);
  const controls = `**/api/v1/sessions/${id}/generation-controls{,?*}`;
  await page.goto(`/sessions/${id}`);
  const card = page.getByRole('region', { name: 'Voices', exact: true });
  await expect(card).toBeVisible();
  const modeSaved = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname ===
        `/api/v1/sessions/${id}/voice-setup` &&
      response.request().method() === 'PATCH'
  );
  await card.getByRole('radio', { name: /Multiple voices/ }).check();
  expect((await modeSaved).ok()).toBeTruthy();
  await expect(card.getByTestId('cast-summary')).toContainText('0 characters');
  await page.route(controls, (route) =>
    route.fulfill({
      status: 500,
      json: {
        error: { code: 'cast_unavailable', message: 'Cast unavailable for now' }
      }
    })
  );
  await card.getByRole('button', { name: 'Refresh cast', exact: true }).click();
  const disclosure = card.getByTestId('cast-disclosure');
  await expect(disclosure).toHaveAttribute('open');
  await card.getByTestId('cast-summary').click();
  await expect
    .poll(() =>
      disclosure.evaluate((node) => (node as HTMLDetailsElement).open)
    )
    .toBe(false);
  const alert = card.getByTestId('cast-error');
  await expect(alert).toContainText('Cast unavailable for now');
  await expect(
    card.getByRole('button', { name: 'Retry loading cast', exact: true })
  ).toBeVisible();

  await page.unroute(controls);
  await card
    .getByRole('button', { name: 'Retry loading cast', exact: true })
    .click();
  await expect(alert).toHaveCount(0);
  await expect(card.getByTestId('cast-summary')).toContainText('0 characters');
  await expect
    .poll(() =>
      disclosure.evaluate((node) => (node as HTMLDetailsElement).open)
    )
    .toBe(false);
});

test('unsaved cast draft guards navigation and keeps warnings visible', async ({
  page
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page);
  const id = await createAudiobookSession(page);
  const card = await openSessionMultiVoice(page, id);
  await card.getByRole('button', { name: 'Add speaker', exact: true }).click();
  await card
    .locator('article')
    .first()
    .getByLabel('Identity notes')
    .fill('An unsaved disclosure change.');

  await page
    .getByRole('combobox', { name: 'Project section' })
    .selectOption(`/sessions/${id}/text`);
  const guard = page.getByRole('dialog', { name: 'Keep your cast changes?' });
  await expect(guard).toBeVisible();
  await guard.getByRole('button', { name: 'Keep editing' }).click();
  await expect(page).toHaveURL(`/sessions/${id}`);
  await expect(
    card.locator('article').first().getByLabel('Identity notes')
  ).toHaveValue('An unsaved disclosure change.');
  await expect(
    card.getByText(
      'Save or discard the cast changes before switching voice mode.'
    )
  ).toBeVisible();
});
