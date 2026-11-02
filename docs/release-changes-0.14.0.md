# Pandrator 0.14.0 changes

Changes since [0.13.0](release-notes-0.13.0.md). For downloads and a short overview,
see the [release notes](release-notes-0.14.0.md).

## Work alone or with a team

Solo remains the default. An Administrator can enable individual accounts and
shared workspaces. Members can edit, process and export team projects; workspace
Managers also manage membership. Existing projects stay private until their
owner shares viewing access or copies them into a workspace. Assistants enroll
separately for each person. See [collaboration](guides/collaboration.md).

Optional project links are off by default. Owners and workspace Managers can
issue expiring links for project management or review of one selected translation.
Reviewers submit proposed corrections; an editor compares and accepts them.
Acceptance checks for intervening changes and retains the original revision.
Visitors use separate credentials. See [project links](guides/project-links.md).

Hosting settings show the effective origin and protection settings. Remote
sign-in gains per-account backoff, optional security logs and proxy, Fail2Ban
and CrowdSec examples. Uploaded document previews are sandboxed. Hosting and
sharing remain explicit choices; enabling collaboration does not expose a
listener. See [public hosting](operations/public-hosting-protection.md).

## Review and direct speech

Review windows have collapsible tools, independent scrolling and phone drawers.
Long text previews are paged. Drafts and pending saves receive consistent
protection across text, delivery and PDF editing. See
[reviewing and previewing work](guides/review-and-preview.md).

**Speakers & delivery** adds an annotation workspace for manual edits and AI or
MCP drafts, including word selections, speaker assignments and phrase directions.
Model limits remain visible. Narration can pack whole sentences or clauses;
the whole-sentence threshold defaults to 300 characters. Automatic targets are
350 characters for Qwen and 540 for Drama 3 and Kitten TTS 2, with shorter Drama
targets for CJK and reduced output budgets. Provider limits still apply. See
[narration lengths](reference/narration-segmentation.md).

Workflow cards show elapsed time, metered cost and an approximate time remaining.
Project tools move below the interface switch, with a Tools menu on phones.

## Local speech models

audio.cpp moves to **0.9.3**. New speech routes include Phonon, Ming 3.0 TTS,
XTTS v2 and experimental Ming 3.0 Audio. XTTS requires a WAV reference and its
model licence restricts use to non-commercial purposes. Ming's conditioning modes
are checked before generation.

New Kitten checkpoints list sixteen languages and 124 voices; older checkpoints
retain ten languages and 48 voices. Saved legacy Kitten Q8 selections remain
pinned. Irodori MeanFlow defaults to four inference steps. Singing models and
hardware-specific model components stay out of narration choices.

Drama 3 translates speech directions and vocal events into its native controls.
Cast speakers render as separate parts. Native multi-speaker requests, infill
and previous-take conditioning are not integrated. Optional disk-backed experts
reduce resident memory at the cost of speed; even the small model is large,
and an 8 GiB GPU is not guaranteed to fit it.

Saved model and voice choices are preserved. Updated package definitions can
require replacement weight downloads. See [providers and voices](guides/providers-and-voices.md)
for licences, supported controls and compute requirements. Linux CUDA remains
best-effort without local NVIDIA hardware verification.

## Exports and background work

Export contents remain visible when settings collapse. Subtitle exports preserve
body text and cue timing, retain display-only labels independently of speech
exclusions, and report conflicting display targets. Video exports omit inherited
chapters and report measured durations. Saving an unchanged workflow retains its
selected translation.

Assembly and ordinary audio/video exports can wait for estimated working space
and resume the same job after space becomes available. Source files and accepted
recordings are retained. One managed worker remains the default; explicitly
setting `PANDRATOR_WORKER_COUNT=2` allows independent jobs to overlap. Shared
endpoints, GPU resources and conflicting project work remain serialized. See
[troubleshooting](operations/troubleshooting.md).

## Updates and integrations

Application activation preserves configured ports and HTTPS/proxy exposure and
handles nested installation roots. The ownership migration avoids repeated
lookups when upgrading a large usage history.

Preview GET and HEAD requests only read existing media. Direct clients use POST
to prepare a missing preview; the browser handles this automatically. Direct HTTP
job submission and remote CLI enqueue accept validated diagnostic and session
bundle commands; processing jobs use their workflow-specific endpoints.

Pixi moves to 0.83.0. Python and frontend dependencies receive compatible updates,
including available security fixes. Frontend builds use Node 24 and retain
SvelteKit 2 and TypeScript 5. See [updates and repair](operations/updates-data-and-repair.md)
and the [MCP guide](../pandrator_mcp/README.md) for operational details.
