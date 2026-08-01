import { expect, test, type Page, type Route } from '@playwright/test';
import { resolve } from 'node:path';

type RuntimeService = {
  id: string;
  component_id: string;
  desired_running: boolean;
  endpoint: string;
  process: { pid: number } | null;
  health: { state: string; service_id: string };
};

const managerStatic = resolve(
  process.cwd(),
  '..',
  'pandrator_manager',
  'recovery_ui',
  'static'
);

const definition = (id: string, label: string, serviceKey: string | null) => ({
  id,
  label,
  description:
    id === 'pandrator'
      ? 'The Pandrator browser application.'
      : 'Fast local text to speech.',
  guidance: '',
  section: id === 'pandrator' ? 'core' : 'text_to_speech',
  service_key: serviceKey,
  supported_actions: ['install', 'update', 'repair', 'remove', 'start', 'stop'],
  compute_variants: ['cpu'],
  install_options: [],
  capabilities: [],
  models: [],
  languages: [],
  estimated_download_bytes: 0,
  estimated_installed_bytes: 0,
  size_provenance: 'unknown',
  size_note: ''
});

const component = (id: string, label: string, serviceKey: string | null) => ({
  definition: definition(id, label, serviceKey),
  desired: { present: true, compute: 'cpu', options: {} },
  inspection: {
    component_id: id,
    state: 'present',
    problems: [],
    evidence: [],
    resolved: { compute: 'cpu', platform: 'test', options: {} }
  },
  compute_choices: [{ value: 'cpu', label: 'CPU', available: true }]
});

async function fulfillJson(route: Route, body: unknown) {
  await route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify(body)
  });
}

async function installManagerFixture(page: Page) {
  let service: RuntimeService = {
    id: 'tts.kokoro',
    component_id: 'kokoro',
    desired_running: true,
    endpoint: 'http://127.0.0.1:8880',
    process: { pid: 8123 },
    health: { state: 'healthy', service_id: 'tts.kokoro' }
  };
  const runtimeRequests: Array<{
    action: string;
    idempotencyKey: string | undefined;
  }> = [];

  await page.route('**/recovery', (route) =>
    route.fulfill({
      contentType: 'text/html',
      path: resolve(managerStatic, 'index.html')
    })
  );
  await page.route('**/recovery/styles.css', (route) =>
    route.fulfill({
      contentType: 'text/css',
      path: resolve(managerStatic, 'styles.css')
    })
  );
  await page.route('**/recovery/app.js', (route) =>
    route.fulfill({
      contentType: 'text/javascript',
      path: resolve(managerStatic, 'app.js')
    })
  );
  await page.route('**/v1/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/v1/session') {
      await fulfillJson(route, {
        csrf_token: 'manager-test-csrf',
        session: {
          remembered: false,
          expires_at: '2026-07-31T12:00:00Z',
          absolute_expires_at: '2026-07-31T12:00:00Z'
        },
        active_session_count: 1,
        policy: { remembered_idle_ttl_seconds: 86400 }
      });
    } else if (path === '/v1/status') {
      await fulfillJson(route, {
        ready: true,
        manager_version: 'test',
        configuration_revision: 1,
        active_operation_id: null
      });
    } else if (path === '/v1/application') {
      await fulfillJson(route, {
        installed: true,
        component_state: 'present',
        running: true,
        healthy: true
      });
    } else if (path === '/v1/network') {
      await fulfillJson(route, {
        application: {
          mode: 'local',
          bind_host: '127.0.0.1',
          port: 8097,
          remote_enabled: false,
          browser_url: 'http://127.0.0.1:8097',
          trusted_hosts: [],
          proxy_hops: 0,
          private_network_candidates: []
        },
        manager: {
          remote_enabled: false,
          browser_url: 'http://127.0.0.1:8098'
        }
      });
    } else if (path === '/v1/components') {
      await fulfillJson(route, {
        items: [
          component('pandrator', 'Pandrator', null),
          component('kokoro', 'Kokoro', 'tts.kokoro')
        ]
      });
    } else if (path === '/v1/services') {
      await fulfillJson(route, { items: [service] });
    } else if (path === '/v1/operations') {
      await fulfillJson(route, { items: [] });
    } else if (path === '/v1/activity') {
      await fulfillJson(route, { items: [] });
    } else if (path === '/v1/releases') {
      await fulfillJson(route, { items: [], current: {} });
    } else if (path.startsWith('/v1/runtime/')) {
      const action = path.split('/').at(-1) ?? '';
      runtimeRequests.push({
        action,
        idempotencyKey: request.headers()['idempotency-key']
      });
      service =
        action === 'stop'
          ? {
              ...service,
              desired_running: false,
              process: null,
              health: { ...service.health, state: 'stopped' }
            }
          : {
              ...service,
              desired_running: true,
              process: { pid: 8456 },
              health: { ...service.health, state: 'healthy' }
            };
      await fulfillJson(route, { items: [service] });
    } else {
      await fulfillJson(route, {});
    }
  });

  return { runtimeRequests };
}

async function signIn(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const closeTour = page.getByRole('button', { name: 'Close tour' });
  if (await closeTour.isVisible()) await closeTour.click();
}

test('manager shows and controls a running optional engine', async ({
  page
}) => {
  const fixture = await installManagerFixture(page);
  await page.goto('/recovery');

  await page.getByText('Text to speech', { exact: true }).click();
  const card = page.locator('[data-component-id="kokoro"]');
  await expect(card.getByText('Running', { exact: true })).toBeVisible();
  await card.getByRole('button', { name: 'Stop Kokoro' }).click();

  await expect(card.getByText('Stopped', { exact: true })).toBeVisible();
  await expect(
    card.getByRole('button', { name: 'Start Kokoro' })
  ).toBeVisible();
  expect(fixture.runtimeRequests).toHaveLength(1);
  expect(fixture.runtimeRequests[0].action).toBe('stop');
  expect(fixture.runtimeRequests[0].idempotencyKey).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
  );

  await page.getByRole('tab', { name: 'Activity' }).click();
  const serviceRow = page.locator('.service-row').filter({ hasText: 'Kokoro' });
  await expect(serviceRow.getByText('Stopped', { exact: true })).toBeVisible();
  await expect(
    serviceRow.getByRole('button', { name: 'Start Kokoro' })
  ).toBeVisible();
});

test('Pandrator refreshes and controls the same engine state', async ({
  page
}) => {
  let service: RuntimeService = {
    id: 'tts.kokoro',
    component_id: 'kokoro',
    desired_running: true,
    endpoint: 'http://127.0.0.1:8880',
    process: { pid: 8123 },
    health: { state: 'healthy', service_id: 'tts.kokoro' }
  };
  const runtimeRequests: Array<{
    action: string;
    idempotencyKey: string | undefined;
  }> = [];
  await page.route('**/api/v1/manager/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/v1/manager/status') {
      await fulfillJson(route, {
        available: true,
        configured: true,
        status: {
          configuration_revision: 1,
          active_operation_id: null
        }
      });
    } else if (path === '/api/v1/manager/components') {
      await fulfillJson(route, {
        items: [component('kokoro', 'Kokoro', 'tts.kokoro')]
      });
    } else if (path === '/api/v1/manager/services') {
      await fulfillJson(route, { items: [service] });
    } else if (path === '/api/v1/manager/releases') {
      await fulfillJson(route, { current: {}, items: [] });
    } else if (path.startsWith('/api/v1/manager/runtime/')) {
      const action = path.split('/').at(-1) ?? '';
      runtimeRequests.push({
        action,
        idempotencyKey: request.headers()['idempotency-key']
      });
      service = {
        ...service,
        desired_running: false,
        process: null,
        health: { ...service.health, state: 'stopped' }
      };
      await fulfillJson(route, { items: [service] });
    } else {
      await fulfillJson(route, {});
    }
  });

  await signIn(page);
  await page.goto('/providers?tab=local');
  const card = page.locator('#component-kokoro');
  await expect(card.getByText('Running', { exact: true })).toBeVisible();
  await card.getByRole('button', { name: 'Stop' }).click();

  await expect(card.getByText('Stopped', { exact: true })).toBeVisible();
  await expect(card.getByRole('button', { name: 'Start' })).toBeVisible();
  expect(runtimeRequests).toHaveLength(1);
  expect(runtimeRequests[0].action).toBe('stop');
  expect(runtimeRequests[0].idempotencyKey).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
  );
});

test('Pandrator sends selected Qwen and Fish install options to the manager', async ({
  page
}) => {
  const qwen = component('qwen3_tts', 'Qwen3-TTS', 'tts.qwen3');
  qwen.definition.install_options = [
    {
      key: 'initial_model',
      label: 'Initial voice mode',
      state_field: 'options',
      default: 'base',
      choices: [
        { value: 'base', label: 'Prebuilt voices' },
        { value: 'customvoice', label: 'Voice cloning' }
      ]
    },
    {
      key: 'model_size',
      label: 'Model size',
      state_field: 'options',
      default: '0.6b',
      choices: [
        { value: '0.6b', label: '0.6B' },
        { value: '1.7b', label: '1.7B' }
      ]
    },
    {
      key: 'precision',
      label: 'Precision',
      state_field: 'quantization',
      default: 'q8_0',
      choices: [
        { value: 'q8_0', label: 'Q8' },
        { value: 'f16', label: 'F16' }
      ]
    }
  ];
  qwen.desired.options = { initial_model: 'base', model_size: '0.6b' };
  qwen.desired.quantization = 'q8_0';
  qwen.inspection.resolved.options = {
    initial_model: 'base',
    model_size: '0.6b'
  };
  qwen.inspection.resolved.quantization = 'q8_0';

  const fish = component('fish_s2', 'Fish Speech S2 Pro', 'tts.fish_s2');
  fish.definition.install_options = [
    {
      key: 'precision',
      label: 'Model quantization',
      state_field: 'quantization',
      default: 'q8_0',
      choices: [
        { value: 'q8_0', label: 'Q8' },
        { value: 'q4_k_m', label: 'Q4 K M' }
      ]
    }
  ];
  fish.desired.present = false;
  fish.inspection.state = 'absent';

  const planRequests: Array<Record<string, unknown>> = [];
  await page.route('**/api/v1/manager/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/v1/manager/status') {
      await fulfillJson(route, {
        available: true,
        configured: true,
        status: { configuration_revision: 7, active_operation_id: null }
      });
    } else if (path === '/api/v1/manager/components') {
      await fulfillJson(route, { items: [qwen, fish] });
    } else if (path === '/api/v1/manager/services') {
      await fulfillJson(route, { items: [] });
    } else if (path === '/api/v1/manager/releases') {
      await fulfillJson(route, { current: {}, items: [] });
    } else if (path === '/api/v1/manager/plans') {
      planRequests.push(request.postDataJSON() as Record<string, unknown>);
      await fulfillJson(route, {
        id: `plan-${planRequests.length}`,
        digest: `digest-${planRequests.length}`,
        kind: planRequests.length === 1 ? 'update' : 'install',
        tasks: [],
        warnings: [],
        confirmations: [],
        estimated_download_bytes: 0,
        estimated_disk_bytes: 0
      });
    } else {
      await fulfillJson(route, {});
    }
  });

  await signIn(page);
  await page.goto('/providers?tab=local');

  const qwenCard = page.locator('#component-qwen3_tts');
  await qwenCard.getByLabel('Initial voice mode').selectOption('customvoice');
  await qwenCard.getByLabel('Model size').selectOption('1.7b');
  await qwenCard.getByLabel('Precision').selectOption('f16');
  await qwenCard.getByRole('button', { name: 'Apply configuration' }).click();
  await expect.poll(() => planRequests.length).toBe(1);
  expect(planRequests[0]).toMatchObject({
    kind: 'update',
    desired: {
      qwen3_tts: {
        present: true,
        compute: 'cpu',
        quantization: 'f16',
        options: { initial_model: 'customvoice', model_size: '1.7b' }
      }
    },
    expected_revision: 7
  });
  await page.getByRole('button', { name: 'Close plan' }).click();

  const fishCard = page.locator('#component-fish_s2');
  await fishCard.getByLabel('Model quantization').selectOption('q4_k_m');
  await fishCard.getByRole('button', { name: 'Install locally' }).click();
  await expect.poll(() => planRequests.length).toBe(2);
  expect(planRequests[1]).toMatchObject({
    kind: 'install',
    desired: {
      fish_s2: {
        present: true,
        compute: 'cpu',
        quantization: 'q4_k_m',
        options: {}
      }
    },
    expected_revision: 7
  });
});
