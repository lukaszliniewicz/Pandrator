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
