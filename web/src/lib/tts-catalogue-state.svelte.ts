import type {
  TtsCatalogue,
  TtsCompactCatalogue,
  VoiceRecord
} from './api-models';
import type { SpeechCatalogueScope } from './speech-catalogue-api';
import { errorMessage } from './errors';
import {
  getTtsCompactCatalogue,
  getTtsServiceDetail,
  getVoiceLibrary,
  speechCatalogueKey
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
  private loadedScope = '';
  private serviceRequests = new Map<string, Promise<void>>();

  constructor(
    private readonly getScope: () => SpeechCatalogueScope = () => ({})
  ) {}

  invalidate() {
    this.request++;
    this.voiceRequest++;
    this.serviceRequests.clear();
    this.checkingServices = [];
    this.voicesLoading = false;
  }

  dispose() {
    this.disposed = true;
  }

  async load(force = false): Promise<CatalogueLoad | undefined> {
    const scope = this.getScope();
    const scopeKey = speechCatalogueKey(scope);
    if (this.loaded && this.loadedScope === scopeKey && !force) {
      if (!this.voicesLoaded && !this.voicesLoading)
        void this.reloadVoices(false).catch(() => undefined);
      return;
    }
    const request = ++this.request;
    this.serviceRequests.clear();
    this.checkingServices = [];
    this.serviceErrors = {};
    if (this.loadedScope !== scopeKey) {
      this.loaded = false;
      this.catalogue = { services: [] };
      this.voices = [];
      this.voicesLoaded = false;
    }
    this.loadedScope = scopeKey;
    const isCurrent = () =>
      !this.disposed &&
      request === this.request &&
      speechCatalogueKey(this.getScope()) === scopeKey;
    this.error = '';
    // Saved references should not hold the provider/model controls hostage.
    void this.reloadVoices(force).catch(() => undefined);
    try {
      const services = await getTtsCompactCatalogue(false, force, scope);
      if (!isCurrent()) return;
      this.catalogue = services;
      return { catalogue: services, current: isCurrent };
    } catch (caught) {
      if (isCurrent()) this.error = errorMessage(caught);
    }
  }

  async reloadVoices(force = true) {
    const catalogueRequest = this.request;
    const scope = this.getScope();
    const scopeKey = speechCatalogueKey(scope);
    const request = ++this.voiceRequest;
    const isCurrent = () =>
      !this.disposed &&
      catalogueRequest === this.request &&
      request === this.voiceRequest &&
      speechCatalogueKey(this.getScope()) === scopeKey;
    this.voicesLoading = true;
    this.voiceError = '';
    try {
      const voices = await getVoiceLibrary(force, scope);
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
    if (!this.catalogue.can_refresh || !this.catalogue.workspace_id)
      return Promise.resolve();
    const pending = this.serviceRequests.get(serviceId);
    if (pending) return pending;
    const request = this.request;
    const scopeKey = speechCatalogueKey(this.getScope());
    const isCurrent = () =>
      !this.disposed &&
      request === this.request &&
      speechCatalogueKey(this.getScope()) === scopeKey;
    this.checkingServices = [...this.checkingServices, serviceId];
    this.serviceErrors = { ...this.serviceErrors, [serviceId]: '' };
    const task = getTtsServiceDetail(serviceId, {
      refresh: true,
      workspaceId: this.catalogue.workspace_id
    })
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
}
