# Remote and headless Pandrator

Pandrator and Manager bind to loopback by default. A headless workstation,
home server, LAN/VPN host, external HTTPS server, or GPU pod is possible, but
it remains a single-owner deployment. Pandrator is not designed as a public
multi-user service.

## Supported topologies

| Topology | Recommended boundary |
| --- | --- |
| Same computer | Loopback; native Manager opens the local browser |
| Headless computer on a trusted network | VPN or HTTPS reverse proxy |
| Home/LAN installation | HTTPS with a private CA, or deliberately acknowledged private HTTP restricted to an exact CIDR |
| Internet server or GPU pod | Stable HTTPS application origin; optional separate HTTPS recovery origin; network policy around listeners |
| Externally managed application | Stable HTTPS application origin without Manager control exposed |

Use stable names and persistent volumes. Losing Pandrator data or Manager state
changes the corresponding installation identity and requires deliberate MCP
re-enrollment.

## Prepare a managed headless installation

The native launcher can configure the exact workstation-facing origins:

```bash
pandrator-manager-launcher setup \
  --workspace /srv \
  --remote-setup-url https://recovery.example.com \
  --remote-pandrator-url https://pandrator.example.com \
  --trusted-proxy-hops 1 \
  --no-open
```

The workspace example creates `/srv/Pandrator`. If ingress runs in another
container or network namespace, `--network-bind-host 0.0.0.0` may be necessary;
restrict that listener with firewall or platform network policy. Do not expose
it broadly merely because the application also has authentication.

The recovery URL is a bootstrap path, not a permanent bearer to paste into a
script. Keep the Manager's permanent local client endpoint private.

## HTTPS, proxies, and host identity

Configure the exact public scheme, host, and port. Terminate TLS at a trusted
reverse proxy or ingress and set only the real proxy-hop count. Preserve the
original host/protocol headers through that known chain. Redirects and a vague
“trust every proxy” configuration make identity and security checks ambiguous.

For private HTTP, use only the Manager's explicit trusted-private-network mode,
an exact private origin, and a restricted source network. Public Internet
targets require HTTPS. A VPN is often simpler than operating a private CA and
exposing additional services.

## Persist the right state

A durable deployment needs:

- the Manager workspace and state, when Manager is used;
- the Pandrator data root and databases;
- custom voices and models stored as user data;
- generated media that is not exported elsewhere; and
- deployment configuration for the exact origins and proxy boundary.

Do not put secrets into container images, Git, MCP target files, or prompts.
Use the platform secret store and make the data volumes owner-readable only.

## MCP from another workstation

Run `pandrator-mcp` locally beside the agent host and bind that process to one
fixed remote target. Enroll through owner-approved browser consent, pin the
application identity, and run `doctor` before enabling writes. Optional
app-down Manager recovery uses a different origin, audience, credential, and
enrollment.

The Manager-owned Streamable HTTP MCP is deliberately loopback-only and is not
the remote deployment surface. Do not forward port 8099 through ingress. The
agent-workstation stdio process preserves approved local-file access without
creating a second public listener. See [agent connections](agent-connections.md)
for the transport choice.

The sidecar rejects model-supplied destinations, redirects, unexpected DNS
zones, identity changes, and credential substitution. Exact configuration and
scope commands are in the [MCP guide](../../pandrator_mcp/README.md).

## Deployment review checklist

- One owner and one explicit workspace are identified.
- Application and optional recovery origins use stable DNS.
- TLS certificates and proxy-hop count match the deployed path.
- Listeners are restricted by host firewall or network policy.
- Manager's permanent local endpoint is not public.
- Application data and Manager state are on persistent storage.
- Secrets come from the deployment secret store.
- Backups cover data, voice material, and custom models.
- MCP application and recovery credentials have separate least-privilege
  scopes.
- `doctor` passes from the actual agent workstation.
- A rollback or previous runtime slot remains available.

For Manager-specific network flags and authorization lifetimes, see the
[Manager guide](../../pandrator_manager/README.md). For the data boundary, see
[privacy and security](../security/privacy-and-security.md).
