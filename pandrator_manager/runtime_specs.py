"""Resolved process specifications for Pandrator and first-party services."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .components.runtime_bootstrap import (
    KOKORO_MANIFEST_NAME,
    KOKORO_RUNNER_NAME,
)
from .components.slots import active_component_path
from .context import WorkspaceLayout
from .models import (
    ComputeVariant,
    HealthProbeSpec,
    ManagedProcessSpec,
    ResolvedComponentState,
    RestartPolicy,
)
from .network import EndpointExposure
from .releases.bundles import active_release_bundle

PANDRATOR_API_SERVICE = "pandrator.api"
PANDRATOR_MCP_SERVICE = "pandrator.mcp"
PANDRATOR_WORKER_SERVICE = "pandrator.worker"
PANDRATOR_MCP_PORT = 8099
PANDRATOR_CORE_SERVICES = frozenset(
    {PANDRATOR_API_SERVICE, PANDRATOR_WORKER_SERVICE}
)
PANDRATOR_SERVICE_START_ORDER = (
    PANDRATOR_API_SERVICE,
    PANDRATOR_WORKER_SERVICE,
    PANDRATOR_MCP_SERVICE,
)
PANDRATOR_SERVICE_STOP_ORDER = tuple(reversed(PANDRATOR_SERVICE_START_ORDER))

_FISHS2_MODEL_FILENAMES = {
    "f16": "s2-pro-f16.gguf",
    "q8_0": "s2-pro-q8_0.gguf",
    "q6_k": "s2-pro-q6_k.gguf",
    "q5_k_m": "s2-pro-q5_k_m.gguf",
    "q4_k_m": "s2-pro-q4_k_m.gguf",
    "q3_k": "s2-pro-q3_k.gguf",
    "q2_k": "s2-pro-q2_k.gguf",
}


def _environment_python(environment: Path) -> Path:
    return environment / (
        "python.exe" if os.name == "nt" else "bin/python"
    )


def runtime_python(layout: WorkspaceLayout) -> Path:
    release = active_release_bundle(layout)
    if release is not None:
        return release.python
    active = active_component_path(layout, "pandrator")
    candidates = (
        *(
            (_environment_python(active / ".pixi" / "envs" / "default"),)
            if active is not None
            else ()
        ),
        _environment_python(
            layout.root / "Pandrator" / ".pixi" / "envs" / "default"
        ),
        _environment_python(
            layout.root / ".pixi" / "envs" / "default"
        ),
        _environment_python(
            layout.environments
            / "pandrator_installer"
            / ".pixi"
            / "envs"
            / "default"
        ),
        *((Path(sys.executable),) if not getattr(sys, "frozen", False) else ()),
    )
    fallback = _environment_python(
        layout.environments / "pandrator" / ".pixi" / "envs" / "default"
    )
    return next((candidate for candidate in candidates if candidate.is_file()), fallback)


def application_root(layout: WorkspaceLayout) -> Path:
    release = active_release_bundle(layout)
    if release is not None:
        return release.application_root
    active = active_component_path(layout, "pandrator")
    if active is not None:
        return active
    legacy = layout.root / "Pandrator"
    return legacy if legacy.is_dir() else Path.cwd()


def _supports_serve_option(root: Path, option: str) -> bool:
    """Detect optional CLI features in a source-backed legacy installation.

    Release bundles and installed packages follow the manager's current
    runtime contract.  A migrated source checkout can predate optional serve
    arguments, however, and argparse would otherwise reject the entire launch.
    """

    cli = root / "pandrator" / "web" / "cli.py"
    if not cli.is_file():
        return True
    try:
        source = cli.read_text(encoding="utf-8")
    except OSError:
        return True
    return option in source


def _supports_managed_mcp(root: Path, python: Path) -> bool:
    """Detect the MCP module without executing an untrusted application runtime."""

    if (root / "pandrator_mcp" / "__main__.py").is_file():
        return True
    candidates: tuple[Path, ...]
    if os.name == "nt":
        candidates = (python.parent / "Lib" / "site-packages",)
    else:
        candidates = tuple(
            (python.parent.parent / "lib").glob("python*/site-packages")
        )
    return any(
        (candidate / "pandrator_mcp" / "__main__.py").is_file()
        for candidate in candidates
    )


def pandrator_runtime_specs(
    layout: WorkspaceLayout,
    *,
    host: str = "127.0.0.1",
    port: int = 8097,
    bootstrap_token: str = "",
    exposure: EndpointExposure | None = None,
    preferences: dict[str, str] | None = None,
    mcp_port: int = PANDRATOR_MCP_PORT,
    mcp_supported: bool | None = None,
) -> tuple[ManagedProcessSpec, ...]:
    selected_exposure = exposure or EndpointExposure(
        bind_host=host,
        port=port,
    )
    host = selected_exposure.bind_host
    port = selected_exposure.port
    if port == 0:
        raise ValueError("Pandrator requires a fixed application port.")
    root = application_root(layout)
    release = active_release_bundle(layout)
    modern_serve_contract = _supports_serve_option(root, "--public-url")
    active = active_component_path(layout, "pandrator")
    manifest = root / "pixi.toml"
    pixi = layout.bin / ("pixi.exe" if os.name == "nt" else "pixi")
    if release is not None:
        executable = str(release.python)
        command_prefix: tuple[str, ...] = ()
    elif active is not None and manifest.is_file():
        # `pixi run` creates/synchronizes the private environment from the
        # checked-in lock file. A frozen manager executable is not a Python
        # interpreter and must never receive `-m pandrator`.
        executable = str(pixi)
        command_prefix = (
            "run",
            "--manifest-path",
            str(manifest),
            "--locked",
            "python",
        )
    else:
        executable = str(runtime_python(layout))
        command_prefix = ()
    selected_python = Path(executable) if not command_prefix else runtime_python(layout)
    include_mcp = (
        _supports_managed_mcp(root, selected_python)
        if mcp_supported is None
        else bool(mcp_supported)
    )
    if include_mcp and mcp_port == port:
        raise ValueError("Pandrator API and MCP require distinct fixed ports.")
    environment = {
        "PANDRATOR_DATA_DIR": str(layout.data),
        "CRISPASR_CACHE_DIR": str(layout.cache / "crispasr"),
        "PANDRATOR_MANAGER_DESCRIPTOR": str(layout.descriptor),
        "PANDRATOR_MANAGER_CREDENTIAL": str(layout.credential),
        "PANDRATOR_WORKSPACE": str(layout.workspace),
        **(preferences or {}),
    }
    if bootstrap_token:
        environment["PANDRATOR_BOOTSTRAP_TOKEN"] = bootstrap_token
    crispasr = active_component_path(layout, "crispasr")
    if crispasr is not None:
        crispasr_executable = crispasr / (
            "crispasr.exe" if os.name == "nt" else "crispasr"
        )
        if crispasr_executable.is_file():
            environment["CRISPASR_EXECUTABLE"] = str(crispasr_executable)
    worker_environment = {
        key: value
        for key, value in environment.items()
        if key not in {
            "PANDRATOR_BOOTSTRAP_TOKEN",
            "PANDRATOR_OWNER_PASSWORD",
        }
    }
    api_arguments = (
        *command_prefix,
        "-m",
        "pandrator",
        "--data-dir",
        str(layout.data),
        "serve",
        "--host",
        host,
        "--port",
        str(port),
        "--no-open-browser",
    )
    if selected_exposure.remote_enabled:
        for trusted_host in selected_exposure.allowed_hosts:
            api_arguments += ("--trusted-host", trusted_host)
        if selected_exposure.proxy_hops:
            api_arguments += (
                "--proxy-hops",
                str(selected_exposure.proxy_hops),
            )
        if (
            selected_exposure.public_url
            and modern_serve_contract
        ):
            api_arguments += (
                "--public-url",
                selected_exposure.public_url,
            )
        if selected_exposure.allow_insecure_remote:
            api_arguments += ("--allow-insecure-remote",)
    probe_base_url = selected_exposure.local_base_url
    specifications: list[ManagedProcessSpec] = [
        ManagedProcessSpec(
            service_id=PANDRATOR_API_SERVICE,
            component_id="pandrator",
            label="Pandrator API",
            executable=executable,
            arguments=api_arguments,
            cwd=str(root),
            environment=environment,
            ports=(port,),
            readiness=HealthProbeSpec(
                kind="http",
                url=f"{probe_base_url}/api/v1/health",
                expected_service=(
                    "pandrator" if modern_serve_contract else None
                ),
                expected_protocol=(
                    "v1" if modern_serve_contract else None
                ),
                expected_json=(
                    {"version": release.metadata.version}
                    if release is not None
                    else (
                        {}
                        if modern_serve_contract
                        else {"status": "ok"}
                    )
                ),
            ),
            # The first `pixi run` may have to create the private application
            # environment from the lock file before the HTTP server appears.
            startup_timeout_seconds=15 * 60,
            restart=RestartPolicy(maximum_restarts=3),
        )
    ]
    if include_mcp:
        specifications.append(
            ManagedProcessSpec(
                service_id=PANDRATOR_MCP_SERVICE,
                component_id="pandrator",
                label="Pandrator MCP",
                executable=executable,
                arguments=(
                    *command_prefix,
                    "-m",
                    "pandrator_mcp",
                    "http",
                    "--workspace",
                    str(layout.workspace),
                    "--config",
                    str(layout.mcp_configuration),
                    "--token-file",
                    str(layout.mcp_credential),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(mcp_port),
                ),
                cwd=str(root),
                environment=worker_environment,
                ports=(mcp_port,),
                dependencies=(PANDRATOR_API_SERVICE,),
                readiness=HealthProbeSpec(
                    kind="http",
                    url=f"http://127.0.0.1:{mcp_port}/health",
                    expected_service="pandrator-mcp",
                    expected_protocol="2026-07-28",
                ),
                startup_timeout_seconds=60,
                restart=RestartPolicy(maximum_restarts=3),
                required=False,
            )
        )
    specifications.append(
        ManagedProcessSpec(
            service_id=PANDRATOR_WORKER_SERVICE,
            component_id="pandrator",
            label="Pandrator worker",
            executable=executable,
            arguments=(
                *command_prefix,
                "-m",
                "pandrator",
                "--data-dir",
                str(layout.data),
                "worker",
            ),
            cwd=str(root),
            environment=worker_environment,
            dependencies=(PANDRATOR_API_SERVICE,),
            readiness=HealthProbeSpec(kind="none"),
            restart=RestartPolicy(maximum_restarts=3),
        )
    )
    return tuple(specifications)


def silero_runtime_spec(
    layout: WorkspaceLayout,
    *,
    port: int = 8001,
) -> ManagedProcessSpec:
    repository = layout.root / "silero-fastapi"
    pixi = layout.bin / ("pixi.exe" if os.name == "nt" else "pixi")
    return ManagedProcessSpec(
        service_id="tts.silero",
        component_id="silero",
        label="Silero",
        executable=str(pixi),
        arguments=(
            "run",
            "--locked",
            "silero-fastapi",
            "--data-dir",
            str(layout.data / "models" / "silero"),
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--device",
            "cpu",
        ),
        cwd=str(repository),
        environment={
            "XDG_CACHE_HOME": str(layout.cache),
        },
        ports=(port,),
        readiness=HealthProbeSpec(
            kind="http",
            url=f"http://127.0.0.1:{port}/ready",
            expected_json={"status": "ready"},
        ),
        startup_timeout_seconds=180,
        restart=RestartPolicy(maximum_restarts=3),
    )


def component_runtime_spec(
    layout: WorkspaceLayout,
    component_id: str,
    resolved: ResolvedComponentState,
) -> ManagedProcessSpec | None:
    """Resolve an installed built-in component into a bounded launch spec."""

    root = active_component_path(layout, component_id)
    if root is None:
        return None
    pixi = layout.bin / ("pixi.exe" if os.name == "nt" else "pixi")
    compute = resolved.compute.value
    quantization = resolved.quantization
    options = resolved.options
    common_environment = {
        "XDG_CACHE_HOME": str(layout.cache),
        "HF_HOME": str(layout.cache / "huggingface"),
        "PIP_CACHE_DIR": str(layout.cache / "pip"),
        "PIXI_CACHE_DIR": str(layout.cache / "pixi"),
        "PANDRATOR_MODELS_DIR": str(layout.data / "models"),
    }

    def python_bootstrap(
        *,
        service_id: str,
        label: str,
        port: int,
        arguments: tuple[str, ...],
        expected_json: dict | None = None,
        environment: dict[str, str] | None = None,
        readiness: HealthProbeSpec | None = None,
        startup_timeout: float = 30 * 60,
    ) -> ManagedProcessSpec:
        manifest = root / "pyproject.toml"
        return ManagedProcessSpec(
            service_id=service_id,
            component_id=component_id,
            label=label,
            executable=str(pixi),
            arguments=(
                "run",
                "--manifest-path",
                str(manifest),
                "python",
                "run.py",
                *arguments,
                "--pixi-path",
                str(pixi),
            ),
            cwd=str(root),
            environment={**common_environment, **(environment or {})},
            ports=(port,),
            readiness=readiness
            or HealthProbeSpec(
                kind="http",
                url=f"http://127.0.0.1:{port}/health",
                expected_json=expected_json or {"status": "ok"},
                timeout_seconds=3,
            ),
            startup_timeout_seconds=startup_timeout,
            restart=RestartPolicy(maximum_restarts=3),
        )

    if component_id == "xtts":
        return python_bootstrap(
            service_id="tts.xtts",
            label="XTTS v2",
            port=8020,
            arguments=("--backend", compute),
        )
    if component_id == "fish_speech":
        fish_quantization = str(quantization or "q6_k").strip().lower()
        try:
            fish_model_filename = _FISHS2_MODEL_FILENAMES[fish_quantization]
        except KeyError as error:
            allowed = ", ".join(sorted(_FISHS2_MODEL_FILENAMES))
            raise ValueError(
                "Fish S2 Pro does not support quantization "
                f"{fish_quantization!r}; choose one of: {allowed}."
            ) from error
        fish_models = layout.data / "models" / "fish_speech"
        fish_state = layout.state / "services" / "fish_speech"
        return python_bootstrap(
            service_id="tts.fish_speech",
            label="Fish S2 Pro",
            port=8022,
            arguments=(),
            environment={
                "FISHS2_BACKEND": compute,
                "FISHS2_MODEL_QUANT": fish_quantization,
                "FISHS2_PORT": "8022",
                "FISHS2_MODEL_PATH": str(fish_models / fish_model_filename),
                "FISHS2_TOKENIZER_PATH": str(fish_models / "tokenizer.json"),
                "FISHS2_RUNTIME_DIR": str(
                    layout.data / "runtime" / "fish_speech"
                ),
                "FISHS2_VOICES_DIR": str(fish_state / "voices"),
                "FISHS2_LOGS_DIR": str(layout.logs / "fish_speech"),
                "FISHS2_TEMP_DIR": str(layout.cache / "fish_speech" / "tmp"),
            },
            # The first Linux launch may compile s2.cpp before loading a
            # multi-gigabyte model.  Subsequent launches reuse both artifacts.
            startup_timeout=60 * 60,
        )
    if component_id == "chatterbox":
        return python_bootstrap(
            service_id="tts.chatterbox",
            label="Chatterbox",
            port=8040,
            arguments=(
                "--host",
                "127.0.0.1",
                "--port",
                "8040",
                "--backend",
                "cpu" if resolved.compute == ComputeVariant.CPU else "cuda",
            ),
        )
    if component_id == "qwen_tts":
        qwen_state = layout.state / "services" / "qwen_tts"
        return python_bootstrap(
            service_id="tts.qwen",
            label="Qwen3 TTS",
            port=8042,
            arguments=(
                "--host",
                "127.0.0.1",
                "--port",
                "8042",
                "--backend",
                compute,
                "--model-size",
                str(options.get("model_size") or "1.7b"),
                "--quantization",
                str(quantization or options.get("quantization") or "q8_0"),
                "--initial-model",
                str(options.get("initial_model") or "base"),
            ),
            environment={
                "PANDRATOR_QWEN_STATE_DIR": str(qwen_state),
                "PANDRATOR_QWEN_MODELS_DIR": str(
                    layout.data / "models" / "qwen_tts"
                ),
                "PANDRATOR_QWEN_BIN_DIR": str(
                    layout.data / "runtime" / "qwen_tts"
                ),
                "PANDRATOR_QWEN_VOICES_DIR": str(qwen_state / "voices"),
            },
            # Qwen synthesis is delegated to a single KoboldCpp child and may
            # legitimately occupy the API's generation worker for minutes.
            # The listening socket is the correct liveness contract; `/readyz`
            # remains available to callers that need child readiness.
            readiness=HealthProbeSpec(
                kind="tcp",
                host="127.0.0.1",
                port=8042,
            ),
        )
    if component_id == "magpie":
        return python_bootstrap(
            service_id="tts.magpie",
            label="Magpie",
            port=8030,
            arguments=(
                "--host",
                "127.0.0.1",
                "--port",
                "8030",
                "--device",
                "cpu" if resolved.compute == ComputeVariant.CPU else "cuda",
            ),
        )
    if component_id == "rvc":
        script = root / ("run.bat" if os.name == "nt" else "run.sh")
        executable = (
            Path(os.environ.get("ComSpec", r"C:\Windows\System32\cmd.exe"))
            if os.name == "nt"
            else Path("/bin/bash")
        )
        prefix = (
            ("/d", "/s", "/c", str(script))
            if os.name == "nt"
            else (str(script),)
        )
        return ManagedProcessSpec(
            service_id="audio.rvc",
            component_id="rvc",
            label="RVC",
            executable=str(executable),
            arguments=(
                *prefix,
                "--host",
                "127.0.0.1",
                "--port",
                "8050",
                "--backend",
                "cpu" if resolved.compute == ComputeVariant.CPU else "auto",
                "--models-dir",
                str(layout.data / "models" / "rvc"),
                "--pixi-path",
                str(pixi),
            ),
            cwd=str(root),
            environment=common_environment,
            ports=(8050,),
            readiness=HealthProbeSpec(
                kind="http",
                url="http://127.0.0.1:8050/health",
                expected_json={"status": "ok", "ready": True},
            ),
            startup_timeout_seconds=30 * 60,
            restart=RestartPolicy(maximum_restarts=3),
        )
    if component_id == "silero":
        return ManagedProcessSpec(
            service_id="tts.silero",
            component_id="silero",
            label="Silero",
            executable=str(pixi),
            arguments=(
                "run",
                "--locked",
                "silero-fastapi",
                "--data-dir",
                str(layout.data / "models" / "silero"),
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                "8001",
                "--device",
                "cpu",
            ),
            cwd=str(root),
            environment=common_environment,
            ports=(8001,),
            readiness=HealthProbeSpec(
                kind="http",
                url="http://127.0.0.1:8001/ready",
                expected_json={"status": "ready"},
            ),
            startup_timeout_seconds=10 * 60,
            restart=RestartPolicy(maximum_restarts=3),
        )
    if component_id == "voxtral":
        script = root / ("run.ps1" if os.name == "nt" else "run.sh")
        if os.name == "nt":
            executable = (
                Path(os.environ.get("SystemRoot", r"C:\Windows"))
                / "System32"
                / "WindowsPowerShell"
                / "v1.0"
                / "powershell.exe"
            )
            arguments = (
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-ProjectRoot",
                str(root),
                "-BindHost",
                "127.0.0.1",
                "-Port",
                "8000",
                "-Model",
                "gguf",
            )
        else:
            executable = Path("/bin/bash")
            arguments = (
                str(script),
                "--project-root",
                str(root),
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
                "--model",
                "gguf",
            )
        return ManagedProcessSpec(
            service_id="tts.voxtral",
            component_id="voxtral",
            label="Voxtral",
            executable=str(executable),
            arguments=arguments,
            cwd=str(root),
            environment=common_environment,
            ports=(8000,),
            readiness=HealthProbeSpec(
                kind="http",
                url="http://127.0.0.1:8000/health",
                expected_json={"status": "ok"},
            ),
            startup_timeout_seconds=30 * 60,
            restart=RestartPolicy(maximum_restarts=3),
        )
    if component_id == "voxcpm":
        return ManagedProcessSpec(
            service_id="tts.voxcpm",
            component_id="voxcpm",
            label="VoxCPM2",
            executable=str(pixi),
            arguments=(
                "run",
                "--manifest-path",
                str(root / "pyproject.toml"),
                "python",
                "pandrator-manager-run.py",
                "--backend",
                compute,
                "--host",
                "127.0.0.1",
                "--port",
                "8021",
                "--pixi-path",
                str(pixi),
            ),
            cwd=str(root),
            environment={
                **common_environment,
                "VOXCPM_HOST": "127.0.0.1",
                "VOXCPM_PORT": "8021",
                "VOXCPM_MODELS_DIR": str(layout.data / "models" / "voxcpm"),
                "VOXCPM_VOICES_DIR": str(layout.data / "voices" / "voxcpm"),
                "VOXCPM_LOGS_DIR": str(layout.logs / "voxcpm"),
                "VOXCPM_DEVICE": compute,
            },
            ports=(8021,),
            readiness=HealthProbeSpec(
                kind="http",
                url="http://127.0.0.1:8021/health",
                expected_json={"status": "ok"},
            ),
            startup_timeout_seconds=30 * 60,
            restart=RestartPolicy(maximum_restarts=3),
        )
    if component_id == "kokoro":
        return ManagedProcessSpec(
            service_id="tts.kokoro",
            component_id="kokoro",
            label="Kokoro",
            executable=str(pixi),
            arguments=(
                "run",
                "--manifest-path",
                str(root / KOKORO_MANIFEST_NAME),
                "python",
                str(root / KOKORO_RUNNER_NAME),
                "--backend",
                compute,
                "--host",
                "127.0.0.1",
                "--port",
                "8880",
                "--model-dir",
                str(layout.data / "models" / "kokoro"),
            ),
            cwd=str(root),
            environment={
                **common_environment,
                "PANDRATOR_KOKORO_STATE_DIR": str(
                    layout.state / "services" / "kokoro"
                ),
            },
            ports=(8880,),
            # Kokoro performs CPU-bound synthesis in the API process. During a
            # sustained generation run the event loop can legitimately delay
            # `/health`, which must not be mistaken for a crashed service.
            # The listening socket remains an accurate liveness signal while
            # inference is in progress.
            readiness=HealthProbeSpec(
                kind="tcp",
                host="127.0.0.1",
                port=8880,
            ),
            startup_timeout_seconds=30 * 60,
            restart=RestartPolicy(maximum_restarts=3),
        )
    return None
