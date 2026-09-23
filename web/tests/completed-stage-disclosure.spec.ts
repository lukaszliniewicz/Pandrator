import { expect, test, type Page } from '@playwright/test';

type StageJson = Record<string, unknown>;

async function login(page: Page) {
  page.on('pageerror', () => {});
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
}

async function createSession(page: Page, name: string) {
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const headers = { 'X-CSRF-Token': auth.csrf_token };
  const response = await page.request.post('/api/v1/sessions', {
    headers,
    data: { name: `${name} ${crypto.randomUUID()}`, workflow_kind: 'audiobook' }
  });
  expect(response.ok()).toBeTruthy();
  const id = (await response.json()).id as string;
  const created = await page.request.post(
    `/api/v1/sessions/${id}/generation-plan`,
    {
      headers,
      data: { segments: [{ text: 'First block.' }, { text: 'Second block.' }] }
    }
  );
  expect(created.ok()).toBeTruthy();
  const status = await (
    await page.request.get(
      `/api/v1/sessions/${id}/generation-plan/status?summary=true`
    )
  ).json();
  await page.route(
    `**/api/v1/sessions/${id}/generation-plan/status*`,
    (route) =>
      route.fulfill({
        json: {
          ...status,
          can_generate: false,
          warning: null,
          blocked_reason: 'Prepare a speech plan first.',
          items: status.items.map((item: Record<string, unknown>) => ({
            ...item,
            reviewed: false,
            audio_reuse_checked: false,
            reusable_segment_count: null,
            stale_segment_count: null,
            audio_settings_stale_segment_count: null,
            audio_identity_unknown_segment_count: null
          }))
        }
      })
  );
  return id;
}

function mockWorkflow(page: Page, id: string, getStages: () => StageJson[]) {
  return page.route(`**/api/v1/sessions/${id}/workflow`, (route) =>
    route.fulfill({
      json: {
        session_id: id,
        workflow_kind: 'audiobook',
        workflow_preset: 'default',
        revision: 1,
        sources: [],
        stages: getStages()
      }
    })
  );
}

function artifact(
  id: string,
  version: number,
  extra: Record<string, unknown> = {}
) {
  return {
    id,
    session_id: null,
    kind: 'srt',
    role: 'transcription',
    raw_role: 'transcription',
    path: `/fake/${id}.srt`,
    relative_path: `${id}.srt`,
    mime_type: 'text/plain',
    size_bytes: 1234,
    content_hash: `hash-${id}`,
    state: 'current',
    metadata_json: {},
    created_at: '2026-01-02T03:04:05Z',
    version,
    is_selected: false,
    parent_ids: [],
    settings_hash: null,
    ...extra
  };
}

function stage(
  key: string,
  title: string,
  number: number,
  status: string,
  extra: Record<string, unknown> = {}
): StageJson {
  return {
    number,
    key,
    title,
    explanation: `${title} explanation.`,
    status,
    executable: true,
    included: true,
    artifacts: [],
    artifact: null,
    selected_artifact_id: null,
    selection_revision: 0,
    job_id: null,
    progress: null,
    detail: null,
    usage: null,
    run_metrics: null,
    ...extra
  };
}

function completedTranscribe(selectedId = 'art-t2') {
  const v2 = artifact('art-t2', 2, {
    is_selected: selectedId === 'art-t2',
    metadata_json: { original_filename: 'chapter-one.srt' }
  });
  const v1 = artifact('art-t1', 1, { is_selected: selectedId === 'art-t1' });
  const selected = selectedId === 'art-t2' ? v2 : v1;
  return stage('transcribe', 'Transcribe media', 1, 'completed', {
    artifacts: [v2, v1],
    artifact: selected,
    selected_artifact_id: selectedId,
    selection_revision: 3,
    usage: {
      input_tokens: 10,
      output_tokens: 5,
      cached_input_tokens: 0,
      total_tokens: 15,
      cost_usd: 0.01,
      model_id: 'example-model'
    },
    run_metrics: {
      started_at: '2026-01-02T03:00:00Z',
      finished_at: '2026-01-02T03:01:05Z',
      duration_seconds: 65
    }
  });
}

function card(page: Page, title: string) {
  return page
    .locator('article')
    .filter({ has: page.getByRole('heading', { name: title, exact: true }) });
}

test('selected artifact summaries do not display an undefined version', async ({
  page
}) => {
  await login(page);
  const id = await createSession(page, 'Version summary');
  await mockWorkflow(page, id, () => [
    stage('segment', 'Segment narration', 2, 'completed', {
      artifact: {
        id: 'summary-only',
        role: 'prepared_text',
        metadata_json: {}
      },
      selected_artifact_id: 'summary-only'
    })
  ]);
  await page.goto(`/sessions/${id}`);
  const segment = card(page, 'Segment narration');
  await expect(segment).toContainText('Selected ·');
  await expect(segment).not.toContainText('undefined');
});

test('completed preprocessing stage starts folded and expands with keyboard', async ({
  page
}) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await login(page);
  const id = await createSession(page, 'Disclosure fold');
  await mockWorkflow(page, id, () => [completedTranscribe()]);
  await page.goto(`/sessions/${id}`);
  const transcribe = card(page, 'Transcribe media');
  await expect(transcribe).toBeVisible();
  const summary = transcribe.getByTestId('stage-disclosure-summary');
  await expect(summary).toContainText('Selected v2 · chapter-one.srt');
  await expect(summary).toContainText('1m 5s');
  await expect(summary).toContainText('$0.0100');
  const toggle = transcribe.getByTestId('stage-disclosure-toggle');
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  await expect(toggle).toContainText('Show details');
  const controlled = await toggle.getAttribute('aria-controls');
  expect(controlled).toBeTruthy();
  await expect(transcribe.locator(`#${controlled}`)).toHaveCount(1);
  const body = transcribe.getByTestId('stage-disclosure-body');
  await expect(body).toBeHidden();
  await expect(
    transcribe.getByRole('combobox', { name: 'Selected version', exact: true })
  ).toBeHidden();
  await toggle.focus();
  await expect(toggle).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  await expect(toggle).toContainText('Hide details');
  await expect(body).toBeVisible();
  await expect(
    transcribe.getByRole('combobox', { name: 'Selected version', exact: true })
  ).toBeVisible();
  await expect(
    transcribe.getByRole('button', { name: 'Preview selected', exact: true })
  ).toBeVisible();
  // Expanded: compact stats live only in the shared metrics row, not twice.
  await expect(summary).not.toContainText('1m 5s');
  await expect(summary).not.toContainText('$0.0100');
  await expect(
    transcribe.getByLabel('Transcribe media run metrics')
  ).toContainText('1m 5s');
  await page.keyboard.press('Space');
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  await expect(body).toBeHidden();
  await expect(summary).toContainText('1m 5s');
  expect(errors).toEqual([]);
});

test('expanded stage survives version selection refresh', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await login(page);
  const id = await createSession(page, 'Disclosure select');
  let selected = 'art-t2';
  await mockWorkflow(page, id, () => [completedTranscribe(selected)]);
  await page.route(
    `**/api/v1/sessions/${id}/stages/transcribe/selection`,
    (route) =>
      route.fulfill({
        json: {
          session_id: id,
          workflow_kind: 'audiobook',
          workflow_preset: 'default',
          revision: 2,
          sources: [],
          stages: [completedTranscribe('art-t1')]
        }
      })
  );
  await page.goto(`/sessions/${id}`);
  const transcribe = card(page, 'Transcribe media');
  const toggle = transcribe.getByTestId('stage-disclosure-toggle');
  await toggle.click();
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  const selectedRoute = page.waitForResponse(
    (response) =>
      response.url().includes('/stages/transcribe/selection') &&
      response.request().method() === 'PUT'
  );
  await transcribe
    .getByRole('combobox', { name: 'Selected version', exact: true })
    .selectOption('art-t1');
  await selectedRoute;
  selected = 'art-t1';
  await expect(
    transcribe.getByTestId('stage-disclosure-summary')
  ).toContainText('Selected v1 · Transcription');
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  await expect(toggle).toContainText('Hide details');
  await expect(transcribe.getByTestId('stage-disclosure-body')).toBeVisible();
  expect(errors).toEqual([]);
});

test('failed, stale, running and unreliable alignment stages never collapse', async ({
  page
}) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await login(page);
  const id = await createSession(page, 'Disclosure warnings');
  const failedArtifact = artifact('art-f1', 1, { is_selected: true });
  const staleArtifact = artifact('art-s1', 1, { is_selected: true });
  const unreliableArtifact = artifact('art-u2', 2, {
    is_selected: true,
    metadata_json: {
      alignment_method: 'asr_lexical_projection',
      alignment_coverage: 0.2,
      alignment_eligible_coverage: 0.2,
      alignment_quality: 0.3,
      word_count: 100,
      cue_count: 10
    }
  });
  await mockWorkflow(page, id, () => [
    stage('correct', 'Correct text', 1, 'failed', {
      artifacts: [failedArtifact],
      artifact: failedArtifact,
      selected_artifact_id: 'art-f1',
      detail: 'Correction failed on batch two.'
    }),
    stage('translate', 'Translate text', 2, 'stale', {
      artifacts: [staleArtifact],
      artifact: staleArtifact,
      selected_artifact_id: 'art-s1'
    }),
    stage('transcribe', 'Transcribe media', 3, 'running', {
      job_id: 'job-9',
      progress: 0.5,
      detail: 'Transcribing…'
    }),
    stage('prepare_text', 'Prepare text', 4, 'completed', {
      artifacts: [unreliableArtifact],
      artifact: unreliableArtifact,
      selected_artifact_id: 'art-u2'
    }),
    stage('optimize_tts', 'Optimize speech', 5, 'completed', {
      artifacts: [staleArtifact],
      artifact: staleArtifact,
      selected_artifact_id: 'art-s1',
      detail: 'Heads up: two cues need review.'
    })
  ]);
  await page.goto(`/sessions/${id}`);
  const failed = card(page, 'Correct text');
  await expect(failed).toBeVisible();
  await expect(failed.getByTestId('stage-disclosure-toggle')).toHaveCount(0);
  await expect(
    failed.getByText('Correction failed on batch two.')
  ).toBeVisible();
  await expect(
    failed.getByRole('button', { name: 'Retry', exact: true })
  ).toBeVisible();
  const stale = card(page, 'Translate text');
  await expect(stale.getByTestId('stage-disclosure-toggle')).toHaveCount(0);
  await expect(stale.getByTestId('stage-disclosure-body')).toBeVisible();
  const running = card(page, 'Transcribe media');
  await expect(running.getByTestId('stage-disclosure-toggle')).toHaveCount(0);
  await expect(
    running.getByRole('progressbar', { name: 'Transcribe media progress' })
  ).toBeVisible();
  await expect(
    running.getByRole('button', { name: 'Cancel', exact: true })
  ).toBeVisible();
  const unreliable = card(page, 'Prepare text');
  await expect(unreliable.getByTestId('stage-disclosure-toggle')).toHaveCount(
    0
  );
  await expect(
    unreliable.getByText('Caption alignment is unreliable')
  ).toBeVisible();
  const detailed = card(page, 'Optimize speech');
  await expect(detailed.getByTestId('stage-disclosure-toggle')).toHaveCount(0);
  await expect(detailed.getByTestId('stage-disclosure-body')).toBeVisible();
  expect(errors).toEqual([]);
});

test('main surfaces, artifact-less and toggle-enabled stages never collapse', async ({
  page
}) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await login(page);
  const id = await createSession(page, 'Disclosure exempt');
  const audioArtifact = artifact('art-g1', 1, {
    is_selected: true,
    role: 'generation_audio',
    raw_role: 'generation_audio',
    kind: 'wav'
  });
  const exportArtifact = artifact('art-e1', 1, {
    is_selected: true,
    role: 'export_package',
    raw_role: 'export_package',
    kind: 'zip'
  });
  await mockWorkflow(page, id, () => [
    stage('generate_audio', 'Generate audio', 1, 'completed', {
      artifacts: [audioArtifact],
      artifact: audioArtifact,
      selected_artifact_id: 'art-g1'
    }),
    stage('export', 'Export', 2, 'completed', {
      artifacts: [exportArtifact],
      artifact: exportArtifact,
      selected_artifact_id: 'art-e1'
    }),
    stage('correct', 'Correct text', 3, 'completed'),
    stage('translate', 'Translate text', 4, 'completed', {
      artifacts: [artifact('art-x1', 1)],
      artifact: null,
      selected_artifact_id: 'art-missing'
    }),
    stage('optimize_tts', 'Optimize speech', 5, 'ready', {
      toggle: true,
      enabled: true,
      toggle_only: false
    })
  ]);
  await page.goto(`/sessions/${id}`);
  const audio = card(page, 'Generate audio');
  await expect(audio).toBeVisible();
  await expect(audio.getByTestId('stage-disclosure-toggle')).toHaveCount(0);
  await expect(audio.getByTestId('stage-disclosure-body')).toBeVisible();
  const output = card(page, 'Export');
  await expect(output.getByTestId('stage-disclosure-toggle')).toHaveCount(0);
  await expect(
    output.getByRole('button', { name: 'Export now', exact: true })
  ).toBeVisible();
  const bare = card(page, 'Correct text');
  await expect(bare.getByTestId('stage-disclosure-toggle')).toHaveCount(0);
  await expect(bare.getByTestId('stage-disclosure-body')).not.toHaveAttribute(
    'hidden',
    ''
  );
  await expect(
    bare.getByRole('button', { name: 'Settings', exact: true })
  ).toBeVisible();
  // A lone selected_artifact_id with no matching artifact record is stale,
  // not a usable selection, so the stage must not fold.
  const dangling = card(page, 'Translate text');
  await expect(dangling.getByTestId('stage-disclosure-toggle')).toHaveCount(0);
  await expect(dangling.getByTestId('stage-disclosure-body')).toBeVisible();
  const toggled = card(page, 'Optimize speech');
  await expect(toggled.getByTestId('stage-disclosure-toggle')).toHaveCount(0);
  await expect(
    toggled.getByTestId('stage-disclosure-body')
  ).not.toHaveAttribute('hidden', '');
  await expect(
    toggled.getByRole('button', { name: 'Timing & settings', exact: true })
  ).toBeVisible();
  expect(errors).toEqual([]);
});

test('active stage completing stays expanded instead of jumping shut', async ({
  page
}) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await login(page);
  const id = await createSession(page, 'Disclosure phase');
  let finished = false;
  await mockWorkflow(page, id, () =>
    finished
      ? [completedTranscribe()]
      : [
          stage('transcribe', 'Transcribe media', 1, 'running', {
            job_id: 'job-1',
            progress: 0.4,
            detail: 'Transcribing…'
          })
        ]
  );
  await page.route(`**/api/v1/jobs/job-1/cancel`, (route) =>
    route.fulfill({
      json: {
        id: 'job-1',
        kind: 'transcribe',
        status: 'cancelled',
        progress: 0
      }
    })
  );
  await page.goto(`/sessions/${id}`);
  const transcribe = card(page, 'Transcribe media');
  await expect(transcribe).toBeVisible();
  await expect(transcribe.getByTestId('stage-disclosure-toggle')).toHaveCount(
    0
  );
  await expect(
    transcribe.getByRole('progressbar', { name: 'Transcribe media progress' })
  ).toBeVisible();
  finished = true;
  await transcribe.getByRole('button', { name: 'Cancel', exact: true }).click();
  const toggle = transcribe.getByTestId('stage-disclosure-toggle');
  await expect(toggle).toContainText('Hide details');
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  await expect(transcribe.getByTestId('stage-disclosure-body')).toBeVisible();
  await expect(
    transcribe.getByRole('combobox', { name: 'Selected version', exact: true })
  ).toBeVisible();
  expect(errors).toEqual([]);
});

test('folded stage forced open by a run latches open when it completes', async ({
  page
}) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await login(page);
  const id = await createSession(page, 'Disclosure latch');
  type Phase = 'done' | 'running';
  let phase: Phase = 'done';
  let selected = 'art-t2';
  const snapshot = (stages: StageJson[], revision: number) => ({
    session_id: id,
    workflow_kind: 'audiobook',
    workflow_preset: 'default',
    revision,
    sources: [],
    stages
  });
  const runningTranscribe = () =>
    stage('transcribe', 'Transcribe media', 1, 'running', {
      artifacts: [artifact('art-t2', 2), artifact('art-t1', 1)],
      artifact: null,
      selected_artifact_id: selected,
      job_id: 'job-7',
      progress: 0.3,
      detail: 'Transcribing…'
    });
  await mockWorkflow(page, id, () =>
    phase === 'done' ? [completedTranscribe(selected)] : [runningTranscribe()]
  );
  await page.route(
    `**/api/v1/sessions/${id}/stages/transcribe/selection`,
    (route) => {
      if (selected === 'art-t2') {
        selected = 'art-t1';
        phase = 'running';
        return route.fulfill({
          json: snapshot([runningTranscribe()], 2)
        });
      }
      selected = 'art-t2';
      phase = 'done';
      return route.fulfill({
        json: snapshot([completedTranscribe(selected)], 3)
      });
    }
  );
  await page.goto(`/sessions/${id}`);
  const transcribe = card(page, 'Transcribe media');
  // Starts folded; the user never expands it. The version picker is hidden
  // with the body, so drive the refresh with a forced selection.
  await expect(transcribe.getByTestId('stage-disclosure-toggle')).toContainText(
    'Show details'
  );
  const firstSelect = page.waitForResponse(
    (response) =>
      response.url().includes('/stages/transcribe/selection') &&
      response.request().method() === 'PUT'
  );
  // Simulate an external update through the existing selection handler.
  // This is not a user interaction assertion: the folded picker is intentionally hidden.
  await transcribe.locator('select.version-select').evaluate((node) => {
    (node as HTMLSelectElement).value = 'art-t1';
    node.dispatchEvent(new Event('change', { bubbles: true }));
  });
  await firstSelect;
  // The run forces the body open with no toggle to fold it away.
  await expect(transcribe.getByTestId('stage-disclosure-toggle')).toHaveCount(
    0
  );
  await expect(
    transcribe.getByRole('progressbar', { name: 'Transcribe media progress' })
  ).toBeVisible();
  const secondSelect = page.waitForResponse(
    (response) =>
      response.url().includes('/stages/transcribe/selection') &&
      response.request().method() === 'PUT'
  );
  await transcribe
    .getByRole('combobox', { name: 'Selected version', exact: true })
    .selectOption('art-t2');
  await secondSelect;
  // Completing must not re-collapse: the forced expansion latches open.
  const toggle = transcribe.getByTestId('stage-disclosure-toggle');
  await expect(toggle).toContainText('Hide details');
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  await expect(transcribe.getByTestId('stage-disclosure-body')).toBeVisible();
  // The user can still explicitly fold again afterwards.
  await toggle.click();
  await expect(toggle).toContainText('Show details');
  await expect(transcribe.getByTestId('stage-disclosure-body')).toBeHidden();
  expect(errors).toEqual([]);
});

test('switching sessions starts folded again', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await login(page);
  const first = await createSession(page, 'Disclosure first');
  const second = await createSession(page, 'Disclosure second');
  await mockWorkflow(page, first, () => [completedTranscribe()]);
  await mockWorkflow(page, second, () => [completedTranscribe()]);
  await page.goto(`/sessions/${first}`);
  const firstCard = card(page, 'Transcribe media');
  await expect(firstCard).toBeVisible();
  await firstCard.getByTestId('stage-disclosure-toggle').click();
  await expect(
    firstCard.getByTestId('stage-disclosure-toggle')
  ).toHaveAttribute('aria-expanded', 'true');
  // Exercise client-side navigation, not a full page reload that resets all state.
  await page.evaluate((sessionId) => {
    const link = document.createElement('a');
    link.href = `/sessions/${sessionId}`;
    link.dataset.testid = 'switch-session-client-link';
    link.textContent = 'Switch test session';
    link.style.cssText =
      'position:fixed;top:80px;right:16px;z-index:9999;background:var(--paper);padding:8px;';
    document.body.append(link);
    document.documentElement.dataset.sameDocumentCheck = 'retained';
  }, second);
  await page.getByTestId('switch-session-client-link').click();
  await expect(page).toHaveURL(`/sessions/${second}`);
  expect(
    await page.evaluate(
      () => document.documentElement.dataset.sameDocumentCheck
    )
  ).toBe('retained');
  const secondCard = card(page, 'Transcribe media');
  await expect(secondCard).toBeVisible();
  await expect(secondCard.getByTestId('stage-disclosure-toggle')).toContainText(
    'Show details'
  );
  await expect(secondCard.getByTestId('stage-disclosure-body')).toBeHidden();
  expect(errors).toEqual([]);
});
