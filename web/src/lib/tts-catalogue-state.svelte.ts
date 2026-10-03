import type {
  TtsCatalogue,
  TtsCompactCatalogue,
  TtsService,
  VoiceRecord
} from './api-models';
import { sessionApi } from './domain-api';
import { errorMessage } from './errors';
import { getTtsCompactCatalogue, getVoiceLibrary } from './tts-catalogue-cache';

type CatalogueLoad = {
  catalogue: TtsCompactCatalogue;
  current: () => boolean;
};

/** Component-local catalogue data; pending work can only update its current owner. */
export class TtsCatalogueState {
  catalogue = $state<TtsCatalogue>({ services: [] });
  voices = $state<VoiceRecord[]>([]);
  error = $state('');
  private loaded = false;
  private request = 0;
  private voiceRequest = 0;
  private disposed = false;
  private discoveryRequests = new Map<string, number>();

  invalidate() {
    this.request++;
    this.discoveryRequests.clear();
  }

  dispose() {
    this.disposed = true;
  }

  async load(force = false): Promise<CatalogueLoad | undefined> {
    if (this.loaded && !force) return;
    const request = ++this.request;
    const voiceRequest = ++this.voiceRequest;
    const isCurrent = () => !this.disposed && request === this.request;
    this.error = '';
    try {
      const [services, voices] = await Promise.all([
        getTtsCompactCatalogue(true, force),
        getVoiceLibrary(force)
      ]);
      if (!isCurrent()) return;
      this.catalogue = services;
      if (voiceRequest === this.voiceRequest) this.voices = voices.items ?? [];
      return { catalogue: services, current: isCurrent };
    } catch (caught) {
      if (isCurrent()) this.error = errorMessage(caught);
    }
  }

  async reloadVoices() {
    const catalogueRequest = this.request;
    const request = ++this.voiceRequest;
    const isCurrent = () =>
      !this.disposed &&
      catalogueRequest === this.request &&
      request === this.voiceRequest;
    try {
      const voices = await getVoiceLibrary(true);
      if (!isCurrent()) return;
      this.voices = voices.items ?? [];
      return this.voices;
    } catch (caught) {
      if (isCurrent()) throw caught;
    }
  }

  finishLoad(load: CatalogueLoad) {
    if (!load.current()) return false;
    this.loaded = true;
    return true;
  }

  async discover(service: TtsService | undefined) {
    if (!service?.api_base) return;
    const catalogueRequest = this.request;
    const request = (this.discoveryRequests.get(service.id) ?? 0) + 1;
    this.discoveryRequests.set(service.id, request);
    try {
      const discovered = await sessionApi.discoverTts(
        service.api_base,
        service.id
      );
      if (
        !discovered?.success ||
        this.disposed ||
        catalogueRequest !== this.request ||
        this.discoveryRequests.get(service.id) !== request ||
        !this.catalogue.services.some(
          (item) => item.id === service.id && item.api_base === service.api_base
        )
      )
        return;
      const services = this.catalogue.services.map((item) =>
        item.id === service.id
          ? {
              ...item,
              models: Array.from(
                new Set([...(discovered.models ?? []), ...(item.models ?? [])])
              ),
              voices: Array.from(
                new Set([...(discovered.voices ?? []), ...(item.voices ?? [])])
              ),
              live_voices: Array.from(new Set(discovered.voices ?? [])),
              online: true,
              available: true
            }
          : item
      );
      this.catalogue = { ...this.catalogue, services };
    } catch {
      /* A reachable service may not expose catalogue routes. */
    }
  }
}
