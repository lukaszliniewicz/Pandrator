import { Buffer } from 'node:buffer';
import { expect, test, type Locator, type Page } from '@playwright/test';
import type { CatalogVoice } from '../src/lib/voice-library-api';

function silentWav() {
  const samples = 24000;
  const header = Buffer.alloc(44);
  header.write('RIFF', 0);
  header.writeUInt32LE(36 + samples * 2, 4);
  header.write('WAVE', 8);
  header.write('fmt ', 12);
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20);
  header.writeUInt16LE(1, 22);
  header.writeUInt32LE(8000, 24);
  header.writeUInt32LE(16000, 28);
  header.writeUInt16LE(2, 32);
  header.writeUInt16LE(16, 34);
  header.write('data', 36);
  header.writeUInt32LE(samples * 2, 40);
  return Buffer.concat([header, Buffer.alloc(samples * 2)]);
}

async function fixture(page: Page, initialArtifact: string) {
  let artifact = initialArtifact;
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const voice: CatalogVoice = {
    key: 'managed:source-lifetime-voice',
    reference: { kind: 'managed', voice_id: 'source-lifetime-voice' },
    id: 'source-lifetime-voice',
    kind: 'managed',
    name: 'Source lifetime speaker',
    description: 'A saved preview updated while its player remains open.',
    voice_category: 'unspecified',
    profile: {
      schema_version: 1,
      pitch: null,
      perceived_age: null,
      textures: [],
      delivery_presets: [],
      use_cases: [],
      languages: [],
      tags: [],
      evidence: {}
    },
    origin: 'user',
    revision: 1,
    collections: [],
    compatibility: []
  };
  await page.route('**/api/v1/services/tts**', (route) =>
    route.fulfill({ json: { services: [], default_service: '' } })
  );
  await page.route('**/api/v1/voice-catalog?**', (route) =>
    route.fulfill({
      json: {
        items: [{ ...voice, preview_artifact_id: artifact }],
        total: 1,
        next_cursor: null,
        facets: {},
        taxonomy: {},
        collections: []
      }
    })
  );
  await page.route('**/api/v1/artifacts/source-lifetime-*/content', (route) =>
    route.fulfill({
      contentType: 'audio/wav',
      body: route.request().url().includes('source-lifetime-invalid/')
        ? Buffer.from('This is deliberately not WAV audio.')
        : silentWav()
    })
  );
  await page.goto('/voices');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(
    page.getByRole('heading', { name: 'Voice library', exact: true })
  ).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const row = page.locator('article').filter({
    has: page.getByRole('button', { name: voice.name, exact: true })
  });
  await row.getByRole('button', { name: 'Listen', exact: true }).click();
  const player = row.locator('.audio-player');
  await expect(player).toBeVisible();
  const originalAudio = await player.locator('audio').elementHandle();
  expect(originalAudio).not.toBeNull();
  return {
    player,
    errors,
    originalAudio,
    async changeSource(next: string) {
      artifact = next;
      const response = page.waitForResponse(
        (response) =>
          response.url().includes('/api/v1/voice-catalog?') && response.ok()
      );
      await page
        .getByLabel('Search voices', { exact: true })
        .fill(`Source lifetime ${next}`);
      await response;
      await expect
        .poll(() =>
          player.locator('audio').evaluate((audio: HTMLAudioElement) => ({
            src: audio.currentSrc,
            duration: audio.duration,
            readyState: audio.readyState
          }))
        )
        .toEqual({
          src: expect.stringContaining(`/api/v1/artifacts/${next}/content`),
          duration: 3,
          readyState: expect.any(Number)
        });
      expect(
        await originalAudio!.evaluate(
          (audio) =>
            audio.isConnected &&
            audio === document.querySelector('.audio-player audio')
        )
      ).toBe(true);
    }
  };
}

async function setPreferences(player: Locator) {
  const volume = player.getByRole('slider', { name: 'Volume', exact: true });
  await volume.focus();
  await volume.press('Home');
  for (let step = 0; step < 6; step++) await volume.press('ArrowRight');
  await expect
    .poll(() =>
      player
        .locator('audio')
        .evaluate((audio: HTMLAudioElement) => audio.volume)
    )
    .toBeCloseTo(0.3);
  await player.getByRole('button', { name: 'Mute', exact: true }).click();
}

async function assertFreshPlayer(player: Locator) {
  await expect(player.getByRole('alert')).toHaveCount(0);
  await expect(
    player.getByRole('button', { name: 'Reload audio' })
  ).toHaveCount(0);
  await expect(
    player.getByRole('button', { name: 'Play', exact: true })
  ).toBeVisible();
  await expect(
    player.getByRole('slider', { name: 'Playback position' })
  ).toHaveValue('0');
  expect(
    await player.locator('audio').evaluate((audio: HTMLAudioElement) => ({
      paused: audio.paused,
      muted: audio.muted,
      volume: audio.volume
    }))
  ).toEqual({ paused: true, muted: true, volume: expect.closeTo(0.3) });
}

for (const width of [1280, 390]) {
  test(`native audio error clears when the mounted catalogue player changes source at ${width}px`, async ({
    page
  }, info) => {
    await page.setViewportSize({ width, height: 900 });
    const { player, changeSource, errors } = await fixture(
      page,
      'source-lifetime-invalid'
    );
    await expect(player.getByRole('alert')).toBeVisible();
    await setPreferences(player);
    await changeSource('source-lifetime-valid');
    await page.screenshot({
      path: info.outputPath('native-source-replacement.png'),
      fullPage: true
    });
    await assertFreshPlayer(player);
    await player.getByRole('button', { name: 'Unmute', exact: true }).click();
    const play = player.getByRole('button', { name: 'Play', exact: true });
    await play.focus();
    await play.press('Enter');
    await expect(
      player.getByRole('button', { name: 'Pause', exact: true })
    ).toBeVisible();
    expect(
      await player
        .locator('audio')
        .evaluate((audio: HTMLAudioElement) => audio.paused)
    ).toBe(false);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth
      )
    ).toBe(true);
    expect(errors).toEqual([]);
  });

  for (const returnToOriginal of [false, true]) {
    test(`obsolete Play rejection is ignored after ${returnToOriginal ? 'A-B-A' : 'A-B'} source replacement at ${width}px`, async ({
      page
    }, info) => {
      await page.setViewportSize({ width, height: 900 });
      const { player, changeSource, errors } = await fixture(
        page,
        'source-lifetime-old'
      );
      await expect
        .poll(() =>
          player
            .locator('audio')
            .evaluate((audio: HTMLAudioElement) => audio.duration)
        )
        .toBe(3);
      await setPreferences(player);
      await player.locator('audio').evaluate((audio: HTMLAudioElement) => {
        const fixtureWindow = window as Window & {
          releaseOldPlay?: () => void;
          restorePlay?: () => void;
        };
        const nativePlay = audio.play;
        audio.play = () =>
          new Promise<void>((_resolve, reject) => {
            fixtureWindow.releaseOldPlay = () =>
              reject(
                new DOMException('Previous source replaced.', 'AbortError')
              );
          });
        fixtureWindow.restorePlay = () => {
          audio.play = nativePlay;
        };
      });
      await player.getByRole('button', { name: 'Play', exact: true }).click();
      await expect
        .poll(() =>
          page.evaluate(
            () =>
              typeof (
                window as Window & {
                  releaseOldPlay?: () => void;
                }
              ).releaseOldPlay
          )
        )
        .toBe('function');
      await changeSource('source-lifetime-new');
      if (returnToOriginal) await changeSource('source-lifetime-old');
      await page.evaluate(() => {
        const fixtureWindow = window as Window & {
          releaseOldPlay?: () => void;
          restorePlay?: () => void;
        };
        fixtureWindow.restorePlay?.();
        fixtureWindow.releaseOldPlay?.();
      });
      // Let the rejected promise's catch finish before checking the visible state.
      await page.evaluate(
        () =>
          new Promise<void>((resolve) => requestAnimationFrame(() => resolve()))
      );
      await page.screenshot({
        path: info.outputPath('obsolete-play-rejection.png'),
        fullPage: true
      });
      await assertFreshPlayer(player);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth
        )
      ).toBe(true);
      expect(errors).toEqual([]);
    });
  }
}
