"""Small platform helpers for installer path and runtime decisions."""

import json
import os
import platform
import tempfile


PIXI_VERSION = "0.72.0"
PIXI_RELEASE_BASE_URL = f"https://github.com/prefix-dev/pixi/releases/download/v{PIXI_VERSION}"
PIXI_DOWNLOAD_ASSETS = {
    ("windows", "x86_64"): {
        "url": f"{PIXI_RELEASE_BASE_URL}/pixi-x86_64-pc-windows-msvc.zip",
        "sha256": "dc3a55c204692ad38a52a8c745ff2a0d2e7a48fad2c0d2109f12a486cf8937c4",
        "archive_type": "zip",
        "member": "pixi.exe",
    },
    ("linux", "x86_64"): {
        "url": f"{PIXI_RELEASE_BASE_URL}/pixi-x86_64-unknown-linux-musl.tar.gz",
        "sha256": "2c086608809f7bdd9918323cf6f6278bb43b025f4d957ddfd55295cf151c6f21",
        "archive_type": "tar.gz",
        "member": "pixi",
    },
    ("linux", "aarch64"): {
        "url": f"{PIXI_RELEASE_BASE_URL}/pixi-aarch64-unknown-linux-musl.tar.gz",
        "sha256": "8b48fd8b315552ee48d340e89d654a177d1f001810ab741f51f7dcdd7e00e1c1",
        "archive_type": "tar.gz",
        "member": "pixi",
    },
}

LAUNCHER_SETTINGS_DIRNAME = "pandrator"
LAUNCHER_SETTINGS_FILENAME = "installer.json"


def normalized_system(system=None):
    value = (system or platform.system() or os.name).lower()
    if value in {"nt", "win32", "cygwin"}:
        return "windows"
    if value in {"posix"}:
        return "linux"
    return value


def normalized_machine(machine=None):
    value = (machine or platform.machine() or "").lower()
    if value in {"amd64", "x64"}:
        return "x86_64"
    if value in {"arm64"}:
        return "aarch64"
    return value


def is_windows(system=None):
    return normalized_system(system) == "windows"


def is_linux(system=None):
    return normalized_system(system) == "linux"


def is_appimage_environment(environ=None):
    values = os.environ if environ is None else environ
    return bool(values.get("APPIMAGE"))


def launcher_settings_path(system=None, environ=None, home=None):
    """Return the per-user installer settings path without creating it."""
    values = os.environ if environ is None else environ
    resolved_home = os.path.abspath(os.path.expanduser(home or os.path.expanduser("~")))
    if normalized_system(system) == "windows":
        config_root = values.get("LOCALAPPDATA")
        if not config_root:
            config_root = os.path.join(resolved_home, "AppData", "Local")
    else:
        config_root = values.get("XDG_CONFIG_HOME")
        if not config_root or not os.path.isabs(os.path.expanduser(config_root)):
            config_root = os.path.join(resolved_home, ".config")
    return os.path.join(
        os.path.abspath(os.path.expanduser(config_root)),
        LAUNCHER_SETTINGS_DIRNAME,
        LAUNCHER_SETTINGS_FILENAME,
    )


def load_remembered_launcher_workspace(system=None, environ=None, home=None):
    """Load a remembered workspace only while its parent directory still exists."""
    settings_path = launcher_settings_path(system, environ, home)
    try:
        with open(settings_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    workspace = payload.get("workspace")
    if not isinstance(workspace, str) or not workspace.strip():
        return None
    resolved = os.path.abspath(os.path.expanduser(workspace.strip()))
    return resolved if os.path.isdir(resolved) else None


def remember_launcher_workspace(workspace, system=None, environ=None, home=None):
    """Atomically remember the launcher's selected workspace for future runs."""
    resolved = os.path.abspath(os.path.expanduser(os.fspath(workspace)))
    if not os.path.isdir(resolved):
        raise ValueError(f"Installer workspace does not exist: {resolved}")

    settings_path = launcher_settings_path(system, environ, home)
    settings_dir = os.path.dirname(settings_path)
    os.makedirs(settings_dir, exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(
        prefix=f".{LAUNCHER_SETTINGS_FILENAME}-",
        suffix=".tmp",
        dir=settings_dir,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump({"workspace": resolved}, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, settings_path)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.remove(temporary_path)
        except FileNotFoundError:
            pass
        raise
    return settings_path


def _looks_like_install_root(path):
    return (
        os.path.isfile(os.path.join(path, "config.json"))
        and os.path.isfile(os.path.join(path, "Pandrator", "pyproject.toml"))
        and os.path.isfile(
            os.path.join(path, "Pandrator", "pandrator", "web", "static", "index.html")
        )
    )


def _looks_like_workspace(path):
    return _looks_like_install_root(os.path.join(path, "Pandrator"))


def infer_installed_workspace_from_cwd(cwd):
    """Return the installer workspace when launched inside an existing install."""
    if not cwd:
        return None

    current = os.path.abspath(cwd)
    if _looks_like_workspace(current):
        return current

    for candidate in (current, *list(_iter_parent_paths(current, max_depth=4))):
        if _looks_like_install_root(candidate):
            return os.path.dirname(candidate)

    return None


def _iter_parent_paths(path, max_depth=4):
    current = os.path.abspath(path)
    for _ in range(max_depth):
        parent = os.path.dirname(current)
        if parent == current:
            break
        yield parent
        current = parent


def resolve_launcher_workspace(value=None, system=None, environ=None, cwd=None, home=None):
    values = os.environ if environ is None else environ
    explicit_value = value or values.get("PANDRATOR_INSTALLER_WORKSPACE")
    if explicit_value:
        return os.path.abspath(os.path.expanduser(explicit_value))

    resolved_system = normalized_system(system)
    if resolved_system == "linux" and is_appimage_environment(values):
        remembered = load_remembered_launcher_workspace(
            system=resolved_system,
            environ=values,
            home=home,
        )
        if remembered:
            return remembered
        return os.path.abspath(os.path.expanduser(home or os.path.expanduser("~")))

    current_directory = os.path.abspath(cwd or os.getcwd())
    inferred_workspace = infer_installed_workspace_from_cwd(current_directory)
    if inferred_workspace:
        return inferred_workspace

    return current_directory


def pixi_binary_name(system=None):
    return "pixi.exe" if is_windows(system) else "pixi"


def pixi_temp_suffix(system=None):
    return ".exe" if is_windows(system) else ""


def pixi_download_url(system=None, machine=None):
    return pixi_download_asset(system, machine)["url"]


def pixi_download_asset(system=None, machine=None):
    resolved_system = normalized_system(system)
    resolved_machine = normalized_machine(machine)
    if not resolved_machine:
        resolved_machine = "x86_64"
    asset = PIXI_DOWNLOAD_ASSETS.get((resolved_system, resolved_machine))
    if asset is not None:
        return dict(asset)

    raise RuntimeError(
        f"Unsupported Pixi platform: system={resolved_system}, machine={resolved_machine}"
    )


def pixi_manifest_platform(system=None, machine=None):
    resolved_system = normalized_system(system)
    resolved_machine = normalized_machine(machine)

    if resolved_system == "windows":
        return "win-64"
    if resolved_system == "linux":
        if resolved_machine == "aarch64":
            return "linux-aarch64"
        return "linux-64"

    raise RuntimeError(
        f"Unsupported Pixi manifest platform: system={resolved_system}, machine={resolved_machine}"
    )


def pixi_env_python_path(env_root, system=None):
    if is_windows(system):
        return os.path.join(env_root, "python.exe")
    return os.path.join(env_root, "bin", "python")
