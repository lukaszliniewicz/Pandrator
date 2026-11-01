# Solo use and shared workspaces

Pandrator starts in **Solo** mode. You can work privately without creating team
accounts or shared workspaces. For a new team installation, finish the normal
installation, then enable collaboration using the same steps as an existing
Solo installation. Installing an update does not enable collaboration.

## Three different places

| Name | Purpose |
| --- | --- |
| Installation directory (Manager's `--workspace`) | Filesystem location containing runtimes, data, models and Manager state. It is not a team or a permission boundary between people. |
| Personal workspace (**My workspace**) | One person's private projects, folders and settings. |
| Shared workspace | Team-owned projects and folders. All Members can create, edit, run and export ordinary workspace content. |

Folders organize projects **inside one application workspace**. They do not
add a second sharing or permissions hierarchy. A private folder is not a shared
workspace, and putting something into a folder does not invite another person.

## Enable collaboration, now or later

1. Open **Application settings → Manage users** as the initial Administrator. Issue a
   password setup link for your own account. Open it yourself, set a password,
   and sign in again. A Manager-launched browser alone is not a password.
2. Open **Application settings → Enable collaboration**. Read the explanation, select the
   acknowledgment and enable collaboration. You must be signed in as an
   Administrator, and the initial Administrator account must have a password.
3. Use **Manage users** to create a separate account for each person. The default
   instance role is **User**. Issue their password setup link and send it
   privately to that person. Links expire and can be used once; the dialog shows
   the expiry. Issue a replacement if necessary.
4. Each person opens their own link, sets a password, then signs in with their
   login name and password. Password setup does not itself sign them in.

You can create accounts before enabling collaboration, but they cannot use
named sign-in while Solo is active. A teammate who has already set a password
must wait for enablement; the Solo sign-in screen still asks for the Owner
password. Never give them the Owner's password or a Manager/MCP bearer instead.

Existing projects and folders stay in the initial user's personal workspace.
Nothing is automatically shared or copied. If an old browser lacks permission
to create or join workspaces after enablement, sign out and sign in again.

## Instance roles and workspace roles

| Role | Authority |
| --- | --- |
| Instance **Administrator** | Manage accounts, roles, password-reset links and installation/model acquisition. It does not automatically grant access to another person's private projects or a workspace the Administrator has not joined. |
| Instance **User** | Work with authorized personal/shared content and use available processing services. A User cannot trigger installation or download of a missing model. Ask an Administrator to acquire it. |
| Workspace **Member** | Create, edit, organize, process and export ordinary content throughout that shared workspace. No separate per-project edit grant is needed. |
| Workspace **Manager** | The same content rights, plus workspace membership and management. A workspace Manager can still be an instance User. |

Only grant the instance Administrator role when installation/account
administration is needed. Team editing does not require it. Resetting a password
or changing security authority invalidates affected credentials; the person
should sign in and re-enroll their assistant when necessary. Disabling an account
blocks its access while retaining its work.

## Start a shared project

Open **Workspaces & invitations**, create a workspace, then invite an existing
account as Member or Manager. The recipient signs in and accepts the invitation
from their inbox. Invitations expire after seven days; pending invitations can
be revoked. A workspace Manager can issue a new invitation if needed.

Choose the shared workspace in the workspace selector before creating a project
or folder. New work belongs to that workspace. Other Members can continue it
even if its creator leaves. Keep private work in **My workspace**.

Creating a project while viewing a folder places the whole project, including its
language versions, in that folder. The creation dialog keeps the workspace and
folder you opened it from. If that folder changes or is removed before creation,
reopen the dialog from the current folder. Creating from **All projects** or
**Unfiled** leaves the new project Unfiled.

To leave a workspace, another active Manager must remain. Promote an appropriate
Member first. Leaving removes your access, including when you are an instance
Administrator; the content stays with the workspace. If disabling an account
leaves a workspace without an active Manager, an instance Administrator can use
workspace recovery to promote an existing active Member. Recovery does not join
the Administrator to the workspace or grant access to its content.

## Bring existing private work into a team

Open the private project and use **Copy to workspace**. Choose the
destination, review the preview and give the working copy a useful name. If the
project needs destination provider connections, select them explicitly. Private
credentials and personal libraries are not silently shared; unresolved or
unsupported state is reported before copying.

The copy is a distinct project owned by the destination workspace. Its current
supported project state and selected dependencies are copied; future changes do
not synchronize with the original. The original remains private and unchanged.
A copy starts **Unfiled**; choose a destination folder after copying. Personal
folders do not become shared folders through this action.

For read-only access to a private project, use **Can view** for an existing
account. This allows authorized viewing and completed-output access; it does
not grant editing, processing or creation of new exports. The recipient must
approve access for their assistant separately. Revoke the view grant when it is
no longer needed. Use a shared workspace for collaborative editing.

## Assistants, hosting and recovery

Each teammate should enroll their own assistant through browser PKCE approval,
selecting only the needed workspaces/projects and scopes. Do not share the
installation's managed HTTP bearer or Owner credential. See
[assistant enrollment](../operations/agent-connections.md#teams-and-named-accounts)
and the [MCP collaboration guide](../../pandrator_mcp/guides/collaboration.md).

Enabling collaboration does not change bind addresses, open a public listener or
configure HTTPS. A team on other computers needs a separately configured stable
application origin, trusted hosts and HTTPS/proxy settings. Follow
[remote and headless operation](../operations/remote-and-headless.md).

Before a major update, preserve a consistent backup of the application database,
managed media and secrets together, with the matching application version. Use
the supported maintenance/backup procedure; copying only a live SQLite database
or restoring a database over newer media is not a complete recovery set. See
[updates, data and repair](../operations/updates-data-and-repair.md).
