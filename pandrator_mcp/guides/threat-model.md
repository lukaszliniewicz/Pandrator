# Pandrator MCP threat model

## Protected assets

- application and Manager credentials;
- provider credentials stored by Pandrator;
- source documents, generated media, and artifact metadata;
- host installation state and managed service processes;
- immutable workflow and Manager plan digests;
- audit identity, idempotency records, and durable work state.

## Trust boundaries

| Mode | Boundary | Required controls |
|---|---|---|
| Local managed | stdio host → sidecar → validated local Manager → loopback Pandrator | exact Manager process identity, protected descriptor and credential, loopback-only endpoints |
| LAN/VPN | sidecar → configured private origin | explicit CIDR and HTTP acknowledgement, DNS entirely within CIDR, no redirects or inherited proxy |
| External HTTPS | sidecar → public TLS origin | hostname verification, exact scheme/host/port, public-only DNS answers, identity pinning |
| Externally managed | sidecar → application only | scoped application credential, no Manager gateway, platform secret store |
| App-down recovery | sidecar → separate Manager HTTPS origin | distinct audience-bound expiring credential, Manager identity pin, remote-agent policy, plan/digest confirmation |

## Principal threats and controls

**Model-supplied SSRF destination.** No tool accepts an origin, host, proxy,
credential, or CA path. Only `TargetRegistry` turns a named process-selected
profile into a resolved endpoint.

**DNS rebinding or mixed-zone resolution.** Resolution occurs through network
policy before every request. All answers must belong to one accepted trust
zone; link-local, metadata, multicast, and unspecified addresses are rejected.
Redirects are disabled.

**Credential disclosure.** Profiles contain only approved backend handles.
`CredentialResolver` is the only secret-resolution boundary. Secret wrappers
hide `repr` and string conversion. Downstream response size is bounded and
Pandrator's redactor sanitizes work results, events, and errors.

**Confused deputy between application and Manager.** Application and recovery
credentials have distinct audiences and scopes. The Manager's permanent local
client secret never leaves the target host. Manager writes require target-side
policy in addition to application scopes.

**Target replacement.** Enrollment pins application ID, canonical origin, and
Manager ID. A mismatch fails closed and requires explicit re-enrollment.

**Replay or duplicate mutation.** Every mutation requires an idempotency key.
Plan-backed operations bind that key to the authenticated subject, target,
action, normalized argument digest, and immutable plan digest.

**Prompt injection from source content.** Guides are packaged product
knowledge, not retrieval over user documents. Source text is data, never tool
instruction. Tool catalogs and next actions are registered server-side.

**Overbroad agent authority.** Scoped principals, action risk classes,
review-first plans, target-side maximum policy, explicit confirmations, and
auditable work records provide independent enforcement layers.

## Phase acceptance criteria

1. The standalone package has no import path to Pandrator database, ORM, queue,
   or application-service modules.
2. No MCP application client calls generic raw job creation.
3. Tool schemas contain no connection or credential material.
4. Local, LAN, public, externally managed, and recovery profiles have tests for
   accepted and rejected address sets.
5. Target identity changes, TLS failures, redirects, oversized responses, and
   unavailable credential backends fail closed.
6. No remote Manager mutation ships before the versioned automation enrollment
   contract, target-side policy, scopes, idempotency, plan digest, and audit
   controls are implemented together.
