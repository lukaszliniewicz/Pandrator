import { expect, test, type Page, type Route } from '@playwright/test';
import type { GenerationRun, GenerationSegment } from '../src/lib/api-models';

async function fixture(page: Page) {
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
      name: `Alternate draft ${crypto.randomUUID()}`,
      workflow_kind: 'audiobook'
    }
  });
  expect(response.ok()).toBeTruthy();
  const sessionId = (await response.json()).id as string;
  const endpoint = `/api/v1/sessions/${sessionId}`;
  const plan = await page.request.post(`${endpoint}/generation-plan`, {
    headers,
    data: { segments: [{ text: 'Controlled narration.' }] }
  });
  expect(plan.ok()).toBeTruthy();
  const settings = (model: string) => ({
    service: 'chatterbox',
    tts_service: 'chatterbox',
    model,
    voice: `${model}-en`,
    speaker: `${model}-en`,
    language: 'en',
    target_language: 'en',
    generation_prompt: `${model} instructions`,
    chatterbox_exaggeration: 0.75,
    chatterbox_cfg_weight: 0.65
  });
  const root: GenerationRun = {
    id: 'root',
    session_id: sessionId,
    plan_revision_id: 'original-plan',
    sequence_number: 1,
    label: 'Original narration',
    status: 'completed',
    progress: 1,
    operation: 'generate',
    settings_snapshot: {
      tts: settings('history-model'),
      rvc: {
        enabled: true,
        model: 'history-rvc',
        pitch: 3,
        f0_method: 'harvest',
        index_rate: 0.6
      }
    },
    timing_repair: {
      kind: 'repair',
      result_generation_run_id: 'repair',
      result_plan_revision_id: 'repair-plan',
      result_sequence_number: 2,
      applied_count: 1,
      attempt_count: 1,
      status: 'completed',
      versions: [
        {
          generation_run_id: 'repair',
          plan_revision_id: 'repair-plan',
          sequence_number: 2,
          status: 'completed',
          repair_status: 'applied'
        }
      ]
    }
  };
  const repair: GenerationRun = {
    ...root,
    id: 'repair',
    plan_revision_id: 'repair-plan',
    sequence_number: 2,
    early_repair_parent_run_id: 'root',
    label: 'Repaired narration',
    timing_repair: undefined,
    settings_snapshot: {
      tts: settings('repair-model'),
      rvc: { enabled: false }
    }
  };
  let activeId = 'segment-active';
  const segment = (id: string): GenerationSegment => ({
    id,
    plan_revision_id: id === 'segment-edited' ? 'edited-plan' : 'active-plan',
    ordinal: 0,
    revision: 1,
    text: 'Controlled narration.',
    status: 'ready',
    node_kind: 'paragraph',
    paragraph_break_after: false,
    marked: false,
    removed: false,
    speech_plan: {},
    source_segment_ids: [0],
    takes: []
  });
  let settingsRequests = 0;
  await page.route(`**${endpoint}/settings/tts`, (route) => {
    settingsRequests++;
    return route.fulfill({ json: { effective: settings('current-model') } });
  });
  await page.route(/\/api\/v1\/services\/tts(?:\?.*)?$/, (route) =>
    route.fulfill({
      json: {
        default_service: 'chatterbox',
        services: [
          {
            id: 'chatterbox',
            name: 'Chatterbox fixture',
            online: true,
            available: true,
            models: ['current-model', 'history-model', 'repair-model'],
            default_model: 'current-model',
            voice_catalogues: Object.fromEntries(
              ['current-model', 'history-model', 'repair-model'].map(
                (model) => [model, [`${model}-en`, `${model}-pl`]]
              )
            ),
            default_voices_by_language: Object.fromEntries(
              ['current-model', 'history-model', 'repair-model'].map(
                (model) => [model, { en: `${model}-en`, pl: `${model}-pl` }]
              )
            ),
            voice_metadata: Object.fromEntries(
              ['current-model', 'history-model', 'repair-model'].flatMap(
                (model) =>
                  ['en', 'pl'].map((language) => [
                    `${model}:${model}-${language}`,
                    { language, locale: language, name: `${model} ${language}` }
                  ])
              )
            ),
            model_catalog: [
              'current-model',
              'history-model',
              'repair-model'
            ].map((id) => ({
              id,
              language_support: { coverage: 'exact', languages: ['en', 'pl'] }
            }))
          }
        ]
      }
    })
  );
  await page.route('**/api/v1/voices', (route) =>
    route.fulfill({ json: { items: [] } })
  );
  await page.route('**/api/v1/rvc/models', (route) =>
    route.fulfill({ json: { items: ['history-rvc', 'draft-rvc'] } })
  );
  const posts: Record<string, unknown>[] = [];
  const keys: string[] = [];
  const acceptedKeys = new Set<string>();
  let loseAcceptedResponse = false;
  let submissionFailure = false;
  let holdSubmission = false;
  let heldSubmission: Route | undefined;
  let refreshFailure = false;
  const finishSubmission = (route: Route) =>
    route.fulfill(
      submissionFailure
        ? {
            status: 409,
            json: { error: { message: 'Controlled generation conflict' } }
          }
        : {
            json: {
              ...root,
              id: 'queued',
              status: 'queued',
              timing_repair: undefined
            }
          }
    );
  await page.route(`**${endpoint}/generation-runs`, (route) => {
    if (route.request().method() === 'POST') {
      posts.push(route.request().postDataJSON());
      const key = route.request().headers()['idempotency-key'];
      keys.push(key);
      if (loseAcceptedResponse) {
        acceptedKeys.add(key);
        if (posts.length === 1) return route.abort('failed');
      }
      if (holdSubmission) {
        heldSubmission = route;
        return;
      }
      return finishSubmission(route);
    }
    return route.fulfill(
      refreshFailure && posts.length
        ? {
            status: 503,
            json: { error: { message: 'Controlled refresh failure' } }
          }
        : { json: { items: [repair, root] } }
    );
  });
  await page.route(`**${endpoint}/generation-segments?*`, (route) => {
    const runId = new URL(route.request().url()).searchParams.get(
      'generation_run_id'
    );
    return route.fulfill({
      json: {
        items: [segment(runId ? `segment-${runId}` : activeId)],
        total: 1,
        next_cursor: null,
        plan_revision_id: runId
          ? `${runId}-plan`
          : activeId === 'segment-edited'
            ? 'edited-plan'
            : 'active-plan'
      }
    });
  });
  let heldSave: Route | undefined;
  await page.route('**/api/v1/generation-segments/segment-*', (route) => {
    heldSave = route;
  });
  await page.goto(`/sessions/${sessionId}`);
  await page.getByRole('button', { name: 'Generation', exact: true }).click();
  await expect(
    page.getByRole('textbox', {
      name: 'Script text for segment 1',
      exact: true
    })
  ).toBeVisible();
  const dialog = page.getByRole('dialog', {
    name: 'Regenerate 1 selected segment with…',
    exact: true
  });
  async function open() {
    await page
      .getByRole('button', { name: 'Regenerate segment 1', exact: true })
      .click();
    await page
      .getByRole('menuitem', {
        name: 'Regenerate with different settings…',
        exact: true
      })
      .click();
    await expect(dialog).toBeVisible();
    await expect(
      dialog.getByRole('combobox', { name: 'Speech service', exact: true })
    ).toHaveValue('chatterbox');
  }
  return {
    posts,
    keys,
    acceptedKeys,
    loseAcceptedResponse() {
      loseAcceptedResponse = true;
    },
    dialog,
    open,
    settingsRequests: () => settingsRequests,
    failSubmission(value: boolean) {
      submissionFailure = value;
    },
    holdSubmission() {
      holdSubmission = true;
    },
    failRefresh() {
      refreshFailure = true;
    },
    async releaseSubmission() {
      if (!heldSubmission) return;
      const route = heldSubmission;
      heldSubmission = undefined;
      await finishSubmission(route);
    },
    async waitForSave() {
      await expect.poll(() => Boolean(heldSave)).toBe(true);
    },
    async releaseSave(fail = false) {
      if (!heldSave) return;
      const route = heldSave;
      heldSave = undefined;
      if (fail)
        await route.fulfill({
          status: 409,
          json: { error: { message: 'Controlled edit conflict' } }
        });
      else {
        activeId = 'segment-edited';
        await route.fulfill({
          json: {
            ...segment(activeId),
            previous_segment_id: new URL(route.request().url()).pathname
              .split('/')
              .at(-1),
            revision: 2,
            text: 'Edited narration.'
          }
        });
      }
    }
  };
}

const submit = (page: Page) =>
  page.getByRole('button', { name: 'Create alternate take', exact: true });

test('alternate settings preserve project defaults and submit compatible model, language and RVC choices', async ({
  page
}, info) => {
  const data = await fixture(page);
  await data.open();
  const { dialog } = data;
  await expect(
    dialog.getByRole('combobox', { name: 'Model', exact: true })
  ).toHaveValue('current-model');
  await expect(
    dialog.getByRole('textbox', {
      name: 'Generation prompt / instructions',
      exact: true
    })
  ).toHaveValue('current-model instructions');
  await dialog
    .getByRole('combobox', { name: 'Model', exact: true })
    .selectOption('history-model');
  await expect(
    dialog.getByRole('combobox', {
      name: /^Voice \/ managed reference/
    })
  ).toHaveValue('history-model-en');
  await dialog
    .getByRole('combobox', { name: 'Speech language', exact: true })
    .selectOption('pl');
  await expect(
    dialog.getByRole('combobox', {
      name: /^Voice \/ managed reference/
    })
  ).toHaveValue('history-model-pl');
  await dialog
    .getByRole('textbox', {
      name: 'Generation prompt / instructions',
      exact: true
    })
    .fill('Custom narration instructions');
  await dialog
    .getByRole('checkbox', { name: 'Convert the new take with RVC' })
    .check();
  await dialog
    .getByRole('combobox', { name: 'RVC model' })
    .selectOption('draft-rvc');
  await dialog
    .getByRole('spinbutton', { name: 'Pitch', exact: true })
    .fill('-2');
  await dialog
    .getByRole('spinbutton', { name: 'Index rate', exact: true })
    .fill('0.45');
  await page.screenshot({ path: info.outputPath('alternate-desktop.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  const box = await dialog.boundingBox();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(390);
  await submit(page).scrollIntoViewIfNeeded();
  await expect(submit(page)).toBeInViewport();
  await page.screenshot({ path: info.outputPath('alternate-narrow.png') });
  await submit(page).click();
  await expect.poll(() => data.posts.length).toBe(1);
  expect(data.posts[0]).toEqual({
    operation: 'regenerate',
    segment_ids: ['segment-active'],
    generation_run_id: null,
    run_override: {},
    speech_plan_revision_id: 'active-plan',
    stale_only: false,
    settings_source_run_id: null,
    selected_segment_override: {
      tts: {
        service: 'chatterbox',
        tts_service: 'chatterbox',
        model: 'history-model',
        voice: 'history-model-pl',
        speaker: 'history-model-pl',
        language: 'pl',
        target_language: 'pl',
        generation_prompt: 'Custom narration instructions',
        chatterbox_exaggeration: 0.75,
        chatterbox_cfg_weight: 0.65
      },
      rvc: {
        enabled: true,
        model: 'draft-rvc',
        rvc_model: 'draft-rvc',
        pitch: -2,
        f0_method: 'rmvpe',
        index_rate: 0.45
      }
    }
  });
  await expect(dialog).toHaveCount(0);
});

test('alternate drafts use selected repair and original snapshots and reset after cancellation', async ({
  page
}) => {
  const data = await fixture(page);
  const picker = page.getByRole('combobox', {
    name: 'Audio view',
    exact: true
  });
  await picker.selectOption('root');
  await expect(page.locator('tbody tr[data-segment-id]')).toHaveAttribute(
    'data-segment-id',
    'segment-repair'
  );
  await data.open();
  await expect(
    data.dialog.getByRole('combobox', { name: 'Model', exact: true })
  ).toHaveValue('repair-model');
  await data.dialog
    .getByRole('button', { name: 'Cancel', exact: true })
    .click();
  await page
    .getByRole('button', { name: 'Display options', exact: true })
    .click();
  await page
    .getByRole('button', { name: 'Show split / repair history', exact: true })
    .click();
  await page
    .getByRole('region', { name: 'Timing repair history' })
    .getByRole('button', { name: 'View original' })
    .click();
  await expect(page.locator('tbody tr[data-segment-id]')).toHaveAttribute(
    'data-segment-id',
    'segment-root'
  );
  await data.open();
  await expect(
    data.dialog.getByRole('combobox', { name: 'Model', exact: true })
  ).toHaveValue('history-model');
  await expect(
    data.dialog.getByRole('spinbutton', { name: 'Pitch', exact: true })
  ).toHaveValue('3');
  await data.dialog
    .getByRole('textbox', {
      name: 'Generation prompt / instructions',
      exact: true
    })
    .fill('Discard this draft');
  await data.dialog
    .getByRole('button', { name: 'Cancel', exact: true })
    .click();
  await data.open();
  await expect(
    data.dialog.getByRole('textbox', {
      name: 'Generation prompt / instructions',
      exact: true
    })
  ).toHaveValue('history-model instructions');
  await submit(page).click();
  await expect.poll(() => data.posts.length).toBe(1);
  expect(data.posts[0].generation_run_id).toBe('root');
  expect(data.posts[0].settings_source_run_id).toBe('root');
  expect(data.posts[0].segment_ids).toEqual(['segment-root']);
  expect(data.posts[0].speech_plan_revision_id).toBeNull();
});

test('a rejected alternate submission retains the edited draft for retry', async ({
  page
}, info) => {
  const data = await fixture(page);
  data.failSubmission(true);
  await data.open();
  await data.dialog
    .getByRole('textbox', {
      name: 'Generation prompt / instructions',
      exact: true
    })
    .fill('Keep my draft');
  await submit(page).click();
  await expect.poll(() => data.posts.length).toBe(1);
  await expect(data.dialog).toBeVisible();
  await expect(data.dialog.getByRole('alert')).toContainText(
    'Controlled generation conflict'
  );
  await expect(
    data.dialog.getByRole('textbox', {
      name: 'Generation prompt / instructions',
      exact: true
    })
  ).toHaveValue('Keep my draft');
  await page.screenshot({ path: info.outputPath('alternate-rejected.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await data.dialog.getByRole('alert').scrollIntoViewIfNeeded();
  await expect(data.dialog.getByRole('alert')).toBeInViewport();
  await submit(page).scrollIntoViewIfNeeded();
  await expect(submit(page)).toBeInViewport();
  await page.screenshot({
    path: info.outputPath('alternate-rejected-narrow.png')
  });
  data.failSubmission(false);
  await submit(page).click();
  await expect.poll(() => data.posts.length).toBe(2);
  expect(data.posts[1]).toEqual(data.posts[0]);
  await expect(data.dialog).toHaveCount(0);
});

for (const fail of [false, true]) {
  test(`alternate submission ${fail ? 'retains the draft after' : 'waits and follows IDs through'} a pending edit ${fail ? 'failure' : 'save'}`, async ({
    page
  }) => {
    const data = await fixture(page);
    await page
      .getByRole('textbox', { name: 'Script text for segment 1', exact: true })
      .fill('Edited narration.');
    await data.open();
    await data.waitForSave();
    await data.dialog
      .getByRole('textbox', {
        name: 'Generation prompt / instructions',
        exact: true
      })
      .fill('After the edit');
    await submit(page).click();
    expect(data.posts).toHaveLength(0);
    try {
      await data.releaseSave(fail);
      if (fail) {
        await expect(data.dialog).toBeVisible();
        await expect(data.dialog.getByRole('alert')).toContainText(
          'Controlled edit conflict'
        );
        await expect(submit(page)).toBeEnabled();
        await expect(
          data.dialog.getByRole('textbox', {
            name: 'Generation prompt / instructions',
            exact: true
          })
        ).toHaveValue('After the edit');
        expect(data.posts).toHaveLength(0);
      } else {
        await expect.poll(() => data.posts.length).toBe(1);
        expect(data.posts[0].segment_ids).toEqual(['segment-edited']);
        await expect(data.dialog).toHaveCount(0);
      }
    } finally {
      await data.releaseSave();
    }
  });
}

test('alternate dialog contains keyboard focus and restores it on Escape', async ({
  page
}) => {
  const data = await fixture(page);
  await data.open();
  const close = data.dialog.getByRole('button', {
    name: 'Close alternate regeneration'
  });
  await expect(close).toBeFocused();
  await page.keyboard.press('Shift+Tab');
  await expect(submit(page)).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(close).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(data.dialog).toHaveCount(0);
  await expect(
    page.getByRole('button', { name: 'Regenerate segment 1', exact: true })
  ).toBeFocused();
});

test('pending alternate submission freezes the draft and cannot be dismissed until accepted', async ({
  page
}) => {
  const data = await fixture(page);
  await data.open();
  data.holdSubmission();
  await submit(page).click();
  await expect.poll(() => data.posts.length).toBe(1);
  try {
    await expect(data.dialog).toBeVisible();
    await expect(
      data.dialog.getByRole('textbox', {
        name: 'Generation prompt / instructions',
        exact: true
      })
    ).toBeDisabled();
    await expect(
      data.dialog.getByRole('button', { name: 'Close alternate regeneration' })
    ).toBeDisabled();
    await page.keyboard.press('Escape');
    await expect(data.dialog).toBeVisible();
  } finally {
    await data.releaseSubmission();
  }
  await expect(data.dialog).toHaveCount(0);
});

test('an accepted alternate is closed even when its following history refresh fails', async ({
  page
}) => {
  const data = await fixture(page);
  await data.open();
  data.failRefresh();
  await submit(page).click();
  await expect.poll(() => data.posts.length).toBe(1);
  await expect(data.dialog).toHaveCount(0);
  await expect(
    page.getByRole('alert').filter({ hasText: 'Controlled refresh failure' })
  ).toBeVisible();
  expect(data.posts).toHaveLength(1);
});

test('retrying an accepted alternate whose response was lost reuses its request key', async ({
  page
}) => {
  const data = await fixture(page);
  await data.open();
  data.loseAcceptedResponse();
  await submit(page).click();
  await expect(data.dialog.getByRole('alert')).toBeVisible();
  await expect(submit(page)).toBeEnabled();
  expect(data.acceptedKeys.size).toBe(1);
  await submit(page).click();
  await expect.poll(() => data.posts.length).toBe(2);
  expect(data.posts[1]).toEqual(data.posts[0]);
  expect(data.keys[1]).toBe(data.keys[0]);
  expect(data.acceptedKeys.size).toBe(1);
  await expect(data.dialog).toHaveCount(0);
});

test('changing an alternate draft or cancelling it starts a fresh request attempt', async ({
  page
}) => {
  const data = await fixture(page);
  await data.open();
  data.failSubmission(true);
  await submit(page).click();
  await expect(data.dialog.getByRole('alert')).toBeVisible();
  await data.dialog
    .getByRole('textbox', {
      name: 'Generation prompt / instructions',
      exact: true
    })
    .fill('Changed intent');
  await submit(page).click();
  await expect.poll(() => data.posts.length).toBe(2);
  await expect(submit(page)).toBeEnabled();
  expect(data.keys[1]).not.toBe(data.keys[0]);
  await data.dialog
    .getByRole('button', { name: 'Cancel', exact: true })
    .click();
  await data.open();
  await submit(page).click();
  await expect.poll(() => data.posts.length).toBe(3);
  await expect(submit(page)).toBeEnabled();
  expect(data.posts[2]).toEqual(data.posts[0]);
  expect(new Set(data.keys).size).toBe(3);
});

test('leaving history through an edit retains and names the saved settings source', async ({
  page
}) => {
  const data = await fixture(page);
  await page
    .getByRole('combobox', { name: 'Audio view', exact: true })
    .selectOption('root');
  await expect(page.locator('tbody tr[data-segment-id]')).toHaveAttribute(
    'data-segment-id',
    'segment-repair'
  );
  await page
    .getByRole('textbox', { name: 'Script text for segment 1', exact: true })
    .fill('Edited historical narration.');
  await page.keyboard.press('Tab');
  await data.waitForSave();
  await data.releaseSave();
  await expect(page.locator('tbody tr[data-segment-id]')).toHaveAttribute(
    'data-segment-id',
    'segment-edited'
  );
  await expect(
    page.getByRole('combobox', { name: 'Audio view', exact: true })
  ).toHaveValue('');
  await data.open();
  await expect(
    data.dialog.getByRole('combobox', { name: 'Model', exact: true })
  ).toHaveValue('repair-model');
  await expect(data.dialog).toContainText(
    'Uses History · Repaired narration as the source'
  );
  await submit(page).click();
  await expect.poll(() => data.posts.length).toBe(1);
  expect(data.posts[0].settings_source_run_id).toBe('repair');
  expect(data.posts[0].generation_run_id).toBeNull();
  expect(data.posts[0].segment_ids).toEqual(['segment-edited']);
  expect(data.posts[0].speech_plan_revision_id).toBe('edited-plan');
});
