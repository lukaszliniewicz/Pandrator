# Collaboration, accounts and workspace access

Pandrator is Solo by default. Collaboration is an explicit installation policy,
not a network deployment mode. The initial Administrator first sets a password
and signs in, then enables collaboration. Existing personal projects stay private.
Use the user's existing authorization; this guide grants no additional access.

## Optional project links and external review

Public sharing is an independent Administrator opt-in, disabled by default, and
can also be used in Solo. Inspect `pandrator_get_public_sharing_settings` with an
Administrator enrollment (`app.admin`), or let the Administrator configure the
feature in Application settings. Enabling it does not configure Internet
hosting. Do not enable sharing merely to make an unrelated workflow succeed.

Project owners/workspace Managers use `pandrator_list_project_links`,
`pandrator_create_project_link` and `pandrator_revoke_project_link`. Creation
requires a stable request UUID. `manage_project` delegates project work using
configured resources and can incur provider costs. `review_translation` pins
the selected translation artifact and allows pending text corrections only.
The capability URL is returned once; handle it privately and do not substitute
it for this sidecar's target credential or send it automatically.

Use `pandrator_list_link_submissions` and the paged
`pandrator_get_link_submission` before `pandrator_review_link_submission`.
Reviewer labels are unverified. Accepting explicitly creates and selects a new
reviewed translation; supply the inspected submission revision, source hash,
document revision, selected artifact and a stable idempotency key. A conflict
requires renewed inspection, not mechanically changing guards to force the
write. Rejection changes only disposition. If `source_unavailable` is true,
saved corrected text remains inspectable but cannot be accepted.

Guest links cannot approve themselves, grant more access or acquire
Administrator scopes. Revocation/disablement retains submitted corrections for
ordinary authorized project editors. Admitted processing follows normal job
authorization/cancellation rules. See the application's project-link guide for
browser workflows and the remote-target guide for hosting boundaries.

## Use the right identity

Each person uses a separate application account and enrolls their assistant with
browser S256 PKCE (`pandrator-mcp target login NAME`). They sign in as themselves,
review the requested scopes and approve the relevant workspaces or project-only
view grants. One approved target/token does not automatically cover every
workspace the person can see in the browser. Resource approval and membership
are both required.

Keep target profiles, application identity pins and credential storage on the
assistant's computer. Do not paste tokens, reset links or passwords into tool
arguments. The installation's managed HTTP MCP bearer is a separate local
Owner/service credential, not a teammate identity. For named users, use a
per-person stdio sidecar with explicit application enrollment rather than sharing
that bearer or a local Owner bootstrap profile.

`pandrator_get_target_status` shows target identity, granted/requested scopes and
missing requested scopes; it does not currently show the signed-in user's name
or instance role. Confirm the account in the enrollment browser. Do not infer
Administrator status from the target identity or from the presence of a tool.

## Scopes and roles

| Task | Application scopes |
| --- | --- |
| Read approved workspaces and projects | `app.read` |
| Edit shared content or manage membership as a workspace Manager | `app.read`, `app.write` |
| Create a workspace or accept an invitation | `app.read`, `app.write`, `app.workspaces.enroll` |
| Run/export supported workflows | `app.read`, `app.write`, `app.run`; add `app.cancel` for cancellation |
| Manage instance accounts/reset links | `app.admin` (current instance Administrator required) |
| Enable collaboration | `app.admin`, `app.write` (current instance Administrator and initial Administrator password required) |
| Inspect workspaces with no active Manager | `app.admin`, `app.read` |
| Recover management by promoting an existing Member | `app.admin`, `app.read`, `app.write` |
| Review/execute installation or model plans | Current Administrator plus `app.admin`, `manager.read` to plan and `manager.mutate` to execute |

A workspace Member can create/edit/run/export content throughout that workspace.
A workspace Manager additionally governs membership and settings. These roles do
not confer the instance Administrator role. Only an instance Administrator can
trigger installation/download of missing models. Users can use available models;
report `model_acquisition_required` rather than attempting an alternate download.
Administrator status does not grant arbitrary private/project access.

Workspace tools disclose their full prerequisites in their descriptions and
`pandrator/requiredScopes` tool metadata. These are prerequisites, not proof of
current membership/role. The server checks current authority on each operation.
A denied operation returns the required scopes and this guide as a next action;
inspect account/resource approval as well as scope. Do not blindly retry a denial.
Re-run target login with the complete desired scope set to seek additional human
approval; include the scopes still needed, not only the newly added scope.
An existing token cannot grow its own authority.

## Shared-workspace workflow

1. Read this guide and `pandrator_get_target_status`. In Solo, an authorized
   Administrator can inspect mode with `pandrator_get_installation_mode`; opt-in
   requires the current policy revision and prior password setup.
2. After collaboration is enabled, `pandrator_list_workspaces` discovers only
   workspaces approved for the credential. Create with `pandrator_create_workspace`
   when authorized, or inspect `pandrator_list_my_workspace_invitations` and accept
   with `pandrator_accept_workspace_invitation`.
3. A workspace Manager searches existing accounts and invites them with the
   current workspace revision. Each recipient accepts for their own identity.
4. Pass the intended `workspace_id` explicitly to `pandrator_create_session` and
   other creation tools that advertise it. Keep returned project/session IDs,
   revisions and idempotency keys. Shared work is workspace-owned and remains
   after its creator leaves. CAS conflicts require re-reading current state.
5. Use the existing workflow tools to process and export authorized content.
   Check work to a terminal state and verify the resulting artifact before
   claiming completion.

For existing private work, use `pandrator_preview_project_copy`, explicitly choose
any destination connections, then call `pandrator_copy_current_project` with the
preview fingerprint and idempotency key. Poll the returned copy through
`pandrator_get_project_copy`. It is a distinct destination-owned current-state
copy; the source stays private, credentials stay private, and future edits do not
synchronize. Copies start Unfiled. A Can view grant is read-only and is not a
substitute for workspace membership when editing or processing is required.

## Current organization and recovery limits

There is no persistent MCP workspace-selection operation. `pandrator_list_sessions`
has no `workspace_id` filter, and no project-folder MCP tool is registered. Use
returned IDs and explicit workspace arguments where offered; use the browser for
folder organization. Never invent an unsupported argument or create duplicate
projects to simulate switching workspaces.

Only a workspace Manager may change membership. Before leaving, another active
Manager must remain. If account disablement leaves no active Manager, an instance
Administrator can use `pandrator_list_workspaces_without_manager` and
`pandrator_recover_workspace_manager` to promote an existing active Member, with
the current revision. Recovery adds no membership/content access for the
Administrator. Revocation, disabled accounts and stale authority remain enforced.

Configure HTTPS/exact origins separately for other computers; keep the Manager's
local credential and managed MCP listener private. Application enrollment and
optional app-down Manager recovery enrollment are different audiences.
