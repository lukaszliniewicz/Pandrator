import { expect, test, type Page } from '@playwright/test';
import { resolve } from 'node:path';

const managerStatic = resolve(
  process.cwd(),
  '..',
  'pandrator_manager/recovery_ui/static'
);

async function fixture(page: Page) {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  for (const [suffix, contentType] of [
    ['', 'text/html'],
    ['/styles.css', 'text/css'],
    ['/app.js', 'text/javascript'],
    ['/model-groups.js', 'text/javascript']
  ]) {
    await page.route(`**/recovery${suffix}`, (route) =>
      route.fulfill({
        contentType,
        path: resolve(managerStatic, suffix ? suffix.slice(1) : 'index.html')
      })
    );
  }
  const engine = {
    definition: {
      id: 'kokoro',
      label: 'Kokoro',
      description: 'A saved local speech engine.',
      guidance: '',
      section: 'text_to_speech',
      service_key: 'tts.kokoro',
      supported_actions: ['update', 'repair', 'remove'],
      compute_variants: ['cpu'],
      install_options: [],
      capabilities: [],
      models: [],
      languages: [],
      estimated_download_bytes: 0,
      estimated_installed_bytes: 0,
      size_provenance: 'unknown',
      size_note: ''
    },
    desired: { present: true, compute: 'cpu', options: {} },
    inspection: {
      component_id: 'kokoro',
      state: 'present',
      installed_revision: 'saved-revision',
      problems: [],
      evidence: [],
      resolved: { compute: 'cpu', platform: 'test', options: {} }
    },
    compute_choices: [{ value: 'cpu', label: 'CPU', available: true }]
  };
  const plan = {
    id: 'reviewed-kokoro-plan',
    digest: 'reviewed-kokoro-digest',
    kind: 'update',
    desired: { kokoro: engine.desired },
    tasks: [{ id: 'update-kokoro', label: 'Update Kokoro' }],
    confirmations: [
      { key: 'reviewed-confirmation', message: 'Keep the existing workspace.' }
    ],
    preflight: [],
    warnings: [],
    estimated_download_bytes: 0,
    estimated_disk_bytes: 0
  };
  const payloads: unknown[] = [];
  let rejectNext = true;
  let release: () => void = () => {};
  let gate = Promise.resolve();
  await page.route('**/v1/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    let body: unknown = {};
    if (path === '/v1/session')
      body = {
        csrf_token: 'manager-test-csrf',
        session: { remembered: false },
        policy: {}
      };
    else if (path === '/v1/status')
      body = {
        ready: true,
        manager_version: 'test',
        configuration_revision: 1
      };
    else if (path === '/v1/application')
      body = {
        installed: true,
        component_state: 'present',
        running: false,
        healthy: false
      };
    else if (path === '/v1/network')
      body = {
        application: {
          mode: 'local',
          port: 8097,
          owner_authentication_initialized: true
        },
        manager: {}
      };
    else if (path === '/v1/components') body = { items: [engine] };
    else if (path === '/v1/services') body = { items: [] };
    else if (path === '/v1/plans') body = plan;
    else if (path === '/v1/operations' && request.method() === 'POST') {
      payloads.push(request.postDataJSON());
      await gate;
      if (rejectNext) {
        rejectNext = false;
        await route.fulfill({
          status: 409,
          json: {
            error: {
              code: 'operation_conflict',
              message: 'The update could not be accepted yet.'
            }
          }
        });
        return;
      }
      body = { id: 'accepted-update', kind: 'update', state: 'queued' };
    } else if (
      ['/v1/operations', '/v1/activity', '/v1/releases'].includes(path)
    )
      body = { items: [] };
    await route.fulfill({ json: body });
  });
  await page.goto('/recovery');
  await expect(page.locator('#manager-health-text')).toHaveText(
    'Manager ready'
  );
  await page
    .locator('#provider-catalogue')
    .evaluate((element: HTMLDetailsElement) => {
      element.open = true;
    });
  const card = page.locator('[data-component-id="kokoro"]');
  await card.locator('summary').click();
  const review = card.getByRole('button', {
    name: 'Review update',
    exact: true
  });
  await review.click();
  const dialog = page.locator('#plan-dialog');
  await expect(dialog).toBeVisible();
  return {
    dialog,
    review,
    payloads,
    errors,
    hold() {
      gate = new Promise<void>((resolveGate) => {
        release = resolveGate;
      });
    },
    release() {
      release();
    }
  };
}

const expectedRequest = {
  plan_id: 'reviewed-kokoro-plan',
  plan_digest: 'reviewed-kokoro-digest',
  accepted_confirmations: ['reviewed-confirmation']
};

for (const width of [1280, 390]) {
  for (const dismissal of ['Escape', 'buttons']) {
    test(`pending Manager plan keeps its dialog during ${dismissal} dismissal at ${width}px`, async ({
      page
    }, info) => {
      await page.setViewportSize({ width, height: 950 });
      const f = await fixture(page);
      const confirm = f.dialog.getByRole('button', {
        name: 'Apply reviewed update'
      });
      f.hold();
      await confirm.click();
      await expect.poll(() => f.payloads.length).toBe(1);
      try {
        if (dismissal === 'Escape') {
          await page.keyboard.press('Escape');
          await expect(f.dialog).toBeVisible();
        } else {
          await expect(
            f.dialog.getByRole('button', { name: 'Close', exact: true })
          ).toBeDisabled();
          await expect(
            f.dialog.getByRole('button', { name: 'Go back', exact: true })
          ).toBeDisabled();
        }
        await expect(confirm).toBeDisabled();
        await expect(f.dialog).toHaveAttribute('aria-busy', 'true');
      } finally {
        f.release();
      }
      await expect(f.dialog.getByRole('alert')).toHaveText(
        'The update could not be accepted yet.'
      );
      await expect(f.dialog).toBeVisible();
      await expect(confirm).toBeEnabled();
      await expect(
        f.dialog.getByRole('button', { name: 'Go back', exact: true })
      ).toBeEnabled();
      await page.screenshot({
        path: info.outputPath('rejected-reviewed-plan.png'),
        fullPage: true
      });
      await confirm.click();
      await expect(f.dialog).not.toBeVisible();
      await expect(page.locator('#message')).toContainText('Plan accepted.');
      expect(f.payloads).toEqual([expectedRequest, expectedRequest]);
      expect(f.errors).toEqual([]);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth
        )
      ).toBe(true);
    });
  }
  test(`rejected Manager plan remains retryable and idle dismissal restores focus at ${width}px`, async ({
    page
  }, info) => {
    await page.setViewportSize({ width, height: 950 });
    const f = await fixture(page);
    await f.dialog
      .getByRole('button', { name: 'Apply reviewed update' })
      .click();
    await expect(f.dialog.getByRole('alert')).toHaveText(
      'The update could not be accepted yet.'
    );
    await expect(
      f.dialog.getByRole('button', { name: 'Apply reviewed update' })
    ).toBeEnabled();
    await page.screenshot({
      path: info.outputPath('idle-reviewed-plan.png'),
      fullPage: true
    });
    await page.keyboard.press('Escape');
    await expect(f.dialog).not.toBeVisible();
    await expect(f.review).toBeFocused();
    expect(f.payloads).toEqual([expectedRequest]);
    expect(f.errors).toEqual([]);
  });
}

for (const width of [1280, 390]) {
  test(`Manager authorization loss forcibly dismisses a pending plan at ${width}px`, async ({
    page
  }, info) => {
    await page.setViewportSize({ width, height: 950 });
    const f = await fixture(page);
    f.hold();
    await f.dialog
      .getByRole('button', { name: 'Apply reviewed update' })
      .click();
    await expect.poll(() => f.payloads.length).toBe(1);
    try {
      await page.route('**/v1/status', (route) =>
        route.fulfill({
          status: 401,
          json: {
            error: {
              code: 'authentication_required',
              message: 'This browser authorization expired.'
            }
          }
        })
      );
      await expect(page.locator('#manager-health-text')).toHaveText(
        'Authorization required'
      );
      await expect(f.dialog).not.toBeVisible();
      await expect(page.locator('#application-primary')).toBeDisabled();
    } finally {
      f.release();
    }
    // The rejected mutation must finish without restoring authorized controls.
    await expect(page.locator('#plan-error')).toHaveText(
      'The update could not be accepted yet.'
    );
    await expect(f.dialog).not.toBeVisible();
    await expect(page.locator('#application-primary')).toBeDisabled();
    await expect(page.locator('#manager-health-text')).toHaveText(
      'Authorization required'
    );
    await page.screenshot({
      path: info.outputPath('expired-pending-plan.png'),
      fullPage: true
    });
    expect(f.payloads).toEqual([expectedRequest]);
    expect(f.errors).toEqual([]);
  });
}
