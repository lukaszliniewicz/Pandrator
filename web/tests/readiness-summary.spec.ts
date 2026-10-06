import { expect, test } from '@playwright/test';

const fixtures = [
  {
    name: 'current local runtimes without legacy services',
    count: 2,
    crispasr: true,
    audioCpp: true,
    capabilities: {
      services: { xtts: false, kokoro: false, rvc: false, chatterbox: false },
      stt: {
        crispasr: true,
        audio_cpp_tools: { runtime: { available: true } },
        models: { whisper: { installed: true }, qwen3: { installed: true } },
        forced_aligners: [{ available: true, engine: 'audio.cpp' }]
      },
      operations: { transcribe_voice: true }
    }
  },
  {
    name: 'legacy services and duplicate operation identities',
    count: 6,
    crispasr: true,
    audioCpp: true,
    capabilities: {
      services: { xtts: true, kokoro: true, rvc: true, chatterbox: true },
      rvc: { available: true },
      stt: {
        crispasr: true,
        audio_cpp_tools: {
          runtime: { available: true },
          models: {
            qwen3_asr_0_6b: { cached: true },
            htdemucs_q8_0: { cached: true }
          }
        },
        models: { whisper: { installed: true }, qwen3: { installed: true } },
        forced_aligners: [{ available: true, engine: 'audio.cpp' }]
      },
      operations: { transcribe_voice: true }
    }
  },
  {
    name: 'cached weights without installed runtimes',
    count: 0,
    crispasr: false,
    audioCpp: false,
    capabilities: {
      services: {},
      stt: {
        crispasr: false,
        audio_cpp_tools: {
          runtime: { available: false },
          models: { qwen3_asr_0_6b: { cached: true } }
        },
        models: { whisper: { installed: true }, qwen3: { installed: true } }
      }
    }
  },
  {
    name: 'older minimal capability response',
    count: 1,
    crispasr: false,
    audioCpp: false,
    capabilities: { services: { xtts: true } }
  }
];

for (const width of [1280, 390]) {
  test(`home and setup show current local speech components at ${width}px`, async ({
    page
  }, info) => {
    await page.setViewportSize({ width, height: 844 });
    let capabilities: object = fixtures[0].capabilities;
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.route('**/api/v1/capabilities*', (route) =>
      route.fulfill({ json: capabilities })
    );
    await page.route('**/api/v1/events/snapshot*', async (route) => {
      const response = await route.fetch();
      const snapshot = await response.json();
      await route.fulfill({
        response,
        json: { ...snapshot, capabilities }
      });
    });
    await page.goto('/');
    await page.getByLabel('Owner password').fill('pandrator-e2e');
    await page.getByRole('button', { name: 'Sign in' }).click();
    await expect(
      page.getByRole('heading', { name: 'Create a session' })
    ).toBeVisible();
    const tour = page.getByRole('button', { name: 'Close tour' });
    if (await tour.isVisible()) await tour.click();

    for (const fixture of fixtures) {
      await test.step(fixture.name, async () => {
        capabilities = fixture.capabilities;
        await page.goto('/');
        await page.reload();
        const readiness = page
          .locator('aside')
          .filter({ hasText: 'Readiness' });
        await expect(
          readiness.getByText(
            new RegExp(
              `^${fixture.count} (?:local|installed) components detected$`
            )
          )
        ).toBeVisible();
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth
          )
        ).toBeTruthy();
        if (fixture === fixtures[0]) {
          await page.screenshot({
            path: info.outputPath('home-readiness.png'),
            fullPage: true
          });
        }
        await page.goto('/?setup=1');
        const checklist = page.getByRole('dialog', {
          name: 'Set up Pandrator'
        });
        await expect(
          checklist.getByText(
            `${fixture.count} local components currently detected.`
          )
        ).toBeVisible();
        await expect(
          checklist.getByText(
            `CrispASR is ${fixture.crispasr ? 'detected' : 'not detected'}.`,
            {
              exact: false
            }
          )
        ).toBeVisible();
        await expect(
          checklist.getByText(
            `audio.cpp is ${fixture.audioCpp ? 'detected' : 'not detected'}.`,
            {
              exact: false
            }
          )
        ).toBeVisible();
        if (fixture === fixtures[0]) {
          await checklist
            .getByText('Readiness summary', { exact: true })
            .scrollIntoViewIfNeeded();
          expect(
            await page.evaluate(
              () => document.documentElement.scrollWidth <= window.innerWidth
            )
          ).toBeTruthy();
          await page.screenshot({
            path: info.outputPath('setup-readiness.png'),
            fullPage: true
          });
        }
      });
    }
    expect(errors).toEqual([]);
  });
}
