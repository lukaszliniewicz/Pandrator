import { expect, test, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { readFile } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import type {
  SoundCandidate,
  SoundCatalogue,
  SoundGeneration,
  SoundModel
} from '../src/lib/sound-api';

const soundModel: SoundModel = {
  id: 'stable_audio_3_small_sfx_q8_0',
  label: 'Sound effects model',
  family: 'stable_audio',
  cached: true,
  kinds: ['sound'],
  description: 'Sound effects only',
  backends: ['cpu'],
  duration: { minimum: 1, maximum: 30, default: 4 },
  supports_lyrics: false,
  seed_maximum: 4294967295,
  download_bytes: 0,
  available: true,
  parameters: {}
};
const musicModel: SoundModel = {
  ...soundModel,
  id: 'stable_audio_3_small_music_q8_0',
  label: 'Music model',
  kinds: ['music'],
  description: 'Music only',
  duration: { minimum: 1, maximum: 90, default: 12 }
};

async function fixture(
  page: Page,
  {
    musicAvailable = false,
    delayCatalogue = false,
    candidates = [],
    music = musicModel
  }: {
    musicAvailable?: boolean;
    delayCatalogue?: boolean;
    candidates?: SoundCandidate[];
    music?: SoundModel;
  } = {}
) {
  const errors: string[] = [];
  const requests: SoundGeneration[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(
    page.getByRole('heading', { name: 'Create a project', exact: true })
  ).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const catalogue: SoundCatalogue = {
    runtime: { available: true, version: '0.9.1' },
    models: [soundModel, { ...music, available: musicAvailable }],
    defaults: { model_id: soundModel.id, duration_seconds: 4, threads: 4 }
  };
  let releaseCatalogue = () => {};
  const catalogueReady = delayCatalogue
    ? new Promise<void>((resolve) => {
        releaseCatalogue = resolve;
      })
    : Promise.resolve();
  await page.route('**/api/v1/sounds/catalogue', async (route) => {
    await catalogueReady;
    await route.fulfill({ json: catalogue });
  });
  await page.route('**/api/v1/sounds/candidates?*', (route) =>
    route.fulfill({ json: { items: candidates, next_cursor: null } })
  );
  await page.route('**/api/v1/sounds/candidates/*', (route) =>
    route.fulfill({
      json: candidates.find((item) => route.request().url().endsWith(item.id))
    })
  );
  await page.route('**/api/v1/sounds/generate', (route) => {
    requests.push(route.request().postDataJSON() as SoundGeneration);
    return route.fulfill({
      status: 400,
      json: {
        code: 'fixture_request',
        message: 'Request recorded by the test.'
      }
    });
  });
  await page.goto('/sounds');
  await expect(
    page.getByRole('combobox', { name: 'Create', exact: true })
  ).toBeVisible();
  if (!delayCatalogue)
    await expect(
      page.getByRole('combobox', { name: 'Model', exact: true })
    ).toHaveValue(soundModel.id);
  return { errors, requests, releaseCatalogue };
}

for (const width of [1280, 390]) {
  test(`an unavailable music choice clears sound settings and cannot submit a sound-only model at ${width}px`, async ({
    page
  }, info) => {
    await page.setViewportSize({ width, height: 900 });
    const f = await fixture(page);
    const create = page.getByRole('combobox', { name: 'Create', exact: true });
    const model = page.getByRole('combobox', { name: 'Model', exact: true });
    const prompt = page.getByLabel('Description', { exact: true });
    await create.selectOption('music');
    await prompt.fill('A quiet instrumental piano intro.');
    await expect(model).toHaveValue('');
    await expect(model).toBeDisabled();
    await expect(
      page.getByText('No models are available for music.', { exact: true })
    ).toBeVisible();
    await expect(
      page.getByLabel('Duration (seconds)', { exact: true })
    ).toBeDisabled();
    await expect(
      page.getByRole('button', { name: 'Generate', exact: true })
    ).toBeDisabled();
    await expect(
      page.getByText('Length: 1–30 seconds.', { exact: true })
    ).not.toBeVisible();
    expect(
      await page.evaluate(
        () =>
          document.documentElement.scrollWidth <=
          document.documentElement.clientWidth
      )
    ).toBe(true);
    const accessibility = await new AxeBuilder({ page }).analyze();
    expect(
      accessibility.violations.filter((violation) =>
        ['serious', 'critical'].includes(violation.impact ?? '')
      )
    ).toEqual([]);
    await page.screenshot({
      path: info.outputPath('unavailable-music.png'),
      fullPage: true
    });
    // The submit handler must guard programmatic submissions as well as clicks.
    await page.locator('form').first().dispatchEvent('submit');
    await expect(page.getByRole('alert')).toContainText(
      'Choose an available model and backend'
    );
    expect(f.requests).toEqual([]);

    await create.selectOption('sound');
    await expect(model).toHaveValue(soundModel.id);
    await expect(prompt).toHaveValue('A quiet instrumental piano intro.');
    await expect(
      page.getByLabel('Duration (seconds)', { exact: true })
    ).toHaveValue('4');
    await page.getByRole('button', { name: 'Generate', exact: true }).click();
    await expect.poll(() => f.requests.length).toBe(1);
    expect(f.requests[0]).toMatchObject({
      kind: 'sound',
      model_id: soundModel.id,
      parameters: {}
    });
    expect(f.errors).toEqual([]);
  });
}

test('a late catalogue response preserves the selected kind and chooses its compatible model', async ({
  page
}) => {
  const f = await fixture(page, { musicAvailable: true, delayCatalogue: true });
  try {
    await page
      .getByRole('combobox', { name: 'Create', exact: true })
      .selectOption('music');
    await page
      .getByLabel('Description', { exact: true })
      .fill('A slow piano melody.');
    f.releaseCatalogue();
    await expect(
      page.getByRole('combobox', { name: 'Model', exact: true })
    ).toHaveValue(musicModel.id);
    await expect(
      page.getByLabel('Duration (seconds)', { exact: true })
    ).toHaveValue('12');
    await page.getByRole('button', { name: 'Generate', exact: true }).click();
    await expect.poll(() => f.requests.length).toBe(1);
    expect(f.requests[0]).toMatchObject({
      kind: 'music',
      model_id: musicModel.id,
      duration_seconds: 12
    });
    expect(f.errors).toEqual([]);
  } finally {
    f.releaseCatalogue();
  }
});

test('reusing an incompatible take cannot submit until a compatible model is chosen', async ({
  page
}) => {
  const candidate: SoundCandidate = {
    id: 'incompatible-take',
    status: 'ready',
    job_id: null,
    artifact_id: null,
    item_id: null,
    duration_seconds: 4,
    sample_rate: 44100,
    channels: 1,
    created_at: '2026-10-12T00:00:00Z',
    provenance: {
      model_id: soundModel.id,
      kind: 'music',
      prompt: 'A recorded music description.',
      backend: 'cpu',
      parameters: { guidance_scale: 2 }
    }
  };
  const f = await fixture(page, {
    musicAvailable: true,
    candidates: [candidate]
  });
  await page.getByRole('button', { name: 'Use settings', exact: true }).click();
  const model = page.getByRole('combobox', { name: 'Model', exact: true });
  await expect(
    page.getByRole('combobox', { name: 'Create', exact: true })
  ).toHaveValue('music');
  await expect(model).toHaveValue('');
  await expect(
    page.getByRole('button', { name: 'Generate', exact: true })
  ).toBeDisabled();
  await page.locator('form').first().dispatchEvent('submit');
  await expect(page.getByRole('alert')).toContainText(
    'Choose an available model and backend'
  );
  expect(f.requests).toEqual([]);
  await model.selectOption(musicModel.id);
  await page.getByRole('button', { name: 'Generate', exact: true }).click();
  await expect.poll(() => f.requests.length).toBe(1);
  expect(f.requests[0]).toMatchObject({
    kind: 'music',
    model_id: musicModel.id,
    parameters: {}
  });
  expect(f.errors).toEqual([]);
});

const yueModel: SoundModel = {
  ...musicModel,
  id: 'yue2_main_q8_0',
  label: 'YuE2 Q8',
  family: 'yue2',
  supports_lyrics: true,
  duration: { minimum: 8, maximum: 360, default: 30 },
  parameters: {
    instrumental: { type: 'boolean', label: 'Instrumental', default: false },
    cot: {
      type: 'string',
      label: 'Chain-of-thought mode',
      default: 'full',
      options: ['off', 'melody', 'full']
    }
  },
  instrumental_adapter: {
    id: 'yue2_instrumental_ar_v3abc_bf16',
    repository: 'Mothersuperior/YuE2-instrumental-cot-full-loras',
    revision: '947f2f4b28978b2b6c3e316e6a87925c76bf3c4b',
    filename: 'ar_lora_inst_v3abc.bf16.safetensors',
    size_bytes: 139502088,
    sha256: 'e408fd3148b75b1165f7ddbf63db575d83bb6402a0b5f876fcb767dbcb2c5414',
    license: 'CC-BY-NC-4.0',
    source_url:
      'https://huggingface.co/Mothersuperior/YuE2-instrumental-cot-full-loras'
  }
};
const instrumentalCandidate: SoundCandidate = {
  id: 'instrumental-take',
  status: 'ready',
  job_id: null,
  artifact_id: 'instrumental-audio',
  item_id: null,
  duration_seconds: 8,
  sample_rate: 44100,
  channels: 2,
  created_at: '2026-10-12T12:00:00Z',
  provenance: {
    model_id: yueModel.id,
    kind: 'music',
    prompt: 'A solo piano introduction.',
    lyrics: '[instrumental]',
    parameters: { instrumental: true, cot: 'full' },
    backend: 'cpu',
    duration_seconds: 8
  }
};

for (const width of [1280, 390]) {
  test(`YuE2 instrumental settings restore and preserve typed lyrics at ${width}px`, async ({
    page
  }, info) => {
    await page.setViewportSize({ width, height: 1000 });
    const f = await fixture(page, {
      musicAvailable: true,
      music: yueModel,
      candidates: [instrumentalCandidate]
    });
    await page
      .getByRole('button', { name: 'Use settings', exact: true })
      .click();
    const toggle = page.getByRole('checkbox', {
      name: 'Instrumental',
      exact: true
    });
    await expect(toggle).toBeChecked();
    await expect(
      page.getByRole('link', { name: /Adapter licence: CC BY-NC 4.0/ })
    ).toBeVisible();
    await expect(
      page.getByRole('textbox', { name: /Lyrics/ })
    ).not.toBeVisible();
    await page.screenshot({
      path: info.outputPath('yue2-instrumental.png'),
      fullPage: true
    });
    await toggle.uncheck();
    const lyrics = page.getByRole('textbox', { name: /Lyrics/ });
    await expect(lyrics).toHaveValue('');
    await lyrics.fill('Lyrics I want to keep');
    await page.getByText('Advanced settings', { exact: true }).click();
    await page
      .getByRole('combobox', { name: /Chain-of-thought mode/ })
      .selectOption('off');
    await toggle.check();
    await expect(lyrics).not.toBeVisible();
    await expect(
      page.getByRole('combobox', { name: /Chain-of-thought mode/ })
    ).not.toBeVisible();
    await page.getByRole('button', { name: 'Generate', exact: true }).click();
    await expect.poll(() => f.requests.length).toBe(1);
    expect(f.requests[0]).toMatchObject({
      model_id: yueModel.id,
      lyrics: '[instrumental]',
      parameters: { instrumental: true, cot: 'full' }
    });
    await toggle.uncheck();
    await expect(lyrics).toHaveValue('Lyrics I want to keep');
    await page.getByRole('button', { name: 'Generate', exact: true }).click();
    await expect.poll(() => f.requests.length).toBe(2);
    expect(f.requests[1].lyrics).toBe('Lyrics I want to keep');
    expect(f.requests[1].parameters?.instrumental).toBe(false);
    await toggle.check();
    await page
      .getByRole('combobox', { name: 'Create', exact: true })
      .selectOption('sound');
    await expect(toggle).not.toBeVisible();
    await page.getByRole('button', { name: 'Generate', exact: true }).click();
    await expect.poll(() => f.requests.length).toBe(3);
    expect(f.requests[2].parameters).toEqual({});
    await page
      .getByRole('combobox', { name: 'Create', exact: true })
      .selectOption('music');
    await expect(toggle).not.toBeChecked();
    expect(
      await page.evaluate(
        () =>
          document.documentElement.scrollWidth <=
          document.documentElement.clientWidth
      )
    ).toBe(true);
    const accessibility = await new AxeBuilder({ page }).analyze();
    expect(
      accessibility.violations.filter((v) =>
        ['serious', 'critical'].includes(v.impact ?? '')
      )
    ).toEqual([]);
    expect(f.errors).toEqual([]);
  });
}

function sampleWav() {
  const samples = 8000;
  const wav = Buffer.alloc(44 + samples * 2);
  wav.write('RIFF', 0);
  wav.writeUInt32LE(wav.length - 8, 4);
  wav.write('WAVEfmt ', 8);
  wav.writeUInt32LE(16, 16);
  wav.writeUInt16LE(1, 20);
  wav.writeUInt16LE(1, 22);
  wav.writeUInt32LE(8000, 24);
  wav.writeUInt32LE(16000, 28);
  wav.writeUInt16LE(2, 32);
  wav.writeUInt16LE(16, 34);
  wav.write('data', 36);
  wav.writeUInt32LE(samples * 2, 40);
  for (let i = 0; i < samples; i++)
    wav.writeInt16LE(Math.round(5000 * Math.sin(i / 10)), 44 + i * 2);
  return wav;
}

for (const format of ['wav', 'flac']) {
  test(`ready candidates and saved variants download the original ${format} file`, async ({
    page
  }, info) => {
    const failed = {
      ...instrumentalCandidate,
      id: 'failed-take',
      status: 'failed',
      artifact_id: null
    };
    const f = await fixture(page, {
      musicAvailable: true,
      music: yueModel,
      candidates: [failed]
    });
    await expect(
      page.getByRole('link', { name: 'Download candidate audio', exact: true })
    ).toHaveCount(0);
    // Exercise the authenticated file endpoint and real import/save lifecycle.
    // Browser download requests do not use page-scoped route interception.
    await page.unroute('**/api/v1/sounds/candidates?*');
    await page.unroute('**/api/v1/sounds/candidates/*');
    let original = sampleWav();
    if (format === 'flac') {
      const flacPath = info.outputPath('sample.flac');
      // A seekable output lets FFmpeg finalize the FLAC duration metadata.
      execFileSync('ffmpeg', ['-v', 'error', '-i', 'pipe:0', '-y', flacPath], {
        input: original
      });
      original = await readFile(flacPath);
    }
    const filename = `download-sample.${format}`;
    await page.getByLabel('Import sound audio', { exact: true }).setInputFiles({
      name: filename,
      mimeType: `audio/${format}`,
      buffer: original
    });
    const take = page
      .getByRole('article', { name: 'Candidate take', exact: true })
      .filter({
        has: page.getByRole('textbox', { name: 'Name', exact: true })
      });
    const link = take.getByRole('link', {
      name: 'Download candidate audio',
      exact: true
    });
    await expect(link).toBeVisible();
    const candidateDownload = page.waitForEvent('download');
    await link.click();
    const download = await candidateDownload;
    expect(download.suggestedFilename()).toMatch(new RegExp(`\\.${format}$`));
    expect(await readFile((await download.path())!)).toEqual(original);
    await take
      .getByRole('textbox', { name: 'Name', exact: true })
      .fill(`Saved piano ${format}`);
    await take
      .locator('form')
      .getByRole('button', { name: 'Save', exact: true })
      .click();
    const savedLink = page.getByRole('link', {
      name: `Download Saved piano ${format} variant`,
      exact: true
    });
    await expect(savedLink).toBeVisible();
    const savedDownload = page.waitForEvent('download');
    await savedLink.click();
    const saved = await savedDownload;
    expect(saved.suggestedFilename()).toMatch(new RegExp(`\\.${format}$`));
    expect(await readFile((await saved.path())!)).toEqual(original);
    await page.screenshot({
      path: info.outputPath(`saved-${format}-download.png`),
      fullPage: true
    });
    expect(f.errors).toEqual([]);
  });
}
