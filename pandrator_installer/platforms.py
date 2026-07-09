"""Small platform helpers for installer path and runtime decisions."""

import os
import platform


WINDOWS_PIXI_DOWNLOAD_URL = (
    "https://github.com/prefix-dev/pixi/releases/latest/download/"
    "pixi-x86_64-pc-windows-msvc.exe"
)
LINUX_X86_64_PIXI_DOWNLOAD_URL = (
    "https://github.com/prefix-dev/pixi/releases/latest/download/"
    "pixi-x86_64-unknown-linux-musl"
)
LINUX_AARCH64_PIXI_DOWNLOAD_URL = (
    "https://github.com/prefix-dev/pixi/releases/latest/download/"
    "pixi-aarch64-unknown-linux-musl"
)


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


def _looks_like_install_root(path):
    return (
        os.path.isfile(os.path.join(path, "config.json"))
        and os.path.isfile(os.path.join(path, "Pandrator", "main.py"))
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
    resolved_system = normalized_system(system)
    resolved_machine = normalized_machine(machine)

    if resolved_system == "windows" and resolved_machine in {"x86_64", ""}:
        return WINDOWS_PIXI_DOWNLOAD_URL

    if resolved_system == "linux":
        if resolved_machine in {"x86_64", ""}:
            return LINUX_X86_64_PIXI_DOWNLOAD_URL
        if resolved_machine == "aarch64":
            return LINUX_AARCH64_PIXI_DOWNLOAD_URL

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
