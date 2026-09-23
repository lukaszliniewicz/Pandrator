import { expect, test, type Page } from '@playwright/test';

async function signIn(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const closeTour = page.getByRole('button', { name: 'Close tour' });
  if (await closeTour.isVisible()) await closeTour.click();
}

async function createSession(page: Page, workflowKind: string) {
  const authStatus = await page.request.get('/api/v1/auth/status');
  const csrfToken = (await authStatus.json()).csrf_token;
  const response = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': csrfToken },
    data: {
      name: `Session view ${crypto.randomUUID()}`,
      workflow_kind: workflowKind
    }
  });
  expect(response.ok()).toBeTruthy();
  return response.json();
}

test('audiobook rows expose compact options and merge adjacent segments', async ({
  page
}, info) => {
  await signIn(page);
  const session = await createSession(page, 'audiobook');
  const { csrf_token } = await (
    await page.request.get('/api/v1/auth/status')
  ).json();
  const plan = await page.request.post(
    `/api/v1/sessions/${session.id}/generation-plan`,
    {
      headers: { 'X-CSRF-Token': csrf_token },
      data: {
        segments: [{ text: 'First passage.' }, { text: 'Second passage.' }]
      }
    }
  );
  expect(plan.ok()).toBeTruthy();
  await page.goto(`/sessions/${session.id}`);
  await page.getByRole('button', { name: 'Generation', exact: true }).click();
  const table = page.getByTestId('generation-segment-table');
  await expect(table.locator('tbody tr[data-segment-id]')).toHaveCount(2);
  await expect(table.locator('.boundary-row')).toHaveCount(0);
  await expect(
    page.locator('.session-shell > a[href="/sessions"]')
  ).toHaveCount(0);
  await expect(
    page.getByRole('button', { name: 'Show waveform', exact: true })
  ).toHaveCount(0);
  const row = table.locator('tbody tr[data-segment-id]').first();
  const alignment = await row.evaluate((node) => {
    const check = node.querySelector('input')!.getBoundingClientRect();
    const number = node.querySelector('td:nth-child(2)')!;
    const range = document.createRange();
    range.selectNodeContents(number);
    const box = range.getBoundingClientRect();
    return Math.abs(check.top + check.height / 2 - (box.top + box.height / 2));
  });
  expect(alignment).toBeLessThan(5);
  await page
    .getByRole('button', { name: 'Options for segment 1', exact: true })
    .click();
  const options = page.getByRole('dialog', {
    name: 'Options for segment 1',
    exact: true
  });
  await expect(
    options.getByRole('combobox', { name: 'Segment role', exact: true })
  ).toBeVisible();
  await page.screenshot({
    path: info.outputPath('compact-segment-options.png')
  });
  await page.keyboard.press('Escape');
  await expect(options).toBeHidden();
  await page
    .getByRole('button', { name: 'Merge segment 1 with next', exact: true })
    .click();
  await expect(table.locator('tbody tr[data-segment-id]')).toHaveCount(1);
  await expect(
    table.getByRole('button', {
      name: 'Narrator. Inspect voice and delivery for segment 1, phrase 1',
      exact: true
    })
  ).toHaveText('First passage. Second passage.');
});

test('session tabs sit above the title and stay sticky', async ({ page }) => {
  await signIn(page);
  const session = await createSession(page, 'voiceover');
  await page.goto(`/sessions/${session.id}`);
  const nav = page.getByRole('navigation', { name: 'Session sections' });
  await expect(nav).toBeVisible();
  await expect(nav.getByRole('link', { name: 'Sources' })).toBeVisible();
  await expect(nav.getByRole('link', { name: 'Overview' })).toHaveAttribute(
    'aria-current',
    'page'
  );

  const order = await page.evaluate(() => {
    const navEl = document.querySelector('nav[aria-label="Session sections"]');
    const title = document.querySelector('.session-shell h1');
    const navBox = navEl?.getBoundingClientRect();
    const titleBox = title?.getBoundingClientRect();
    const style = navEl ? getComputedStyle(navEl) : null;
    return {
      navTop: navBox?.top ?? -1,
      titleTop: titleBox?.top ?? -1,
      position: style?.position ?? '',
      top: style?.top ?? '',
      overflowX: style?.overflowX ?? ''
    };
  });
  expect(order.titleTop).toBeGreaterThan(0);
  expect(order.navTop).toBeLessThan(order.titleTop);
  expect(order.position).toBe('sticky');
  expect(order.top).toBe('0px');
  expect(order.overflowX).toBe('auto');

  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  await page.waitForTimeout(150);
  const stuckTop = await nav.evaluate((el) => el.getBoundingClientRect().top);
  expect(stuckTop).toBeLessThanOrEqual(1);
  // Desktop: the global hamburger stays in the DOM (md:hidden), so assert
  // hidden rather than absent.
  await expect(
    page.getByRole('button', { name: 'Open navigation' })
  ).toBeHidden();

  await page.setViewportSize({ width: 390, height: 800 });
  await page.reload();
  await expect(nav).toBeHidden();
  const header = page.locator('.mobile-app-header');
  await expect(header).toBeVisible();
  const section = header.getByRole('combobox', { name: 'Session section' });
  await expect(section).toHaveValue(`/sessions/${session.id}`);
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  const geometry = await header.evaluate((el) => ({
    top: el.getBoundingClientRect().top,
    height: el.getBoundingClientRect().height,
    overflow: document.documentElement.scrollWidth - window.innerWidth
  }));
  expect(geometry.top).toBe(0);
  expect(geometry.height).toBe(64);
  expect(geometry.overflow).toBeLessThanOrEqual(1);
  await section.selectOption(`/sessions/${session.id}/sources`);
  await expect(page).toHaveURL(`/sessions/${session.id}/sources`);
});

test('sources tab exposes copyable filesystem paths', async ({ page }) => {
  await signIn(page);
  const session = await createSession(page, 'voiceover');
  // Chromium honors granted clipboard permissions; Firefox headless may not,
  // so only the Chromium leg asserts the real clipboard contents below.
  if (test.info().project.name === 'chromium') {
    await page
      .context()
      .grantPermissions(['clipboard-read', 'clipboard-write']);
  }
  const managedPath = '/managed/sources/upload-only.srt';
  const externalPath = '/data/media/episode-7.wav';
  const unicodePath = '/data/media/München – tëst 🎙 episode-7.wav';
  const longPath = `/data/media/${'a'.repeat(200)}.wav`;
  const base = {
    kind: 'wav',
    mime_type: 'audio/wav',
    size_bytes: 1024,
    content_hash: 'abc',
    state: 'current',
    revision: 1,
    reference_count: 1,
    current_reference_count: 1,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString()
  };
  const attachment = {
    id: 'attachment-1',
    role: 'primary',
    is_current: true,
    revision: 1
  };
  await page.route(
    `**/api/v1/sessions/${session.id}/sources`,
    async (route) => {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          items: [
            {
              ...base,
              id: 'source-managed-upload',
              artifact_id: '',
              display_name: 'upload-only.srt',
              kind: 'srt',
              // Managed upload: no original location, only the managed file.
              path: managedPath,
              external_path: null,
              attachment
            },
            {
              ...base,
              id: 'source-external-only',
              artifact_id: '',
              display_name: 'episode-7.wav',
              external_path: externalPath,
              attachment
            },
            {
              ...base,
              id: 'source-both-paths',
              artifact_id: '',
              display_name: 'both-paths.wav',
              path: '/managed/sources/both-paths.wav',
              external_path: externalPath,
              attachment
            },
            {
              ...base,
              id: 'source-unicode',
              artifact_id: '',
              display_name: 'unicode-name.wav',
              path: unicodePath,
              external_path: null,
              attachment
            },
            {
              ...base,
              id: 'source-long-path',
              artifact_id: '',
              display_name: 'long-path.wav',
              path: longPath,
              external_path: null,
              attachment
            },
            {
              ...base,
              id: 'source-no-path',
              artifact_id: '',
              display_name: 'no-path.srt',
              kind: 'srt',
              path: null,
              external_path: null,
              attachment
            }
          ]
        })
      });
    }
  );
  await page.goto(`/sessions/${session.id}/sources`);

  // Managed upload without an external path still offers its managed file.
  await expect(page.getByText(managedPath)).toBeVisible();
  await page
    .getByRole('button', { name: 'Copy source path for upload-only.srt' })
    .click();
  await expect(
    page.getByText('Source path copied for upload-only.srt.')
  ).toBeVisible();
  if (test.info().project.name === 'chromium') {
    const clipboard = await page.evaluate(() => navigator.clipboard.readText());
    expect(clipboard).toBe(managedPath);
  }

  // External-only source falls back to the original location.
  await expect(page.getByText(externalPath).first()).toBeVisible();
  await page
    .getByRole('button', { name: 'Copy source path for episode-7.wav' })
    .click();
  await expect(
    page.getByText('Source path copied for episode-7.wav.')
  ).toBeVisible();
  if (test.info().project.name === 'chromium') {
    const clipboard = await page.evaluate(() => navigator.clipboard.readText());
    expect(clipboard).toBe(externalPath);
  }

  // Both paths: the managed file is copied, the original is labeled.
  await expect(page.getByText('/managed/sources/both-paths.wav')).toBeVisible();
  await expect(page.getByText(`Original file: ${externalPath}`)).toBeVisible();
  await page
    .getByRole('button', { name: 'Copy source path for both-paths.wav' })
    .click();
  if (test.info().project.name === 'chromium') {
    const clipboard = await page.evaluate(() => navigator.clipboard.readText());
    expect(clipboard).toBe('/managed/sources/both-paths.wav');
  }

  // Unicode and very long paths render without breaking the page layout.
  await expect(page.getByText(unicodePath)).toBeVisible();
  await expect(page.getByText(longPath)).toBeVisible();
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth
  );
  expect(overflow).toBeLessThanOrEqual(1);

  // A source with neither path offers no copy control.
  await expect(
    page.getByRole('button', { name: 'Copy source path for no-path.srt' })
  ).toHaveCount(0);
});

test('source copy reports clipboard denial and cleans up the fallback field', async ({
  page
}) => {
  await signIn(page);
  const session = await createSession(page, 'voiceover');
  await page.route(
    `**/api/v1/sessions/${session.id}/sources`,
    async (route) => {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          items: [
            {
              id: 'source-denied',
              artifact_id: '',
              display_name: 'denied.wav',
              kind: 'wav',
              mime_type: 'audio/wav',
              path: '/managed/sources/denied.wav',
              external_path: null,
              size_bytes: 1024,
              content_hash: 'abc',
              state: 'current',
              revision: 1,
              reference_count: 1,
              current_reference_count: 1,
              created_at: new Date().toISOString(),
              updated_at: new Date().toISOString(),
              attachment: {
                id: 'attachment-9',
                role: 'primary',
                is_current: true,
                revision: 1
              }
            }
          ]
        })
      });
    }
  );
  // Remove the async clipboard API so the execCommand fallback runs, with its
  // result driven by a window flag for a deterministic denied/allowed split.
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', {
      value: undefined,
      configurable: true
    });
    (window as unknown as { __execResult: boolean }).__execResult = false;
    document.execCommand = () =>
      (window as unknown as { __execResult: boolean }).__execResult;
  });
  await page.goto(`/sessions/${session.id}/sources`);
  const copyButton = page.getByRole('button', {
    name: 'Copy source path for denied.wav'
  });
  await expect(copyButton).toBeVisible();

  // execCommand reports failure: no success toast, error shown instead.
  await copyButton.click();
  await expect(
    page.getByText('Clipboard copy was rejected by the browser.')
  ).toBeVisible();
  await expect(
    page.getByText('Source path copied for denied.wav.')
  ).toHaveCount(0);

  // execCommand succeeds: success toast appears and the helper cleans up.
  await page.evaluate(() => {
    (window as unknown as { __execResult: boolean }).__execResult = true;
  });
  await copyButton.click();
  await expect(
    page.getByText('Source path copied for denied.wav.')
  ).toBeVisible();
  const strayFields = await page.evaluate(
    () => document.querySelectorAll('textarea').length
  );
  expect(strayFields).toBe(0);
});

test('intermediate assemblies can be removed without touching final exports', async ({
  page
}) => {
  await signIn(page);
  const session = await createSession(page, 'voiceover');
  let assemblyDeleted = false;
  const exportArtifact = {
    id: 'final-export',
    session_id: session.id,
    kind: 'export',
    role: 'export',
    relative_path: 'exports/final-voiceover.mp4',
    path: '/managed/exports/final-voiceover.mp4',
    mime_type: 'video/mp4',
    size_bytes: 98765,
    content_hash: 'export-hash',
    state: 'current',
    metadata_json: {},
    created_at: new Date().toISOString()
  };
  const assemblyArtifact = {
    id: 'assembly-mix',
    session_id: session.id,
    kind: 'audio',
    role: 'assembled_audio',
    relative_path: 'assemblies/mix.wav',
    path: '/managed/assemblies/mix.wav',
    mime_type: 'audio/wav',
    size_bytes: 43210,
    content_hash: 'assembly-hash',
    state: 'current',
    metadata_json: {},
    created_at: new Date().toISOString()
  };
  await page.route('**/api/v1/jobs?limit=500', (route) =>
    route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ items: [] })
    })
  );
  await page.route(
    `**/api/v1/sessions/${session.id}/generation-runs`,
    (route) =>
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ items: [] })
      })
  );
  await page.route('**/api/v1/artifacts?**', async (route) => {
    const url = new URL(route.request().url());
    if (
      url.searchParams.get('session_id') !== session.id ||
      url.searchParams.get('output_only') !== 'true'
    ) {
      await route.fallback();
      return;
    }
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        items: assemblyDeleted
          ? [exportArtifact]
          : [exportArtifact, assemblyArtifact]
      })
    });
  });
  const deletedIds: string[] = [];
  await page.route(
    `**/api/v1/sessions/${session.id}/outputs/*`,
    async (route) => {
      if (route.request().method() !== 'DELETE') {
        await route.fallback();
        return;
      }
      const artifactId = route.request().url().split('/').at(-1) ?? '';
      deletedIds.push(artifactId);
      assemblyDeleted = true;
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ deleted: true })
      });
    }
  );
  await page.goto(`/sessions/${session.id}/output`);
  await expect(
    page.getByText('Completed audio and video exports')
  ).toBeVisible();
  await expect(page.getByText('Intermediate audio assemblies')).toBeVisible();
  // Final usable exports come before intermediate assemblies.
  const completedSection = page.locator('section', {
    hasText: 'Completed outputs'
  });
  await expect(completedSection.locator('h3')).toHaveText([
    'Completed audio and video exports',
    'Intermediate audio assemblies'
  ]);
  await expect(
    page.getByRole('link', { name: 'Download final-voiceover.mp4' })
  ).toBeVisible();

  page.once('dialog', (dialog) => dialog.accept());
  await page.getByRole('button', { name: /Remove assembly/ }).click();
  await expect(page.getByText('Assembly removed.')).toBeVisible();
  expect(deletedIds).toEqual(['assembly-mix']);
  await expect(
    page.getByRole('button', { name: /Remove assembly/ })
  ).toHaveCount(0);
  await expect(
    page.getByRole('link', { name: 'Download final-voiceover.mp4' })
  ).toBeVisible();

  await page.reload();
  await expect(page.getByText('Intermediate audio assemblies')).toHaveCount(0);
  await expect(
    page.getByRole('link', { name: 'Download final-voiceover.mp4' })
  ).toBeVisible();
});
