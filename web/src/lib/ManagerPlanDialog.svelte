<script lang="ts">
  import { CheckCircle2, CircleAlert, X } from '@lucide/svelte';
  import { modalFocus } from './modal-focus';

  export type ManagerPlan = {
    id: string;
    digest: string;
    kind: string;
    tasks: Array<{
      id: string;
      label: string;
      kind: string;
      component_id?: string | null;
    }>;
    preflight?: Array<{
      code: string;
      status: 'pass' | 'warning' | 'error';
      message: string;
      details?: Record<string, unknown>;
    }>;
    warnings: string[];
    confirmations: Array<{
      key: string;
      kind: string;
      message: string;
      url?: string | null;
    }>;
    estimated_download_bytes: number;
    estimated_disk_bytes: number;
    impacts?: {
      release?: {
        product: string;
        version: string;
        channel: string;
        sequence: number;
        legacy_data?: { size_bytes: number; file_count: number } | null;
      };
      uninstall?: {
        purge_data: boolean;
        preserve_data: boolean;
        export_data?: string | null;
        data_bytes: number;
        data_files: number;
        package_distribution_retained: boolean;
        legacy_data?: { size_bytes: number; file_count: number } | null;
        legacy_data_reconciled?: boolean;
      };
    };
    application_impacts?: {
      managed_provider_bindings?: Array<{
        component_id: string;
        provider_id: string;
        service_id: string;
        label: string;
        selected_default: boolean;
        message: string;
      }>;
    };
  };

  let {
    plan,
    onclose,
    onconfirm
  }: {
    plan: ManagerPlan;
    onclose: () => void;
    onconfirm: () => void;
  } = $props();

  const formatBytes = (value: number) => {
    if (!value) return 'No estimate';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let amount = value;
    let unit = 0;
    while (amount >= 1024 && unit < units.length - 1) {
      amount /= 1024;
      unit += 1;
    }
    return `${amount >= 10 || unit === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[unit]}`;
  };
</script>

<div
  class="fixed inset-0 z-[70] grid place-items-center bg-black/45 p-3 backdrop-blur-sm"
  role="presentation"
>
  <div
    use:modalFocus={{ onclose }}
    class="surface flex max-h-[92vh] w-full max-w-2xl flex-col overflow-hidden rounded-3xl"
    role="dialog"
    aria-modal="true"
    aria-labelledby="manager-plan-title"
  >
    <header
      class="flex items-start justify-between gap-4 border-b border-[var(--line)] px-5 py-4 sm:px-7"
    >
      <div>
        <div class="eyebrow">Review exact plan</div>
        <h2 id="manager-plan-title" class="mt-1 text-2xl font-semibold">
          {plan.kind.replaceAll('_', ' ')}
        </h2>
      </div>
      <button
        class="btn btn-icon btn-secondary"
        aria-label="Close plan"
        onclick={onclose}><X size={18} /></button
      >
    </header>
    <div class="modal-scroll p-5 sm:p-7">
      <div class="grid gap-3 sm:grid-cols-2">
        <div class="rounded-xl border border-[var(--line)] p-3">
          <div class="muted text-xs">Download estimate</div>
          <strong class="mt-1 block text-sm"
            >{formatBytes(plan.estimated_download_bytes)}</strong
          >
        </div>
        <div class="rounded-xl border border-[var(--line)] p-3">
          <div class="muted text-xs">Disk estimate</div>
          <strong class="mt-1 block text-sm"
            >{formatBytes(plan.estimated_disk_bytes)}</strong
          >
        </div>
      </div>
      {#if plan.impacts?.release}
        <div class="mt-3 rounded-xl border border-[var(--line)] p-3 text-sm">
          <strong
            >{plan.impacts.release.product}
            {plan.impacts.release.version}</strong
          >
          <span class="muted ml-1"
            >· {plan.impacts.release.channel} · sequence {plan.impacts.release
              .sequence}</span
          >
          {#if plan.impacts.release.legacy_data}
            <div class="muted mt-1 text-xs">
              Legacy data reconciliation: {plan.impacts.release.legacy_data
                .file_count} file(s), {formatBytes(
                plan.impacts.release.legacy_data.size_bytes
              )}; sources retained through activation.
            </div>
          {/if}
        </div>
      {/if}
      {#if plan.impacts?.uninstall}
        <div class="mt-3 rounded-xl border border-red-400/30 p-3 text-sm">
          <strong
            >{plan.impacts.uninstall.purge_data
              ? 'User data will be permanently purged'
              : 'User data will be preserved'}</strong
          >
          {#if plan.impacts.uninstall.export_data}<div
              class="muted mt-1 text-xs"
            >
              Export: {plan.impacts.uninstall.export_data}
            </div>{/if}
          {#if plan.impacts.uninstall.legacy_data}
            <div class="muted mt-1 text-xs">
              Known legacy data: {plan.impacts.uninstall.legacy_data.file_count} file(s),
              {formatBytes(plan.impacts.uninstall.legacy_data.size_bytes)}{plan
                .impacts.uninstall.legacy_data_reconciled
                ? ' will be reconciled before removal.'
                : '.'}
            </div>
          {/if}
          <div class="muted mt-1 text-xs">
            Removing this installation does not uninstall the Python package
            distribution.
          </div>
        </div>
      {/if}
      <h3 class="mt-5 text-sm font-semibold">Tasks</h3>
      <ol class="mt-2 space-y-2">
        {#each plan.tasks as task, index}
          <li
            class="flex gap-3 rounded-xl border border-[var(--line)] p-3 text-sm"
          >
            <span class="muted">{index + 1}</span><span>{task.label}</span>
          </li>
        {/each}
      </ol>
      {#if plan.preflight?.length}
        <h3 class="mt-5 text-sm font-semibold">Host preflight</h3>
        <ul class="mt-2 space-y-2">
          {#each plan.preflight as check}
            <li
              class="flex items-start gap-2 rounded-xl border border-[var(--line)] p-3 text-sm"
            >
              {#if check.status === 'pass'}<CheckCircle2
                  class="mt-0.5 shrink-0 text-[var(--success)]"
                  size={15}
                />{:else}<CircleAlert
                  class="mt-0.5 shrink-0 text-amber-500"
                  size={15}
                />{/if}
              <span
                ><strong>{check.code}</strong><span class="muted ml-1"
                  >{check.message}</span
                ></span
              >
            </li>
          {/each}
        </ul>
      {/if}
      {#if plan.warnings.length}<div
          class="mt-4 rounded-xl border border-amber-400/40 bg-amber-500/10 p-3 text-sm"
        >
          {plan.warnings.join(' ')}
        </div>{/if}
      {#if plan.confirmations.length}
        <h3 class="mt-5 text-sm font-semibold">Required confirmations</h3>
        <ul class="mt-2 space-y-2">
          {#each plan.confirmations as confirmation}
            <li class="rounded-xl border border-[var(--line)] p-3 text-sm">
              {confirmation.message}{#if confirmation.url}<a
                  class="ml-1 text-[var(--accent)] underline"
                  href={confirmation.url}
                  target="_blank"
                  rel="noreferrer">Review terms</a
                >{/if}
            </li>
          {/each}
        </ul>
      {/if}
      {#if plan.application_impacts?.managed_provider_bindings?.length}
        <h3 class="mt-5 text-sm font-semibold">Pandrator provider impact</h3>
        <ul class="mt-2 space-y-2">
          {#each plan.application_impacts.managed_provider_bindings as impact}
            <li
              class="rounded-xl border border-amber-400/40 bg-amber-500/10 p-3 text-sm"
            >
              <strong>{impact.label}</strong>{#if impact.selected_default}<span
                  class="ml-1 font-semibold">(current default)</span
                >{/if}
              <div class="muted mt-1 text-xs">{impact.message}</div>
            </li>
          {/each}
        </ul>
      {/if}
    </div>
    <footer
      class="flex justify-end gap-2 border-t border-[var(--line)] px-5 py-4 sm:px-7"
    >
      <button class="btn btn-secondary" onclick={onclose}>Cancel</button>
      <button class="btn btn-primary" onclick={onconfirm}
        >Confirm exact plan</button
      >
    </footer>
  </div>
</div>
