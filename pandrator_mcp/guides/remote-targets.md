# Remote Pandrator targets

The MCP server is normally a local stdio sidecar launched by an agent host. One
process is bound to one named target chosen in host configuration, outside
model-visible tool arguments.

Approved source and output roots belong to the sidecar host, independently of
the target location. A workstation sidecar can therefore import a file from
its Downloads directory into a remote Pandrator server and later download the
server's immutable result. Both transfers stream outside model context and can
resume after interruption.

## Public hosting and optional review links

The application's optional sharing controls do not expose a new MCP listener.
Keep using the local stdio sidecar bound to its approved remote target. Permanent
Manager endpoints and the managed HTTP MCP listener stay private.

An administrative enrollment can inspect `pandrator_get_public_sharing_settings`
for the effective application origin, secure-cookie state and trusted proxy
configuration. Match public sharing to that existing HTTPS origin. Do not use a
model-supplied capability URL as a target destination, enrollment credential or
redirect override.

Operators can opt into `PANDRATOR_SECURITY_LOG=stdout` or a protected absolute
file path for fixed-field authentication events. The application's public-hosting
guide supplies fail2ban/CrowdSec and reverse-proxy examples; installing a daemon,
changing a firewall or enabling Cloudflare is a separate operator action.
Behind a proxy, ban the verified visitor at a compatible enforcement point.
Cloudflare Access remains an additional permission layer: a Pandrator link alone
does not bypass it. Follow the collaboration guide for submission inspection and
guarded acceptance tools.

## Local managed installation

The profile's `workspace` is the absolute parent directory selected during
Manager installation: `/srv` for `/srv/Pandrator`, for example. Supplying the
inner application directory makes discovery look one level too deep.
The sidecar validates the
Manager descriptor and process identity, discovers the current loopback
application endpoint, and then connects. Release paths and ports may change
without changing the target identity.

## Home, LAN, or VPN

Prefer HTTPS even on a private network. Plain HTTP is allowed only when the
profile explicitly acknowledges it and supplies the precise private CIDR. DNS
must resolve entirely inside that CIDR. Link-local and cloud metadata
addresses remain forbidden.

Use a stable DNS name or reserved address, configure Pandrator's exact public
origin and trusted hosts, initialize a strong owner password, and restrict
firewall access to the intended LAN or VPN.

## External server

Expose Pandrator through an HTTPS reverse proxy with a valid certificate. Set
the exact public origin and trusted proxy-hop count on the server. The profile
uses that same origin; redirects are rejected. Keep the Manager's permanent
local client endpoint private.

Direct app-down Manager recovery is optional. If enabled, expose a distinct
HTTPS recovery origin and enroll a scoped, expiring automation credential
through the human recovery UI. Never copy the Manager's local client secret.

## Pod or container

Persist the Pandrator workspace and, for managed deployments, Manager state on
durable volumes. Losing either volume changes the corresponding target
identity and requires explicit re-enrollment. Terminate TLS at a trusted
ingress, preserve the configured public origin, use deployment secrets for
credentials, and apply network policy so only the required application and
optional recovery routes are reachable.

Models in Codex, Antigravity, OpenCode, or Claude Code can help generate a pod,
reverse-proxy, firewall, or system-service setup quickly. Treat that output as
infrastructure code: give the agent the intended domain and platform, review
the generated manifests, keep credentials in the platform secret store, and
verify the resulting target with the MCP doctor command before enabling
mutations.

A safe deployment request asks the agent to stop before deployment or public
exposure, show the proposed listeners and trust boundaries, persist both
application and Manager state, leave secret placeholders instead of values,
and provide validation and rollback commands. The operator still owns DNS,
TLS, firewall, proxy-hop, and volume decisions.

## Workstation enrollment sequence

On the workstation that runs the MCP host, install the MCP package with its
`credential-stores` extra and ensure the user's operating-system keychain is available and unlocked. The `--headless` approval
flow still requires that keychain. Follow the README's installation section,
then enroll the HTTPS target:

1. add the fixed target and least-privilege application scopes;
2. run `pandrator-mcp target login NAME`;
3. pin the authenticated application identity;
4. optionally run `target configure-recovery NAME --origin HTTPS_ORIGIN` and
   then separately run `target login NAME --manager-recovery`;
5. run `pandrator-mcp doctor --target NAME`; and
6. generate the selected host fragment with
   `pandrator-mcp host-config HOST --target NAME`.

Application enrollment uses the signed-in account's consent and S256 PKCE.
Recovery enrollment separately requires an authorized Manager recovery browser.
The CLI stores each result directly in the operating-system keyring.

## Rotation, revocation, and removal

Re-running `pandrator-mcp target login NAME` rotates the application
credential for the same client ID. The target revokes the preceding token and
the local keyring entry is replaced. Manager recovery rotates separately with
`--manager-recovery`.

Revocation belongs to the target owner. On the application host, use
`pandrator --data-dir DATA auth automation-client list` followed by
`pandrator --data-dir DATA auth automation-client revoke CLIENT_ID --yes`.
For Manager recovery, use `pandrator-manager --workspace WORKSPACE
automation-client list` and the matching `revoke CLIENT_ID --yes` command or
the recovery browser.

After remote revocation, `pandrator-mcp target logout NAME --yes` deletes the
workstation credential and clears its local enrollment metadata. Add
`--manager-recovery` for that audience. Finally, remove the non-secret profile
with `target remove NAME --yes`. Removal refuses to silently orphan a
configured local credential; the operator must explicitly delete or preserve
it. None of these local commands deletes remote Pandrator data.

## Identity and credential rules

- Enrollment pins the stable application instance ID and canonical public
  origin.
- A managed target also pins the Manager instance ID.
- Application and recovery credentials have different audiences and cannot be
  substituted.
- Target profiles contain credential handles, never secret values.
- Connection URLs, tokens, proxy choices, and CA paths are process
  configuration and never MCP tool inputs.
- Absolute local paths are also profile configuration. Tools expose only root
  names and relative entries, and a remote target never returns its storage
  path.
- Local logout and profile removal do not claim server-side revocation; revoke
  as the target owner first, then clean up the workstation.

## Team identities

For collaboration, use a separate named account and per-person stdio enrollment.
The managed local Owner bearer is not a teammate login. Approve the required
workspaces/projects and complete scope set in the browser; changing a target's
requested scopes does not expand an old token. Read the `collaboration` guide.
