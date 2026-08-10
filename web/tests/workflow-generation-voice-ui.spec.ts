import { readFileSync } from 'node:fs';
import { expect, test, type Page } from '@playwright/test';

const source = (path: string) =>
  readFileSync(new URL(`../src/lib/${path}`, import.meta.url), 'utf8');

async function signIn(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const closeTour = page.getByRole('button', { name: 'Close tour' });
  if (await closeTour.isVisible()) await closeTour.click();
}

test('generation and voice controls expose resolved selection semantics', () => {
  const models = source('api-models.ts');
  const stageCard = source('WorkflowStageCard.svelte');
  const runDialogs = source('WorkflowRunDialogs.svelte');
  const workspace = source('SessionWorkspace.svelte');
  const voiceLibrary = source('VoiceLibraryModal.svelte');

  expect(models).toContain('resolved_input?:');
  expect(models).toContain('reasons?: string[];');
  expect(stageCard).toContain('Generate from: {stage.resolved_input.label}');
  expect(stageCard).toContain('` v${stage.resolved_input.version}`');
  expect(stageCard).toContain('Change the input role in Customize workflow.');
  expect(stageCard).toContain("stage's Selected version control");

  expect(runDialogs).toContain('source_lineage_changed');
  expect(runDialogs).toContain('Selected output lineage changed');
  expect(runDialogs).toContain('settings_unverifiable');
  expect(runDialogs).toContain(
    'Legacy output has no comparable settings history'
  );
  expect(runDialogs).toContain('Reuse selected outputs');
  expect(runDialogs).toContain('Rerun prerequisites');
  expect(workspace).toContain('pending.mismatches.map((item) => item.stage)');

  expect(voiceLibrary).toContain('class="modal-scroll min-h-0 flex-1');
  expect(workspace).toContain('availableTtsServices');
  expect(workspace).toContain('unavailableTtsServices');
  expect(workspace).toContain('label="Available"');
  expect(workspace).toContain('label="Unavailable"');
  expect(workspace).toContain('class="text-[var(--muted)]"');
  expect(workspace).toContain('available: true');
  expect(workspace).toContain('clonedVoiceGroups');
  expect(workspace).toContain('Multilingual / language not set');
});

test('generation settings make source, availability, voice language, and reuse choices visible', async ({
  page
}) => {
  await signIn(page);
  const authStatus = await page.request.get('/api/v1/auth/status');
  const csrfToken = (await authStatus.json()).csrf_token;
  const created = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': csrfToken },
    data: {
      name: `Generation controls ${crypto.randomUUID()}`,
      workflow_kind: 'voiceover'
    }
  });
  expect(created.ok()).toBeTruthy();
  const session = await created.json();

  await page.route(
    `**/api/v1/sessions/${session.id}/workflow`,
    async (route) => {
      const response = await route.fetch();
      const snapshot = await response.json();
      const generation = snapshot.stages.find(
        (stage: { key: string }) => stage.key === 'generate_audio'
      );
      generation.status = 'ready';
      generation.artifact = null;
      generation.artifacts = [];
      generation.resolved_input = {
        artifact_id: 'translation-v3',
        role: 'translation',
        stage_key: 'translate',
        version: 3,
        label: 'Translation'
      };
      snapshot.sources = [
        {
          id: 'source-srt',
          filename: 'source.srt',
          kind: 'srt',
          role: 'upload'
        }
      ];
      await route.fulfill({ response, json: snapshot });
    }
  );

  const services = {
    default_service: 'offline-local',
    services: [
      {
        id: 'cloud',
        name: 'Cloud API',
        available: true,
        online: false,
        models: ['cloning'],
        default_model: 'cloning',
        default_voice: 'voice-en',
        supports_voice_cloning: true,
        supports_prebuilt_voices: false,
        model_voice_modes: { cloning: 'cloning' },
        live_voices: ['voice-en', 'voice-de', 'voice-unspecified']
      },
      {
        id: 'offline-local',
        name: 'Local engine',
        available: false,
        online: false,
        availability_reason: 'Service is not running',
        models: ['local']
      }
    ],
    value: {},
    revision: 1,
    previews: []
  };
  await page.route('**/api/v1/services/tts**', (route) =>
    route.fulfill({ contentType: 'application/json', json: services })
  );
  await page.route('**/api/v1/voices', (route) =>
    route.fulfill({
      contentType: 'application/json',
      json: {
        items: [
          {
            id: 'managed-en',
            name: 'English Ada',
            language: 'en',
            revision: 1,
            metadata_json: {
              providers: {
                cloud: { status: 'ready', voice_id: 'voice-en' }
              }
            }
          },
          {
            id: 'managed-de',
            name: 'German Bruno',
            language: 'de',
            revision: 1,
            metadata_json: {
              providers: {
                cloud: { status: 'ready', voice_id: 'voice-de' }
              }
            }
          },
          {
            id: 'managed-unspecified',
            name: 'Mystery Voice',
            language: null,
            revision: 1,
            metadata_json: {
              providers: {
                cloud: { status: 'ready', voice_id: 'voice-unspecified' }
              }
            }
          }
        ]
      }
    })
  );
  await page.route(
    `**/api/v1/sessions/${session.id}/stages/generate_audio/settings-mismatches`,
    (route) =>
      route.fulfill({
        contentType: 'application/json',
        json: {
          mismatches: [
            {
              stage: 'translate',
              changed_fields: [],
              reasons: ['source_lineage_changed']
            },
            {
              stage: 'correct',
              changed_fields: [],
              reasons: ['settings_unverifiable']
            }
          ]
        }
      })
  );
  let generationRunPayload: Record<string, unknown> | null = null;
  await page.route(
    `**/api/v1/sessions/${session.id}/stages/generate_audio/run`,
    (route) => {
      generationRunPayload = route.request().postDataJSON() as Record<
        string,
        unknown
      >;
      return route.fulfill({
        contentType: 'application/json',
        json: { id: 'generation-job', status: 'queued' }
      });
    }
  );

  await page.goto(`/sessions/${session.id}`);
  const generationCard = page
    .getByRole('heading', { name: 'Generate audio', exact: true })
    .locator('xpath=ancestor::article');
  await expect(generationCard).toContainText('Generate from: Translation v3');
  await generationCard.getByRole('button', { name: 'Settings' }).click();

  const settingsDialog = page.getByRole('dialog');
  const serviceSelect = settingsDialog.getByLabel('TTS service');
  await expect(serviceSelect).toHaveValue('cloud');
  await expect(
    serviceSelect.locator('optgroup[label="Available"] option')
  ).toHaveText(['Cloud API · available']);
  const unavailableOption = serviceSelect.locator(
    'optgroup[label="Unavailable"] option'
  );
  await expect(unavailableOption).toHaveText('Local engine · unavailable');
  await expect(unavailableOption).toBeDisabled();

  const voiceSelect = settingsDialog.getByLabel('Voice');
  await expect(voiceSelect.locator('optgroup')).toHaveCount(3);
  await expect(voiceSelect.locator('optgroup').nth(0)).toHaveAttribute(
    'label',
    'English'
  );
  await expect(voiceSelect.locator('optgroup').nth(1)).toHaveAttribute(
    'label',
    'German'
  );
  await expect(voiceSelect.locator('optgroup').nth(2)).toHaveAttribute(
    'label',
    'Multilingual / language not set'
  );
  await voiceSelect.selectOption('voice-en');

  await settingsDialog
    .getByRole('button', { name: 'Manage Voice Library' })
    .click();
  const voiceDialog = page.getByRole('dialog', { name: 'Voice Library' });
  const scroller = voiceDialog.locator('.modal-scroll');
  await expect(scroller).toHaveCSS('overflow-y', 'auto');
  await scroller.evaluate((element) => {
    const spacer = document.createElement('div');
    spacer.style.height = '2400px';
    spacer.setAttribute('data-scroll-test', 'true');
    element.append(spacer);
    element.scrollTop = element.scrollHeight;
  });
  await expect
    .poll(() => scroller.evaluate((element) => element.scrollTop))
    .toBeGreaterThan(0);
  await voiceDialog
    .getByRole('button', { name: 'Close Voice Library' })
    .click();
  await settingsDialog.getByRole('button', { name: 'Save settings' }).click();
  await expect(settingsDialog).toHaveCount(0);

  await generationCard.getByRole('button', { name: 'Run now' }).click();
  const mismatchDialog = page.getByRole('dialog', {
    name: 'Choose prerequisite outputs'
  });
  await expect(mismatchDialog).toContainText('Translation');
  await expect(mismatchDialog).toContainText('Correction');
  await expect(mismatchDialog).toContainText('Selected output lineage changed');
  await expect(mismatchDialog).toContainText(
    'Legacy output has no comparable settings history'
  );
  await expect(
    mismatchDialog.getByRole('button', {
      name: 'Reuse all listed prerequisite outputs without rerunning them'
    })
  ).toBeVisible();
  await expect(
    mismatchDialog.getByRole('button', { name: 'Rerun prerequisites' })
  ).toBeVisible();
  await mismatchDialog
    .getByRole('button', { name: 'Close settings change prompt' })
    .click();

  await page
    .getByLabel('Workspace mode')
    .getByRole('button', { name: 'Generate automatically' })
    .click();
  await page.getByRole('button', { name: 'Generate audio segments' }).click();
  await expect.poll(() => generationRunPayload).not.toBeNull();
  expect(generationRunPayload).not.toHaveProperty('reuse_stages');
  await expect(
    page.getByText(/Rerunning stale translate and correct/)
  ).toBeVisible();

  const currentTtsSettings = await page.request.get(
    `/api/v1/sessions/${session.id}/settings/tts`
  );
  const currentTtsSettingsBody = await currentTtsSettings.json();
  const savedUnavailable = await page.request.put(
    `/api/v1/sessions/${session.id}/settings/tts`,
    {
      headers: {
        'X-CSRF-Token': csrfToken,
        'If-Match': `"${currentTtsSettingsBody.revision}"`
      },
      data: {
        value: {
          tts_service: 'offline-local',
          service: 'offline-local'
        }
      }
    }
  );
  expect(savedUnavailable.ok()).toBeTruthy();
  await page.reload();
  const reloadedGenerationCard = page
    .getByRole('heading', { name: 'Generate audio', exact: true })
    .locator('xpath=ancestor::article');
  await reloadedGenerationCard
    .getByRole('button', { name: 'Settings' })
    .click();
  const unavailableDialog = page.getByRole('dialog');
  await expect(unavailableDialog.getByLabel('TTS service')).toHaveValue(
    'offline-local'
  );
  await expect(
    unavailableDialog.getByText('Service is not running')
  ).toBeVisible();
  await expect(
    unavailableDialog.getByRole('button', { name: 'Save settings' })
  ).toBeDisabled();
  await expect(
    unavailableDialog.getByRole('button', { name: 'Save as defaults' })
  ).toBeDisabled();
  await unavailableDialog.getByRole('button', { name: 'Cancel' }).click();
  await page
    .getByLabel('Workspace mode')
    .getByRole('button', { name: 'Review each stage' })
    .click();
  generationRunPayload = null;
  await reloadedGenerationCard.getByRole('button', { name: 'Run now' }).click();
  await expect(page.getByText('Service is not running')).toBeVisible();
  expect(generationRunPayload).toBeNull();
});
