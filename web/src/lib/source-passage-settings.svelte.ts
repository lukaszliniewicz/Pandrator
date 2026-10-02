import { sessionApi } from './domain-api';
import { errorMessage } from './errors';
import {
  SOURCE_PASSAGE_DEFAULTS,
  SOURCE_PASSAGE_SECTION,
  coerceSourcePassageValues,
  isSourcePassageConflict,
  previewSourcePassages,
  rebuildSourcePassages,
  sourcePassagePayload,
  sourcePassageStatus,
  validateSourcePassageValues,
  type SourcePassageKey,
  type SourcePassagePreviewResponse,
  type SourcePassageRebuildResponse,
  type SourcePassageStatus
} from './source-passages';

/** Owns the dialog draft and rejects results from an older source or opening. */
export class SourcePassageSettingsState {
  values = $state({ ...SOURCE_PASSAGE_DEFAULTS });
  available = $state(true);
  overrideCount = $state(0);
  touched = $state(false);
  artifactId = $state('');
  status = $state<SourcePassageStatus | null>(null);
  statusLoading = $state(false);
  statusError = $state('');
  preview = $state<SourcePassagePreviewResponse | null>(null);
  previewLoading = $state(false);
  previewError = $state('');
  rebuildLoading = $state(false);
  rebuildError = $state('');
  rebuildResult = $state<SourcePassageRebuildResponse | null>(null);
  errors = $derived(validateSourcePassageValues(this.values));
  valid = $derived(Object.keys(this.errors).length === 0);
  customized = $derived(this.touched || this.overrideCount > 0);

  private opening = 0;
  private source = 0;
  private edit = 0;
  private statusRequest = 0;
  private active = false;

  constructor(
    private sessionId: string,
    private refreshWorkspace: () => Promise<void>
  ) {}

  async open(artifactId: string) {
    this.close();
    this.active = true;
    this.setArtifact(artifactId);
    this.values = { ...SOURCE_PASSAGE_DEFAULTS };
    this.touched = false;
    this.overrideCount = 0;
    const opening = this.opening;
    const edit = this.edit;
    try {
      const settings = await sessionApi.settings(
        this.sessionId,
        SOURCE_PASSAGE_SECTION
      );
      if (!this.active || opening !== this.opening) return;
      if (edit === this.edit)
        this.values = coerceSourcePassageValues(settings.effective);
      this.overrideCount = Object.keys(settings.override ?? {}).length;
      this.available = true;
    } catch {
      if (this.active && opening === this.opening) this.available = false;
    }
  }

  close() {
    this.active = false;
    this.opening++;
    this.setArtifact('');
    this.source++;
    this.resetResults();
  }

  setArtifact(artifactId: string) {
    if (artifactId === this.artifactId) return;
    this.artifactId = artifactId;
    this.source++;
    this.resetResults();
  }

  private resetResults() {
    this.statusRequest++;
    this.status = null;
    this.statusLoading = false;
    this.statusError = '';
    this.preview = null;
    this.previewLoading = false;
    this.previewError = '';
    this.rebuildLoading = false;
    this.rebuildError = '';
    this.rebuildResult = null;
  }

  private current(source: number) {
    return this.active && source === this.source;
  }

  setValue(key: SourcePassageKey, raw: string) {
    this.values[key] = raw === '' ? Number.NaN : Number(raw);
    this.touched = true;
    this.edit++;
    this.preview = null;
    this.previewError = '';
    this.rebuildError = '';
    this.rebuildResult = null;
  }

  update(): { section: string; value: Record<string, unknown> } | null {
    return this.valid && this.customized
      ? {
          section: SOURCE_PASSAGE_SECTION,
          value: sourcePassagePayload(this.values)
        }
      : null;
  }

  async loadStatus(replace = false) {
    if (!this.active || !this.artifactId || (this.statusLoading && !replace))
      return;
    const source = this.source;
    const request = ++this.statusRequest;
    this.statusLoading = true;
    this.statusError = '';
    try {
      const status = await sourcePassageStatus(this.sessionId, this.artifactId);
      if (this.current(source) && request === this.statusRequest)
        this.status = status;
    } catch (caught) {
      if (this.current(source) && request === this.statusRequest) {
        this.status = null;
        this.statusError = errorMessage(caught);
      }
    } finally {
      if (this.current(source) && request === this.statusRequest)
        this.statusLoading = false;
    }
  }

  async runPreview() {
    if (
      !this.active ||
      !this.artifactId ||
      this.previewLoading ||
      this.rebuildLoading
    )
      return;
    if (!this.valid) {
      this.previewError =
        'Correct the highlighted source-passage values before previewing.';
      return;
    }
    const source = this.source;
    const edit = this.edit;
    this.preview = null;
    this.previewLoading = true;
    this.previewError = '';
    this.rebuildError = '';
    try {
      const preview = await previewSourcePassages(
        this.sessionId,
        this.artifactId,
        sourcePassagePayload(this.values)
      );
      if (!this.current(source) || edit !== this.edit) return;
      this.preview = preview;
      await this.loadStatus();
    } catch (caught) {
      if (this.current(source) && edit === this.edit)
        this.previewError = errorMessage(caught);
    } finally {
      if (this.current(source)) this.previewLoading = false;
    }
  }

  async runRebuild() {
    if (!this.active || !this.artifactId || this.rebuildLoading) return;
    if (!this.preview || !this.status) {
      this.rebuildError =
        'Reload status and run a fresh preview before rebuilding.';
      return;
    }
    const source = this.source;
    const edit = this.edit;
    const preview = this.preview;
    this.rebuildLoading = true;
    this.rebuildError = '';
    this.rebuildResult = null;
    try {
      const result = await rebuildSourcePassages(
        this.sessionId,
        this.artifactId,
        {
          expected_source_revision_id: preview.revision_id,
          expected_source_content_hash: preview.content_hash,
          expected_settings_revision: preview.settings_revision,
          expected_settings_hash: preview.settings_hash,
          source_passages: sourcePassagePayload(this.values)
        }
      );
      if (!this.current(source) || edit !== this.edit) return;
      this.rebuildResult = result;
      this.preview = null;
      await this.refreshWorkspace();
      if (this.current(source)) await this.loadStatus(true);
    } catch (caught) {
      if (!this.current(source) || edit !== this.edit) return;
      const message = errorMessage(caught);
      if (
        isSourcePassageConflict(caught) ||
        /revision_conflict|source_changed|stale/i.test(message)
      ) {
        this.preview = null;
        this.rebuildError = `${message} Status and settings were refreshed; your draft was preserved. Run a fresh preview, then rebuild again. No silent retry was attempted.`;
        await this.loadStatus(true);
        if (!this.current(source)) return;
        try {
          const settings = await sessionApi.settings(
            this.sessionId,
            SOURCE_PASSAGE_SECTION
          );
          if (!this.current(source)) return;
          this.overrideCount = Object.keys(settings.override ?? {}).length;
          this.available = true;
        } catch {
          if (this.current(source)) this.available = false;
        }
      } else {
        this.rebuildError = message;
      }
    } finally {
      if (this.current(source)) this.rebuildLoading = false;
    }
  }
}
