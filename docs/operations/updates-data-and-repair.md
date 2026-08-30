# Updates, data, repair, and removal

Pandrator Manager treats application and service runtimes as replaceable,
versioned components. Projects, generated media, configuration, and Manager
state live outside those runtime slots. This separation makes an update or
repair safer, but it is not a substitute for a backup.

## Know the workspace

One Manager instance owns one explicit workspace. Native launchers create it
as `<selected parent>/Pandrator`; command-line operations should pass the same
workspace deliberately. Avoid copying individual runtime directories between
workspaces or editing active-slot metadata by hand.

The workspace contains application and component runtimes, caches, Manager
state, and the configured Pandrator data location. Data and replaceable
runtimes have different ownership and uninstall behavior.

## Review updates

Manager can check Pandrator and installed local components. Use **Review
updates** to construct one plan or inspect an individual component action.
Before execution, the plan reports affected components, downloads, disk and
path checks, runtime impact, and required confirmation.

Components are downloaded to staging, checked, and activated side by side. A
failed activation should leave the previous working slot available. Do not
manually delete the old slot while an operation is active.

For a native installation, downloading and running a newer Manager executable
or AppImage updates the entry point. It should discover the remembered
workspace; if prompted, select the same parent directory as before.

## Jobs survive the browser

Closing a browser tab does not stop transcription, generation, assembly,
installation, or another durable operation. Reopen Pandrator or Manager and
inspect the active work. Starting the same expensive action again can create a
second job rather than “wake up” the first one.

Use cancellation only when the operation reports it is cancellable. A process
or service can require a short cleanup phase after cancellation.

## Repair before reinstalling

Repair is appropriate when a managed runtime is incomplete, its environment is
damaged, a service fails readiness checks, or an activation was interrupted.
Review the repair plan: it should replace owned runtime material while
preserving user data. If repair repeatedly fails, download diagnostics before
removing anything.

For exact CLI plans, operation records, service control, and recovery access,
use the [Manager guide](../../pandrator_manager/README.md).

## Backups

Keep an independent backup of important projects before a major update,
storage migration, or uninstall. Stop active writes or make a consistent
application-level export before copying a data store. A filesystem snapshot is
only useful when all related files and databases represent one point in time.

Voice references, custom models, provider configuration, and generated media
may live in managed data rather than the source checkout. Verify what your
backup includes by restoring a copy to a safe location.

## Uninstall choices

Manager removal is plan-first. User data is preserved by default. Exporting
data, preserving it in place, and purging it are deliberately distinct
choices; purge requires its own destructive confirmation. An export refuses to
overwrite an existing destination and is verified before removal continues.

Read the proposed paths. The Manager should remove only files owned by the
selected installation. If the reported workspace or data root is not the one
you expect, stop rather than “trying it to see what happens”—a philosophical
method best reserved for soup, not storage.

## Safe maintenance checklist

- Confirm the workspace and data root.
- Let active jobs finish or cancel them cleanly.
- Review the exact update, repair, or uninstall plan.
- Check available disk space and download integrity.
- Back up irreplaceable projects and voice material.
- Keep the previous working runtime slot until activation succeeds.
- Download and inspect diagnostics after a failure.
- Verify important sessions and one local service after maintenance.
