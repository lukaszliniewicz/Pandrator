# Next release

- Optional public project links are disabled by default in Application settings. Owners and workspace Managers can issue expiring project-management or pinned translation-review links; submitted corrections have a paged UI/MCP review and explicit, conflict-checked acceptance. Visitor credentials are separate from normal sign-in. See [project links](guides/project-links.md).
- Public-hosting status, per-account sign-in backoff, optional trusted-address JSONL security events and example fail2ban/CrowdSec/proxy configuration make remote operation easier to inspect. See [hosting protection](operations/public-hosting-protection.md). No CAPTCHA or external protection service is required by default.
- Artifact documents are sandboxed so uploaded web content cannot run with a visiting account's application-origin authority. Project-bound source replacements can stage small or large uploads before impact review and confirmation.

- Review and preview workspaces use collapsible tools with independent scrolling and phone drawers. Draft and pending-save protection is consistent across narration, delivery and PDF editing; large text previews are paged. See [the review guide](guides/review-and-preview.md).

- Independent jobs can use two managed workers when explicitly enabled with
  `PANDRATOR_WORKER_COUNT=2`; one remains the default. Shared endpoint, GPU and
  session resources remain serialized. Caption finalization prepares files and
  lineage outside SQLite writers, with bounded retries for safe SQL mutations
  and strict job/resource lease fencing.
- Export contents stay visible with settings collapsed, including soundtrack,
  caption and audio-version choices. Selected subtitle revisions can retain
  display-only labels independently of speech exclusions. Subtitle formatting
  preserves body text and cue windows, and reports conflicting display targets.
- Webinar video exports omit inherited chapters and report measured video,
  audio and container durations. Inactive mix preferences reuse source/voiceover
  soundtrack masters. First saving an unchanged workflow retains its selected
  translation; actual input changes still invalidate that selection.
- Assembly and ordinary media/audio exports wait on insufficient estimated
  working space, retain accepted inputs and recordings, and resume the same
  job after capacity returns. Storage waits expose their next check and release
  compute resources; timestamps use explicit UTC across reloads.
- Caption alignment records source-relative boundary offsets and a bounded
  outlier list after fallback. These diagnostics supplement coverage and timing
  quality; they do not change alignment acceptance or timestamps.

- Solo remains the default. Administrators can enable named accounts and shared
  workspaces; Members create/edit/process/export team content, while workspace
  Managers govern membership. Private projects remain private until explicitly
  shared for viewing or copied. See [the collaboration guide](guides/collaboration.md).
- Administrator MCP user listing preserves enabled account access; workspace
  tools disclose complete required scopes and enrollment guidance. Per-person
  PKCE enrollment and workspace approval are documented for assistants.
- Signed application activation honors nested application roots during installed
  status checks and retains the configured port and HTTPS/proxy exposure.

- Media preview GET and HEAD requests only read existing previews, waveforms or
  active jobs. Direct clients use POST on the same path to prepare a missing
  derivative, with current read/run permissions and browser CSRF protection.
  The application prepares an authorized cache miss automatically through POST.
- Direct HTTP job submission accepts worker diagnostics (`noop`) and session
  bundle exports through validated commands. Other jobs use their workflow,
  generation, voice, sound or model endpoint. This also applies to remote CLI
  `job enqueue`; trusted local CLI queue operations remain available. Session
  bundle admission checks current project permissions and supports retry keys.
- Project tools move below the interface switch, with a mobile Tools menu.
  Narration settings are grouped in one expandable overview section with
  contextual help. Short-dialogue recognition remains the default for new projects.
- Speakers & delivery has a dedicated annotation workspace for manual edits and
  AI or MCP draft review, including selected-word speaker assignments, phrase
  directions, annotation previews and guarded saves.
- Simple and Advanced workflow cards show elapsed time, metered cost and an
  approximate time remaining based on the current run. Activity actions sit
  below their progress bar.

- Narration packing has a configurable whole-sentence threshold, defaulting to
  300 characters, plus explicit whole-sentence and clause-packing choices.
  An oversized sentence retains the existing splitting fallback and length
  limits; whole-sentence mode keeps its pieces separate from nearby sentences.
- Qwen's automatic narration target is 350 characters. Custom lengths remain
  available, and lower output-token budgets still reduce the automatic target.
- audio.cpp moves to `0.9.2-drama3-base-small` with verified runtime pins, five
  Drama 3 packages, Danish Plapre 3, and updated Kitten TTS 2 variants. Saved
  selections for the old Kitten Q8 package retain their original ID and weights.
- Drama 3 translates speech XML and legacy pSSML into inline directions,
  model-specific vocal events and paired phrase controls. Cast speakers render
  as separate voice parts; native multi-speaker dialogue, prior-take conditioning
  and infill remain unavailable. Model response and timing require listening checks.
- An optional Drama 3 disk-expert setting lowers resident expert memory at the
  cost of speed. It does not guarantee an 8 GiB GPU will fit. Existing model,
  voice and Kokoro Japanese choices remain preserved during updates.
- Automatic narration targets increase to 540 characters for Drama 3 and Kitten
  TTS 2, independently of GPU memory. Drama uses 270 for CJK and scales down for
  explicitly reduced output budgets. Enforced model limits still apply; Plapre
  keeps its 270-character target because longer forced chunks may omit text.
