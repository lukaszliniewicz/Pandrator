import type {
  TtsCatalogue,
  TtsCompactCatalogue,
  TtsService,
  VoiceRecord
} from './api-models';
import { sessionApi } from './domain-api';
import { errorMessage } from './errors';
import {
  getTtsCompactCatalogue,
  getTtsServiceDetail,
  getVoiceLibrary
} from './tts-catalogue-cache';

type CatalogueLoad = {
  catalogue: TtsCompactCatalogue;
  current: () => boolean;
};

/** Component-local catalogue data; pending work can only update its current owner. */
export class TtsCatalogueState {
  catalogue = $state<TtsCatalogue>({ services: [] });
  voices = $state<VoiceRecord[]>([]);
  error = $state('');
  voicesLoading = $state(false);
  voiceError = $state('');
  checkingServices = $state<string[]>([]);
  serviceErrors = $state<Record<string, string>>({});
  private loaded = false;
  private voicesLoaded = false;
  private request = 0;
  private voiceRequest = 0;
  private disposed = false;
  private discoveryRequests = new Map<string, number>();
  private serviceRequests = new Map<string, Promise<void>>();

  invalidate() {
    this.request++;
    this.voiceRequest++;
    this.discoveryRequests.clear();
    this.serviceRequests.clear();
    this.checkingServices = [];
    this.voicesLoading = false;
  }

  dispose() {
    this.disposed = true;
  }

  async load(force = false): Promise<CatalogueLoad | undefined> {
    if (this.loaded && !force) {
      if (!this.voicesLoaded && !this.voicesLoading)
        void this.reloadVoices(false).catch(() => undefined);
      return;
    }
    const request = ++this.request;
    this.serviceRequests.clear();
    this.checkingServices = [];
    this.serviceErrors = {};
    const isCurrent = () => !this.disposed && request === this.request;
    this.error = '';
    // Saved references should not hold the provider/model controls hostage.
    void this.reloadVoices(force).catch(() => undefined);
    try {
      const services = await getTtsCompactCatalogue(force, force);
      if (!isCurrent()) return;
      this.catalogue = services;
      return { catalogue: services, current: isCurrent };
    } catch (caught) {
      if (isCurrent()) this.error = errorMessage(caught);
    }
  }

  async reloadVoices(force = true) {
    const catalogueRequest = this.request;
    const request = ++this.voiceRequest;
    const isCurrent = () =>
      !this.disposed &&
      catalogueRequest === this.request &&
      request === this.voiceRequest;
    this.voicesLoading = true;
    this.voiceError = '';
    try {
      const voices = await getVoiceLibrary(force);
      if (!isCurrent()) return;
      this.voices = voices.items ?? [];
      this.voicesLoaded = true;
      return this.voices;
    } catch (caught) {
      if (isCurrent()) {
        this.voiceError = errorMessage(caught);
        throw caught;
      }
    } finally {
      if (isCurrent()) this.voicesLoading = false;
    }
  }

  /** Refresh just the selected provider; ordinary opening keeps cached models visible. */
  refreshService(serviceId: string): Promise<void> {
    const pending = this.serviceRequests.get(serviceId);
    if (pending) return pending;
    const request = this.request;
    const isCurrent = () => !this.disposed && request === this.request;
    this.checkingServices = [...this.checkingServices, serviceId];
    this.serviceErrors = { ...this.serviceErrors, [serviceId]: '' };
    const task = getTtsServiceDetail(serviceId, { refresh: true })
      .then(({ service }) => {
        if (!isCurrent()) return;
        this.catalogue = {
          ...this.catalogue,
          services: this.catalogue.services.map((item) =>
            item.id === serviceId ? { ...item, ...service } : item
          )
        };
      })
      .catch((caught) => {
        if (isCurrent())
          this.serviceErrors = {
            ...this.serviceErrors,
            [serviceId]: errorMessage(caught)
          };
      })
      .finally(() => {
        if (isCurrent())
          this.checkingServices = this.checkingServices.filter(
            (id) => id !== serviceId
          );
        if (this.serviceRequests.get(serviceId) === task)
          this.serviceRequests.delete(serviceId);
      });
    this.serviceRequests.set(serviceId, task);
    return task;
  }

  finishLoad(load: CatalogueLoad) {
    if (!load.current()) return false;
    this.loaded = true;
    return true;
  }

  async discover(service: TtsService | undefined) {
    if (!service?.api_base) return;
    // Native Google catalogues are refreshed by the catalogue API with their
    // own credentials and per-model voice metadata.
    if (
      ['gemini', 'vertex_ai'].includes(service.id) &&
      service.supports_dynamic_catalog
    )
      return;
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
