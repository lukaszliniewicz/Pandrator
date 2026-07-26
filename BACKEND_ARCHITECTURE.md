# Backend architecture

The web backend is composed in layers so HTTP, durable work, persistence, and
provider integrations can evolve independently without changing public
contracts.

## Dependency direction

Dependencies point inward:

1. `pandrator.web.api` composes one Flask application.
2. `application_services` constructs the process-level dependency graph.
3. `http_lifecycle` owns request IDs, authentication guards, CSRF,
   maintenance mode, security headers, and error projection.
4. `api_routes` translates HTTP requests and responses.
5. Use-case services such as workspace, generation, workflow, credential, and
   TTS catalogue services coordinate domain behavior.
6. Query/repository helpers and domain services own database access.
7. Models, the database wrapper, managed paths, and external adapters form the
   infrastructure boundary.

HTTP handlers should parse and validate transport input, delegate reusable
work, and serialize the result. They should not become an alternative home for
domain workflows or reusable queries.

## Application composition

`create_app` has four responsibilities:

- construct `ApplicationServices`;
- configure Flask and optional proxy handling;
- register the shared HTTP lifecycle and domain Blueprints;
- start background maintenance when enabled.

`ApplicationServices` is the ownership boundary for process-level services.
The same objects are exposed through `app.extensions["pandrator"]` for
extensions and tests. A service must be constructed once and injected into its
consumers rather than recreated in route handlers.

## HTTP domains

Every public route is owned by one of these Blueprints:

- `system`
- `auth`
- `tts`
- `sessions`
- `generation`
- `workflow`
- `jobs`
- `media`
- `providers`
- `library`
- `frontend`

`DomainBlueprints` contains the URL-to-domain policy. Internal endpoint names
are Blueprint-qualified; URLs, methods, response bodies, and the generated
OpenAPI contract remain the public compatibility surface.

When adding a route:

1. place the transport handler with its coherent domain;
2. add or extend a use-case service if behavior is reusable;
3. keep database queries out of repeated serialization loops;
4. add the schema to `openapi.py` and regenerate `openapi.json`;
5. add contract and authorization coverage.

## Durable job handlers

`JobHandlerRegistry` is the only registration abstraction used by the worker.
It rejects duplicate kinds and records a domain owner for each handler.
`job_handler_domains` groups registrations for text, generation, voice, source,
delivery, and workflow jobs. Each registration also owns a minimal durable
payload contract; malformed or obsolete payloads fail with a precise
missing-field diagnostic before domain code runs.

Workflow methods are resolved at dispatch time. This preserves test injection
and controlled runtime overrides without reintroducing a conditional dispatch
chain. Adding a job kind requires a domain registration and its handler; it
must not add a new central `if/elif` branch.

Worker behavior such as claiming, lease fencing, retries, cancellation,
resource locks, redaction, and completion remains in `jobs.py` and is
independent of domain handlers.

## TTS provider boundary

`TtsProviderAdapter` standardizes:

- health and availability;
- catalogue enrichment;
- synthesis, including the established retry and cancellation callbacks;
- voice upload.

`TtsProviderRegistry` resolves built-in and custom provider adapters.
`TtsCatalogueService` owns settings, credential-status projection, parallel
health probes, dynamic catalogues, and persisted previews. Workflow handlers
use the same registry instance for synthesis and voice upload.

The existing function-based provider implementation remains behind
`LegacyTtsAdapter` while individual providers are migrated. New provider
details belong in an adapter, not in Flask routes or job dispatch.

## Compatibility controls

- Keep HTTP paths, methods, schemas, and OpenAPI output stable unless an
  intentional migration is documented.
- Add characterization coverage before moving coupled behavior.
- Keep structural changes separate from feature changes.
- Preserve the application extension keys used by tests and integrations.
- Run Python, Svelte, production-build, and Playwright validation after
  backend structural changes.
