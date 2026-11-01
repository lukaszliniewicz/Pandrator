# Project links and translation review

Link sharing is optional and disabled in a default installation. Named accounts and shared workspaces remain the usual choice for an ongoing team. A link is useful for a temporary project collaborator or an external translation reviewer who does not need an account.

## Enable sharing

As an installation Administrator, open **Application settings → Web hosting and public sharing (optional)**. Configure the actual HTTPS listener/proxy first, then enable public links for that same application origin. Loopback HTTP is supported for local testing. Enabling sharing does not open a port or configure DNS, TLS, a proxy or firewall.

This setting works with Solo and collaboration installations. Hosting information in the panel shows the effective origin, secure-cookie state, trusted hosts and proxy-hop count. See [remote hosting](../operations/remote-and-headless.md) and [optional server protections](../operations/public-hosting-protection.md).

## Create a link

Open the project and choose **Share → Access links**. Only the personal project owner or a shared-workspace Manager can issue or revoke links. Give each recipient or purpose a clear label, choose access and set an expiry between one and thirty days; the default is seven.

| Access | Recipient can do |
| --- | --- |
| Manage project | Edit and process this project, manage its language versions and export results using its configured resources. This can spend money through configured providers. Installation administration, accounts, model acquisition, private workspace libraries, further sharing and creating unrelated projects remain outside the grant. |
| Review this translation | Read the selected translation in the currently open language version, correct passage text and submit changes for project-editor review. Timing and speakers are read-only; source media and other project files are not included. |

The complete URL is shown only once. Copy it and send it privately. Anyone holding it can use its stated authority until expiry or revocation. It cannot be recovered from the issued-links list; revoke and replace it if necessary. Use a separate link for each intended recipient, and do not put capability URLs in public logs or documents.

The **Named account access** action retains the existing Can view workflow; it requires collaboration and an existing account. It does not turn a viewing grant into an editing role.

Project-management visitors can upload new files, including large files through
the normal chunked upload protocol. **Replace source** first stages the candidate
inside this project and shows the existing impact preview. Uploading the
candidate does not itself replace the current source. Confirm the replacement
or cancel it; creating an unrelated project from that dialog requires an ordinary
account.

## Review a translation

Open the complete link and select **Open shared work**. The secret is removed from the address bar before API requests. Review does not sign you into the owner's account or expose installation controls.

Edit passage text and move through the pages. Changes across pages remain in the draft until you finish and choose **Submit corrections**. A passage may correspond to several displayed subtitle cues; the owner's existing subtitle profile controls the final formatting. A supplied name is an unverified review label.

A successful submission creates pending corrections. It does not change the selected subtitles, generate speech or publish output. After submission you receive a confirmation and can leave or close the window. Failed submissions retain the draft for retry. Leaving through the application with unsaved changes offers submitting, discarding or continuing to edit; closing the browser window uses its native unsaved-changes warning.

Visitor sessions have shorter lifetimes than the sharing link. While an edited review is visible, a lightweight identity check keeps an active session alive within its absolute limit. If access expires, the review can request the original complete link again without clearing the draft. **Download unsaved draft** provides a local JSON copy for recovery; it contains corrections and source identifiers, not the sharing secret. A revoked or expired link requires the owner's help.

## Inspect and accept corrections

Project editors open **Share → Submitted corrections**; a workspace Member without sharing-management rights can open **Submitted corrections** directly. Filter pending, accepted or rejected work, open **Review changes**, and page through the before/after text.

**Accept and use reviewed subtitles** creates a new immutable reviewed translation and selects it for output. It preserves the original revision. The server checks that the source hash, document head, subtitle settings and selected translation still match the inspected base. If another person changed them, the submission remains pending and reports a conflict. Compare the corrections with the current version before applying them manually or requesting a new review link.

**Reject submission** records a disposition without changing the translation. If the pinned source was removed, saved corrected text remains available but acceptance is blocked. Revoking a link or disabling external sharing retains submitted work for authorized project editors.

## Revoke access

Use **Revoke** beside an issued link. Existing visitor credentials become invalid on subsequent requests. Turning link sharing off in Application settings revokes all links and visitor sessions; re-enabling requires new links. Admitted processing follows normal project job authorization and cancellation rules; access revocation does not silently delete completed work or discard accepted jobs.

Visitors use separate scoped browser credentials. Leaving shared access preserves any normal account login in the same browser. Opening another project link does not grant access to unrelated projects.

## MCP workflow

The application must be updated to a build supporting these tools. The assistant stays bound to its configured Pandrator target; a capability URL is not a replacement for its enrollment credential.

1. An Administrator can inspect `pandrator_get_public_sharing_settings`. Changing the opt-in requires an Administrator enrollment with `app.admin`, or the Administrator can use Application settings.
2. An authorized project owner/Manager lists links with `pandrator_list_project_links` and creates one with `pandrator_create_project_link`. Choose `manage_project` or `review_translation`; the latter requires the exact selected translation artifact. Supply a stable request UUID for retries and handle the one-time URL privately.
3. Use `pandrator_list_link_submissions`, then `pandrator_get_link_submission` to inspect bounded before/after pages. Review labels are unverified. A `source_unavailable` result contains retained corrected text but cannot be accepted.
4. Use `pandrator_review_link_submission` with `action=accept` or `reject`, the inspected submission revision and a stable idempotency key. Acceptance additionally requires the source hash, document revision and selected artifact. Refresh on conflict; do not blindly replace the expected values to force stale work through.
5. Revoke with `pandrator_revoke_project_link` at the current link revision. Existing credentials and original files are not a substitute for current project authorization.

MCP review and publication require ordinary project editing/review rights. A guest link cannot create more links, approve its own submission or acquire Administrator scope.
