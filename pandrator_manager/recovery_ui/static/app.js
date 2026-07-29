"use strict";

let csrf = "";
let snapshot = {
  status: null,
  application: null,
  network: null,
  components: [],
  services: [],
  operations: [],
  activity: [],
  releases: { items: [], current: {} },
};
let selectedPlan = null;
let selectedPlanTitle = "";
let activeOperation = null;
let refreshInFlight = false;
let refreshTimer = null;
let pollingStopped = false;
let catalogueSignature = "";
let selectionInitialized = false;
let applicationBusy = false;
let networkBusy = false;
let networkDirty = false;
let networkInitialized = false;
let networkPreviousMode = "local";
let activeManagerTab = "install";
let pendingInstallOperationId = "";
let pendingPostInstallAccess = null;
let finishingInstall = false;

const componentState = new Map();
const componentNodes = new Map();
const sectionState = new Map();

const terminalStates = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "recovery_required",
]);

const sectionPresentation = {
  text_to_speech: {
    title: "Text to speech",
    description: "Create speech from text using cloning or ready-made voices.",
  },
  speech_to_text: {
    title: "Speech to text",
    description: "Transcribe recordings and identify words or speakers.",
  },
  speech_to_speech: {
    title: "Speech to speech",
    description: "Convert an existing recording into another trained voice.",
  },
  training: {
    title: "Training tools",
    description: "Advanced tools for creating custom local models.",
  },
};

const byId = (id) => document.getElementById(id);

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function text(tag, value, className = "") {
  const node = document.createElement(tag);
  node.textContent = String(value ?? "");
  if (className) node.className = className;
  return node;
}

function setManagerHealth(state, label) {
  const health = byId("manager-health");
  health.dataset.state = state;
  health.setAttribute("aria-label", label);
  byId("manager-health-text").textContent = label;
  byId("manager-health-tooltip").textContent = label;
}

function activateManagerTab(name, { focus = false } = {}) {
  const selected = document.querySelector(
    `.manager-tab[data-tab="${name}"]`,
  );
  if (!selected) return;
  activeManagerTab = name;
  for (const tab of document.querySelectorAll(".manager-tab")) {
    const active = tab === selected;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
  }
  for (const panel of document.querySelectorAll(".tab-panel")) {
    panel.hidden = panel.dataset.panel !== name;
  }
  if (focus) selected.focus();
}

function handleManagerTabKeydown(event) {
  const tabs = [...document.querySelectorAll(".manager-tab")];
  const current = tabs.indexOf(event.currentTarget);
  if (current < 0) return;
  let next = current;
  if (event.key === "ArrowRight") next = (current + 1) % tabs.length;
  else if (event.key === "ArrowLeft") {
    next = (current - 1 + tabs.length) % tabs.length;
  } else if (event.key === "Home") next = 0;
  else if (event.key === "End") next = tabs.length - 1;
  else return;
  event.preventDefault();
  activateManagerTab(tabs[next].dataset.tab, { focus: true });
}

function openNetworkSettings() {
  activateManagerTab("maintenance", { focus: true });
  const details = byId("network-details");
  details.open = true;
  details.scrollIntoView({ behavior: "smooth", block: "start" });
}

function makeButton(label, action, className = "button secondary") {
  const node = text("button", label, className);
  node.type = "button";
  node.addEventListener("click", action);
  return node;
}

function showMessage(message, error = false) {
  const node = byId("message");
  node.textContent = message;
  node.className = `message${error ? " error" : ""}`;
  if (!message) node.classList.add("hidden");
}

function idempotencyKey() {
  if (typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  const value = crypto.getRandomValues(new Uint8Array(16));
  value[6] = (value[6] & 0x0f) | 0x40;
  value[8] = (value[8] & 0x3f) | 0x80;
  const hex = Array.from(value, (item) =>
    item.toString(16).padStart(2, "0"),
  ).join("");
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20),
  ].join("-");
}

function privateHttpWorkstation() {
  const hostname = location.hostname.toLowerCase();
  const loopback =
    hostname === "localhost" ||
    hostname === "::1" ||
    hostname === "[::1]" ||
    hostname.startsWith("127.");
  return location.protocol === "http:" && !loopback;
}

function chooseRememberedBrowser() {
  if (!privateHttpWorkstation()) return Promise.resolve(true);
  const dialog = byId("trust-browser-dialog");
  const remember = byId("remember-browser");
  dialog.returnValue = "";
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener(
      "close",
      () => {
        resolve(dialog.returnValue === "continue" && remember.checked);
      },
      { once: true },
    );
  });
}

function sessionDate(value) {
  const date = new Date(Number(value) * 1000);
  return Number.isNaN(date.getTime()) ? "an unknown time" : date.toLocaleString();
}

function renderBrowserSession(payload) {
  const menu = byId("session-menu");
  if (!payload?.session) {
    menu.classList.add("hidden");
    return;
  }
  const session = payload.session;
  const count = Number(payload.active_session_count || 1);
  const remembered = Boolean(session.remembered);
  const summaryLabel = remembered
    ? "Browser remembered"
    : "Browser authorized";
  const summary = byId("session-summary");
  summary.setAttribute("aria-label", summaryLabel);
  summary.title = summaryLabel;
  byId("session-summary-label").textContent = summaryLabel;
  const idleDays = Math.max(
    1,
    Math.round(
      Number(payload.policy?.remembered_idle_ttl_seconds || 0) / 86400,
    ),
  );
  byId("session-detail").textContent = remembered
    ? `Authorization renews when used and expires after ${idleDays} day${
        idleDays === 1 ? "" : "s"
      } of inactivity, or by ${sessionDate(
        session.absolute_expires_at,
      )} at the latest. ${count} authorized browser${
        count === 1 ? "" : "s"
      } in total.`
    : `This authorization ends when the browser closes or by ${sessionDate(
        session.expires_at,
      )}.`;
  const note = byId("session-security-note");
  const insecure = Boolean(payload.policy?.insecure_private_http);
  note.textContent = insecure
    ? "This is a trusted-network HTTP session. Prefer HTTPS for long-lived remote access."
    : "";
  note.classList.toggle("hidden", !insecure);
  menu.classList.remove("hidden");
}

async function resumeBrowserSession() {
  const response = await fetch("/v1/session", {
    headers: { Accept: "application/json" },
  });
  const payload = await response.json().catch(() => ({}));
  if (response.ok) {
    csrf = payload.csrf_token;
    renderBrowserSession(payload);
    return true;
  }
  return false;
}

async function establishBrowserSession() {
  const params = new URLSearchParams(location.hash.slice(1));
  const token = params.get("token");
  if (!token) {
    if (await resumeBrowserSession()) return;
    throw new Error(
      "This browser is not authorized for Pandrator Manager. Open the manager again from Pandrator, the tray icon, or the launcher.",
    );
  }
  history.replaceState(null, "", "/recovery");
  const remember = await chooseRememberedBrowser();
  const response = await fetch("/v1/recovery/exchange", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token, remember }),
  });
  if (!response.ok) {
    if (await resumeBrowserSession()) return;
    throw new Error("The recovery link is invalid or expired.");
  }
  const payload = await response.json();
  csrf = payload.csrf_token;
  renderBrowserSession(payload);
}

async function requestJson(path, options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers || {});
  if (!["GET", "HEAD"].includes(method)) {
    if (!csrf) {
      throw new Error("Open a fresh recovery link before making changes.");
    }
    headers.set("X-CSRF-Token", csrf);
    headers.set("Idempotency-Key", idempotencyKey());
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, { ...options, method, headers });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const envelope = payload.error || {};
    const authenticationExpired =
      response.status === 401 && envelope.code === "authentication_required";
    if (authenticationExpired) {
      csrf = "";
      renderBrowserSession(null);
    }
    const error = new Error(
      envelope.message ||
        (authenticationExpired
          ? "This browser authorization expired or was revoked. Open Pandrator Manager again from Pandrator, the tray icon, or the launcher."
          : `Manager returned HTTP ${response.status}.`),
    );
    error.code = envelope.code || "";
    error.details = envelope.details || {};
    throw error;
  }
  return payload;
}

function bytes(value) {
  if (!value) return "not available";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let amount = Number(value);
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) {
    amount /= 1024;
    index += 1;
  }
  return `${amount >= 10 || index === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`;
}

function stateLabel(value) {
  return String(value || "unknown").replaceAll("_", " ");
}

function componentStateLabel(value) {
  return (
    {
      present: "Installed",
      absent: "Not installed",
      degraded: "Needs repair",
      unknown: "Not detected",
    }[value] || stateLabel(value)
  );
}

function serviceFor(component) {
  return snapshot.services.find(
    (service) =>
      service.id === component.definition.service_key ||
      service.component_id === component.definition.id,
  );
}

function selectable(component) {
  const state = component.inspection.state;
  const supported = new Set(component.definition.supported_actions || []);
  return (
    (["absent", "unknown"].includes(state) && supported.has("install")) ||
    (state === "degraded" && supported.has("repair"))
  );
}

function controlsFor(component) {
  const id = component.definition.id;
  let state = componentState.get(id);
  if (state) return state;

  const desired = component.desired || {};
  const options = { ...(desired.options || {}) };
  const definitions = component.definition.install_options || [];
  let quantization = desired.quantization || null;
  for (const option of definitions) {
    if (option.state_field === "quantization") {
      quantization ||= option.default;
    } else if (!options[option.key]) {
      options[option.key] = option.default;
    }
  }
  const choices = component.compute_choices || [];
  const preferredCompute =
    desired.compute ||
    component.inspection.resolved?.compute ||
    choices.find((choice) => choice.value === "auto")?.value ||
    choices.find((choice) => choice.available)?.value ||
    "cpu";
  state = {
    selected: false,
    expanded: false,
    compute: preferredCompute,
    quantization,
    options,
  };
  componentState.set(id, state);
  return state;
}

function selectedOptionValue(state, option) {
  return option.state_field === "quantization"
    ? state.quantization || option.default
    : state.options[option.key] || option.default;
}

function setSelectedOptionValue(state, option, value) {
  if (option.state_field === "quantization") {
    state.quantization = value;
  } else {
    state.options[option.key] = value;
  }
}

function requirementsMet(choice, values) {
  const requirements = choice.requires || {};
  return Object.entries(requirements).every(([key, allowed]) =>
    (allowed || []).includes(values[key]),
  );
}

function refreshOptionAvailability(component) {
  const nodes = componentNodes.get(component.definition.id);
  if (!nodes) return;
  const state = controlsFor(component);
  const definitions = component.definition.install_options || [];

  // Two passes are enough for the current shallow option dependencies and make
  // a CustomVoice selection immediately move Qwen to its valid 1.7B size.
  for (let pass = 0; pass < 2; pass += 1) {
    const values = Object.fromEntries(
      definitions.map((option) => [
        option.key,
        selectedOptionValue(state, option),
      ]),
    );
    for (const option of definitions) {
      const select = nodes.optionSelects.get(option.key);
      if (!select) continue;
      let currentAvailable = false;
      let firstAvailable = "";
      for (const choice of option.choices || []) {
        const optionNode = [...select.options].find(
          (candidate) => candidate.value === choice.value,
        );
        const available = requirementsMet(choice, values);
        if (optionNode) optionNode.disabled = !available;
        if (available && !firstAvailable) firstAvailable = choice.value;
        if (available && choice.value === select.value) {
          currentAvailable = true;
        }
      }
      if (!currentAvailable && firstAvailable) {
        select.value = firstAvailable;
        setSelectedOptionValue(state, option, firstAvailable);
      }
    }
  }
}

function selectionChanged(component, selected) {
  const state = controlsFor(component);
  const pandrator = snapshot.components.find(
    (item) => item.definition.id === "pandrator",
  );
  if (
    component.definition.id === "pandrator" &&
    !selected &&
    snapshot.components.some(
      (item) =>
        item.definition.id !== "pandrator" &&
        controlsFor(item).selected &&
        selectable(item),
    )
  ) {
    state.selected = true;
    showMessage(
      "Pandrator remains selected because optional engines need the application.",
    );
  } else {
    state.selected = selected;
  }
  if (
    selected &&
    component.definition.id !== "pandrator" &&
    pandrator &&
    selectable(pandrator)
  ) {
    controlsFor(pandrator).selected = true;
    updateComponentCard(pandrator);
  }
  updateComponentCard(component);
  updateSelectionSummary();
}

function capabilityLabels(definition) {
  const userFacingCapabilities = new Set([
    "voice_cloning",
    "prebuilt_voices",
    "voice_conversion",
    "custom_models",
    "transcription",
    "word_timestamps",
    "speaker_diarization",
    "voice_design",
    "emotion_steering",
  ]);
  return (definition.capabilities || [])
    .filter(
      (capability) =>
        capability.available && userFacingCapabilities.has(capability.id),
    )
    .map((capability) => capability.label);
}

function makeCapabilityLine(definition) {
  const labels = capabilityLabels(definition);
  return text(
    "p",
    labels.join(" · "),
    `engine-metadata${labels.length ? "" : " hidden"}`,
  );
}

function makeModelList(definition) {
  const root = document.createElement("div");
  root.className = "model-list";
  for (const item of definition.models || []) {
    const row = document.createElement("div");
    row.className = "model-row";
    row.append(text("strong", item.label));
    if (item.description) row.append(text("p", item.description));
    if (item.estimated_download_bytes) {
      row.append(
        text(
          "p",
          `Model download: about ${bytes(item.estimated_download_bytes)}`,
        ),
      );
    }
    if (item.license_name) {
      const copy = document.createElement("p");
      copy.append(document.createTextNode(`${item.license_name}. `));
      if (item.license_url) {
        const link = text("a", "Review terms");
        link.href = item.license_url;
        link.target = "_blank";
        link.rel = "noreferrer";
        copy.append(link);
      }
      row.append(copy);
    }
    if (item.usage_note) row.append(text("p", item.usage_note));
    root.append(row);
  }
  return root;
}

function buildComponentDetails(component, nodes) {
  if (nodes.detailsBuilt) return;
  const definition = component.definition;
  const state = controlsFor(component);
  const details = document.createElement("div");
  details.className = "engine-details";
  if (definition.guidance || definition.description) {
    details.append(
      text(
        "p",
        definition.guidance || definition.description,
        "guidance",
      ),
    );
  }

  if ((definition.languages || []).length) {
    const languageSection = document.createElement("section");
    languageSection.className = "engine-detail-section";
    languageSection.append(
      text("h4", "Languages"),
      text("p", definition.languages.join(", "), "language-copy"),
    );
    details.append(languageSection);
  }

  if ((definition.models || []).length) {
    const modelSection = document.createElement("section");
    modelSection.className = "engine-detail-section";
    modelSection.append(
      text("h4", "Models and licences"),
      makeModelList(definition),
    );
    details.append(modelSection);
  }

  const optionGrid = document.createElement("div");
  optionGrid.className = "option-grid";
  const optionSelects = new Map();
  const computeChoices = component.compute_choices || [];
  if (computeChoices.length > 1) {
    const label = document.createElement("label");
    label.append(document.createTextNode("Use this computer's"));
    const select = document.createElement("select");
    select.dataset.role = "compute";
    for (const choice of computeChoices) {
      const option = text("option", choice.label);
      option.value = choice.value;
      option.disabled = !choice.available;
      option.title = choice.reason || "";
      if (choice.value === state.compute) option.selected = true;
      select.append(option);
    }
    if (![...select.options].some((item) => item.selected && !item.disabled)) {
      const fallback = [...select.options].find((item) => !item.disabled);
      if (fallback) {
        fallback.selected = true;
        state.compute = fallback.value;
      }
    }
    select.addEventListener("change", () => {
      state.compute = select.value;
    });
    label.append(
      select,
      text(
        "small",
        "Automatic uses the best compatible runtime detected on this computer.",
      ),
    );
    optionGrid.append(label);
  }

  for (const optionDefinition of definition.install_options || []) {
    const label = document.createElement("label");
    label.append(document.createTextNode(optionDefinition.label));
    const select = document.createElement("select");
    select.dataset.optionKey = optionDefinition.key;
    const current = selectedOptionValue(state, optionDefinition);
    for (const choiceDefinition of optionDefinition.choices || []) {
      const option = text("option", choiceDefinition.label);
      option.value = choiceDefinition.value;
      option.title = choiceDefinition.description || "";
      option.selected = choiceDefinition.value === current;
      select.append(option);
    }
    select.addEventListener("change", () => {
      setSelectedOptionValue(state, optionDefinition, select.value);
      refreshOptionAvailability(component);
    });
    label.append(select);
    if (optionDefinition.description) {
      label.append(text("small", optionDefinition.description));
    }
    optionGrid.append(label);
    optionSelects.set(optionDefinition.key, select);
  }
  if (optionGrid.childElementCount) details.append(optionGrid);

  const installMeta = document.createElement("div");
  installMeta.className = "install-meta";
  const estimatePrefix =
    definition.size_provenance === "estimate" ? "Estimate" : "Size";
  installMeta.append(
    text(
      "span",
      `${estimatePrefix}: ${bytes(
        definition.estimated_download_bytes,
      )} download`,
    ),
    text(
      "span",
      `${bytes(definition.estimated_installed_bytes)} installed`,
    ),
  );
  if (definition.size_note) {
    installMeta.append(text("p", definition.size_note, "size-note"));
  }
  details.append(installMeta);

  const problem = text("div", "", "problem hidden");
  const unsupported = text(
    "div",
    "This engine is visible for migration and removal, but its manager installation recipe is still being qualified.",
    "unsupported-note hidden",
  );
  const actions = document.createElement("div");
  actions.className = "card-actions";
  details.append(problem, unsupported, actions);

  nodes.card.append(details);
  Object.assign(nodes, {
    details,
    problem,
    unsupported,
    actions,
    optionSelects,
    detailsBuilt: true,
  });
  refreshOptionAvailability(component);
  updateComponentCard(component);
}

function buildComponentCard(component) {
  const definition = component.definition;
  const state = controlsFor(component);
  const card = document.createElement("details");
  card.className = "engine-card";
  card.dataset.componentId = definition.id;
  card.open = state.expanded;

  const summary = document.createElement("summary");
  const selectLabel = document.createElement("label");
  selectLabel.className = "engine-select";
  selectLabel.title = `Select ${definition.label}`;
  selectLabel.addEventListener("click", (event) => event.stopPropagation());
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.setAttribute(
    "aria-label",
    `Select ${definition.label} for installation`,
  );
  checkbox.addEventListener("change", () =>
    selectionChanged(component, checkbox.checked),
  );
  selectLabel.append(checkbox);

  const summaryCopy = document.createElement("div");
  summaryCopy.className = "engine-summary-copy";
  const titleLine = document.createElement("div");
  titleLine.className = "engine-title-line";
  const status = text(
    "span",
    componentStateLabel(component.inspection.state),
    `engine-state ${component.inspection.state}`,
  );
  titleLine.append(text("span", definition.label, "engine-title"), status);
  summaryCopy.append(
    titleLine,
    text("p", definition.description || "", "engine-summary"),
    makeCapabilityLine(definition),
  );
  summary.append(
    selectLabel,
    summaryCopy,
    text("span", "⌄", "engine-chevron"),
  );
  card.append(summary);

  const nodes = {
    card,
    checkbox,
    status,
    problem: null,
    unsupported: null,
    actions: null,
    optionSelects: new Map(),
    detailsBuilt: false,
  };
  componentNodes.set(definition.id, nodes);
  card.addEventListener("toggle", () => {
    state.expanded = card.open;
    if (card.open) buildComponentDetails(component, nodes);
  });
  if (card.open) buildComponentDetails(component, nodes);
  return card;
}

function catalogueShape() {
  return JSON.stringify(
    snapshot.components
      .filter((component) => component.definition.id !== "pandrator")
      .map((component) => ({
        definition: component.definition,
        compute_choices: component.compute_choices,
      })),
  );
}

function renderCatalogue() {
  const root = byId("components");
  const catalogueComponents = snapshot.components.filter(
    (component) => component.definition.id !== "pandrator",
  );
  const signature = catalogueShape();
  if (signature === catalogueSignature && componentNodes.size) {
    for (const component of catalogueComponents) {
      updateComponentCard(component);
    }
    updateSelectionSummary();
    return;
  }
  if (
    catalogueSignature &&
    root.contains(document.activeElement) &&
    document.activeElement !== root
  ) {
    // Never replace an open/focused native select during polling.
    for (const component of snapshot.components) updateComponentCard(component);
    return;
  }

  catalogueSignature = signature;
  componentNodes.clear();
  clear(root);
  const groups = new Map();
  for (const component of catalogueComponents) {
    const section = component.definition.section || "core";
    if (!groups.has(section)) groups.set(section, []);
    groups.get(section).push(component);
    controlsFor(component);
  }

  if (!selectionInitialized) {
    const pandrator = snapshot.components.find(
      (component) => component.definition.id === "pandrator",
    );
    if (pandrator && selectable(pandrator)) {
      controlsFor(pandrator).selected = true;
    }
    selectionInitialized = true;
  }

  for (const [section, components] of groups.entries()) {
    const presentation = sectionPresentation[section] || {
      title: stateLabel(section),
      description: "",
    };
    const sectionNode = document.createElement("details");
    sectionNode.className = "component-section";
    sectionNode.dataset.section = section;
    sectionNode.open = sectionState.get(section) === true;
    sectionNode.addEventListener("toggle", () => {
      sectionState.set(section, sectionNode.open);
    });
    const summary = document.createElement("summary");
    const copy = document.createElement("div");
    copy.append(
      text("div", presentation.title, "section-title"),
      text("div", presentation.description, "section-description"),
    );
    summary.append(
      copy,
      text(
        "span",
        `${components.length} item${components.length === 1 ? "" : "s"}`,
        "section-count",
      ),
    );
    const grid = document.createElement("div");
    grid.className = "engine-grid";
    for (const component of components) {
      grid.append(buildComponentCard(component));
    }
    sectionNode.append(summary, grid);
    root.append(sectionNode);
  }

  if (!catalogueComponents.length) {
    root.append(text("p", "No speech engines are available.", "muted"));
  }
  for (const component of catalogueComponents) {
    updateComponentCard(component);
  }
  updateSelectionSummary();
}

function updateComponentCard(component) {
  const nodes = componentNodes.get(component.definition.id);
  if (!nodes) return;
  const state = controlsFor(component);
  const definition = component.definition;
  const inspection = component.inspection;
  const supported = new Set(definition.supported_actions || []);
  const canSelect = selectable(component) && !activeOperation;
  if (!selectable(component)) state.selected = false;

  nodes.card.classList.toggle("selected", state.selected && canSelect);
  nodes.checkbox.checked = state.selected && canSelect;
  nodes.checkbox.disabled = !canSelect;
  nodes.checkbox.parentElement.classList.toggle("hidden", !selectable(component));
  nodes.status.textContent = componentStateLabel(inspection.state);
  nodes.status.className = `engine-state ${inspection.state}`;
  if (!nodes.detailsBuilt) return;
  nodes.problem.textContent = (inspection.problems || []).join(" ");
  nodes.problem.classList.toggle("hidden", !(inspection.problems || []).length);
  nodes.unsupported.classList.toggle(
    "hidden",
    !(
      ["absent", "unknown"].includes(inspection.state) &&
      !supported.has("install")
    ),
  );

  clear(nodes.actions);
  const service = serviceFor(component);
  if (canSelect) {
    nodes.actions.append(
      makeButton(
        inspection.state === "degraded"
          ? "Select for repair"
          : "Select for installation",
        () => selectionChanged(component, true),
        "button secondary",
      ),
    );
  }
  if (inspection.state === "degraded" && supported.has("repair")) {
    nodes.actions.append(
      makeButton("Review repair", () => planComponent(component, "repair")),
    );
  }
  if (inspection.state === "present" && supported.has("update")) {
    nodes.actions.append(
      makeButton("Review update", () => planComponent(component, "update")),
    );
  }
  if (inspection.state === "present" && supported.has("repair")) {
    nodes.actions.append(
      makeButton(
        "Repair installation",
        () => planComponent(component, "repair"),
        "button secondary",
      ),
    );
  }
  if (
    definition.id !== "pandrator" &&
    inspection.state === "present" &&
    definition.service_key &&
    supported.has("start") &&
    !service?.process
  ) {
    nodes.actions.append(
      makeButton(
        "Start engine",
        () => runtime(definition.service_key, "start"),
        "button secondary",
      ),
    );
  }
  if (
    definition.id !== "pandrator" &&
    service?.process &&
    supported.has("stop")
  ) {
    nodes.actions.append(
      makeButton(
        "Stop engine",
        () => runtime(definition.service_key, "stop"),
        "button secondary",
      ),
    );
  }
  if (inspection.state === "present" && supported.has("remove")) {
    nodes.actions.append(
      makeButton(
        "Remove",
        () => planComponent(component, "remove"),
        "button secondary danger",
      ),
    );
  }
  for (const control of nodes.card.querySelectorAll("select")) {
    control.disabled = Boolean(activeOperation);
  }
}

function updateSelectionSummary() {
  const selected = snapshot.components.filter(
    (component) => selectable(component) && controlsFor(component).selected,
  );
  const bar = byId("selection-bar");
  bar.classList.toggle("hidden", !selected.length);
  const includesPandrator = selected.some(
    (component) => component.definition.id === "pandrator",
  );
  const engineCount = selected.length - (includesPandrator ? 1 : 0);
  let summary = "Nothing selected";
  if (includesPandrator && engineCount) {
    summary = `Pandrator + ${engineCount} optional engine${
      engineCount === 1 ? "" : "s"
    }`;
  } else if (includesPandrator) {
    summary = "Pandrator will be installed";
  } else if (engineCount) {
    summary = `${engineCount} optional engine${
      engineCount === 1 ? "" : "s"
    } selected`;
  }
  byId("selection-count").textContent = summary;
  const review = byId("review-selection");
  review.disabled = !selected.length || Boolean(activeOperation);
  review.textContent = "Review installation";
}

function pandratorComponent() {
  return snapshot.components.find(
    (component) => component.definition.id === "pandrator",
  );
}

function renderApplicationMaintenance() {
  const component = pandratorComponent();
  const detail = byId("application-maintenance-detail");
  const actions = byId("application-maintenance-actions");
  clear(actions);
  if (!component) {
    detail.textContent = "Checking the Pandrator installation…";
    return;
  }

  const state = component.inspection.state;
  const supported = new Set(component.definition.supported_actions || []);
  if (["absent", "unknown"].includes(state)) {
    detail.textContent =
      "Pandrator is not installed yet. Start from Install & launch.";
    actions.append(
      makeButton(
        "Go to installation",
        () => activateManagerTab("install", { focus: true }),
        "button secondary",
      ),
    );
  } else if (state === "degraded") {
    detail.textContent =
      "The application installation is incomplete. Repair leaves projects and generated media untouched.";
    if (supported.has("repair")) {
      actions.append(
        makeButton(
          "Review repair",
          () => planComponent(component, "repair"),
          "button primary",
        ),
      );
    }
  } else {
    detail.textContent =
      "Pandrator is installed. Review an update or verify and repair its managed files.";
    if (supported.has("update")) {
      actions.append(
        makeButton(
          "Review update",
          () => planComponent(component, "update"),
          "button primary",
        ),
      );
    }
    if (supported.has("repair")) {
      actions.append(
        makeButton(
          "Repair installation",
          () => planComponent(component, "repair"),
          "button secondary",
        ),
      );
    }
  }
  for (const button of actions.querySelectorAll("button")) {
    button.disabled = Boolean(activeOperation);
  }
}

function setApplicationState(label, state = "") {
  const node = byId("application-state");
  node.textContent = label;
  node.className = `application-state${state ? ` ${state}` : ""}`;
}

function renderApplication() {
  const current = snapshot.application;
  const primary = byId("application-primary");
  const more = byId("application-more");
  const problem = byId("application-problem");
  renderApplicationMaintenance();
  primary.classList.toggle("busy", applicationBusy);
  primary.disabled = applicationBusy || !current || Boolean(activeOperation);
  problem.classList.add("hidden");
  problem.textContent = "";

  if (!current) {
    setApplicationState("Checking…");
    byId("application-detail").textContent =
      "The manager is inspecting the application.";
    primary.textContent = "Please wait…";
    more.classList.add("hidden");
    return;
  }
  if (!current.installed) {
    setApplicationState("Not installed", "absent");
    byId("application-detail").textContent =
      "Required for optional speech engines · approximately 650 MB to download.";
    primary.textContent = "Review installation";
    primary.dataset.action = "install";
    more.classList.add("hidden");
    return;
  }
  if (current.component_state === "degraded") {
    setApplicationState("Needs repair", "degraded");
    byId("application-detail").textContent =
      "The application installation is incomplete. Your projects remain separate.";
    primary.textContent = "Review repair";
    primary.dataset.action = "repair";
    more.classList.add("hidden");
    problem.textContent = "Repair the application before trying to start it.";
    problem.classList.remove("hidden");
    return;
  }
  if (current.running && current.healthy) {
    setApplicationState("Installed and running", "present");
    byId("application-detail").textContent =
      "Your browser workspace is ready.";
    primary.textContent = applicationBusy ? "Opening…" : "Open Pandrator";
    primary.dataset.action = "launch";
    more.classList.remove("hidden");
    return;
  }
  if (current.running) {
    setApplicationState("Starting", "degraded");
    byId("application-detail").textContent =
      "The manager will finish starting the application and open it when healthy.";
    primary.textContent = applicationBusy ? "Starting…" : "Finish starting";
    primary.dataset.action = "launch";
    more.classList.remove("hidden");
    return;
  }
  setApplicationState("Installed", "present");
  byId("application-detail").textContent =
    "The application is stopped.";
  primary.textContent = applicationBusy ? "Starting…" : "Start Pandrator";
  primary.dataset.action = "launch";
  more.classList.add("hidden");
}

function updateNetworkFields() {
  const mode = byId("network-mode").value;
  const remote = mode !== "local";
  const proxy = mode === "https_proxy";
  for (const node of document.querySelectorAll(".network-remote-field")) {
    node.classList.toggle("hidden", !remote);
  }
  for (const node of document.querySelectorAll(".network-password-field")) {
    node.classList.toggle("hidden", !remote);
  }
  for (const node of document.querySelectorAll(".network-proxy-field")) {
    node.classList.toggle("hidden", !proxy);
  }
  const warning = byId("network-warning");
  warning.classList.toggle("hidden", !remote);
  if (mode === "private_network") {
    byId("network-mode-help").textContent =
      "Convenient on a trusted LAN or private VPN; traffic is not encrypted.";
    warning.textContent =
      "Private-network mode uses HTTP. Use it only on a network you trust; use the HTTPS option for public cloud addresses.";
    if (byId("network-bind-host").value === "127.0.0.1") {
      byId("network-bind-host").value = "0.0.0.0";
    }
    const candidates =
      snapshot.network?.application?.private_network_candidates || [];
    if (!byId("network-public-url").value.trim() && candidates.length) {
      byId("network-public-url").value = candidates[0].url;
    }
    byId("network-public-url-help").textContent = candidates.length
      ? "A private address detected on this computer is filled in for you. Change it only if you use another LAN hostname."
      : "No private IPv4 address was detected. Enter the LAN hostname or IP address you use from another device.";
  } else if (proxy) {
    byId("network-mode-help").textContent =
      "Recommended for pods, rented GPU machines, and internet-facing servers.";
    warning.textContent =
      "Only configure the number of reverse proxies you operate. The public URL must already terminate HTTPS.";
    byId("network-public-url-help").textContent =
      "Enter the exact HTTPS origin configured in your proxy or ingress.";
  } else {
    byId("network-mode-help").textContent =
      "Pandrator listens only on this computer.";
    byId("network-bind-host").value = "127.0.0.1";
    byId("network-public-url-help").textContent =
      "Pandrator is not exposed outside this computer.";
  }
  const application = snapshot.network?.application;
  if (application?.owner_authentication_initialized) {
    byId("network-password-help").textContent =
      "Leave blank to keep the current owner password.";
  } else if (mode === "private_network") {
    byId("network-password-help").textContent =
      "For safety, initialize the owner password on the server or use HTTPS; a password cannot be submitted over remote plain HTTP.";
  } else {
    byId("network-password-help").textContent =
      "Required before first network exposure; it is never saved by the manager.";
  }
  const installed = Boolean(snapshot.application?.installed);
  const save = byId("save-network");
  save.disabled =
    networkBusy ||
    Boolean(activeOperation) ||
    (remote && !installed);
  save.classList.toggle("busy", networkBusy);
  save.textContent = networkBusy ? "Saving…" : "Save access settings";
}

function renderNetwork() {
  const current = snapshot.network;
  if (!current) return;
  const application = current.application;
  const manager = current.manager;
  const accessLabel = application.remote_enabled
    ? application.mode === "https_proxy"
      ? "Available through HTTPS"
      : "Available on private network"
    : "This device only";
  byId("network-summary").textContent = accessLabel;
  byId("network-summary").className = `access-value${
    application.remote_enabled ? " remote" : ""
  }`;
  byId("network-maintenance-summary").textContent = accessLabel;
  byId("network-browser-url").textContent = application.remote_enabled
    ? `· ${application.browser_url}`
    : "";
  byId("manager-network-detail").textContent = manager.remote_enabled
    ? `Setup manager remote link: ${manager.browser_url}`
    : "The setup manager is currently available only on the server itself.";

  const editing =
    byId("network-details").contains(document.activeElement) || networkDirty;
  if (!networkInitialized || !editing) {
    byId("network-mode").value = application.mode;
    networkPreviousMode = application.mode;
    byId("network-port").value = String(application.port || 8097);
    byId("network-public-url").value = application.public_url || "";
    byId("network-bind-host").value = application.bind_host || "127.0.0.1";
    byId("network-proxy-hops").value = String(
      Math.max(1, Number(application.proxy_hops || 1)),
    );
    byId("network-trusted-hosts").value = (
      application.trusted_hosts || []
    ).join(", ");
    networkInitialized = true;
  }
  updateNetworkFields();
}

async function saveNetwork() {
  if (networkBusy) return;
  const mode = byId("network-mode").value;
  const remote = mode !== "local";
  const password = byId("network-owner-password").value;
  const confirmation = byId("network-owner-confirm").value;
  if (password !== confirmation) {
    showMessage("The owner password confirmation does not match.", true);
    return;
  }
  if (password && password.length < 10) {
    showMessage("The owner password must contain at least 10 characters.", true);
    return;
  }
  const publicUrl = byId("network-public-url").value.trim().replace(/\/+$/, "");
  if (remote) {
    const requiredScheme = mode === "https_proxy" ? "https://" : "http://";
    if (!publicUrl.toLowerCase().startsWith(requiredScheme)) {
      showMessage(
        `${stateLabel(mode)} access requires an address beginning with ${requiredScheme}`,
        true,
      );
      return;
    }
  }
  const port = Number(byId("network-port").value);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    showMessage("Choose a Pandrator port between 1 and 65535.", true);
    return;
  }
  networkBusy = true;
  updateNetworkFields();
  try {
    const result = await requestJson("/v1/network/application", {
      method: "PUT",
      body: JSON.stringify({
        exposure: {
          mode,
          bind_host: remote
            ? byId("network-bind-host").value
            : "127.0.0.1",
          port,
          public_url: remote ? publicUrl : null,
          trusted_hosts: byId("network-trusted-hosts")
            .value.split(",")
            .map((value) => value.trim())
            .filter(Boolean),
          proxy_hops:
            mode === "https_proxy"
              ? Number(byId("network-proxy-hops").value)
              : 0,
          allow_insecure_remote: mode === "private_network",
        },
        owner_password: password || null,
        replace_owner_password: Boolean(
          password && snapshot.network?.application?.owner_authentication_initialized,
        ),
        restart_if_running: true,
      }),
    });
    snapshot.network = result;
    byId("network-owner-password").value = "";
    byId("network-owner-confirm").value = "";
    networkDirty = false;
    await refresh();
    showMessage(
      remote
        ? `Pandrator access is configured at ${result.application.browser_url}.`
        : "Pandrator is restricted to this device.",
    );
  } catch (error) {
    showMessage(error.message, true);
  } finally {
    networkBusy = false;
    renderNetwork();
  }
}

function renderServices() {
  const root = byId("services");
  clear(root);
  for (const service of snapshot.services) {
    const row = document.createElement("div");
    row.className = "service-row";
    const copy = document.createElement("div");
    copy.append(
      text("strong", service.id),
      text(
        "div",
        `${stateLabel(service.health?.state)}${service.process?.pid ? ` · PID ${service.process.pid}` : ""}`,
        "meta",
      ),
    );
    row.append(copy, text("span", service.endpoint || "No endpoint"));
    root.append(row);
  }
  if (!snapshot.services.length) {
    root.append(text("p", "No managed services are registered.", "muted"));
  }
}

function activityLabel(event) {
  const action = event.payload?.action;
  const service = event.service_id || event.payload?.service_id;
  const labels = {
    "application.action_requested": `${stateLabel(action)} Pandrator requested`,
    "application.started": "Pandrator started",
    "application.stopped": "Pandrator stopped",
    "application.restarted": "Pandrator restarted",
    "application.launch_ready": "Pandrator browser sign-in prepared",
    "application.network_updated": "Pandrator network access updated",
    "application.action_failed": `${stateLabel(action)} Pandrator failed`,
    "runtime.action_requested": `${stateLabel(action)} ${service || "service"} requested`,
    "runtime.action_completed": `${stateLabel(action)} ${service || "service"} completed`,
    "runtime.action_failed": `${stateLabel(action)} ${service || "service"} failed`,
  };
  return labels[event.event_type] || stateLabel(event.event_type);
}

function renderActivity() {
  const root = byId("activity");
  clear(root);
  const events = snapshot.activity
    .filter(
      (event) =>
        event.event_type.startsWith("application.") ||
        event.event_type.startsWith("runtime.action"),
    )
    .map((event) => ({
      label: activityLabel(event),
      created_at: event.created_at,
      error: event.payload?.error || "",
    }));
  const operations = snapshot.operations.map((operation) => ({
    label: `${stateLabel(operation.kind)} · ${stateLabel(operation.state)} · ${Math.round(operation.progress * 100)}%`,
    created_at: operation.created_at,
    error: operation.error?.message || "",
  }));
  const combined = [...events, ...operations]
    .sort(
      (first, second) =>
        new Date(second.created_at || 0) - new Date(first.created_at || 0),
    )
    .slice(0, 14);
  for (const item of combined) {
    const row = document.createElement("div");
    row.className = "activity-row";
    const copy = document.createElement("div");
    copy.append(text("div", item.label));
    if (item.error) copy.append(text("div", item.error, "problem"));
    const timestamp = document.createElement("time");
    timestamp.dateTime = item.created_at || "";
    timestamp.textContent = item.created_at
      ? new Date(item.created_at).toLocaleString()
      : "";
    row.append(copy, timestamp);
    root.append(row);
  }
  if (!combined.length) {
    root.append(text("p", "No activity yet.", "muted"));
  }
}

function renderActiveOperation() {
  const panel = byId("active-operation");
  if (!activeOperation || terminalStates.has(activeOperation.state)) {
    panel.classList.add("hidden");
    document.body.classList.remove("operation-running");
    return;
  }
  panel.classList.remove("hidden");
  document.body.classList.add("operation-running");
  byId("operation-title").textContent = stateLabel(activeOperation.kind);
  byId("operation-detail").textContent =
    `${stateLabel(activeOperation.state)}${activeOperation.current_task_id ? ` · ${stateLabel(activeOperation.current_task_id)}` : ""}`;
  byId("operation-progress").value = Number(activeOperation.progress || 0);
}

function focusActiveOperation() {
  renderApplication();
  renderCatalogue();
  renderNetwork();
  renderActiveOperation();
  const panel = byId("active-operation");
  window.requestAnimationFrame(() => {
    panel.scrollIntoView({ behavior: "smooth", block: "center" });
    panel.focus({ preventScroll: true });
  });
}

async function finishPendingInstall() {
  if (!pendingInstallOperationId || finishingInstall) return;
  const operation = snapshot.operations.find(
    (item) => item.id === pendingInstallOperationId,
  );
  if (!operation || !terminalStates.has(operation.state)) return;
  if (operation.state !== "succeeded") {
    pendingInstallOperationId = "";
    pendingPostInstallAccess = null;
    showMessage(
      `Installation ${stateLabel(operation.state)}. Review the activity details before trying again.`,
      true,
    );
    return;
  }
  finishingInstall = true;
  const access = pendingPostInstallAccess;
  pendingInstallOperationId = "";
  pendingPostInstallAccess = null;
  let accessWarning = "";
  try {
    if (access) {
      try {
        await requestJson("/v1/network/application", {
          method: "PUT",
          body: JSON.stringify(access),
        });
      } catch (error) {
        accessWarning = ` Access settings need attention: ${error.message}`;
      }
    }
    const result = await requestJson("/v1/application/launch", {
      method: "POST",
      body: JSON.stringify({}),
    });
    if (result.launch_url) {
      window.location.assign(result.launch_url);
      return;
    }
    showMessage(`Pandrator is installed and running.${accessWarning}`);
  } catch (error) {
    showMessage(
      `Pandrator is installed, but could not be opened automatically: ${error.message}${accessWarning}`,
      true,
    );
  } finally {
    finishingInstall = false;
  }
}

function renderReleaseStatus() {
  const root = byId("release-status");
  clear(root);
  const current = snapshot.releases?.current || {};
  const labels = Object.values(current)
    .map(
      (release) =>
        `${release.product || "product"} ${release.version || "unknown version"}`,
    )
    .join(" · ");
  root.append(
    text(
      "p",
      labels || "No signed release has been accepted in this workspace.",
      "meta",
    ),
  );
}

async function refresh() {
  if (refreshInFlight) return;
  refreshInFlight = true;
  try {
    const [
      status,
      application,
      network,
      components,
      services,
      operations,
      activity,
      releases,
    ] = await Promise.all([
      requestJson("/v1/status"),
      requestJson("/v1/application"),
      requestJson("/v1/network"),
      requestJson("/v1/components"),
      requestJson("/v1/services"),
      requestJson("/v1/operations"),
      requestJson("/v1/activity?limit=80"),
      requestJson("/v1/releases"),
    ]);
    snapshot = {
      status,
      application,
      network,
      components: components.items || [],
      services: services.items || [],
      operations: operations.items || [],
      activity: activity.items || [],
      releases,
    };
    activeOperation =
      snapshot.operations.find(
        (operation) => !terminalStates.has(operation.state),
      ) || null;
    byId("status").textContent = JSON.stringify(status, null, 2);
    setManagerHealth("ready", "Manager ready");
    renderApplication();
    renderNetwork();
    renderCatalogue();
    renderServices();
    renderActivity();
    renderReleaseStatus();
    renderActiveOperation();
    await finishPendingInstall();
  } catch (error) {
    showMessage(error.message, true);
    if (error.code === "authentication_required") {
      pollingStopped = true;
      if (refreshTimer) window.clearTimeout(refreshTimer);
      setManagerHealth("error", "Browser authorization expired");
    } else {
      setManagerHealth("error", "Manager unavailable");
    }
  } finally {
    refreshInFlight = false;
  }
}

async function poll() {
  if (pollingStopped) return;
  await refresh();
  if (!pollingStopped) {
    refreshTimer = window.setTimeout(poll, 2500);
  }
}

function desiredFor(component, present = true) {
  const state = controlsFor(component);
  return {
    present,
    compute: state.compute || component.desired?.compute || "auto",
    quantization: state.quantization || null,
    options: {
      ...(component.desired?.options || {}),
      ...state.options,
      start_after_install: present,
    },
  };
}

async function createPlan(kind, desired, title) {
  try {
    const operationPlan = await requestJson("/v1/plans", {
      method: "POST",
      body: JSON.stringify({
        kind,
        desired,
        expected_revision: snapshot.status.configuration_revision,
      }),
    });
    showPlan(operationPlan, title);
  } catch (error) {
    showMessage(error.message, true);
  }
}

async function planSelection() {
  const selected = snapshot.components.filter(
    (component) => selectable(component) && controlsFor(component).selected,
  );
  if (!selected.length) {
    showMessage("Select at least one item to install.", true);
    return;
  }
  const desired = Object.fromEntries(
    selected.map((component) => [
      component.definition.id,
      desiredFor(component, true),
    ]),
  );
  await createPlan(
    "install",
    desired,
    selected.length > 1
      ? `Install ${selected.length} selected items`
      : `Install ${selected[0].definition.label}`,
  );
}

async function planComponent(component, kind) {
  await createPlan(
    kind,
    {
      [component.definition.id]: desiredFor(component, kind !== "remove"),
    },
    `${stateLabel(kind)} ${component.definition.label}`,
  );
}

function selectedPlanAccessMode() {
  return (
    document.querySelector('input[name="plan-access-mode"]:checked')?.value ||
    "local"
  );
}

function updatePlanAccessFields() {
  const mode = selectedPlanAccessMode();
  byId("plan-lan-fields").classList.toggle(
    "hidden",
    mode !== "private_network",
  );
  byId("plan-https-fields").classList.toggle(
    "hidden",
    mode !== "https_proxy",
  );
  byId("plan-password-fields").classList.toggle(
    "hidden",
    mode === "local",
  );
}

function preparePlanAccess() {
  const root = byId("plan-access");
  const includesPandrator =
    selectedPlan?.kind === "install" &&
    Boolean(selectedPlan.desired?.pandrator?.present);
  root.classList.toggle("hidden", !includesPandrator);
  if (!includesPandrator) return;

  const application = snapshot.network?.application || {};
  const currentMode = application.mode || "local";
  const selected =
    document.querySelector(
      `input[name="plan-access-mode"][value="${currentMode}"]`,
    ) ||
    document.querySelector('input[name="plan-access-mode"][value="local"]');
  selected.checked = true;

  const candidates = application.private_network_candidates || [];
  const candidateSelect = byId("plan-lan-candidate");
  clear(candidateSelect);
  if (candidates.length) {
    for (const candidate of candidates) {
      const option = document.createElement("option");
      option.value = candidate.url;
      option.textContent = `${candidate.url} · ${candidate.interface}`;
      candidateSelect.append(option);
    }
  } else {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No private IPv4 address detected";
    candidateSelect.append(option);
  }
  byId("plan-lan-url").value =
    currentMode === "private_network" ? application.public_url || "" : "";
  byId("plan-https-url").value =
    currentMode === "https_proxy" ? application.public_url || "" : "";
  byId("plan-owner-password").value = "";
  byId("plan-owner-confirm").value = "";
  updatePlanAccessFields();
}

function collectPlanAccess() {
  if (byId("plan-access").classList.contains("hidden")) return null;
  const mode = selectedPlanAccessMode();
  const application = snapshot.network?.application || {};
  const port = Number(application.port || 8097);
  const password = byId("plan-owner-password").value;
  const confirmation = byId("plan-owner-confirm").value;
  if (password !== confirmation) {
    throw new Error("The owner password confirmation does not match.");
  }
  if (password && password.length < 10) {
    throw new Error("The owner password must contain at least 10 characters.");
  }
  if (
    mode !== "local" &&
    !application.owner_authentication_initialized &&
    !password
  ) {
    throw new Error(
      "Choose an owner password before making Pandrator available to other devices.",
    );
  }

  let publicUrl = null;
  if (mode === "private_network") {
    publicUrl =
      byId("plan-lan-url").value.trim() ||
      byId("plan-lan-candidate").value.trim();
    if (!publicUrl.toLowerCase().startsWith("http://")) {
      throw new Error(
        "Choose a detected LAN address or enter one beginning with http://.",
      );
    }
  } else if (mode === "https_proxy") {
    publicUrl = byId("plan-https-url").value.trim().replace(/\/+$/, "");
    if (!publicUrl.toLowerCase().startsWith("https://")) {
      throw new Error("Enter the HTTPS address provided by your proxy or ingress.");
    }
  }
  return {
    exposure: {
      mode,
      bind_host:
        mode === "private_network"
          ? "0.0.0.0"
          : mode === "https_proxy"
            ? "127.0.0.1"
            : "127.0.0.1",
      port,
      public_url: publicUrl,
      trusted_hosts: [],
      proxy_hops: mode === "https_proxy" ? 1 : 0,
      allow_insecure_remote: mode === "private_network",
    },
    owner_password: password || null,
    replace_owner_password: Boolean(
      password && application.owner_authentication_initialized,
    ),
    restart_if_running: true,
  };
}

function showPlan(plan, title = "") {
  selectedPlan = plan;
  selectedPlanTitle = title || stateLabel(plan.kind);
  byId("plan-title").textContent = selectedPlanTitle;
  preparePlanAccess();
  const summary = byId("plan-summary");
  clear(summary);
  summary.append(
    text(
      "div",
      `Download: about ${bytes(selectedPlan.estimated_download_bytes)}`,
    ),
    text("div", `Disk: about ${bytes(selectedPlan.estimated_disk_bytes)}`),
  );
  const impacts = selectedPlan.impacts || {};
  if (impacts.release) {
    summary.append(
      text(
        "div",
        `${impacts.release.product} ${impacts.release.version} · sequence ${impacts.release.sequence}`,
      ),
    );
  }
  if (impacts.uninstall) {
    summary.append(
      text(
        "div",
        impacts.uninstall.purge_data
          ? "User data: permanently purge"
          : "User data: preserve",
      ),
    );
    if (impacts.uninstall.export_data) {
      summary.append(text("div", `Export: ${impacts.uninstall.export_data}`));
    }
  }

  const tasks = byId("plan-tasks");
  clear(tasks);
  for (const task of selectedPlan.tasks || []) {
    tasks.append(text("li", task.label));
  }
  if (!(selectedPlan.tasks || []).length) {
    tasks.append(text("li", "No changes are required."));
  }

  const confirmations = byId("plan-confirmations");
  clear(confirmations);
  const notices = new Set();
  for (const check of selectedPlan.preflight || []) {
    if (check.status === "pass") continue;
    if (check.status === "error") {
      confirmations.append(
        text("div", `Needs attention · ${check.message}`, "problem"),
      );
    } else if (
      !String(check.message).includes("signed, digest-pinned artifact")
    ) {
      notices.add(String(check.message));
    }
  }
  for (const confirmation of selectedPlan.confirmations || []) {
    const row = document.createElement("div");
    row.append(text("div", confirmation.message));
    if (confirmation.url) {
      const link = text("a", "Review licence");
      link.href = confirmation.url;
      link.target = "_blank";
      link.rel = "noreferrer";
      row.append(link);
    }
    confirmations.append(row);
  }
  for (const warning of selectedPlan.warnings || []) {
    const message = String(warning);
    if (!message.includes("signed, digest-pinned artifact")) {
      notices.add(message);
    }
  }
  if (notices.size) {
    const group = document.createElement("details");
    group.className = "plan-notices";
    group.append(
      text(
        "summary",
        `${notices.size} installation notice${notices.size === 1 ? "" : "s"}`,
      ),
    );
    const list = document.createElement("ul");
    for (const notice of notices) list.append(text("li", notice));
    group.append(list);
    confirmations.append(group);
  }
  byId("confirm-plan").disabled = !(selectedPlan.tasks || []).length;
  byId("plan-dialog").showModal();
}

async function executePlan() {
  if (!selectedPlan) return;
  let postInstallAccess = null;
  try {
    postInstallAccess = collectPlanAccess();
  } catch (error) {
    showMessage(error.message, true);
    return;
  }
  const confirm = byId("confirm-plan");
  confirm.disabled = true;
  confirm.classList.add("busy");
  try {
    activeOperation = await requestJson("/v1/operations", {
      method: "POST",
      body: JSON.stringify({
        plan_id: selectedPlan.id,
        plan_digest: selectedPlan.digest,
        accepted_confirmations: (selectedPlan.confirmations || []).map(
          (confirmation) => confirmation.key,
        ),
      }),
    });
    const kind = activeOperation.kind;
    if (postInstallAccess) {
      pendingInstallOperationId = activeOperation.id;
      pendingPostInstallAccess = postInstallAccess;
    }
    selectedPlan = null;
    selectedPlanTitle = "";
    byId("plan-dialog").close();
    focusActiveOperation();
    if (kind === "uninstall") {
      pollingStopped = true;
      if (refreshTimer !== null) window.clearTimeout(refreshTimer);
      refreshTimer = null;
    }
    showMessage(
      kind === "uninstall"
        ? "Uninstall accepted. The manager will close after handing cleanup to its external helper."
        : "Plan accepted. You may close this page; the manager records progress durably.",
    );
  } catch (error) {
    showMessage(error.message, true);
  } finally {
    confirm.classList.remove("busy");
    confirm.disabled = false;
  }
}

function closePlan() {
  selectedPlan = null;
  selectedPlanTitle = "";
  byId("plan-owner-password").value = "";
  byId("plan-owner-confirm").value = "";
  byId("plan-dialog").close();
}

async function applicationPrimary() {
  const action = byId("application-primary").dataset.action;
  if (action === "install") {
    const component = pandratorComponent();
    if (component) {
      controlsFor(component).selected = true;
      updateSelectionSummary();
      await planSelection();
    }
    return;
  }
  if (action === "repair") {
    const component = pandratorComponent();
    if (component) await planComponent(component, "repair");
    return;
  }
  if (action === "launch") {
    await applicationAction("launch");
  }
}

async function applicationAction(action) {
  if (applicationBusy) return;
  applicationBusy = true;
  renderApplication();
  try {
    const result = await requestJson(`/v1/application/${action}`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    if (action === "launch" && result.launch_url) {
      // Same-tab navigation is reliable even when popup blockers reject a
      // window opened after an asynchronous start.
      window.location.assign(result.launch_url);
      return;
    }
    await refresh();
    showMessage(
      action === "stop"
        ? "Pandrator stopped. The manager remains available."
        : `Pandrator ${action} completed.`,
    );
  } catch (error) {
    const repairHint =
      error.code === "application_runtime_missing"
        ? " Review a Pandrator repair below to create its private runtime."
        : "";
    showMessage(`${error.message}${repairHint}`, true);
    await refresh();
  } finally {
    applicationBusy = false;
    renderApplication();
  }
}

async function runtime(serviceId, action) {
  try {
    await requestJson(`/v1/runtime/${action}`, {
      method: "POST",
      body: JSON.stringify({ service_ids: [serviceId] }),
    });
    await refresh();
    showMessage(`${stateLabel(action)} ${serviceId} completed.`);
  } catch (error) {
    showMessage(error.message, true);
  }
}

async function cancelOperation() {
  if (!activeOperation) return;
  try {
    await requestJson(
      `/v1/operations/${encodeURIComponent(activeOperation.id)}/cancel`,
      {
        method: "POST",
        body: JSON.stringify({}),
      },
    );
    showMessage("Cancellation requested; waiting for a safe boundary.");
  } catch (error) {
    showMessage(error.message, true);
  }
}

function renderDoctor(report) {
  const root = byId("doctor-summary");
  clear(root);
  root.classList.remove("muted");
  root.append(
    text(
      "p",
      report.healthy
        ? "No errors were found."
        : `${report.summary?.error || 0} error(s) and ${report.summary?.warning || 0} warning(s) found.`,
    ),
  );
  const actionable = (report.checks || []).filter(
    (check) => check.status !== "pass",
  );
  for (const check of actionable) {
    const row = document.createElement("div");
    row.className = `diagnostic-row ${check.status}`;
    const copy = document.createElement("div");
    copy.append(
      text("strong", `${check.status.toUpperCase()} · ${check.id}`),
      text("p", check.message, "meta"),
    );
    row.append(copy);
    if (
      check.repairable &&
      String(check.repair_target || "").startsWith("component:")
    ) {
      const componentId = check.repair_target.slice("component:".length);
      const component = snapshot.components.find(
        (item) => item.definition.id === componentId,
      );
      if (
        component &&
        component.definition.supported_actions.includes("repair")
      ) {
        row.append(
          makeButton(
            "Review repair",
            () => planComponent(component, "repair"),
            "button secondary",
          ),
        );
      }
    }
    row.append(text("pre", JSON.stringify(check.details || {}, null, 2)));
    root.append(row);
  }
  if (!actionable.length) {
    root.append(text("p", "Every reported check passed.", "muted"));
  }
}

async function runDoctor() {
  try {
    const report = await requestJson("/v1/doctor");
    renderDoctor(report);
    showMessage(
      report.healthy
        ? "Diagnostics completed without errors."
        : "Diagnostics found issues. Review the details and repair targets.",
      !report.healthy,
    );
  } catch (error) {
    showMessage(error.message, true);
  }
}

function renderLegacy(payload) {
  const root = byId("legacy-status");
  clear(root);
  const report = payload?.report;
  if (!payload?.available || !report) {
    root.append(text("p", "No existing installer configuration was found.", "muted"));
    return;
  }
  root.append(
    text(
      "p",
      report.valid
        ? `${report.positively_identified?.length || 0} component(s) positively identified`
        : "The configuration is malformed and can only be quarantined",
    ),
  );
  const data = report.legacy_data || {};
  if (!data.error) {
    root.append(
      text(
        "p",
        `Known mutable data: ${data.file_count || 0} file(s), ${bytes(data.size_bytes || 0)}. Sources remain untouched.`,
        "meta",
      ),
    );
  }
  const identified = report.positively_identified || [];
  if (report.valid && identified.length) {
    const review = document.createElement("div");
    review.className = "legacy-review";
    review.append(text("h4", "Detected components"));
    const list = document.createElement("ul");
    list.className = "legacy-component-list";
    for (const componentId of identified) {
      const component = snapshot.components.find(
        (item) => item.definition?.id === componentId,
      );
      const desired = report.desired?.[componentId] || {};
      const details = [];
      if (desired.compute) details.push(stateLabel(desired.compute).toUpperCase());
      if (desired.quantization) details.push(String(desired.quantization).toUpperCase());
      if (desired.options?.model_size) {
        details.push(String(desired.options.model_size).toUpperCase());
      }
      if (Array.isArray(desired.options?.models) && desired.options.models.length) {
        details.push(desired.options.models.join(", "));
      }
      if (desired.options?.engine) details.push(stateLabel(desired.options.engine));
      const item = document.createElement("li");
      item.append(
        text(
          "strong",
          component?.definition?.label || stateLabel(componentId),
        ),
      );
      if (details.length) item.append(text("span", details.join(" · "), "meta"));
      list.append(item);
    }
    review.append(list);
    root.append(review);
  }
  for (const warning of report.warnings || []) {
    root.append(text("p", warning, "problem"));
  }
  const unknown = report.unknown_paths || [];
  if (unknown.length) {
    const details = document.createElement("details");
    details.className = "legacy-unknown";
    const summary = text(
      "summary",
      `${unknown.length} unrecognized path(s) will be left untouched`,
    );
    details.append(summary);
    const list = document.createElement("ul");
    for (const path of unknown) list.append(text("li", path));
    details.append(list);
    root.append(details);
  }
  if (report.already_imported) {
    root.append(text("p", "This exact configuration was already imported.", "meta"));
    return;
  }
  root.append(
    makeButton(
      report.valid ? "Import reviewed state" : "Quarantine configuration",
      () => importLegacy(report),
      "button secondary",
    ),
  );
}

async function inspectLegacy() {
  try {
    renderLegacy(await requestJson("/v1/legacy"));
  } catch (error) {
    showMessage(error.message, true);
  }
}

async function importLegacy(report) {
  const action = report.valid ? "import" : "quarantine";
  if (
    !window.confirm(
      `Confirm ${action} of the reviewed configuration with digest ${report.source_digest}?`,
    )
  ) {
    return;
  }
  try {
    const result = await requestJson("/v1/legacy/import", {
      method: "POST",
      body: JSON.stringify({
        source_digest: report.source_digest,
        confirmed: true,
      }),
    });
    renderLegacy({
      available: true,
      report: { ...result.report, already_imported: true },
    });
    await refresh();
    showMessage(
      result.restart_manager_required
        ? "Existing state imported. Restart the manager before starting imported services."
        : "Existing configuration import completed.",
    );
  } catch (error) {
    showMessage(error.message, true);
  }
}

async function reviewRelease() {
  const selected = byId("release-manifest").files?.[0];
  if (!selected) {
    showMessage("Choose a signed JSON release manifest first.", true);
    return;
  }
  if (selected.size > 1024 * 1024) {
    showMessage("The signed release manifest exceeds the 1 MB limit.", true);
    return;
  }
  try {
    const manifest = JSON.parse(await selected.text());
    if (!manifest || Array.isArray(manifest) || typeof manifest !== "object") {
      throw new Error("The signed release manifest must be a JSON object.");
    }
    const operationPlan = await requestJson("/v1/releases/plans", {
      method: "POST",
      body: JSON.stringify({
        manifest,
        expected_revision: snapshot.status.configuration_revision,
        offline: byId("release-offline").checked,
        start_after_activation: !byId("release-keep-stopped").checked,
      }),
    });
    showPlan(operationPlan, "Activate signed update");
  } catch (error) {
    showMessage(error.message, true);
  }
}

async function checkManagerUpdate() {
  const button = byId("check-manager-update");
  const status = byId("manager-update-status");
  button.disabled = true;
  button.classList.add("busy");
  status.textContent = "Checking the signed release channel…";
  try {
    const update = await requestJson("/v1/releases/manager-update");
    if (update.status !== "available" || !update.manifest) {
      status.textContent = `Pandrator Manager ${update.current_version} is current.`;
      return;
    }
    status.textContent =
      `Pandrator Manager ${update.version} is available. Review the exact signed update before installing it.`;
    const operationPlan = await requestJson("/v1/releases/plans", {
      method: "POST",
      body: JSON.stringify({
        manifest: update.manifest,
        expected_revision: snapshot.status.configuration_revision,
        offline: false,
        start_after_activation: true,
      }),
    });
    showPlan(operationPlan, `Update Manager to ${update.version}`);
  } catch (error) {
    status.textContent = "The update check did not complete.";
    showMessage(error.message, true);
  } finally {
    button.disabled = false;
    button.classList.remove("busy");
  }
}

async function reviewUninstall() {
  try {
    const exportValue = byId("uninstall-export").value.trim();
    const operationPlan = await requestJson("/v1/uninstall/plans", {
      method: "POST",
      body: JSON.stringify({
        expected_revision: snapshot.status.configuration_revision,
        purge_data: byId("uninstall-purge").checked,
        export_data: exportValue || null,
      }),
    });
    showPlan(operationPlan, "Uninstall Pandrator");
  } catch (error) {
    showMessage(error.message, true);
  }
}

async function signOutBrowser() {
  try {
    await requestJson("/v1/session", { method: "DELETE" });
    window.location.reload();
  } catch (error) {
    showMessage(error.message, true);
  }
}

async function forgetAuthorizedBrowsers() {
  if (
    !window.confirm(
      "Forget every browser authorized for this manager, including this one?",
    )
  ) {
    return;
  }
  try {
    await requestJson("/v1/browser-sessions", { method: "DELETE" });
    window.location.reload();
  } catch (error) {
    showMessage(error.message, true);
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  for (const tab of document.querySelectorAll(".manager-tab")) {
    tab.addEventListener("click", () =>
      activateManagerTab(tab.dataset.tab),
    );
    tab.addEventListener("keydown", handleManagerTabKeydown);
  }
  activateManagerTab(activeManagerTab);
  byId("refresh").addEventListener("click", refresh);
  byId("open-network-settings").addEventListener(
    "click",
    openNetworkSettings,
  );
  byId("review-selection").addEventListener("click", planSelection);
  byId("application-primary").addEventListener("click", applicationPrimary);
  byId("application-restart").addEventListener("click", () =>
    applicationAction("restart"),
  );
  byId("application-stop").addEventListener("click", () =>
    applicationAction("stop"),
  );
  byId("network-mode").addEventListener("change", (event) => {
    const mode = event.target.value;
    const bind = byId("network-bind-host");
    if (
      mode === "https_proxy" &&
      networkPreviousMode !== "https_proxy" &&
      ["0.0.0.0", "::"].includes(bind.value)
    ) {
      bind.value = "127.0.0.1";
    } else if (
      mode === "private_network" &&
      networkPreviousMode !== "private_network" &&
      bind.value === "127.0.0.1"
    ) {
      bind.value = "0.0.0.0";
    }
    networkPreviousMode = mode;
    networkDirty = true;
    updateNetworkFields();
  });
  for (const control of byId("network-details").querySelectorAll(
    "select, input",
  )) {
    control.addEventListener("input", () => {
      networkDirty = true;
      updateNetworkFields();
    });
    control.addEventListener("change", () => {
      networkDirty = true;
      updateNetworkFields();
    });
  }
  byId("save-network").addEventListener("click", saveNetwork);
  for (const option of document.querySelectorAll(
    'input[name="plan-access-mode"]',
  )) {
    option.addEventListener("change", updatePlanAccessFields);
  }
  byId("confirm-plan").addEventListener("click", executePlan);
  byId("close-plan").addEventListener("click", closePlan);
  byId("cancel-plan").addEventListener("click", closePlan);
  byId("cancel-operation").addEventListener("click", cancelOperation);
  byId("run-doctor").addEventListener("click", runDoctor);
  byId("inspect-legacy").addEventListener("click", inspectLegacy);
  byId("review-release").addEventListener("click", reviewRelease);
  byId("check-manager-update").addEventListener(
    "click",
    checkManagerUpdate,
  );
  byId("review-uninstall").addEventListener("click", reviewUninstall);
  byId("sign-out-session").addEventListener("click", signOutBrowser);
  byId("forget-browser-sessions").addEventListener(
    "click",
    forgetAuthorizedBrowsers,
  );
  try {
    await establishBrowserSession();
    await refresh();
    refreshTimer = window.setTimeout(poll, 2500);
  } catch (error) {
    showMessage(error.message, true);
    setManagerHealth("error", "Authorization required");
  }
});

window.addEventListener("pagehide", () => {
  if (refreshTimer !== null) window.clearTimeout(refreshTimer);
});
