# Manager and recovery

Pandrator Manager owns installation, component versions, managed services,
runtime state, diagnostics, and durable host operations. Normal remote
automation reaches Manager through the authenticated Pandrator application
proxy, preserving the same target and authorization boundary.

For a local-managed target, the sidecar discovers Manager from its validated
workspace descriptor and can still read Manager diagnostics while the
application is stopped. The permanent local Manager credential never appears in
MCP traffic.

Optional direct remote recovery is a separate HTTPS endpoint with a separate,
scoped `pandrator-manager-recovery` credential. It is intended for app-down
diagnostics and explicitly supported recovery actions, not as an unrestricted
Manager API or shell.

The recovery credential is enrolled through an already authorized human
recovery browser. The approval binds its client, subject, application origin,
application instance, Manager instance, exact recovery origin, scopes, and
expiry. It lasts at most 30 days, is rate-limited per client, and is visible
in the Manager audit stream. It cannot read network settings, arbitrary files,
or the permanent local Manager bearer.

Normal remote Manager mutations also require the deployment operator to
enable the server-side single-owner mutation policy. Enrollment and scopes
cannot bypass that gate.

Repairs and component changes use two steps:

1. create and review a plan containing exact revisions, tasks, impact,
   confirmations, and digest;
2. execute that unchanged plan with a fresh idempotency key and explicit user
   confirmation.

Read-only doctor reports never repair automatically. Stop, restart, update,
remove, rollback, and data-purge actions are consequential and require their
declared confirmation level.

For audio.cpp models, inspect `pandrator_manager_status` for known and installed
packages, and `pandrator_get_tts_catalog(refresh=true)` for live selectable and
loaded models. A catalogue entry is not evidence of installation; a configured
model need not be resident in GPU memory.

Use the existing component plan tool with `component_id=audio_cpp` and
`options={"add_models":["fish_audio_s2_pro_q8_0"]}` to retain installed/desired
models and add a package. Alternatively, `options.models` declares the complete
replacement set and may remove omitted models. These options are mutually
exclusive, accept 1–32 unique supported IDs, and cannot contain URLs or paths.
Review the resolved model set, add/remove/retain lists, download/disk estimates,
service restart and confirmations before executing the immutable plan digest.

Model-changing operations refuse to start while application work is active;
new jobs are refused while that maintenance operation is active. Stop or finish
the work, then create a fresh plan if the previous operation failed. Poll with
`pandrator_get_work`; successful completion refreshes the live TTS catalogue
when the application is reachable. If it is restarting, refresh explicitly after
it returns. Select the desired session model with `pandrator_configure_tts`.
Package activation and session model selection are separate steps.

Qwen CustomVoice uses its native speakers, so cloned cast references must be
reassigned. Preview distinguishes request compilation, live model availability,
cast compatibility, and acoustic compliance (which requires a listening test).
Direct runtime stop/restart commands remain explicit operator actions; the
model-operation guard does not turn them into generation-aware maintenance.

When application-proxy access is unavailable, only network/availability
failures may fall back to the enrolled recovery endpoint. Scope denial,
identity mismatch, stale plans, and policy denial are returned unchanged.
