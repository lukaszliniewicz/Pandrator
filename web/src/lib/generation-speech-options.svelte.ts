import type { TtsCatalogue, VoiceRecord } from './api-models';
import { sessionApi } from './domain-api';
import { errorMessage } from './errors';
import {
  getTtsCompactCatalogue,
  getVoiceLibrary,
  speechCatalogueKey
} from './tts-catalogue-cache';

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

  constructor(private readonly getSessionId: () => string) {}

  dispose() {
    this.disposed = true;
    this.request++;
    this.settingsRequest++;
  }

  /** Project mode changes do not require catalogue downloads or discovery. */
  async refreshSettings() {
    const request = ++this.settingsRequest;
    const scope = this.getSessionId();
    const scopeKey = speechCatalogueKey({ sessionId: scope });
    const isCurrent = () =>
      !this.disposed &&
      request === this.settingsRequest &&
      scope === this.getSessionId() &&
      scopeKey === speechCatalogueKey({ sessionId: this.getSessionId() });
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
    const scopeKey = speechCatalogueKey({ sessionId: scope });
    const isCurrent = () =>
      !this.disposed &&
      request === this.request &&
      scope === this.getSessionId() &&
      scopeKey === speechCatalogueKey({ sessionId: this.getSessionId() });
    this.loading = true;
    try {
      const [settings, services, voices] = await Promise.all([
        sessionApi.settings(scope, 'tts'),
        editing
          ? getTtsCompactCatalogue(false, false, { sessionId: scope })
          : Promise.resolve({ services: [] }),
        editing
          ? getVoiceLibrary(false, { sessionId: scope })
          : Promise.resolve({ items: [] })
      ]);
      if (!isCurrent()) return;
      if (settingsRequest === this.settingsRequest) {
        this.error = '';
        this.settings = settings.effective ?? {};
      }
      this.catalogue = services;
      this.voices = voices.items ?? [];
    } catch (caught) {
      if (isCurrent()) this.error = errorMessage(caught);
    } finally {
      if (isCurrent()) this.loading = false;
    }
  }
}
