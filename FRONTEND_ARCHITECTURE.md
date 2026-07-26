# Frontend architecture

Pandrator's Svelte frontend is organized around a generated HTTP contract,
typed domain clients, resource-owning stores, and presentation components. The
goal is to keep server state and transport behavior out of UI components while
retaining explicit, reviewable state transitions.

## Dependency direction

```text
routes and presentation components
              |
              v
session, workflow, and generation stores
              |
              v
domain-api.ts / admin-api.ts
              |
              v
typedApiJson() in api.ts
              |
              v
api.generated.ts <- openapi.json <- pandrator/web/openapi.py
```

Dependencies should flow down this diagram. The generated contract does not
contain runtime behavior, stores do not depend on components, and components
must not create alternative fetch wrappers.

## HTTP and contract boundary

- `pandrator/web/openapi.py` is the source of the web contract.
- `scripts/generate_web_contract.py` writes the deterministic
  `openapi.json`.
- `npm run generate-api` generates `web/src/lib/api.generated.ts`.
- `web/src/lib/api.ts` is the only direct network gateway. It owns URL
  construction, credentials, CSRF headers, body serialization, response
  decoding, cancellation plumbing, and normalized `ApiError` values.
- `web/src/lib/domain-api.ts` groups session, source, artifact, job, workflow,
  and generation operations.
- `web/src/lib/admin-api.ts` groups settings, credentials, providers, speech
  services, voices, tools, PDF editing, pronunciations, and diagnostics.

New JSON endpoints must be described in OpenAPI and added to the appropriate
typed client before a component uses them. Do not add a generic `api(path)`
escape hatch. `apiResponse()` is reserved for response-oriented operations
such as ranged artifact previews where the caller intentionally consumes a
`Response`, rather than a JSON domain object.

Regenerate both contract artifacts after changing schemas or operations:

```bash
python scripts/generate_web_contract.py
npm --prefix web run generate-api
```

CI regenerates both files and rejects drift.

## Server-state ownership

Server resources have one owner:

| Resource | Owner | Responsibilities |
|---|---|---|
| Application snapshot, sessions, jobs, capabilities | `appState` | Bootstrap, SSE lifecycle, burst coordination, application-wide refresh |
| Session record and outcome plan | `SessionStore` | Session-scoped load, cache state, session invalidation |
| Workflow snapshot and live stage progress | `WorkflowStore` | Workflow load/refresh, artifact presentation, event progress patches |
| Generation runs, segments, takes, and output assembly | `GenerationStore` | Cancellation, pagination, mutations, live progress, generation/output invalidation |

`ResourceState<T>` supplies the shared `idle`, `loading`, `ready`, `empty`,
`stale`, and `failed` lifecycle and coalesces concurrent loads. Revalidating a
stale resource preserves its current value and mounted UI; only the initial
load uses the blocking `loading` state. Components may own ephemeral UI
state—open dialogs, selected tabs, draft form values, local playback—but must
not duplicate a store's server cache or event subscription.

## Invalidation

`appState` translates server-sent events into typed invalidation batches.
`InvalidationCoordinator` coalesces bursts, and `invalidationBus` routes only
the affected resource and session IDs to stores. Stores may patch cheap live
progress immediately and refresh canonical state once per batch.

Do not use `window` custom events for application invalidation. Mutations
should either update their owning store immediately or publish a typed
invalidation batch through the shared bus.

## Component boundaries

`SessionWorkspace` is the composition boundary for a session. Workflow-stage
presentation belongs to `WorkflowStageCard`, while stage-run and mismatch
dialogs belong to `WorkflowRunDialogs`.

`GenerationDrawer` coordinates generation UI state and delegates segment
tables, reading mode, and speech-plan review to
`GenerationSegmentTable`, `GenerationReadingView`, and
`SpeechPlanReviewDialog`.

Extracted components receive typed values and callbacks. They should not import
transport clients or establish competing server-state ownership.

## Adding frontend behavior

1. Add or update the Pydantic request schema and OpenAPI operation.
2. Regenerate `openapi.json` and `api.generated.ts`.
3. Add a typed operation to `domain-api.ts` or `admin-api.ts`.
4. Put shared fetching, mutation, cache, and invalidation behavior in the
   resource's store.
5. Keep only presentation and ephemeral interaction state in the component.
6. Add focused behavior tests and run:

   ```bash
   npm --prefix web run check
   npm --prefix web run build
   python -m pytest -q tests
   npm --prefix web run test:e2e
   ```

Architecture tests in `tests/test_frontend_architecture.py` enforce the
single network gateway, generated-client route coverage, contract structure,
typed core boundaries, and the extracted coordinator components.
