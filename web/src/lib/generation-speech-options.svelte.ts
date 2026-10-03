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
  private disposed = false;

  constructor(
    private readonly getSessionId: () => string,
    private readonly normalizeId: (value: unknown) => string
  ) {}

  dispose() {
    this.disposed = true;
    this.request++;
  }

  async load() {
    const request = ++this.request;
    const scope = this.getSessionId();
    const isCurrent = () =>
      !this.disposed &&
      request === this.request &&
      scope === this.getSessionId();
    this.loading = true;
    try {
      const [settings, services, voices] = await Promise.all([
        sessionApi.settings(this.getSessionId(), 'tts'),
        getTtsCompactCatalogue(true),
        getVoiceLibrary()
      ]);
      if (!isCurrent()) return;
      this.error = '';
      this.settings = settings.effective ?? {};
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
      if (service?.api_base && service.online !== false) {
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
