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

When application-proxy access is unavailable, only network/availability
failures may fall back to the enrolled recovery endpoint. Scope denial,
identity mismatch, stale plans, and policy denial are returned unchanged.
