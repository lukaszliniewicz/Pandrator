# Durable work and failure diagnosis

Long-running application jobs and Manager operations are durable work. Every
work item has a typed ID, normalized state, progress, cancellation capability,
and polling interval. Application work uses type `job`; host-management work
uses type `manager_operation`.

The MCP work projection deliberately omits raw queue payloads. Logs are bounded
and redacted. They include operational events and safe identifiers, not source
documents, prompts, credentials, arbitrary Manager task inputs, or raw results.

To diagnose work:

1. inspect the work item;
2. read its bounded log;
3. distinguish retryable infrastructure failures from validation or revision
   failures;
4. inspect current session, provider, or Manager state; and
5. explain a safe next action before mutating anything.

Queued, running, waiting, succeeded, failed, and cancelled are the normalized
states. Cancellation is a request and may take time while a task reaches a safe
boundary. Retrying or re-executing must use the idempotency key associated with
the exact reviewed action.

## Subtitle dispatch runs

Subtitle correction and translation dispatch is passive pull work. Create a
dispatch run, then claim and process one batch at a time; the server does not
push source text to the MCP sidecar. Run list/get responses contain metadata
only. A claim is the disclosure boundary for that batch's canonical task,
source cues, timing policy, bounded boundary context, and short-lived lease
capability. The
authoritative source is `batch.cues`; cue text and optional timing each occur in
one place. Boundary context is evidence only.

Keep the returned `lease_token` scoped to the matching batch ID. Renew it when
model work needs more time, or release the batch when abandoning it so another
worker can claim it. Lease expiry or a stale lease is a state conflict, not a
reason to submit with a different batch ID. Use idempotency keys for claim,
renew, release, and submit, retrying a logical request with the same key.

Submit a typed correction or translation `result`; raw `response_text` is a
legacy adapter path. Accepted batches advance to the next sequential claim. A
rejected result remains repairable under its valid lease. The final accepted
batch automatically finalizes the run. If transient materialization trouble
leaves it `finalizing`, retry the same final submission and idempotency key.
