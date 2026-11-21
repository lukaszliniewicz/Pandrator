# Pandrator 0.15.1 changes

Changes since [0.15.0](release-notes-0.15.0.md). This maintenance release integrates Patrick Nadeau's work in PRs [#170](https://github.com/lukaszliniewicz/Pandrator/pull/170), [#173](https://github.com/lukaszliniewicz/Pandrator/pull/173) and [#174](https://github.com/lukaszliniewicz/Pandrator/pull/174), answers his reports [#171](https://github.com/lukaszliniewicz/Pandrator/issues/171) and, in part, [#169](https://github.com/lukaszliniewicz/Pandrator/issues/169) and [#172](https://github.com/lukaszliniewicz/Pandrator/issues/172), and updates locked packages that had published security advisories.

## Speaker plans

Since 0.15.0 the generation start and regenerate refuse a speech-plan revision that lost the speaker plan its lineage adopted. Three more paths now check, and a fourth is closed for the time being.

- The workflow's audio stage, its preflight and recast refuse such a revision when a spoken block would fall back to the narrator. Without an adopted plan a block is still voiced from its own speech markup, its block voice or its speaker label, so only blocks with none of these are refused. The message names them (the first five, then "and K more") and the earlier plan. A revision whose spoken blocks all carry their own speech runs as before, which includes a cast audiobook made from a reviewed speech-optimised narration ([#170](https://github.com/lukaszliniewicz/Pandrator/pull/170)).
- With performance on, the error for a missing performance plan names the plan the lineage adopted, as the speaker plan refusal does ([#170](https://github.com/lukaszliniewicz/Pandrator/pull/170)).
- An explicit timing repair (`POST /api/v1/generation-runs/{run_id}/repair-timing`, `pandrator_repair_generation_timing`) is refused, preview included, for a voiceover generated with casting or performance directions. A repair replaces speech blocks, and the speaker plan is reviewed block by block, so the repaired blocks would have been voiced without it. The automatic second pass already skipped such voiceovers; the explicit request added in 0.15.0 did not. Split the affected blocks in the speech plan, review the speaker plan and regenerate them instead ([#169](https://github.com/lukaszliniewicz/Pandrator/issues/169)).

Regrouping and re-preparing a cast project's plan, the other cases in [#169](https://github.com/lukaszliniewicz/Pandrator/issues/169), are not changed by this release.

## Long cast books

- A recast preview (`POST /sessions/{id}/generation-recast/preview`, `pandrator_preview_recast`) worked out the same cast assignment again for every speaker span of every block. It now works out each distinct assignment once per call. On a 5,167-segment cast audiobook with every segment recorded, the preparation fell from about 15.7 s to about 8.4 s. A recast start runs the same preparation, so both answer about twice as fast. The result, `selection_hash` included, is unchanged ([#174](https://github.com/lukaszliniewicz/Pandrator/pull/174)).
- While the Cast panel is open, a change of service, model or revision waits 750 ms and only the last one sends a comparison, instead of one per change that the server kept working on after the browser had dropped it. Save & regenerate no longer runs a panel comparison beside the recast dialog's own ([#173](https://github.com/lukaszliniewicz/Pandrator/pull/173)).

## Windows

- With `PANDRATOR_SECURITY_LOG` set to a file path, the security event sink reported itself enabled, created an empty file and dropped every event, because Windows cannot restrict the file to its owner. The file sink now stays off on Windows, and on any platform that cannot restrict the file, and startup says so once in the log, pointing to `PANDRATOR_SECURITY_LOG=stdout`. A relative path is refused in the same way. On other platforms startup now also logs where security events go. The path itself is never logged ([#171](https://github.com/lukaszliniewicz/Pandrator/issues/171)).
- The file-name stem that exports take from the project name is capped at 80 characters. A project name may be 255 characters, which alone could exceed the 260-character path limit. A name that would leave a Windows device name after trimming, such as "CON-", is prefixed as other reserved names are. The check of the data folder's length at startup and the clearer error remain open in [#172](https://github.com/lukaszliniewicz/Pandrator/issues/172).

## MCP

MCP 0.8.1.

- When a workflow plan needs the speech plan prepared first (`generation_preflight_required`), the MCP client now returns the application's reason as a state conflict. Before, it arrived as "The Pandrator resource changed since it was inspected", which sent an agent to re-read instead of preparing the plan.
- The dubbing and voiceover guide describes the timing repair refusal (guide revision 7), and the narration reference describes the speaker plan refusals of the workflow stage and recast.

## Dependencies

Fourteen packages in the locked environment are updated to releases that fix published security advisories: aiohttp 3.14.5, filelock 4.2.12, httpcore2 2.16.2, httpx2 2.16.2, joserfc 1.8.0, multidict 7.2.1, opentelemetry-api 1.45.2, pyasn1 0.6.5, PyJWT 2.15.1, setuptools 84.2.1, soupsieve 4.1.3, urllib3 2.8.0, Werkzeug 3.1.9 and yarl 1.25.1. Nothing else in the lock moves, and `pip-audit` reports no known advisory in the refreshed environment. Werkzeug, urllib3, joserfc and PyJWT sit on the request and sign-in paths, which matters for an installation reachable from the internet.

The Manager is unchanged at 0.9.32.
