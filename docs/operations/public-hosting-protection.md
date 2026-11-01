# Public hosting and optional protection

Keep Pandrator local unless other people need to reach it. For access from other
computers, use a VPN or an HTTPS reverse proxy. Named accounts, shared workspaces
and project links are separate choices; enabling any of them does not configure
DNS, TLS, a listener or a firewall.

## 1. Choose a hosting setup

For a Manager installation, follow [remote and headless setup](remote-and-headless.md).
Keep the Manager's permanent API and managed HTTP MCP listener private. An optional
remote recovery origin has its own setup; the application proxy examples below
do not configure it.

For a standalone Python installation, choose a persistent directory writable by
the account running Pandrator. Replace the directory and hostname in these examples.
Use the **same data directory** for initialization, serving and processing:

```bash
pandrator --data-dir /srv/pandrator/data auth init
pandrator --data-dir /srv/pandrator/data serve \
  --host 127.0.0.1 --port 8097 \
  --public-url https://pandrator.example.com --proxy-hops 1 --no-open-browser
```

The first command prompts for the Owner password. Run the worker in a second
terminal or supervised service:

```bash
pandrator --data-dir /srv/pandrator/data worker
```

The web process accepts requests; the worker processes jobs. HTTPS public mode
enables secure cookies automatically. A normal Manager installation starts these
processes for you, so it does not need these standalone commands.

## 2. Put the application behind HTTPS

The repository provides two examples:

| Ingress | Example | Required setup |
| --- | --- | --- |
| Direct clients → Caddy → Pandrator | [Caddyfile.example](../../deployment/security/Caddyfile.example) | Replace the hostname; point DNS at the server; keep Pandrator on loopback with one trusted proxy hop. |
| Cloudflare → nginx → Pandrator | [nginx snippet](../../deployment/security/nginx-cloudflare-location.conf.example) | Provide the HTTPS server/certificate and a maintained Cloudflare trusted-IP include; restrict direct origin access. |

Use the exact browser-facing origin, including a nondefault port if present.
If your backend uses a different port, change the proxy's upstream too. Validate
the configuration with your installed proxy before reloading it.

Trust only the proxy hops that actually exist. The Cloudflare/nginx example
restores the visitor address from verified Cloudflare peers, then sends one
forwarded address to Pandrator. An Internet client's forwarded headers alone are
not evidence of its address.

See [Caddy reverse proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)
and the [nginx real-IP module](https://nginx.org/en/docs/http/ngx_http_realip_module.html)
for the proxy settings.

## 3. Check the application settings

As an Administrator, open **Application settings → Web hosting and public sharing
(optional) → Hosting and protection setup**. Check the origin, request transport,
secure cookies, trusted hosts and proxy-hop count against your deployment.

Public links are off by default. Enable them only if you need them, using the
same origin as the running application. See [project links and translation
review](../guides/project-links.md) for issuing, reviewing and revoking access.

## 4. Enable security logs if needed

Remote and collaboration sign-in use progressive backoff per IP and per account:
five failed attempts per IP or twenty per account within five minutes. These
counters belong to the current process. Local Solo loopback sign-in keeps its
existing behavior.

To send security events to the process output or service journal, set
`PANDRATOR_SECURITY_LOG=stdout` in the Pandrator process environment. For a managed
installation, set it in the Manager's environment before starting the application.
The managed child inherits it.

Alternatively, set an absolute file path such as
`PANDRATOR_SECURITY_LOG=/var/log/pandrator/security.jsonl`. Create the parent directory
for the service account first. The application restricts the file to its owner;
the file sink needs platform support for those permissions. Use stdout when that
support is unavailable. The sink is inactive when the variable is unset.

The JSONL stream records sign-in and project-link redemption failures or
throttling. It includes UTC time, the server-trusted client address, HTTP status,
request ID, route template and method. It excludes usernames, credentials and
submitted text. Actual failures are logged individually; repeated throttled
messages are coalesced in a bounded cache. Logging failure does not disable login.

Use journal retention or external log rotation. The file sink opens each append
separately, so it follows a rotated file. Protect IP addresses and request logs as
operational personal data. The application's audit history is separate.

## 5. Add Fail2Ban or CrowdSec

Choose the protection tool you already operate, or start with one. Pandrator does
not install these services, change firewall rules or install a remediation agent.

For **Fail2Ban with direct clients**:

1. Copy the [filter](../../deployment/security/fail2ban/pandrator-auth.conf) into
   the installed filter directory.
2. Adapt the [disabled example jail](../../deployment/security/fail2ban/pandrator.local.example)
   to your log path, action and administrator allowlist.
3. Test actual samples with `fail2ban-regex` before enabling the jail. The filter
   needs a version that understands ISO-8601 UTC timestamps.

The filter consumes the application's fixed JSONL format. Verify that the ban
will act on the actual client connection. See the [upstream filter guide](https://github.com/fail2ban/fail2ban/blob/master/FILTERS).

For **CrowdSec**, install the [acquisition example](../../deployment/security/crowdsec/acquisition.yaml.example),
[parser](../../deployment/security/crowdsec/parser.yaml) and
[scenario](../../deployment/security/crowdsec/scenario.yaml) in your existing
Security Engine configuration. Set the log path and review administrator allowlists.
Check parsing with:

```bash
cscli explain --file /var/log/pandrator/security.jsonl --type pandrator
```

Test the scenario with a configured hub test or disposable engine, then choose a
remediation component for your proxy or edge. A scenario alone does not enforce
a ban. See [parser configuration](https://docs.crowdsec.net/docs/log_processor/parsers/create/)
and [scenario configuration](https://docs.crowdsec.net/docs/log_processor/scenarios/format/).

## Cloudflare-specific settings

Use Full (strict) origin TLS and prevent direct-origin bypass. Restore visitor
addresses only from verified Cloudflare networks using their current published
ranges. Handle Pseudo IPv4 deliberately if it overwrites IPv6 headers. See
[Cloudflare headers](https://developers.cloudflare.com/fundamentals/reference/http-headers/)
and [origin restrictions](https://developers.cloudflare.com/fundamentals/concepts/cloudflare-ip-addresses/).

A host firewall sees Cloudflare's socket address. Banning the restored visitor IP
will not match that connection; banning the socket peer can cut off the proxy.
Use edge or proxy-level remediation instead of the direct-client firewall jail.

Bypass shared caching for `/api/*`, `/share/*` and private media. Preserve range
requests, streaming and application security headers, including the document
sandbox on artifact responses. Apply login rate limits narrowly and test them
with the real login/CSRF flow. See [login caching](https://developers.cloudflare.com/cache/troubleshooting/dynamic-content-and-login-issues/)
and [rate-limit guidance](https://developers.cloudflare.com/waf/rate-limiting-rules/best-practices/).

Cloudflare Access adds another permission check. Project-management visitors
using the normal project screens must also satisfy Access. For a no-account
translation-review portal, deliberately allow its required paths:

- `/share/*`;
- `/api/v1/public-links/*`;
- `GET /api/v1/auth/status`, used for the visitor-session heartbeat; and
- the public static-asset paths used by your deployed build.

Pandrator still requires a valid review link and scoped visitor session. A link
does not bypass Access by itself. Check the exact paths after building; a landing
page exception alone is insufficient. See [self-hosted Access applications](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/).
