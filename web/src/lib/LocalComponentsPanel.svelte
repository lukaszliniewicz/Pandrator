<script lang="ts">
  import { errorMessage } from './errors';
  import {
    CheckCircle2,
    CircleAlert,
    Download,
    LoaderCircle,
    Play,
    RefreshCw,
    RotateCcw,
    Square,
    Trash2
  } from '@lucide/svelte';
  import { onMount } from 'svelte';
  import { managerApi } from './admin-api';
  import ManagerPlanDialog, {
    type ManagerPlan
  } from './ManagerPlanDialog.svelte';
  import {
    MANAGER_TERMINAL_STATES,
    managerOperationStore,
    type ManagerOperation
  } from './manager-operation-store.svelte';

  type ComputeVariant =
    'auto' | 'cpu' | 'cuda' | 'vulkan' | 'metal' | 'rocm' | 'wgpu';
  type InstallOptionChoice = {
    value: string;
    label: string;
    description?: string;
    requires?: Record<string, string[]>;
  };
  type InstallOption = {
    key: string;
    label: string;
    description?: string;
    state_field: 'options' | 'quantization';
    default: string;
    choices: InstallOptionChoice[];
  };
  type Definition = {
    id: string;
    label: string;
    description: string;
    guidance?: string;
    service_key?: string | null;
    compute_variants: ComputeVariant[];
    install_options?: InstallOption[];
    supported_actions: string[];
    default_port?: number | null;
  };
  type Inspection = {
    state: 'absent' | 'present' | 'degraded' | 'unknown' | 'unsupported';
    installed_version?: string | null;
    installed_revision?: string | null;
    resolved?: {
      compute?: string;
      quantization?: string | null;
      options?: Record<string, unknown>;
    } | null;
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
    current: Record<
      string,
      {
        version: string;
        channel: string;
        sequence: number;
      }
    >;
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

  const status = $derived(managerOperationStore.status);
  let components = $state<Component[]>([]);
  let services = $state<Service[]>([]);
  let compute = $state<Record<string, ComputeVariant>>({});
  let installOptionValues = $state<Record<string, Record<string, string>>>({});
  let pendingPlan = $state<ManagerPlan | null>(null);
  const operation = $derived(managerOperationStore.operation);
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
  const componentGroups = $derived([
    {
      label: 'Installed',
      items: components.filter((component) =>
        ['present', 'degraded'].includes(component.inspection.state)
      )
    },
    {
      label: 'Not installed',
      items: components.filter(
        (component) =>
          !['present', 'degraded'].includes(component.inspection.state)
      )
    }
  ]);
  const updateCandidates = $derived(
    components.filter(
      (component) =>
        component.inspection.state === 'present' &&
        component.definition.supported_actions.includes('update')
    )
  );
  const operationBusy = $derived(
    Boolean(operation && !MANAGER_TERMINAL_STATES.has(operation.state))
  );

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

  const configuredCompute = (component: Component) =>
    component.desired?.compute ??
    (component.inspection.resolved?.compute as ComputeVariant | undefined) ??
    'auto';

  const selectedCompute = (component: Component) =>
    compute[component.definition.id] ?? configuredCompute(component);

  const normalizeInstallOptionValue = (
    component: Component,
    option: InstallOption,
    rawValue: unknown
  ) => {
    let value = String(rawValue ?? option.default).trim();
    if (
      component.definition.id === 'qwen_tts' &&
      option.key === 'initial_model'
    ) {
      const legacyAliases: Record<string, string> = {
        custom_voice: 'customvoice',
        both: 'base'
      };
      value = legacyAliases[value.toLowerCase()] ?? value.toLowerCase();
    }
    return option.choices.some((choice) => choice.value === value)
      ? value
      : option.default;
  };

  const configuredInstallOption = (
    component: Component,
    option: InstallOption
  ) => {
    const rawValue =
      option.state_field === 'quantization'
        ? (component.desired?.quantization ??
          component.inspection.resolved?.quantization)
        : (component.desired?.options?.[option.key] ??
          component.inspection.resolved?.options?.[option.key]);
    return normalizeInstallOptionValue(component, option, rawValue);
  };

  const selectedInstallOption = (component: Component, option: InstallOption) =>
    installOptionValues[component.definition.id]?.[option.key] ??
    configuredInstallOption(component, option);

  const installChoiceAllowed = (
    component: Component,
    choice: InstallOptionChoice,
    values: Record<string, string> = installOptionValues[
      component.definition.id
    ] ?? {}
  ) =>
    Object.entries(choice.requires ?? {}).every(([dependency, allowed]) => {
      const dependencyOption = component.definition.install_options?.find(
        (option) => option.key === dependency
      );
      const selected =
        values[dependency] ??
        (dependencyOption
          ? configuredInstallOption(component, dependencyOption)
          : '');
      return allowed.includes(selected);
    });

  function selectInstallOption(
    component: Component,
    option: InstallOption,
    value: string
  ) {
    const values = {
      ...(installOptionValues[component.definition.id] ?? {}),
      [option.key]: value
    };
    const options = component.definition.install_options ?? [];
    for (let pass = 0; pass < options.length; pass += 1) {
      let changed = false;
      for (const candidateOption of options) {
        const selected =
          values[candidateOption.key] ??
          configuredInstallOption(component, candidateOption);
        const selectedChoice = candidateOption.choices.find(
          (choice) => choice.value === selected
        );
        if (
          selectedChoice &&
          installChoiceAllowed(component, selectedChoice, values)
        )
          continue;
        const replacement =
          candidateOption.choices.find(
            (choice) =>
              choice.value === candidateOption.default &&
              installChoiceAllowed(component, choice, values)
          ) ??
          candidateOption.choices.find((choice) =>
            installChoiceAllowed(component, choice, values)
          );
        if (replacement && replacement.value !== selected) {
          values[candidateOption.key] = replacement.value;
          changed = true;
        }
      }
      if (!changed) break;
    }
    installOptionValues = {
      ...installOptionValues,
      [component.definition.id]: values
    };
  }

  const configurationChanged = (component: Component) =>
    selectedCompute(component) !== configuredCompute(component) ||
    (component.definition.install_options ?? []).some(
      (option) =>
        selectedInstallOption(component, option) !==
        configuredInstallOption(component, option)
    );

  const serviceFor = (component: Component) =>
    services.find(
      (service) =>
        service.id === component.definition.service_key ||
        service.component_id === component.definition.id
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
      ({ state, label } =
        health === 'failed'
          ? { state: 'failed', label: 'Failed' }
          : { state: 'restarting', label: 'Restarting' });
    } else if (component.inspection.state === 'degraded') {
      ({ state, label } = { state: 'degraded', label: 'Needs repair' });
    } else if (component.inspection.state === 'present' && service) {
      ({ state, label } =
        health === 'failed'
          ? { state: 'failed', label: 'Failed' }
          : { state: 'stopped', label: 'Stopped' });
    } else if (component.inspection.state === 'present') {
      ({ state, label } = {
        state: 'unavailable',
        label: 'Status unavailable'
      });
    } else if (component.inspection.state === 'absent') {
      label = 'Not installed';
    }

    const wantsToRun = Boolean(service?.process || service?.desired_running);
    const action =
      wantsToRun && component.definition.supported_actions.includes('stop')
        ? 'stop'
        : service &&
            component.inspection.state === 'present' &&
            component.definition.supported_actions.includes('start')
          ? 'start'
          : null;
    return { state, label, action };
  };

  function report(caught: unknown) {
    error = errorMessage(caught);
    notice = '';
  }

  async function load() {
    try {
      await managerOperationStore.refresh();
      const currentStatus = managerOperationStore.status;
      if (!currentStatus?.available) {
        components = [];
        services = [];
        return;
      }
      const [componentPayload, servicePayload, releasePayload] =
        await Promise.all([
          managerApi.components<{ items: Component[] }>(),
          managerApi.services<{ items: Service[] }>(),
          managerApi.releases<ReleaseInventory>()
        ]);
      components = componentPayload.items.filter(
        (component) =>
          component.definition.id !== 'pandrator' &&
          Boolean(component.definition.service_key)
      );
      services = servicePayload.items;
      releases = releasePayload;
      const nextCompute: Record<string, ComputeVariant> = {};
      const nextInstallOptionValues: Record<
        string,
        Record<string, string>
      > = {};
      for (const component of components) {
        nextCompute[component.definition.id] = configuredCompute(component);
        nextInstallOptionValues[component.definition.id] = Object.fromEntries(
          (component.definition.install_options ?? []).map((option) => [
            option.key,
            configuredInstallOption(component, option)
          ])
        );
      }
      compute = nextCompute;
      installOptionValues = nextInstallOptionValues;
      error = '';
    } catch (caught) {
      report(caught);
    } finally {
      loading = false;
    }
  }

  async function refreshOperation() {
    if (!operation || MANAGER_TERMINAL_STATES.has(operation.state)) return;
    try {
      await managerOperationStore.refresh();
      const current = managerOperationStore.operation;
      if (current && MANAGER_TERMINAL_STATES.has(current.state)) {
        if (current.state === 'succeeded')
          notice = 'Local component changes completed.';
        else
          error =
            current.error_message || `Operation ended as ${current.state}.`;
        await load();
      }
    } catch (caught) {
      report(caught);
    }
  }

  async function refreshManagerState() {
    if (!status?.available) return;
    try {
      const servicePayload = await managerApi.services<{ items: Service[] }>();
      services = servicePayload.items;
    } catch (caught) {
      report(caught);
    }
  }

  async function pollOperation() {
    if (pollStopped) return;
    await refreshManagerState();
    if (!pollStopped) {
      pollTimer = setTimeout(() => void pollOperation(), 2500);
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
      const requestedOptions = {
        ...(component.inspection.resolved?.options ?? {}),
        ...(component.desired?.options ?? {})
      };
      let requestedQuantization =
        component.desired?.quantization ??
        component.inspection.resolved?.quantization ??
        null;
      for (const option of component.definition.install_options ?? []) {
        const value = selectedInstallOption(component, option);
        if (option.state_field === 'quantization')
          requestedQuantization = value;
        else requestedOptions[option.key] = value;
      }
      pendingPlan = await managerApi.plan<ManagerPlan>({
        kind,
        desired: {
          [component.definition.id]: {
            present: kind !== 'remove',
            compute: selectedCompute(component),
            quantization: requestedQuantization,
            options: {
              ...requestedOptions,
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

  async function createBatchPlan() {
    if (!updateCandidates.length || operationBusy || planning) return;
    planning = 'batch';
    error = '';
    notice = '';
    try {
      const desired = Object.fromEntries(
        updateCandidates.map((component) => [
          component.definition.id,
          {
            present: true,
            compute: configuredCompute(component),
            quantization:
              component.desired?.quantization ??
              component.inspection.resolved?.quantization ??
              null,
            options: {
              ...(component.inspection.resolved?.options ?? {}),
              ...(component.desired?.options ?? {}),
              start_after_install: false
            }
          }
        ])
      );
      pendingPlan = await managerApi.plan<ManagerPlan>({
        kind: 'update',
        desired,
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
      const submitted = await managerApi.submit<ManagerOperation>({
        plan_id: plan.id,
        plan_digest: plan.digest,
        accepted_confirmations: plan.confirmations.map(
          (confirmation) => confirmation.key
        )
      });
      managerOperationStore.setOperation(submitted);
      if (plan.kind === 'uninstall') {
        pollStopped = true;
        if (pollTimer) clearTimeout(pollTimer);
        notice =
          'Uninstall was accepted. Pandrator and the manager will close while the external helper completes cleanup.';
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
      notice =
        action === 'stop'
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
      await managerOperationStore.cancel();
      notice =
        'Cancellation requested. The manager will stop at a safe boundary.';
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
      if (
        !manifest ||
        Array.isArray(manifest) ||
        typeof manifest !== 'object'
      ) {
        throw new Error('The signed release manifest must be a JSON object.');
      }
      pendingPlan = await managerApi.releasePlan<ManagerPlan>({
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
    )
      return;
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
      pendingPlan = await managerApi.uninstallPlan<ManagerPlan>({
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
    let lastTerminal =
      operation && MANAGER_TERMINAL_STATES.has(operation.state)
        ? `${operation.id}:${operation.state}`
        : '';
    const disconnectOperation = managerOperationStore.connect((current) => {
      if (!current || !MANAGER_TERMINAL_STATES.has(current.state)) return;
      const terminal = `${current.id}:${current.state}`;
      if (terminal === lastTerminal) return;
      lastTerminal = terminal;
      if (current.state === 'succeeded')
        notice = 'Local component changes completed.';
      else
        error = current.error_message || `Operation ended as ${current.state}.`;
      void load();
    });
    void load();
    void pollOperation();
    return () => {
      disconnectOperation();
      pollStopped = true;
      if (pollTimer) clearTimeout(pollTimer);
    };
  });
</script>

<section>
  <div class="flex flex-wrap items-end justify-between gap-4">
    <div>
      <div class="eyebrow">Local speech services</div>
      <h2 class="mt-1 text-2xl font-semibold">
        Install and run speech providers
      </h2>
      <p class="muted mt-2 max-w-3xl text-sm">
        Pandrator shows the controls here; the independent local manager
        performs and journals every host change. External endpoints remain
        available without it.
      </p>
    </div>
    <button class="btn btn-secondary" onclick={load} disabled={loading}>
      <RefreshCw size={16} class={loading ? 'animate-spin' : ''} /> Refresh
    </button>
    <button
      class="btn btn-secondary"
      disabled={loading ||
        Boolean(planning) ||
        Boolean(runtimeBusy) ||
        operationBusy ||
        !updateCandidates.length}
      onclick={createBatchPlan}
    >
      {#if planning === 'batch'}<LoaderCircle
          class="animate-spin"
          size={15}
        />{:else}<RefreshCw size={15} />{/if}
      Review updates ({updateCandidates.length})
    </button>
  </div>

  {#if error}
    <div
      role="alert"
      class="mt-4 flex items-start gap-2 rounded-xl border border-red-400/40 bg-red-500/10 px-4 py-3 text-sm"
    >
      <CircleAlert class="mt-0.5 shrink-0" size={16} /><span>{error}</span>
    </div>
  {/if}
  {#if notice}
    <div
      role="status"
      class="mt-4 flex items-center gap-2 rounded-xl border border-[var(--line)] bg-[var(--accent-soft)] px-4 py-3 text-sm"
    >
      <CheckCircle2 size={16} />{notice}
    </div>
  {/if}

  {#if loading}
    <div class="muted mt-6 flex items-center gap-2 text-sm">
      <LoaderCircle class="animate-spin" size={16} /> Contacting the local manager…
    </div>
  {:else if !status?.available}
    <div class="mt-5 rounded-2xl border border-[var(--line)] p-5">
      <div class="flex items-start gap-3">
        <CircleAlert class="mt-0.5 shrink-0 text-amber-500" size={19} />
        <div>
          <h3 class="font-semibold">Local manager unavailable</h3>
          <p class="muted mt-1 text-sm">
            {status?.error?.message ??
              'Start Pandrator Manager to install or supervise local services.'}
          </p>
          <p class="muted mt-2 text-xs">
            Existing external providers and normal Pandrator workflows are not
            affected. Run <code>pandrator-manager open --recovery</code> on this computer
            for recovery.
          </p>
        </div>
      </div>
    </div>
  {:else}
    {#if operation && !MANAGER_TERMINAL_STATES.has(operation.state)}
      <div
        class="mt-5 rounded-2xl border border-[var(--accent)] bg-[var(--accent-soft)] p-4"
      >
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div class="text-sm font-semibold">
              Manager operation in progress
            </div>
            <div class="muted mt-1 text-xs">
              {operation.state.replaceAll('_', ' ')}
              {operation.current_task_id
                ? ` · ${operation.current_task_id}`
                : ''}
            </div>
          </div>
          <button class="btn btn-sm btn-secondary" onclick={cancelOperation}
            >Cancel safely</button
          >
        </div>
        <div class="mt-3 h-2 overflow-hidden rounded-full bg-black/10">
          <div
            class="h-full rounded-full bg-[var(--accent)] transition-[width]"
            style={`width:${Math.round(operation.progress * 100)}%`}
          ></div>
        </div>
      </div>
    {/if}

    <div class="mt-6 space-y-7">
      {#each componentGroups as group}
        {#if group.items.length}<section>
            <div class="mb-3 flex items-center gap-2">
              <h3 class="text-sm font-semibold">{group.label}</h3>
              <span
                class="muted rounded-full border border-[var(--line)] px-2 py-0.5 text-[.65rem] font-bold"
                >{group.items.length}</span
              >
            </div>
            <div class="grid gap-3 lg:grid-cols-2">
              {#each group.items as component}
                {@const service = serviceFor(component)}
                {@const runtimeState = runtimeStateFor(component, service)}
                {@const installed = component.inspection.state === 'present'}
                {@const degraded = component.inspection.state === 'degraded'}
                {@const installedVersion =
                  component.inspection.installed_version?.trim() ?? ''}
                {@const installedRevision =
                  component.inspection.installed_revision?.trim() ?? ''}
                {@const busy =
                  planning === component.definition.id ||
                  runtimeBusy === component.definition.id ||
                  Boolean(
                    operation && !MANAGER_TERMINAL_STATES.has(operation.state)
                  )}
                <article
                  id={`component-${component.definition.id}`}
                  class="scroll-mt-24 rounded-2xl border border-[var(--line)] p-4"
                >
                  <div class="flex items-start justify-between gap-3">
                    <div>
                      <div class="font-semibold">
                        {component.definition.label}
                      </div>
                      <div class="muted mt-1 text-xs">
                        {runtimeState.label}
                        {component.definition.default_port
                          ? ` · port ${component.definition.default_port}`
                          : ''}
                      </div>
                      {#if installedVersion || installedRevision}
                        <div class="muted mt-1 text-xs">
                          {#if installedVersion}<span
                              title={'Installed version ' + installedVersion}
                              >v{installedVersion}</span
                            >{/if}
                          {#if installedVersion && installedRevision}
                            <span class="mx-1">·</span>
                          {/if}
                          {#if installedRevision}<span
                              title={'Installed revision ' + installedRevision}
                              >rev {installedRevision.slice(0, 8)}</span
                            >{/if}
                        </div>
                      {/if}
                    </div>
                    <span
                      class={`status-dot ${runtimeState.state}`}
                      title={runtimeState.label}
                    ></span>
                  </div>

                  {#if component.inspection.problems?.length}
                    <p class="mt-3 text-xs text-amber-600">
                      {component.inspection.problems.join(' ')}
                    </p>
                  {/if}

                  {#if component.definition.description}
                    <p class="muted mt-3 text-xs leading-relaxed">
                      {component.definition.description}
                    </p>
                  {/if}

                  {#if component.definition.compute_variants.length > 1}
                    <label class="mt-4 block text-xs font-semibold">
                      Compute backend
                      <select
                        class="field"
                        value={selectedCompute(component)}
                        disabled={busy}
                        onchange={(event) =>
                          (compute[component.definition.id] = event
                            .currentTarget.value as ComputeVariant)}
                      >
                        <option value="auto">Automatic</option>
                        {#each component.definition.compute_variants as variant}
                          <option value={variant}
                            >{variant.toUpperCase()}</option
                          >
                        {/each}
                      </select>
                    </label>
                  {/if}

                  {#if component.definition.install_options?.length}
                    <div class="mt-4 grid gap-3 sm:grid-cols-2">
                      {#each component.definition.install_options as option}
                        <label class="block text-xs font-semibold">
                          {option.label}
                          <select
                            class="field"
                            value={selectedInstallOption(component, option)}
                            disabled={busy}
                            onchange={(event) =>
                              selectInstallOption(
                                component,
                                option,
                                event.currentTarget.value
                              )}
                          >
                            {#each option.choices as choice}
                              <option
                                value={choice.value}
                                disabled={!installChoiceAllowed(
                                  component,
                                  choice
                                )}>{choice.label}</option
                              >
                            {/each}
                          </select>
                          {#if option.description}
                            <span
                              class="muted mt-1 block font-normal leading-relaxed"
                            >
                              {option.description}
                            </span>
                          {/if}
                        </label>
                      {/each}
                    </div>
                  {/if}

                  <div class="mt-4 flex flex-wrap gap-2">
                    {#if !installed && !degraded && component.definition.supported_actions.includes('install')}
                      <button
                        class="btn btn-sm btn-primary"
                        disabled={busy}
                        onclick={() => createPlan(component, 'install')}
                      >
                        <Download size={13} /> Install locally
                      </button>
                    {/if}
                    {#if degraded && component.definition.supported_actions.includes('repair')}
                      <button
                        class="btn btn-sm btn-primary"
                        disabled={busy}
                        onclick={() => createPlan(component, 'repair')}
                      >
                        <RotateCcw size={13} /> Repair
                      </button>
                    {/if}
                    {#if installed && component.definition.supported_actions.includes('update')}
                      <button
                        class="btn btn-sm btn-secondary"
                        disabled={busy}
                        onclick={() => createPlan(component, 'update')}
                      >
                        <RefreshCw size={13} />
                        {configurationChanged(component)
                          ? 'Apply configuration'
                          : 'Check/update'}
                      </button>
                    {/if}
                    {#if runtimeState.action === 'start'}
                      <button
                        class="btn btn-sm btn-secondary"
                        disabled={busy}
                        onclick={() => runtime(component, 'start')}
                      >
                        <Play size={13} /> Start
                      </button>
                    {/if}
                    {#if runtimeState.action === 'stop'}
                      <button
                        class="btn btn-sm btn-secondary"
                        disabled={busy}
                        onclick={() => runtime(component, 'stop')}
                      >
                        <Square size={13} /> Stop
                      </button>
                    {/if}
                    {#if (installed || degraded) && component.definition.supported_actions.includes('remove')}
                      <button
                        class="btn btn-sm btn-secondary text-red-500"
                        disabled={busy}
                        onclick={() => createPlan(component, 'remove')}
                      >
                        <Trash2 size={13} /> Uninstall
                      </button>
                    {/if}
                  </div>
                </article>
              {/each}
            </div>
          </section>{/if}
      {/each}
    </div>

    <section class="mt-8 rounded-2xl border border-[var(--line)] p-5">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div class="eyebrow">Host maintenance</div>
          <h3 class="mt-1 text-xl font-semibold">
            Diagnostics, signed updates, and uninstall
          </h3>
          <p class="muted mt-2 max-w-3xl text-sm">
            Diagnostics are read-only. Release manifests are verified against
            manager-embedded project keys; this page cannot provide a trust key.
            Host mutations remain local-only by default.
          </p>
        </div>
        <button
          class="btn btn-secondary"
          disabled={doctorBusy}
          onclick={runDoctor}
        >
          {#if doctorBusy}<LoaderCircle
              class="animate-spin"
              size={15}
            />{:else}<RotateCcw size={15} />{/if}
          Run diagnostics
        </button>
      </div>

      {#if doctor}
        <div class="mt-4 rounded-xl border border-[var(--line)] p-4">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <strong
              >{doctor.healthy
                ? 'No diagnostic errors'
                : `${doctor.summary.error} diagnostic error(s)`}</strong
            >
            <span class="muted text-xs"
              >{doctor.summary.pass} passed · {doctor.summary.warning} warnings ·
              {doctor.summary.error} errors</span
            >
          </div>
          <div class="mt-3 space-y-2">
            {#each doctor.checks.filter((check) => check.status !== 'pass') as check}
              <div class="rounded-xl border border-[var(--line)] p-3 text-sm">
                <div class="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <strong
                      class={check.status === 'error'
                        ? 'text-red-500'
                        : 'text-amber-600'}
                      >{check.status.toUpperCase()} · {check.id}</strong
                    >
                    <p class="muted mt-1 text-xs">{check.message}</p>
                  </div>
                  {#if check.repairable && String(check.repair_target ?? '').startsWith('component:')}
                    <button
                      class="btn btn-sm btn-secondary"
                      onclick={() => repairDiagnostic(check)}
                      >Review repair</button
                    >
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
          <button
            class="btn btn-secondary mt-4"
            disabled={legacyBusy}
            onclick={inspectLegacy}
          >
            {#if legacyBusy}<LoaderCircle
                class="animate-spin"
                size={15}
              />{:else}<RotateCcw size={15} />{/if}
            Inspect legacy workspace
          </button>
          {#if legacy}
            <div
              class="mt-3 rounded-xl border border-[var(--line)] p-3 text-xs"
            >
              {#if !legacy.available || !legacy.report}
                <span class="muted"
                  >No legacy installer configuration was found.</span
                >
              {:else}
                <strong>
                  {legacy.report.valid
                    ? `${legacy.report.positively_identified.length} component(s) positively identified`
                    : 'Malformed configuration; quarantine only'}
                </strong>
                <div class="muted mt-1 break-all">
                  Digest: {legacy.report.source_digest}
                </div>
                {#if !legacy.report.legacy_data?.error}
                  <div class="muted mt-1">
                    Known mutable data: {legacy.report.legacy_data
                      ?.file_count ?? 0} file(s),
                    {formatBytes(legacy.report.legacy_data?.size_bytes ?? 0)}
                  </div>
                {:else}
                  <div class="mt-1 text-amber-600">
                    {legacy.report.legacy_data.error}
                  </div>
                {/if}
                {#if legacy.report.unknown_paths.length}
                  <div class="mt-1 text-amber-600">
                    Unknown paths retained: {legacy.report.unknown_paths.join(
                      ', '
                    )}
                  </div>
                {/if}
                {#each legacy.report.warnings as warning}
                  <div class="mt-1 text-amber-600">{warning}</div>
                {/each}
                {#if legacy.report.already_imported}
                  <div class="muted mt-2">
                    This exact configuration was already imported.
                  </div>
                {:else}
                  <button
                    class="btn btn-sm btn-secondary mt-3"
                    disabled={legacyBusy}
                    onclick={importLegacy}
                  >
                    {legacy.report.valid
                      ? 'Import reviewed state'
                      : 'Quarantine reviewed configuration'}
                  </button>
                {/if}
              {/if}
            </div>
          {/if}
        </div>

        <div class="rounded-xl border border-[var(--line)] p-4">
          <h4 class="font-semibold">Signed product update</h4>
          <p class="muted mt-1 text-xs">
            Current: Pandrator {releases.current.pandrator?.version ??
              'not recorded'} · Manager {releases.current['pandrator-manager']
              ?.version ?? 'not recorded'}
          </p>
          <label class="mt-4 block text-xs font-semibold">
            Signed JSON manifest
            <input
              class="field"
              type="file"
              accept="application/json,.json"
              onchange={(event) =>
                (releaseManifest = event.currentTarget.files?.[0] ?? null)}
            />
          </label>
          <label class="mt-3 flex items-start gap-2 text-xs">
            <input
              type="checkbox"
              bind:checked={releaseOffline}
              class="mt-0.5"
            />
            Require the exact artifact in the verified local cache
          </label>
          <label class="mt-2 flex items-start gap-2 text-xs">
            <input
              type="checkbox"
              bind:checked={releaseKeepStopped}
              class="mt-0.5"
            />
            Leave Pandrator stopped after application activation
          </label>
          <button
            class="btn btn-primary mt-4"
            disabled={releaseBusy}
            onclick={createReleasePlan}
          >
            {#if releaseBusy}<LoaderCircle
                class="animate-spin"
                size={15}
              />{:else}<Download size={15} />{/if}
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
            <input
              class="field"
              bind:value={uninstallExport}
              autocomplete="off"
              placeholder="C:\Backups\Pandrator-data.zip"
            />
          </label>
          <label class="mt-3 flex items-start gap-2 text-xs text-red-500">
            <input
              type="checkbox"
              bind:checked={uninstallPurge}
              class="mt-0.5"
            />
            Permanently purge user data after any requested export
          </label>
          <button
            class="btn btn-secondary mt-4 text-red-500"
            disabled={uninstallBusy}
            onclick={createUninstallPlan}
          >
            {#if uninstallBusy}<LoaderCircle
                class="animate-spin"
                size={15}
              />{:else}<Trash2 size={15} />{/if}
            Review uninstall
          </button>
        </div>
      </div>
    </section>
  {/if}
</section>

{#if pendingPlan}
  <ManagerPlanDialog
    plan={pendingPlan}
    onclose={() => (pendingPlan = null)}
    onconfirm={executePlan}
  />
{/if}

<style>
  .field {
    margin-top: 0.4rem;
    width: 100%;
    border: 1px solid var(--line);
    border-radius: 0.72rem;
    background: var(--paper);
    padding: 0.55rem 0.65rem;
    color: var(--ink);
  }
  .status-dot {
    display: block;
    width: 0.65rem;
    height: 0.65rem;
    border-radius: 999px;
    background: var(--muted);
    opacity: 0.5;
  }
  .status-dot.running {
    background: var(--success);
    opacity: 1;
  }
  .status-dot.starting,
  .status-dot.restarting {
    background: var(--accent);
    opacity: 1;
    animation: pulse 1.4s ease-in-out infinite;
  }
  .status-dot.degraded {
    background: #d97706;
    opacity: 1;
  }
  .status-dot.unhealthy,
  .status-dot.failed {
    background: #dc2626;
    opacity: 1;
  }
  @keyframes pulse {
    50% {
      opacity: 0.45;
    }
  }
</style>
