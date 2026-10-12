import type { TtsCatalogue, VoiceRecord } from './api-models';
import { sessionApi } from './domain-api';
import { errorMessage } from './errors';
import { getTtsCompactCatalogue, getVoiceLibrary } from './tts-catalogue-cache';

/** Component-local speech options and the request lifetime that may update them. */
export class GenerationSpeechOptionsState {
  loading = $state(false);
  error = $state('');
  settings = $state<Record<string, unknown>>({});
  catalogue = $state<TtsCatalogue>({ services: [] });
  voices = $state<VoiceRecord[]>([]);
  private request = 0;
  private settingsRequest = 0;
  private disposed = false;

  constructor(
    private readonly getSessionId: () => string,
    private readonly normalizeId: (value: unknown) => string
  ) {}

  dispose() {
    this.disposed = true;
    this.request++;
    this.settingsRequest++;
  }

  /** Project mode changes do not require catalogue downloads or discovery. */
  async refreshSettings() {
    const request = ++this.settingsRequest;
    const scope = this.getSessionId();
    const isCurrent = () =>
      !this.disposed &&
      request === this.settingsRequest &&
      scope === this.getSessionId();
    try {
      const settings = await sessionApi.settings(scope, 'tts');
      if (!isCurrent()) return;
      this.settings = settings.effective ?? {};
      this.error = '';
    } catch (caught) {
      if (isCurrent()) this.error = errorMessage(caught);
    }
  }

  async load({ editing = true } = {}) {
    const request = ++this.request;
    const settingsRequest = ++this.settingsRequest;
    const scope = this.getSessionId();
    const isCurrent = () =>
      !this.disposed &&
      request === this.request &&
      scope === this.getSessionId();
    this.loading = true;
    try {
      const [settings, services, voices] = await Promise.all([
        sessionApi.settings(scope, 'tts'),
        getTtsCompactCatalogue(),
        editing ? getVoiceLibrary() : Promise.resolve({ items: [] })
      ]);
      if (!isCurrent()) return;
      if (settingsRequest === this.settingsRequest) {
        this.error = '';
        this.settings = settings.effective ?? {};
      }
      this.catalogue = services;
      this.voices = voices.items ?? [];

      const service = services.services.find((candidate) =>
        [candidate.id, candidate.name].some(
          (value) =>
            this.normalizeId(value) ===
            this.normalizeId(
              this.settings.service ??
                this.settings.tts_service ??
                services.default_service
            )
        )
      );
      if (editing && service?.api_base && service.online !== false) {
        try {
          const discovered = await sessionApi.discoverTts(
            service.api_base,
            service.id
          );
          if (!isCurrent()) return;
          if (discovered?.success) {
            const refreshed = this.catalogue.services.map((candidate) =>
              candidate.id === service.id
                ? {
                    ...candidate,
                    models: Array.from(
                      new Set([
                        ...(candidate.models ?? []),
                        ...(discovered.models ?? [])
                      ])
                    ),
                    voices: Array.from(
                      new Set([
                        ...(candidate.voices ?? []),
                        ...(discovered.voices ?? [])
                      ])
                    ),
                    live_voices: Array.from(new Set(discovered.voices ?? [])),
                    online: true
                  }
                : candidate
            );
            this.catalogue = { ...this.catalogue, services: refreshed };
          }
        } catch {
          // The saved catalogue remains useful when a backend has no discovery route.
        }
      }
    } catch (caught) {
      if (isCurrent()) this.error = errorMessage(caught);
    } finally {
      if (isCurrent()) this.loading = false;
    }
  }
}
