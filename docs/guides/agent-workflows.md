# End-to-end workflows with an agent

Pandrator's MCP sidecar is designed for requests that describe an outcome,
not a pile of API calls. A capable agent can start from a file on its host,
coordinate Pandrator's durable processing, perform passive language work
itself, and return verified deliverables without placing media bytes,
credentials, or server paths in model context.

A representative request is:

> The latest course session is in Downloads. Transcribe it, correct the English
> subtitles, translate them into German, prepare the German for speech, generate
> the voiceover with an appropriate configured voice, and give me a dubbed video
> with burned German subtitles plus an original-audio version with corrected
> English subtitles.

Provider, model, and voice names can be included as preferences. They are not
hard-coded workflow requirements: the agent resolves them against the target's
live catalog and should ask before making a materially different substitution.

## One workflow, three boundaries

The agent works across three deliberately separate boundaries:

1. **The MCP sidecar host** owns approved local source and output directories.
2. **Pandrator** owns sessions, uploads, artifacts, revisions, plans, jobs, and
   export contracts. It may run locally or at a fixed remote target.
3. **The MCP host model** performs passive correction, translation, document
   cleanup, or speech optimization only after it claims a bounded packet.

The operator configures filesystem roots and the target connection. The model
cannot supply an absolute path, target URL, credential, upload chunk size, or
download transport option.

## Prepare the sidecar once

Expose only directories an agent is allowed to browse, under names that are
meaningful without revealing their absolute paths. Also select one output
directory. Exact commands and platform-specific paths are in the
[Pandrator MCP guide](../../pandrator_mcp/README.md#approved-local-files-and-outputs).

For a remote Pandrator instance, files are still “local” to the sidecar host.
The sidecar streams them to the fixed target through Pandrator's authenticated,
resumable upload API. File bytes do not pass through the language model.

An end-to-end media workflow normally needs `app.read`, `app.write`, and
`app.run`. Add `app.cancel` only when the agent should be able to cancel work.
Target status reports requested and actually granted scopes so missing
authority is visible before a long workflow begins.

## What the agent should do

### 1. Discover before mutating

The agent starts with recommendations, target status, capabilities, and the
matching packaged guide. It should inspect existing sessions before creating a
duplicate. For a new local file it lists approved root names, browses one root,
and selects an exact relative entry returned by the sidecar.

Source import is automatically resumable. Pandrator first reuses an identical
managed source when its size and SHA-256 match; otherwise the sidecar uploads
missing chunks and completes the immutable source. Attachment is protected by
the session revision the agent inspected.

### 2. Use plans for native processing

Transcription, audio generation, and export use Pandrator's normal immutable
plan-and-execute contract. A plan records stages, settings, selected revisions,
provider disclosures, confirmations, and expiry. The agent executes only the
exact digest it reviewed, then polls the returned durable work handle until it
is terminal.

If a source, setting, provider, or selected artifact changes, the plan becomes
stale. The correct response is to inspect and plan again, not to bypass the
revision check.

### 3. Pull passive work sequentially

For subtitle correction, subtitle translation, PDF/EPUB cleanup, or speech
optimization, Pandrator can act as a passive dispatcher. It does not call an
extra model provider. The model already running in the MCP host claims one
batch, performs the requested work, submits every required stable ID exactly
once, and repeats until the run materializes a final artifact.

Claims are the content-disclosure boundary. Run listings contain metadata only.
Boundary context is read-only; it must not be returned as extra work. Leases can
be renewed for slow batches or released when stopping. See
[passive processing](passive-dispatch.md) for the exact contracts.

### 4. Resolve speech choices from the target

Before generation, the agent reads the live TTS catalog. It matches the chosen
service, model, language, and provider-native or registered managed voice,
then updates the current TTS settings revision. A voice display name is not
necessarily the provider voice ID, and a managed voice is selectable only when
its registration for that service is ready.

Natural-language style instructions can be saved with the TTS selection, but
the agent should keep them specific to the material: for example, clear course
narration, restrained emphasis, natural pauses, and faithful pronunciation.
It should generate and review representative takes before committing time and
cost to a long run.

### 5. Export each deliverable explicitly

After generation, the agent lists generation runs and selects a completed run
instead of assuming the newest entry is correct. Each requested output gets its
own typed export plan. This keeps choices such as original, mixed, or
dubbing-only audio; source, translated, or dual subtitles; and soft or burned
subtitles visible in the artifact lineage.

When each export job finishes, the agent lists its artifacts and downloads the
requested immutable output. Downloads resume from a verified partial file,
check size and SHA-256 metadata, and appear atomically inside the approved local
output directory. The final response should report both the Pandrator artifact
ID and the usable local path.

## Recovery and retries

The workflow is designed to survive an interrupted agent turn:

- retry the same logical mutation with the same idempotency key;
- inspect upload state and continue only missing chunks;
- replaying upload completion returns the original artifact result;
- inspect durable jobs instead of starting replacements blindly;
- renew or release a passive batch lease deliberately;
- re-plan after a stale-plan or revision conflict; and
- resume an artifact download instead of restarting it.

Expected failures are returned as MCP tool errors with stable Pandrator codes,
details, and retryability. Authentication and scope errors therefore remain
distinguishable from validation, conflict, or temporary availability failures.

## Quality checks that should not be skipped

End-to-end automation removes clerical work, not editorial judgment. Before
delivery, verify that:

- transcription language and timing are plausible;
- correction preserved meaning and did not silently omit cues;
- translation is natural for speech as well as faithful to the source;
- speech optimization did not translate, summarize, merge, split, or invent;
- representative generated takes fit the intended language, voice, and style;
- the selected generation run is the reviewed one; and
- each exported file contains the intended audio and subtitle combination.

For the underlying data shapes and parameters, continue with the
[subtitle pipeline reference](../reference/subtitle-pipeline.md),
[speech-optimization reference](../reference/speech-optimization.md), and
[document-ingestion reference](../reference/document-ingestion.md).
