import { getContext } from 'svelte';
import { sessionApi } from './domain-api';
import type { SessionRecord } from './api-models';
import { errorMessage } from './errors';

export type InterfaceMode = 'simple' | 'advanced';
export const PROJECT_INTERFACE = Symbol('pandrator-project-interface');

/** View preference belongs to the shared project, never to generation settings. */
export class ProjectInterface {
  pending = $state(false);
  error = $state('');
  private guards = new Set<(mode: InterfaceMode) => Promise<boolean>>();

  constructor(
    private readonly source: () => SessionRecord | null,
    private readonly reload: () => Promise<void>
  ) {}

  get mode(): InterfaceMode {
    return this.source()?.interface_mode ?? 'advanced';
  }

  guard(callback: (mode: InterfaceMode) => Promise<boolean>) {
    this.guards.add(callback);
    return () => this.guards.delete(callback);
  }

  async change(mode: InterfaceMode) {
    if (this.pending || !this.source() || mode === this.mode) return;
    const requestedId = this.source()?.id;
    this.pending = true;
    this.error = '';
    try {
      for (const guard of this.guards) {
        if (!(await guard(mode))) {
          this.error =
            'Finish or close the open editor before switching modes.';
          return;
        }
      }
      const source = this.source();
      if (!source || source.id !== requestedId) return;
      await sessionApi.update(source.id, source.revision, {
        interface_mode: mode
      });
      await this.reload();
    } catch (caught) {
      this.error = errorMessage(caught);
    } finally {
      this.pending = false;
    }
  }
}

export const useProjectInterface = () =>
  getContext<ProjectInterface | undefined>(PROJECT_INTERFACE);
