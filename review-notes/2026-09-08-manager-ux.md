# Manager UX review, 8 September 2026

The Manager has a calm visual structure and useful controls for someone who
already knows their engines. A beginner can find the installation button, but
still has to translate provider names, model variants, and installation states
into a working setup. Clearer audio.cpp wording helps; a guided route to the
first usable result would have the greater effect.

## Scope and evidence

I inspected the running Linux Manager's installed-provider view, then ran the
current 0.9.20 source in a separate, empty workspace. I used the actual browser
interface to inspect initial setup, provider expansion, model choices, the
installation review, Maintenance, Activity, and authorization failures.
I reviewed a Pandrator plus audio.cpp plan without executing it. No model
downloads, production updates, or engine starts/stops were performed.

This was an expert walkthrough, not a usability study with novice participants.
The native folder picker, Windows launcher warnings, long-download behavior,
and completion of a real installation were not exercised.

## Changes made with this review

The Manager now calls audio.cpp **Local speech models (audio.cpp)** and explains
that it is one provider with several selectable models. Its details describe
where to install packages, where to choose the active model in Pandrator, and
which models need a reference recording. The README and Pandrator's service
selection help now explain the same relationship. Stable provider/component
IDs, model defaults, and installation behavior are unchanged.

## Highest-priority improvements

### 1. Give first-time users a route based on their goal

The fresh screen presents five primary TTS providers, transcription, voice
conversion, and training tools. It does not first establish whether the user
wants ready-made narration, voice cloning, or transcription. Selecting only
Pandrator is also possible without explaining how the speech provider will be
chosen afterward.

Add a short optional starting point: **Narrate with ready-made voices**,
**Clone a voice**, **Transcribe a recording**, and **Choose everything myself**.
Each route should show the selected provider/model, whether it needs a voice
recording, and the download estimate. Keep the full catalogue available.
Do not imply that installing the app alone installs a speech model.

Evidence: `recovery_ui/static/index.html`, application and component panels.

### 2. Make the default model choice deliberate and visible

Selecting audio.cpp without opening its details selects the first package,
Qwen3 TTS Base. That is a cloning model. The user has not been asked whether
they have a reference recording or simply want to type text and hear a voice.
The model names lead with terms such as Base, CustomVoice, 1.7B, and Q8_0.

For a guided ready-made-voice route, select a compatible preset-voice model.
For cloning, explain the required recording before installation. In the full
catalogue, lead each model row with its purpose and keep the precise model ID
available as supporting detail. The default should follow the chosen workflow.

Evidence: `recovery_ui/static/app.js`, `controlsFor` falls back to the first
model; `components/catalog.py`, audio.cpp model order and descriptions.

### 3. Show what will be installed in the final review

The actual review said **Install 2 selected items**, about **4.1 GB** download,
and about **6.1 GB** disk. It listed ten steps, including “Ensure verified Pixi”,
“Stage”, “Verify”, and “Activate”, but did not name the selected Qwen3 model.
A beginner cannot confirm the most consequential choice from that dialog.

Lead with the application, provider, selected model names, voice-reference
requirement, compute choice, and estimated space. Keep the exact backend steps
under technical details. A button such as **Install selected items** describes
the action more directly than **Confirm exact plan**. Preserve the existing
plan verification and safety checks underneath this presentation.

Evidence: `recovery_ui/static/app.js`, `showPlan`; `index.html`, `plan-dialog`.

## Other material friction

- **Download estimates change meaning between screens.** The expanded audio.cpp
  card shows about 28 GB download and 35 GB installed for all ten packages.
  The review estimates only the selected package plus the application. Show
  selected-package totals in the card, or label the all-package figure much
  more prominently. Per-model sizes and the selection-aware backend estimate
  already exist.
- **Adding models to an installed provider is not obvious.** Package checkboxes
  share a card with **Review update**, **Repair installation**, and **Remove**.
  Distinguish installed packages from proposed additions and use an explicit
  action such as **Apply model selection** when the selection changes.
- **Technical choices arrive early.** The installation review gives local,
  private-network, and HTTPS proxy/ingress access equal visual weight. Keep
  “This computer” prominent and put remote access behind an expandable choice.
  Maintenance already offers a place to change it later.
- **Blocked authorization looks like loading.** An unauthorized browser displays
  a useful error while the application still says “Checking…” and the engine
  list says “Loading…”. Replace those placeholders with a consistent blocked
  state and clear instructions for reopening through the launcher or tray.
- **Activity remains a technical screen.** A fresh workspace shows stopped API,
  worker, and MCP services, plus expanded Manager JSON. Show a friendly empty
  state first and collapse diagnostics. The API/worker distinction is useful
  for troubleshooting but need not be part of a beginner's mental model.

## Confirmed multi-workspace authorization defect

Opening two Managers on different localhost ports caused authorization to fail.
A focused Luna/xhigh backend investigation reproduced the cause: both use the
host-scoped `pandrator_manager_session` cookie with path `/`. Cookies are shared
across ports, while each Manager validates a different security context. A
poll to one Manager rejects the other Manager's cookie and sends a deletion
header that removes it from the shared browser jar.

The backend session itself remains stored. This is a cookie-isolation defect,
not normal expiry. It should be fixed before claiming reliable simultaneous
multi-workspace use. No authentication code was changed in this UX/copy pass.

Evidence: `pandrator_manager/api/app.py`, `RECOVERY_COOKIE`, `set_session_cookie`,
`clear_session_cookie`, and the invalid-session response; reproduced with two
independent temporary workspaces and Flask clients.

## What is worth keeping

The prominent **Open Pandrator** action works well in the installed view.
Install, Maintenance, and Activity are clearly separated. Optional components
can be selected together, compatibility providers are collapsed when absent,
compute defaults to Automatic, and licence/source links are accessible.
The installation review provides a useful stopping point before downloads.
These are good foundations for a simpler guided route, without replacing the
advanced controls.

The next UX phase should cover goal selection, explicit model selection, and a
model-aware installation review together. The cookie-isolation fix is a
separate bounded correctness change. Test the resulting first-run flow with
people unfamiliar with model names before expanding the catalogue further.

## Validation of the copy changes

- `pytest tests/test_manager_provider_policy.py tests/test_manager_audiocpp.py -q`:
  14 passed using the Manager's Pixi environment.
- Ruff on the changed Python catalogue files and ESLint on the changed Svelte
  components passed.
- `npm run check` reported zero errors and zero warnings; `npm run build`
  regenerated the bundled web assets successfully.
- `python scripts/check_docs.py` and `git diff --check` passed.
- The updated Manager label and the rebuilt application's provider help were
  checked in the browser. The help and select share one grid cell so the
  explanation stays beneath its control.
