<script lang="ts">
  import {
    CheckCircle2,
    CircleAlert,
    Download,
    LoaderCircle,
    Play,
    RefreshCw,
    RotateCcw,
    Square,
    Trash2,
    X
  } from '@lucide/svelte';
  import { onMount } from 'svelte';
  import { managerApi } from './admin-api';

  type ComputeVariant = 'auto' | 'cpu' | 'cuda' | 'vulkan' | 'metal' | 'rocm' | 'wgpu';
  type Definition = {
    id: string;
    label: string;
    description: string;
    service_key?: string | null;
    compute_variants: ComputeVariant[];
    supported_actions: string[];
    default_port?: number | null;
  };
  type Inspection = {
    state: 'absent' | 'present' | 'degraded' | 'unknown' | 'unsupported';
    installed_version?: string | null;
    installed_revision?: string | null;
    resolved?: { compute?: string; quantization?: string | null } | null;
    problems: string[];
  };
  type Component = {
    definition: Definition;
    desired?: {
      present: boolean;
      compute: ComputeVariant;
      quantization?: string | null;
      options?: Record<string, unknown>;
    } | null;
    inspection: Inspection;
  };
  type Service = {
    id: string;
    component_id: string;
    desired_running: boolean;
    endpoint?: string | null;
    process?: { pid: number } | null;
    health?: { state: string; message?: string } | null;
  };
  type ManagerStatus = {
    available: boolean;
    configured: boolean;
    error?: { code: string; message: string };
    status?: {
      configuration_revision: number;
      active_operation_id?: string | null;
    };
  };
  type Confirmation = {
    key: string;
    kind: string;
    message: string;
    url?: string | null;
  };
  type Task = { id: string; label: string; kind: string; component_id?: string | null };
  type Plan = {
    id: string;
    digest: string;
    kind: string;
    tasks: Task[];
    preflight?: Array<{
      code: string;
      status: 'pass' | 'warning' | 'error';
      message: string;
      details?: Record<string, unknown>;
    }>;
    warnings: string[];
    confirmations: Confirmation[];
    estimated_download_bytes: number;
    estimated_disk_bytes: number;
    impacts?: {
      release?: {
        product: string;
        version: string;
        channel: string;
        sequence: number;
        legacy_data?: {
          size_bytes: number;
          file_count: number;
        } | null;
      };
      uninstall?: {
        purge_data: boolean;
        preserve_data: boolean;
        export_data?: string | null;
        data_bytes: number;
        data_files: number;
        package_distribution_retained: boolean;
        legacy_data?: {
          size_bytes: number;
          file_count: number;
        } | null;
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
  type Operation = {
    id: string;
    state: string;
    progress: number;
    current_task_id?: string | null;
    error_message?: string | null;
  };
  type DoctorCheck = {
    id: string;
    category: string;
    status: 'pass' | 'warning' | 'error';
    message: string;
    repairable: boolean;
    repair_target?: string | null;
    details: Record<string, unknown>;
  };
  type DoctorReport = {
    healthy: boolean;
    summary: { pass: number; warning: number; error: number };
    checks: DoctorCheck[];
  };
  type ReleaseInventory = {
    current: Record<string, {
      version: string;
      channel: string;
      sequence: number;
    }>;
    items: Array<Record<string, unknown>>;
  };
  type LegacyReport = {
    source_digest: string;
    valid: boolean;
    already_imported: boolean;
    positively_identified: string[];
    unknown_paths: string[];
    warnings: string[];
    legacy_data?: {
      size_bytes?: number;
      file_count?: number;
      error?: string;
    };
  };
  type LegacyInventory = {
    available: boolean;
    report?: LegacyReport | null;
  };

  let status = $state<ManagerStatus | null>(null);
  let components = $state<Component[]>([]);
  let services = $state<Service[]>([]);
  let compute = $state<Record<string, ComputeVariant>>({});
  let pendingPlan = $state<Plan | null>(null);
  let operation = $state<Operation | null>(null);
  let error = $state('');
  let notice = $state('');
  let loading = $state(true);
  let planning = $state('');
  let runtimeBusy = $state('');
  let doctor = $state<DoctorReport | null>(null);
  let doctorBusy = $state(false);
  let releases = $state<ReleaseInventory>({ current: {}, items: [] });
  let releaseManifest = $state<File | null>(null);
  let releaseOffline = $state(false);
  let releaseKeepStopped = $state(false);
  let releaseBusy = $state(false);
  let legacy = $state<LegacyInventory | null>(null);
  let legacyBusy = $state(false);
  let uninstallExport = $state('');
  let uninstallPurge = $state(false);
  let uninstallBusy = $state(false);
  let pollTimer: ReturnType<typeof setTimeout> | null = null;
  let pollStopped = false;

  const terminalStates = new Set([
    'succeeded',
    'failed',
    'cancelled',
    'recovery_required'
  ]);

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

  const selectedCompute = (component: Component) =>
    compute[component.definition.id]
      ?? component.desired?.compute
      ?? component.inspection.resolved?.compute
      ?? 'auto';

  const serviceFor = (component: Component) =>
    services.find(
      (service) =>
        service.id === component.definition.service_key
        || service.component_id === component.definition.id
    );

  const runtimeStateFor = (component: Component, service?: Service) => {
    const health = service?.health?.state ?? 'unknown';
    let state: string = component.inspection.state;
    let label: string = component.inspection.state.replaceAll('_', ' ');
    if (service?.process) {
      const runningStates: Record<string, { state: string; label: string }> = {
        healthy: { state: 'running', label: 'Running' },
        starting: { state: 'starting', label: 'Starting' },
        degraded: { state: 'degraded', label: 'Running with warnings' },
        unhealthy: { state: 'unhealthy', label: 'Not responding' },
        failed: { state: 'failed', label: 'Failed' }
      };
      ({ state, label } = runningStates[health] ?? {
        state: 'starting',
        label: 'Starting'
      });
    } else if (service?.desired_running) {
      ({ state, label } = health === 'failed'
        ? { state: 'failed', label: 'Failed' }
        : { state: 'restarting', label: 'Restarting' });
    } else if (component.inspection.state === 'degraded') {
      ({ state, label } = { state: 'degraded', label: 'Needs repair' });
    } else if (component.inspection.state === 'present' && service) {
      ({ state, label } = health === 'failed'
        ? { state: 'failed', label: 'Failed' }
        : { state: 'stopped', label: 'Stopped' });
    } else if (component.inspection.state === 'present') {
      ({ state, label } = { state: 'unavailable', label: 'Status unavailable' });
    } else if (component.inspection.state === 'absent') {
      label = 'Not installed';
    }

    const wantsToRun = Boolean(service?.process || service?.desired_running);
    const action =
      wantsToRun && component.definition.supported_actions.includes('stop')
        ? 'stop'
        : service
            && component.inspection.state === 'present'
            && component.definition.supported_actions.includes('start')
          ? 'start'
          : null;
    return { state, label, action };
  };

  function rememberOperation(value: Operation | null) {
    operation = value;
    if (value && !terminalStates.has(value.state)) {
      localStorage.setItem('pandrator-manager-operation', value.id);
    } else {
      localStorage.removeItem('pandrator-manager-operation');
    }
  }

  function report(caught: unknown) {
    error = caught instanceof Error ? caught.message : String(caught);
    notice = '';
  }

  async function load() {
    try {
      status = await managerApi.status<ManagerStatus>();
      if (!status.available) {
        components = [];
        services = [];
        return;
      }
      const [componentPayload, servicePayload, releasePayload] = await Promise.all([
        managerApi.components<{ items: Component[] }>(),
        managerApi.services<{ items: Service[] }>(),
        managerApi.releases<ReleaseInventory>()
      ]);
      components = componentPayload.items.filter(
        (component) =>
          component.definition.id !== 'pandrator'
          && Boolean(component.definition.service_key)
      );
      services = servicePayload.items;
      releases = releasePayload;
      for (const component of components) {
        compute[component.definition.id] = selectedCompute(component);
      }
      const activeId =
        operation && !terminalStates.has(operation.state)
          ? operation.id
          : status.status?.active_operation_id
            ?? localStorage.getItem('pandrator-manager-operation');
      if (activeId) {
        const current = await managerApi.operation<Operation>(activeId);
        rememberOperation(current);
      }
      error = '';
    } catch (caught) {
      report(caught);
    } finally {
      loading = false;
    }
  }

  async function refreshOperation() {
    if (!operation || terminalStates.has(operation.state)) return;
    try {
      const current = await managerApi.operation<Operation>(operation.id);
      rememberOperation(current);
      if (terminalStates.has(current.state)) {
        if (current.state === 'succeeded') notice = 'Local component changes completed.';
        else error = current.error_message || `Operation ended as ${current.state}.`;
        await load();
      }
    } catch (caught) {
      report(caught);
    }
  }

  async function refreshManagerState() {
    if (!status?.available) return;
    try {
      const [nextStatus, servicePayload] = await Promise.all([
        managerApi.status<ManagerStatus>(),
        managerApi.services<{ items: Service[] }>()
      ]);
      status = nextStatus;
      services = servicePayload.items;
      const activeId = nextStatus.status?.active_operation_id;
      if (activeId && (!operation || operation.id !== activeId)) {
        rememberOperation(await managerApi.operation<Operation>(activeId));
      }
    } catch (caught) {
      report(caught);
    }
  }

  async function pollOperation() {
    if (pollStopped) return;
    if (operation && !terminalStates.has(operation.state)) {
      await refreshOperation();
    } else {
      await refreshManagerState();
    }
    if (!pollStopped) {
      const delay =
        operation && !terminalStates.has(operation.state) ? 1000 : 2500;
      pollTimer = setTimeout(() => void pollOperation(), delay);
    }
  }

  async function createPlan(
    component: Component,
    kind: 'install' | 'update' | 'repair' | 'remove'
  ) {
    planning = component.definition.id;
    error = '';
    notice = '';
    try {
      pendingPlan = await managerApi.plan<Plan>({
        kind,
        desired: {
          [component.definition.id]: {
            present: kind !== 'remove',
            compute: selectedCompute(component),
            quantization: component.desired?.quantization ?? null,
            options: {
              ...(component.desired?.options ?? {}),
              start_after_install: kind !== 'remove'
            }
          }
        },
        expected_revision: status?.status?.configuration_revision
      });
    } catch (caught) {
      report(caught);
    } finally {
      planning = '';
    }
  }

  async function executePlan() {
    if (!pendingPlan) return;
    const plan = pendingPlan;
    pendingPlan = null;
    try {
      const submitted = await managerApi.submit<Operation>({
        plan_id: plan.id,
        plan_digest: plan.digest,
        accepted_confirmations: plan.confirmations.map(
          (confirmation) => confirmation.key
        )
      });
      rememberOperation(submitted);
      if (plan.kind === 'uninstall') {
        pollStopped = true;
        if (pollTimer) clearTimeout(pollTimer);
        notice = 'Uninstall was accepted. Pandrator and the manager will close while the external helper completes cleanup.';
      } else {
        notice = 'The manager accepted the operation. You can leave this page.';
        await refreshOperation();
      }
    } catch (caught) {
      report(caught);
    }
  }

  async function runtime(
    component: Component,
    action: 'start' | 'stop' | 'restart'
  ) {
    const serviceId = component.definition.service_key;
    if (!serviceId) return;
    runtimeBusy = component.definition.id;
    try {
      await managerApi.runtime(action, [serviceId]);
      notice = action === 'stop'
        ? `${component.definition.label} stopped.`
        : `${component.definition.label} is starting.`;
      await load();
    } catch (caught) {
      report(caught);
    } finally {
      runtimeBusy = '';
    }
  }

  async function cancelOperation() {
    if (!operation) return;
    try {
      await managerApi.cancel(operation.id);
      notice = 'Cancellation requested. The manager will stop at a safe boundary.';
      await refreshOperation();
    } catch (caught) {
      report(caught);
    }
  }

  async function runDoctor() {
    doctorBusy = true;
    try {
      doctor = await managerApi.doctor<DoctorReport>();
      notice = doctor.healthy
        ? 'Diagnostics completed without errors.'
        : 'Diagnostics found errors. Review the repair targets below.';
      error = '';
    } catch (caught) {
      report(caught);
    } finally {
      doctorBusy = false;
    }
  }

  async function createReleasePlan() {
    if (!releaseManifest) {
      error = 'Choose a signed JSON release manifest first.';
      return;
    }
    if (releaseManifest.size > 1024 * 1024) {
      error = 'The signed release manifest exceeds the 1 MB limit.';
      return;
    }
    releaseBusy = true;
    error = '';
    try {
      const manifest: unknown = JSON.parse(await releaseManifest.text());
      if (!manifest || Array.isArray(manifest) || typeof manifest !== 'object') {
        throw new Error('The signed release manifest must be a JSON object.');
      }
      pendingPlan = await managerApi.releasePlan<Plan>({
        manifest: manifest as Record<string, unknown>,
        expected_revision: status?.status?.configuration_revision,
        offline: releaseOffline,
        start_after_activation: !releaseKeepStopped
      });
    } catch (caught) {
      report(caught);
    } finally {
      releaseBusy = false;
    }
  }

  async function inspectLegacy() {
    legacyBusy = true;
    error = '';
    try {
      legacy = await managerApi.legacy<LegacyInventory>();
      notice = legacy.available
        ? 'Legacy workspace inspection completed without changing the host.'
        : 'No legacy installer configuration was found.';
    } catch (caught) {
      report(caught);
    } finally {
      legacyBusy = false;
    }
  }

  async function importLegacy() {
    const reviewed = legacy?.report;
    if (!reviewed) return;
    const action = reviewed.valid ? 'import' : 'quarantine';
    if (
      !window.confirm(
        `Confirm ${action} of the exact legacy configuration with digest ${reviewed.source_digest}?`
      )
    ) return;
    legacyBusy = true;
    error = '';
    try {
      const result = await managerApi.importLegacy<{
        restart_manager_required: boolean;
        report: LegacyReport;
      }>({
        source_digest: reviewed.source_digest,
        confirmed: true
      });
      legacy = {
        available: true,
        report: { ...result.report, already_imported: true }
      };
      notice = result.restart_manager_required
        ? 'Legacy state imported. Restart the manager before starting imported services.'
        : 'Legacy configuration import completed.';
      await load();
    } catch (caught) {
      report(caught);
    } finally {
      legacyBusy = false;
    }
  }

  async function createUninstallPlan() {
    uninstallBusy = true;
    error = '';
    try {
      pendingPlan = await managerApi.uninstallPlan<Plan>({
        expected_revision: status?.status?.configuration_revision,
        purge_data: uninstallPurge,
        export_data: uninstallExport.trim() || null
      });
    } catch (caught) {
      report(caught);
    } finally {
      uninstallBusy = false;
    }
  }

  function repairDiagnostic(check: DoctorCheck) {
    const target = String(check.repair_target ?? '');
    if (!target.startsWith('component:')) return;
    const componentId = target.slice('component:'.length);
    const component = components.find(
      (candidate) => candidate.definition.id === componentId
    );
    if (component) void createPlan(component, 'repair');
  }

  onMount(() => {
    void load();
    void pollOperation();
    return () => {
      pollStopped = true;
      if (pollTimer) clearTimeout(pollTimer);
    };
  });
</script>

<section>
  <div class="flex flex-wrap items-end justify-between gap-4">
    <div>
      <div class="eyebrow">Local components</div>
      <h2 class="mt-1 text-2xl font-semibold">Install and run local providers</h2>
      <p class="muted mt-2 max-w-3xl text-sm">
        Pandrator shows the controls here; the independent local manager performs
        and journals every host change. External endpoints remain available
        without it.
      </p>
    </div>
    <button class="btn btn-secondary" onclick={load} disabled={loading}>
      <RefreshCw size={16} class={loading ? 'animate-spin' : ''}/> Refresh
    </button>
  </div>

  {#if error}
    <div role="alert" class="mt-4 flex items-start gap-2 rounded-xl border border-red-400/40 bg-red-500/10 px-4 py-3 text-sm">
      <CircleAlert class="mt-0.5 shrink-0" size={16}/><span>{error}</span>
    </div>
  {/if}
  {#if notice}
    <div role="status" class="mt-4 flex items-center gap-2 rounded-xl border border-[var(--line)] bg-[var(--accent-soft)] px-4 py-3 text-sm">
      <CheckCircle2 size={16}/>{notice}
    </div>
  {/if}

  {#if loading}
    <div class="muted mt-6 flex items-center gap-2 text-sm">
      <LoaderCircle class="animate-spin" size={16}/> Contacting the local manager…
    </div>
  {:else if !status?.available}
    <div class="mt-5 rounded-2xl border border-[var(--line)] p-5">
      <div class="flex items-start gap-3">
        <CircleAlert class="mt-0.5 shrink-0 text-amber-500" size={19}/>
        <div>
          <h3 class="font-semibold">Local manager unavailable</h3>
          <p class="muted mt-1 text-sm">
            {status?.error?.message ?? 'Start Pandrator Manager to install or supervise local services.'}
          </p>
          <p class="muted mt-2 text-xs">
            Existing external providers and normal Pandrator workflows are not affected.
            Run <code>pandrator-manager open --recovery</code> on this computer for recovery.
          </p>
        </div>
      </div>
    </div>
  {:else}
    {#if operation && !terminalStates.has(operation.state)}
      <div class="mt-5 rounded-2xl border border-[var(--accent)] bg-[var(--accent-soft)] p-4">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div class="text-sm font-semibold">Manager operation in progress</div>
            <div class="muted mt-1 text-xs">
              {operation.state.replaceAll('_', ' ')}
              {operation.current_task_id ? ` · ${operation.current_task_id}` : ''}
            </div>
          </div>
          <button class="btn btn-sm btn-secondary" onclick={cancelOperation}>Cancel safely</button>
        </div>
        <div class="mt-3 h-2 overflow-hidden rounded-full bg-black/10">
          <div class="h-full rounded-full bg-[var(--accent)] transition-[width]" style={`width:${Math.round(operation.progress * 100)}%`}></div>
        </div>
      </div>
    {/if}

    <div class="mt-5 grid gap-3 lg:grid-cols-2">
      {#each components as component}
        {@const service = serviceFor(component)}
        {@const runtimeState = runtimeStateFor(component, service)}
        {@const installed = component.inspection.state === 'present'}
        {@const degraded = component.inspection.state === 'degraded'}
        {@const busy = planning === component.definition.id || runtimeBusy === component.definition.id || Boolean(operation && !terminalStates.has(operation.state))}
        <article id={`component-${component.definition.id}`} class="scroll-mt-24 rounded-2xl border border-[var(--line)] p-4">
          <div class="flex items-start justify-between gap-3">
            <div>
              <div class="font-semibold">{component.definition.label}</div>
              <div class="muted mt-1 text-xs">
                {runtimeState.label}
                {component.definition.default_port ? ` · port ${component.definition.default_port}` : ''}
              </div>
            </div>
            <span class={`status-dot ${runtimeState.state}`} title={runtimeState.label}></span>
          </div>

          {#if component.inspection.problems?.length}
            <p class="mt-3 text-xs text-amber-600">{component.inspection.problems.join(' ')}</p>
          {/if}

          {#if component.definition.compute_variants.length > 1}
            <label class="mt-4 block text-xs font-semibold">
              Compute backend
              <select
                class="field"
                value={selectedCompute(component)}
                disabled={busy}
                onchange={(event) => compute[component.definition.id] = event.currentTarget.value as ComputeVariant}
              >
                <option value="auto">Automatic</option>
                {#each component.definition.compute_variants as variant}
                  <option value={variant}>{variant.toUpperCase()}</option>
                {/each}
              </select>
            </label>
          {/if}

          <div class="mt-4 flex flex-wrap gap-2">
            {#if !installed && !degraded && component.definition.supported_actions.includes('install')}
              <button class="btn btn-sm btn-primary" disabled={busy} onclick={() => createPlan(component, 'install')}>
                <Download size={13}/> Install locally
              </button>
            {/if}
            {#if degraded && component.definition.supported_actions.includes('repair')}
              <button class="btn btn-sm btn-primary" disabled={busy} onclick={() => createPlan(component, 'repair')}>
                <RotateCcw size={13}/> Repair
              </button>
            {/if}
            {#if installed && component.definition.supported_actions.includes('update')}
              <button class="btn btn-sm btn-secondary" disabled={busy} onclick={() => createPlan(component, 'update')}>
                <RefreshCw size={13}/> Check/update
              </button>
            {/if}
            {#if runtimeState.action === 'start'}
              <button class="btn btn-sm btn-secondary" disabled={busy} onclick={() => runtime(component, 'start')}>
                <Play size={13}/> Start
              </button>
            {/if}
            {#if runtimeState.action === 'stop'}
              <button class="btn btn-sm btn-secondary" disabled={busy} onclick={() => runtime(component, 'stop')}>
                <Square size={13}/> Stop
              </button>
            {/if}
            {#if installed && component.definition.supported_actions.includes('remove')}
              <button class="btn btn-sm btn-secondary text-red-500" disabled={busy} onclick={() => createPlan(component, 'remove')}>
                <Trash2 size={13}/> Remove
              </button>
            {/if}
          </div>
        </article>
      {/each}
    </div>

    <section class="mt-8 rounded-2xl border border-[var(--line)] p-5">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div class="eyebrow">Host maintenance</div>
          <h3 class="mt-1 text-xl font-semibold">Diagnostics, signed updates, and uninstall</h3>
          <p class="muted mt-2 max-w-3xl text-sm">
            Diagnostics are read-only. Release manifests are verified against
            manager-embedded project keys; this page cannot provide a trust key.
            Host mutations remain local-only by default.
          </p>
        </div>
        <button class="btn btn-secondary" disabled={doctorBusy} onclick={runDoctor}>
          {#if doctorBusy}<LoaderCircle class="animate-spin" size={15}/>{:else}<RotateCcw size={15}/>{/if}
          Run diagnostics
        </button>
      </div>

      {#if doctor}
        <div class="mt-4 rounded-xl border border-[var(--line)] p-4">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <strong>{doctor.healthy ? 'No diagnostic errors' : `${doctor.summary.error} diagnostic error(s)`}</strong>
            <span class="muted text-xs">{doctor.summary.pass} passed · {doctor.summary.warning} warnings · {doctor.summary.error} errors</span>
          </div>
          <div class="mt-3 space-y-2">
            {#each doctor.checks.filter((check) => check.status !== 'pass') as check}
              <div class="rounded-xl border border-[var(--line)] p-3 text-sm">
                <div class="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <strong class={check.status === 'error' ? 'text-red-500' : 'text-amber-600'}>{check.status.toUpperCase()} · {check.id}</strong>
                    <p class="muted mt-1 text-xs">{check.message}</p>
                  </div>
                  {#if check.repairable && String(check.repair_target ?? '').startsWith('component:')}
                    <button class="btn btn-sm btn-secondary" onclick={() => repairDiagnostic(check)}>Review repair</button>
                  {/if}
                </div>
              </div>
            {/each}
          </div>
        </div>
      {/if}

      <div class="mt-4 grid gap-4 lg:grid-cols-2">
        <div class="rounded-xl border border-[var(--line)] p-4">
          <h4 class="font-semibold">Legacy workspace migration</h4>
          <p class="muted mt-1 text-xs">
            Inspect Qt-era state and known embedded data without changing it.
            Only positively identified components are imported; unknown paths
            remain untouched.
          </p>
          <button class="btn btn-secondary mt-4" disabled={legacyBusy} onclick={inspectLegacy}>
            {#if legacyBusy}<LoaderCircle class="animate-spin" size={15}/>{:else}<RotateCcw size={15}/>{/if}
            Inspect legacy workspace
          </button>
          {#if legacy}
            <div class="mt-3 rounded-xl border border-[var(--line)] p-3 text-xs">
              {#if !legacy.available || !legacy.report}
                <span class="muted">No legacy installer configuration was found.</span>
              {:else}
                <strong>
                  {legacy.report.valid
                    ? `${legacy.report.positively_identified.length} component(s) positively identified`
                    : 'Malformed configuration; quarantine only'}
                </strong>
                <div class="muted mt-1 break-all">Digest: {legacy.report.source_digest}</div>
                {#if !legacy.report.legacy_data?.error}
                  <div class="muted mt-1">
                    Known mutable data: {legacy.report.legacy_data?.file_count ?? 0} file(s),
                    {formatBytes(legacy.report.legacy_data?.size_bytes ?? 0)}
                  </div>
                {:else}
                  <div class="mt-1 text-amber-600">{legacy.report.legacy_data.error}</div>
                {/if}
                {#if legacy.report.unknown_paths.length}
                  <div class="mt-1 text-amber-600">Unknown paths retained: {legacy.report.unknown_paths.join(', ')}</div>
                {/if}
                {#each legacy.report.warnings as warning}
                  <div class="mt-1 text-amber-600">{warning}</div>
                {/each}
                {#if legacy.report.already_imported}
                  <div class="muted mt-2">This exact configuration was already imported.</div>
                {:else}
                  <button class="btn btn-sm btn-secondary mt-3" disabled={legacyBusy} onclick={importLegacy}>
                    {legacy.report.valid ? 'Import reviewed state' : 'Quarantine reviewed configuration'}
                  </button>
                {/if}
              {/if}
            </div>
          {/if}
        </div>

        <div class="rounded-xl border border-[var(--line)] p-4">
          <h4 class="font-semibold">Signed product update</h4>
          <p class="muted mt-1 text-xs">
            Current: Pandrator {releases.current.pandrator?.version ?? 'not recorded'} ·
            Manager {releases.current['pandrator-manager']?.version ?? 'not recorded'}
          </p>
          <label class="mt-4 block text-xs font-semibold">
            Signed JSON manifest
            <input
              class="field"
              type="file"
              accept="application/json,.json"
              onchange={(event) => releaseManifest = event.currentTarget.files?.[0] ?? null}
            />
          </label>
          <label class="mt-3 flex items-start gap-2 text-xs">
            <input type="checkbox" bind:checked={releaseOffline} class="mt-0.5"/>
            Require the exact artifact in the verified local cache
          </label>
          <label class="mt-2 flex items-start gap-2 text-xs">
            <input type="checkbox" bind:checked={releaseKeepStopped} class="mt-0.5"/>
            Leave Pandrator stopped after application activation
          </label>
          <button class="btn btn-primary mt-4" disabled={releaseBusy} onclick={createReleasePlan}>
            {#if releaseBusy}<LoaderCircle class="animate-spin" size={15}/>{:else}<Download size={15}/>{/if}
            Review signed update
          </button>
        </div>

        <div class="rounded-xl border border-red-400/30 p-4">
          <h4 class="font-semibold">Uninstall Pandrator</h4>
          <p class="muted mt-1 text-xs">
            Managed software and autostart are removed. User data is preserved
            by default. The Python wheel remains controlled by pipx, uv, or pip.
          </p>
          <label class="mt-4 block text-xs font-semibold">
            Optional new ZIP export path on this computer
            <input class="field" bind:value={uninstallExport} autocomplete="off" placeholder="C:\Backups\Pandrator-data.zip"/>
          </label>
          <label class="mt-3 flex items-start gap-2 text-xs text-red-500">
            <input type="checkbox" bind:checked={uninstallPurge} class="mt-0.5"/>
            Permanently purge user data after any requested export
          </label>
          <button class="btn btn-secondary mt-4 text-red-500" disabled={uninstallBusy} onclick={createUninstallPlan}>
            {#if uninstallBusy}<LoaderCircle class="animate-spin" size={15}/>{:else}<Trash2 size={15}/>{/if}
            Review uninstall
          </button>
        </div>
      </div>
    </section>
  {/if}
</section>

{#if pendingPlan}
  <div class="fixed inset-0 z-[70] grid place-items-center bg-black/45 p-3 backdrop-blur-sm">
    <div class="surface flex max-h-[92vh] w-full max-w-2xl flex-col overflow-hidden rounded-3xl" role="dialog" aria-modal="true" aria-labelledby="manager-plan-title">
      <header class="flex items-start justify-between gap-4 border-b border-[var(--line)] px-5 py-4 sm:px-7">
        <div>
          <div class="eyebrow">Review exact plan</div>
          <h2 id="manager-plan-title" class="mt-1 text-2xl font-semibold">{pendingPlan.kind.replaceAll('_', ' ')}</h2>
        </div>
        <button class="btn btn-icon btn-secondary" aria-label="Close plan" onclick={() => pendingPlan = null}><X size={18}/></button>
      </header>
      <div class="modal-scroll p-5 sm:p-7">
        <div class="grid gap-3 sm:grid-cols-2">
          <div class="rounded-xl border border-[var(--line)] p-3"><div class="muted text-xs">Download estimate</div><strong class="mt-1 block text-sm">{formatBytes(pendingPlan.estimated_download_bytes)}</strong></div>
          <div class="rounded-xl border border-[var(--line)] p-3"><div class="muted text-xs">Disk estimate</div><strong class="mt-1 block text-sm">{formatBytes(pendingPlan.estimated_disk_bytes)}</strong></div>
        </div>
        {#if pendingPlan.impacts?.release}
          <div class="mt-3 rounded-xl border border-[var(--line)] p-3 text-sm">
            <strong>{pendingPlan.impacts.release.product} {pendingPlan.impacts.release.version}</strong>
            <span class="muted ml-1">· {pendingPlan.impacts.release.channel} · sequence {pendingPlan.impacts.release.sequence}</span>
            {#if pendingPlan.impacts.release.legacy_data}
              <div class="muted mt-1 text-xs">
                Legacy data reconciliation: {pendingPlan.impacts.release.legacy_data.file_count} file(s),
                {formatBytes(pendingPlan.impacts.release.legacy_data.size_bytes)}; sources retained through activation.
              </div>
            {/if}
          </div>
        {/if}
        {#if pendingPlan.impacts?.uninstall}
          <div class="mt-3 rounded-xl border border-red-400/30 p-3 text-sm">
            <strong>{pendingPlan.impacts.uninstall.purge_data ? 'User data will be permanently purged' : 'User data will be preserved'}</strong>
            {#if pendingPlan.impacts.uninstall.export_data}
              <div class="muted mt-1 text-xs">Export: {pendingPlan.impacts.uninstall.export_data}</div>
            {/if}
            {#if pendingPlan.impacts.uninstall.legacy_data}
              <div class="muted mt-1 text-xs">
                Known legacy data: {pendingPlan.impacts.uninstall.legacy_data.file_count} file(s),
                {formatBytes(pendingPlan.impacts.uninstall.legacy_data.size_bytes)}
                {pendingPlan.impacts.uninstall.legacy_data_reconciled ? ' will be reconciled before removal.' : '.'}
              </div>
            {/if}
            <div class="muted mt-1 text-xs">Removing this installation does not uninstall the Python package distribution.</div>
          </div>
        {/if}
        <h3 class="mt-5 text-sm font-semibold">Tasks</h3>
        <ol class="mt-2 space-y-2">
          {#each pendingPlan.tasks as task, index}
            <li class="flex gap-3 rounded-xl border border-[var(--line)] p-3 text-sm"><span class="muted">{index + 1}</span><span>{task.label}</span></li>
          {/each}
        </ol>
        {#if pendingPlan.preflight?.length}
          <h3 class="mt-5 text-sm font-semibold">Host preflight</h3>
          <ul class="mt-2 space-y-2">
            {#each pendingPlan.preflight as check}
              <li class="flex items-start gap-2 rounded-xl border border-[var(--line)] p-3 text-sm">
                {#if check.status === 'pass'}
                  <CheckCircle2 class="mt-0.5 shrink-0 text-[var(--success)]" size={15}/>
                {:else}
                  <CircleAlert class="mt-0.5 shrink-0 text-amber-500" size={15}/>
                {/if}
                <span><strong>{check.code}</strong><span class="muted ml-1">{check.message}</span></span>
              </li>
            {/each}
          </ul>
        {/if}
        {#if pendingPlan.warnings.length}
          <div class="mt-4 rounded-xl border border-amber-400/40 bg-amber-500/10 p-3 text-sm">{pendingPlan.warnings.join(' ')}</div>
        {/if}
        {#if pendingPlan.confirmations.length}
          <h3 class="mt-5 text-sm font-semibold">Required confirmations</h3>
          <ul class="mt-2 space-y-2">
            {#each pendingPlan.confirmations as confirmation}
              <li class="rounded-xl border border-[var(--line)] p-3 text-sm">
                {confirmation.message}
                {#if confirmation.url}<a class="ml-1 text-[var(--accent)] underline" href={confirmation.url} target="_blank" rel="noreferrer">Review terms</a>{/if}
              </li>
            {/each}
          </ul>
        {/if}
        {#if pendingPlan.application_impacts?.managed_provider_bindings?.length}
          <h3 class="mt-5 text-sm font-semibold">Pandrator provider impact</h3>
          <ul class="mt-2 space-y-2">
            {#each pendingPlan.application_impacts.managed_provider_bindings as impact}
              <li class="rounded-xl border border-amber-400/40 bg-amber-500/10 p-3 text-sm">
                <strong>{impact.label}</strong>
                {#if impact.selected_default}
                  <span class="ml-1 font-semibold">(current default)</span>
                {/if}
                <div class="muted mt-1 text-xs">{impact.message}</div>
              </li>
            {/each}
          </ul>
        {/if}
      </div>
      <footer class="flex justify-end gap-2 border-t border-[var(--line)] px-5 py-4 sm:px-7">
        <button class="btn btn-secondary" onclick={() => pendingPlan = null}>Cancel</button>
        <button class="btn btn-primary" onclick={executePlan}>Confirm exact plan</button>
      </footer>
    </div>
  </div>
{/if}

<style>
  .field{margin-top:.4rem;width:100%;border:1px solid var(--line);border-radius:.72rem;background:var(--paper);padding:.55rem .65rem;color:var(--ink)}
  .status-dot{display:block;width:.65rem;height:.65rem;border-radius:999px;background:var(--muted);opacity:.5}
  .status-dot.running{background:var(--success);opacity:1}
  .status-dot.starting,.status-dot.restarting{background:var(--accent);opacity:1;animation:pulse 1.4s ease-in-out infinite}
  .status-dot.degraded{background:#d97706;opacity:1}
  .status-dot.unhealthy,.status-dot.failed{background:#dc2626;opacity:1}
  @keyframes pulse{50%{opacity:.45}}
</style>
