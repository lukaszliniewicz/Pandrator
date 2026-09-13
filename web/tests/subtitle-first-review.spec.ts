import { Buffer } from 'node:buffer';
import { execFileSync } from 'node:child_process';
import { expect, test, type Page } from '@playwright/test';

function recordingWav(seconds = 1) {
  const size = 16000 * 2 * seconds;
  const buffer = Buffer.alloc(44 + size);
  buffer.write('RIFF', 0);
  buffer.writeUInt32LE(36 + size, 4);
  buffer.write('WAVEfmt ', 8);
  buffer.writeUInt32LE(16, 16);
  buffer.writeUInt16LE(1, 20);
  buffer.writeUInt16LE(1, 22);
  buffer.writeUInt32LE(16000, 24);
  buffer.writeUInt32LE(32000, 28);
  buffer.writeUInt16LE(2, 32);
  buffer.writeUInt16LE(16, 34);
  buffer.write('data', 36);
  buffer.writeUInt32LE(size, 40);
  return buffer;
}

function recordingVideo() {
  return execFileSync(
    'ffmpeg',
    [
      '-v',
      'error',
      '-f',
      'lavfi',
      '-i',
      'color=c=black:s=160x90:r=10:d=1',
      '-f',
      'lavfi',
      '-i',
      'anullsrc=r=16000:cl=mono',
      '-t',
      '1',
      '-c:v',
      'mpeg4',
      '-c:a',
      'aac',
      '-movflags',
      'frag_keyframe+empty_moov',
      '-f',
      'mp4',
      'pipe:1'
    ],
    { timeout: 15000 }
  );
}

async function setup(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const headers = { 'X-CSRF-Token': auth.csrf_token };
  const created = await page.request.post('/api/v1/sessions', {
    headers,
    data: {
      name: `Session flow ${crypto.randomUUID()}`,
      workflow_kind: 'voiceover'
    }
  });
  expect(created.ok()).toBeTruthy();
  const session = await created.json();
  const endpoint = `/api/v1/sessions/${session.id}`;
  const outcome = await (
    await page.request.get(`${endpoint}/outcome-plan`)
  ).json();
  const changed = await page.request.put(`${endpoint}/outcome-plan`, {
    headers: { ...headers, 'If-Match': `"${outcome.revision}"` },
    data: { value: { ...outcome.value, inputs: { generation: 'source' } } }
  });
  expect(changed.ok()).toBeTruthy();
  const uploaded = await page.request.post('/api/v1/uploads', {
    headers,
    multipart: {
      session_id: session.id,
      file: {
        name: 'authoritative.srt',
        mimeType: 'application/x-subrip',
        buffer: Buffer.from(
          '1\n00:00:00,000 --> 00:00:01,000\nHello world. Another complete thought.\n'
        )
      }
    }
  });
  expect(uploaded.ok()).toBeTruthy();
  return { session, endpoint, headers, source: await uploaded.json() };
}

async function speechState(page: Page, endpoint: string) {
  const response = await page.request.get(`${endpoint}/generation-plan/status`);
  expect(response.ok(), await response.text()).toBeTruthy();
  return response.json();
}

async function attachRecording(
  page: Page,
  name: string,
  mimeType: string,
  buffer: Buffer
) {
  const card = page.getByRole('region', { name: 'Session source' });
  await card.getByRole('button', { name: 'Attach audio or video' }).click();
  const picker = page.getByRole('dialog', { name: 'Add a source' });
  await expect(picker).toBeVisible();
  await picker
    .locator('input[type="file"]')
    .setInputFiles({ name, mimeType, buffer });
  await picker
    .getByRole('button', { name: 'Add and select', exact: true })
    .click();
  const confirmation = page.getByRole('dialog', {
    name: 'Use this associated recording?'
  });
  await expect(confirmation).toBeVisible();
  await confirmation
    .getByRole('button', { name: 'Use recording', exact: true })
    .click();
  await expect(confirmation).toBeHidden();
  await expect(card.getByText(name, { exact: true })).toBeVisible();
}

const generationCard = (page: Page) =>
  page.locator('article').filter({
    has: page.getByRole('heading', { name: 'Generate audio', exact: true })
  });

test('early voiceover repair is optional and persists in block settings', async ({
  page
}, testInfo) => {
  const { session, endpoint } = await setup(page);
  await page.goto(`/sessions/${session.id}`);
  await page
    .getByRole('button', { name: 'Block settings', exact: true })
    .click();
  const dialog = page.getByRole('dialog', { name: 'Speech-block settings' });
  const repair = dialog.getByRole('checkbox', {
    name: /Reduce speech getting ahead of subtitles/
  });
  await expect(repair).not.toBeChecked();
  await repair.check();
  await dialog.getByText('Repair thresholds', { exact: true }).click();
  const earlyTime = dialog.getByRole('spinbutton', {
    name: /^Finish early by at least \(ms\)/
  });
  await earlyTime.fill('750');
  await dialog
    .getByRole('spinbutton', { name: /^Finish early by at least \(%\)/ })
    .fill('15');
  await dialog
    .getByRole('spinbutton', { name: /^Minimum timing improvement/ })
    .fill('800');
  await dialog
    .getByRole('spinbutton', { name: /^Minimum cue span per new block/ })
    .fill('1200');
  await dialog.getByRole('button', { name: 'Save block settings' }).click();
  await expect(dialog).toBeHidden();
  const settings = await (
    await page.request.get(`${endpoint}/settings/tts`)
  ).json();
  expect(settings.effective.speech_block_early_repair_enabled).toBe(true);
  expect(settings.effective.speech_block_early_repair_min_shortfall_ms).toBe(
    750
  );
  expect(
    settings.effective.speech_block_early_repair_min_shortfall_percent
  ).toBe(15);
  expect(settings.effective.speech_block_early_repair_min_advance_ms).toBe(800);
  expect(settings.effective.speech_block_early_repair_min_child_span_ms).toBe(
    1200
  );
  await page.reload();
  await page
    .getByRole('button', { name: 'Block settings', exact: true })
    .click();
  await expect(repair).toBeChecked();
  await dialog.getByText('Repair thresholds', { exact: true }).click();
  await expect(earlyTime).toHaveValue('750');
  const help = dialog.getByRole('button', {
    name: /^About Minimum cue span per new block/
  });
  await help.scrollIntoViewIfNeeded();
  await help.focus();
  const tooltip = help.getByRole('tooltip');
  await expect(tooltip).toBeVisible();
  expect(
    await tooltip.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      return (
        element.matches(':popover-open') &&
        rect.top >= 0 &&
        rect.bottom <= innerHeight &&
        rect.left >= 0 &&
        rect.right <= innerWidth &&
        element.contains(
          document.elementFromPoint(
            rect.left + rect.width / 2,
            rect.top + rect.height / 2
          )
        )
      );
    })
  ).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('repair-help-popup.png') });
  await help.press('Escape');
  await expect(tooltip).toBeHidden();
  await expect(dialog).toBeVisible();
  expect(
    await dialog.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      return (
        rect.top >= 0 &&
        rect.bottom <= innerHeight &&
        Math.abs(rect.left + rect.width / 2 - innerWidth / 2) < 2
      );
    })
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath('early-repair-settings.png')
  });
});

test('gentle voiceover slowdown is optional and persists in audio settings', async ({
  page
}, testInfo) => {
  const { session, endpoint } = await setup(page);
  await page.goto(`/sessions/${session.id}/voice`);
  const slowdown = page.getByRole('checkbox', {
    name: /Allow gentle voiceover slowdown/
  });
  await expect(slowdown).not.toBeChecked();
  await slowdown.check();
  await page
    .locator('section.settings-panel')
    .filter({ has: slowdown })
    .getByRole('button', { name: 'Save', exact: true })
    .click();
  await expect
    .poll(async () => {
      const settings = await (
        await page.request.get(`${endpoint}/settings/audio`)
      ).json();
      return settings.effective.synchronization_slowdown_enabled;
    })
    .toBe(true);
  await page.reload();
  await expect(slowdown).toBeChecked();
  await slowdown.scrollIntoViewIfNeeded();
  await page.screenshot({
    path: testInfo.outputPath('voiceover-slowdown-settings.png')
  });
});

test('Source card attaches independent media and keeps timed text after reopening', async ({
  page
}) => {
  const { session, endpoint, source } = await setup(page);
  await page.goto(`/sessions/${session.id}`);
  const card = page.getByRole('region', { name: 'Session source' });
  await expect(card).toBeVisible();
  await expect(
    card.getByRole('button', { name: 'Align existing words' })
  ).toBeHidden();
  await attachRecording(page, 'recording.wav', 'audio/wav', recordingWav());
  await page.reload();
  await expect(
    card.getByText('authoritative.srt', { exact: true })
  ).toBeVisible();
  await expect(card.getByText('recording.wav', { exact: true })).toBeVisible();
  await card.getByRole('button', { name: 'Align existing words' }).click();
  await expect(
    card.getByRole('button', { name: 'Align words', exact: true })
  ).toBeEnabled();
  const status = await (
    await page.request.get(`${endpoint}/sources/subtitle-status`)
  ).json();
  expect(status.subtitle_revision_id).toBe(
    source.attachment.subtitle_revision.revision_id
  );
  expect(status.adoption_required).toBe(false);
});

test('preparing and reviewing a speech plan does not start TTS; Generate pins the selected plan', async ({
  page
}) => {
  const { session, endpoint } = await setup(page);
  // Only availability is simulated; the generation POST is intercepted below.
  await page.route('**/api/v1/services/tts**', async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    if (Array.isArray(payload.services)) {
      payload.services = payload.services.map(
        (service: Record<string, unknown>) => ({
          ...service,
          available: true,
          availability_reason: null
        })
      );
    }
    await route.fulfill({ response, json: payload });
  });
  await page.goto(`/sessions/${session.id}`);
  const planCard = page.getByRole('region', {
    name: 'Speech plan',
    exact: true
  });
  await planCard.getByRole('button', { name: 'Prepare speech plan' }).click();
  await expect(
    planCard.getByRole('button', { name: 'Review plan', exact: true })
  ).toBeEnabled();
  const plans = await speechState(page, endpoint);
  expect(plans.total).toBe(1);
  const runs = await (
    await page.request.get(`${endpoint}/generation-runs`)
  ).json();
  expect(runs.items).toHaveLength(0);
  await planCard
    .getByRole('button', { name: 'Review plan', exact: true })
    .click();
  await expect(
    page.getByRole('button', { name: 'Speech plans', exact: true })
  ).toBeVisible();
  await planCard.getByRole('button', { name: 'Mark reviewed' }).click();
  await expect(
    planCard.getByRole('button', { name: 'Reviewed', exact: true })
  ).toBeDisabled();
  await page.route(`**${endpoint}/generation-runs`, async (route) => {
    if (route.request().method() !== 'POST') return route.continue();
    await route.fulfill({
      status: 422,
      contentType: 'application/json',
      json: {
        error: { code: 'test_only', message: 'No inference in browser test.' }
      }
    });
  });
  const request = page.waitForRequest(
    (item) =>
      item.method() === 'POST' &&
      item.url().endsWith(`${endpoint}/generation-runs`)
  );
  await page.getByRole('button', { name: 'Generation', exact: true }).click();
  await expect(page.locator('[data-generation-layout]')).toHaveAttribute(
    'data-generation-layout',
    'collapsed'
  );
  await generationCard(page)
    .getByRole('button', { name: 'Generate selected plan', exact: true })
    .click();
  const body = (await request).postDataJSON();
  expect(body.speech_plan_revision_id).toBe(plans.selected_revision_id);
  expect(body.stale_only).toBe(false);
});

test('the Generate picker selects an older speech plan without copying a revision', async ({
  page
}) => {
  const { session, endpoint, headers } = await setup(page);
  const current = await speechState(page, endpoint);
  const prepared = await page.request.post(
    `${endpoint}/generation-plan/prepare`,
    {
      headers: { ...headers, 'Idempotency-Key': crypto.randomUUID() },
      data: {
        expected_revision: current.session_revision,
        expected_plan_revision_id: current.selected_revision_id,
        source_artifact_id: current.current_input.artifact_id
      }
    }
  );
  expect(prepared.ok(), await prepared.text()).toBeTruthy();
  const first = await prepared.json();
  const segments = await (
    await page.request.get(`${endpoint}/generation-segments`)
  ).json();
  const edited = await page.request.post(
    `${endpoint}/generation-plan/topology/batch`,
    {
      headers: {
        ...headers,
        'If-Match': `"${first.selected_revision_id}"`,
        'Idempotency-Key': crypto.randomUUID()
      },
      data: {
        expected_revision_id: first.selected_revision_id,
        operations: [
          {
            action: 'split',
            segment_id: segments.items[0].id,
            boundary: { after_sentence: 1 }
          }
        ]
      }
    }
  );
  expect(edited.ok(), await edited.text()).toBeTruthy();
  await page.goto(`/sessions/${session.id}`);
  const picker = generationCard(page).getByLabel('Generate from speech plan');
  await picker.selectOption(first.selected_revision_id);
  await expect(
    page
      .getByRole('region', { name: 'Speech plan', exact: true })
      .getByLabel('Selected speech-plan version')
  ).toHaveValue(first.selected_revision_id);
  const selected = await speechState(page, endpoint);
  expect(selected.total).toBe(2);
  expect(selected.selected_revision_id).toBe(first.selected_revision_id);
  await page.reload();
  await expect(picker).toHaveValue(first.selected_revision_id);
});

test('source reset cancellation preserves work, while confirmation removes only session derivations', async ({
  page
}) => {
  const { session, endpoint, source } = await setup(page);
  await page.goto(`/sessions/${session.id}`);
  const planCard = page.getByRole('region', {
    name: 'Speech plan',
    exact: true
  });
  await planCard.getByRole('button', { name: 'Prepare speech plan' }).click();
  await expect(
    planCard.getByRole('button', { name: 'Review plan', exact: true })
  ).toBeEnabled();
  const card = page.getByRole('region', { name: 'Session source' });
  await card.getByRole('button', { name: 'Remove source' }).click();
  const confirmation = page.getByRole('dialog', {
    name: 'Remove source and reset this session?'
  });
  await expect(confirmation.getByText(/1 speech-plan histories/)).toBeVisible();
  await confirmation
    .getByRole('button', { name: 'Cancel', exact: true })
    .click();
  expect((await speechState(page, endpoint)).total).toBe(1);
  await card.getByRole('button', { name: 'Remove source' }).click();
  await confirmation
    .getByRole('button', { name: 'Remove source and reset', exact: true })
    .click();
  await expect(
    card.getByRole('button', { name: 'Add source', exact: true })
  ).toBeEnabled();
  expect(
    (await (await page.request.get(`${endpoint}/sources/status`)).json())
      .primary
  ).toBeNull();
  expect((await speechState(page, endpoint)).total).toBe(0);
  const library = await (await page.request.get('/api/v1/sources')).json();
  expect(
    library.items.some(
      (item: { id: string }) => item.id === source.source_asset_id
    )
  ).toBe(true);
});

test('a video source can export an audio-only mix and retain its audio settings after reopening', async ({
  page
}) => {
  const { session, endpoint } = await setup(page);
  await page.goto(`/sessions/${session.id}`);
  await attachRecording(page, 'recording.mp4', 'video/mp4', recordingVideo());
  await page.goto(`/sessions/${session.id}/output`);
  const outputMode = page.getByRole('combobox', {
    name: 'Export target',
    exact: true
  });
  await expect(
    outputMode.getByRole('option', { name: 'Rendered video', exact: true })
  ).toBeAttached();
  await outputMode.selectOption('audio');
  await expect(outputMode).toHaveValue('audio');
  await expect(
    page.getByRole('combobox', { name: 'Subtitles', exact: true })
  ).toBeHidden();
  await expect(
    page.getByRole('group', { name: 'Advanced video encoding' })
  ).toBeHidden();
  await page
    .getByRole('combobox', { name: 'Audio result', exact: true })
    .selectOption('mixed');
  await expect(
    page.getByRole('combobox', { name: 'Audio result', exact: true })
  ).toHaveValue('mixed');
  const format = page.getByRole('combobox', {
    name: 'Audio format',
    exact: true
  });
  await format.selectOption('flac');
  const match = page.getByRole('checkbox', {
    name: /Match the recording timeline/
  });
  await expect(match).toBeChecked();
  await match.uncheck();
  await page
    .getByRole('button', { name: 'Save output profile', exact: true })
    .click();
  await expect
    .poll(async () => {
      const saved = await (
        await page.request.get(`${endpoint}/settings/output`)
      ).json();
      return saved.effective?.export_mode;
    })
    .toBe('audio');
  await page.reload();
  await expect(outputMode).toHaveValue('audio');
  await expect(format).toHaveValue('flac');
  await expect(match).not.toBeChecked();
});
