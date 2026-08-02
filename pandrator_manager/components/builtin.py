"""Built-in component metadata and conservative marker-based inspection."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.error import URLError
from urllib.request import urlopen

from ..context import ManagerContext
from ..models import (
    ComponentDefinition,
    ComponentInspection,
    ComponentState,
    ComputeVariant,
    DesiredComponentState,
    HealthResult,
    HealthState,
    ManagedService,
    ResolvedComponentState,
    TaskSpec,
)
from .catalog import presentation_for
from .crispasr import resolve_asset
from .host import resolve_auto_compute, vulkan_requires_quantized_models
from .registry import ComponentRegistry
from .slots import active_component_path


class MarkerComponentDriver:
    driver_id = "marker"

    def inspect(
        self,
        context: ManagerContext,
        definition: ComponentDefinition,
        desired: DesiredComponentState | None,
    ) -> ComponentInspection:
        supported_systems = {value.casefold() for value in definition.supported_systems}
        supported_architectures = {
            value.casefold() for value in definition.supported_architectures
        }
        if (
            context.system.casefold() not in supported_systems
            or context.architecture.casefold() not in supported_architectures
        ):
            return ComponentInspection(
                component_id=definition.id,
                state=ComponentState.UNSUPPORTED,
                desired=desired,
                problems=(
                    f"{definition.label} is not supported on "
                    f"{context.system}/{context.architecture}.",
                ),
            )

        active = active_component_path(context.layout, definition.id)
        active_evidence = tuple(
            f"slot:{marker}"
            for marker in definition.source_markers
            if active is not None and (active / marker).exists()
        )
        legacy_evidence = tuple(
            marker for marker in definition.markers
            if (context.layout.root / marker).exists()
        )
        if (
            active is not None
            and definition.source_markers
            and len(active_evidence) == len(definition.source_markers)
        ):
            evidence = active_evidence
            state = ComponentState.PRESENT
            problems = ()
        elif active_evidence:
            evidence = active_evidence
            state = ComponentState.DEGRADED
            problems = ("The active component slot is incomplete.",)
        elif legacy_evidence:
            evidence = legacy_evidence
            state = (
                ComponentState.PRESENT
                if len(legacy_evidence) == len(definition.markers)
                else ComponentState.DEGRADED
            )
            problems = (
                ()
                if state == ComponentState.PRESENT
                else ("Only part of the legacy component installation was found.",)
            )
        elif not definition.markers and not definition.source_markers:
            evidence = ()
            state = ComponentState.UNKNOWN
            problems = ("No inspection markers are defined.",)
        else:
            evidence = ()
            state = ComponentState.ABSENT
            problems = ()
        resolved = self.resolve(context, definition, desired) if desired else None
        return ComponentInspection(
            component_id=definition.id,
            state=state,
            desired=desired,
            resolved=resolved,
            evidence=evidence,
            problems=problems,
        )

    def resolve(
        self,
        context: ManagerContext,
        definition: ComponentDefinition,
        desired: DesiredComponentState,
    ) -> ResolvedComponentState:
        requested = desired.compute
        supported = definition.compute_variants
        if requested == ComputeVariant.AUTO:
            compute = resolve_auto_compute(context, definition)
        elif requested in supported:
            compute = requested
        else:
            raise ValueError(
                f"{definition.label} does not support compute variant {requested.value}."
            )
        options = dict(desired.options)
        selected_values: dict[str, str] = {}
        quantization = desired.quantization
        for option in definition.install_options:
            value = (
                quantization
                if option.state_field == "quantization"
                else options.get(option.key)
            )
            selected_value = str(value or option.default)
            if definition.id == "qwen_tts" and option.key == "initial_model":
                # The original Manager catalogue used ``custom_voice`` and
                # exposed ``both`` even though the service CLI accepts only
                # ``base`` or ``customvoice``. Existing persisted selections
                # must remain loadable; the second family is now acquired
                # lazily by the service when Pandrator first requests it.
                selected_value = {
                    "custom_voice": "customvoice",
                    "both": "base",
                }.get(selected_value.casefold(), selected_value.casefold())
            selected_values[option.key] = selected_value
        for option in definition.install_options:
            value = selected_values[option.key]
            selected = next(
                (choice for choice in option.choices if choice.value == value),
                None,
            )
            if selected is None:
                raise ValueError(
                    f"{definition.label} does not support {option.label} value "
                    f"{value!r}."
                )
            for dependency, allowed in selected.requires.items():
                if selected_values.get(dependency) not in allowed:
                    raise ValueError(
                        f"{selected.label} is not available with the selected "
                        f"{dependency.replace('_', ' ')}."
                    )
            if option.state_field == "quantization":
                quantization = value
            else:
                options[option.key] = value
        if (
            definition.id == "qwen_tts"
            and requested == ComputeVariant.AUTO
            and compute == ComputeVariant.VULKAN
            and str(quantization or "").casefold() == "f16"
            and vulkan_requires_quantized_models(context)
        ):
            # Polaris-class cards can run Q8 Qwen models through Vulkan, but
            # advertise no native FP16 support and have been observed exiting
            # inside KoboldCpp on the first F16 synthesis request.  Preserve
            # explicit expert selections while making Automatic fail safe.
            compute = ComputeVariant.CPU
        return ResolvedComponentState(
            compute=compute,
            quantization=quantization,
            platform=context.platform_id,
            options=options,
        )

    @staticmethod
    def _task(
        definition: ComponentDefinition,
        suffix: str,
        label: str,
        *,
        dependencies: tuple[str, ...] = (),
        kind: str,
        inputs: dict | None = None,
        expected_outputs: tuple[str, ...] = (),
    ) -> TaskSpec:
        return TaskSpec(
            id=f"{definition.id}:{suffix}",
            kind=kind,
            label=label,
            component_id=definition.id,
            dependencies=dependencies,
            resource_locks=definition.resource_locks,
            # A component's catalogue estimate describes the acquisition/staged
            # payload once. Repeating it on verify, activate, and validation
            # tasks would multiply both the plan total and disk preflight.
            estimated_download_bytes=(
                definition.estimated_download_bytes or 0
                if kind in {"stage_component", "stage_crispasr"}
                else 0
            ),
            estimated_disk_bytes=(
                definition.estimated_installed_bytes or 0
                if kind in {"stage_component", "stage_crispasr"}
                else 0
            ),
            inputs=inputs or {},
            expected_outputs=expected_outputs,
            verification={"driver": definition.driver, "markers": definition.markers},
            rollback={"strategy": "remove_staging_or_restore_previous_slot"},
        )

    def plan_install(
        self,
        context: ManagerContext,
        definition: ComponentDefinition,
        desired: DesiredComponentState,
        inspection: ComponentInspection,
    ) -> tuple[TaskSpec, ...]:
        if definition.id == "crispasr":
            return CrispASRComponentDriver().plan_install(
                context,
                definition,
                desired,
                inspection,
            )
        resolved = self.resolve(context, definition, desired)
        stage = self._task(
            definition,
            "stage",
            f"Stage {definition.label}",
            kind="stage_component",
            inputs={
                "repo_url": definition.repo_url,
                "resolved": resolved.model_dump(mode="json"),
            },
            expected_outputs=definition.owned_paths,
        )
        verify = self._task(
            definition,
            "verify",
            f"Verify {definition.label}",
            kind="verify_component",
            dependencies=(stage.id,),
            expected_outputs=definition.markers,
        )
        activate = self._task(
            definition,
            "activate",
            f"Activate {definition.label}",
            kind="activate_component",
            dependencies=(verify.id,),
            expected_outputs=definition.owned_paths,
        )
        if definition.service_key:
            validate = self._task(
                definition,
                "validate-service",
                f"Validate {definition.label} service",
                kind="validate_service",
                dependencies=(activate.id,),
            )
            return stage, verify, activate, validate
        return stage, verify, activate

    def plan_update(
        self,
        context: ManagerContext,
        definition: ComponentDefinition,
        desired: DesiredComponentState,
        inspection: ComponentInspection,
    ) -> tuple[TaskSpec, ...]:
        return self._plan_replacement(
            context,
            definition,
            desired,
            inspection,
        )

    def plan_repair(
        self,
        context: ManagerContext,
        definition: ComponentDefinition,
        desired: DesiredComponentState,
        inspection: ComponentInspection,
    ) -> tuple[TaskSpec, ...]:
        return self._plan_replacement(
            context,
            definition,
            desired,
            inspection,
        )

    def _plan_replacement(
        self,
        context: ManagerContext,
        definition: ComponentDefinition,
        desired: DesiredComponentState,
        inspection: ComponentInspection,
    ) -> tuple[TaskSpec, ...]:
        install = self.plan_install(context, definition, desired, inspection)
        if not definition.service_key:
            return install
        stop = self._task(
            definition,
            "stop",
            f"Stop {definition.label}",
            kind="stop_service",
        )
        first = install[0].model_copy(
            update={"dependencies": (*install[0].dependencies, stop.id)}
        )
        return stop, first, *install[1:]

    def plan_remove(
        self,
        context: ManagerContext,
        definition: ComponentDefinition,
        inspection: ComponentInspection,
    ) -> tuple[TaskSpec, ...]:
        stop = self._task(
            definition,
            "stop",
            f"Stop {definition.label}",
            kind="stop_service",
        )
        remove = self._task(
            definition,
            "remove",
            f"Remove {definition.label}",
            kind="remove_owned_component",
            dependencies=(stop.id,),
            inputs={"owned_paths": definition.owned_paths},
        )
        return stop, remove

    def launch_spec(
        self,
        context: ManagerContext,
        definition: ComponentDefinition,
        resolved: ResolvedComponentState,
    ) -> ManagedService | None:
        del context, resolved
        if not definition.service_key:
            return None
        port = definition.default_port
        return ManagedService(
            id=f"managed:{definition.id}",
            component_id=definition.id,
            service_key=definition.service_key,
            port=port,
            endpoint=f"http://127.0.0.1:{port}" if port else None,
        )

    def health_probe(
        self,
        context: ManagerContext,
        service: ManagedService,
    ) -> HealthResult:
        del context
        if not service.endpoint:
            return HealthResult(
                state=HealthState.UNKNOWN,
                service_id=service.id,
                message="Service has no assigned endpoint.",
            )
        try:
            with urlopen(f"{service.endpoint.rstrip('/')}/health", timeout=2) as response:
                state = (
                    HealthState.HEALTHY
                    if 200 <= int(response.status) < 300
                    else HealthState.UNHEALTHY
                )
        except (URLError, OSError, ValueError):
            state = HealthState.UNHEALTHY
        return HealthResult(
            state=state,
            service_id=service.id,
            checked_at=datetime.now(timezone.utc),
        )


class CrispASRComponentDriver(MarkerComponentDriver):
    """Install the pinned native CrispASR asset instead of cloning a repository."""

    driver_id = "crispasr"

    def plan_install(
        self,
        context: ManagerContext,
        definition: ComponentDefinition,
        desired: DesiredComponentState,
        inspection: ComponentInspection,
    ) -> tuple[TaskSpec, ...]:
        del inspection
        resolved = self.resolve(context, definition, desired)
        asset, effective = resolve_asset(context, desired.compute, definition)
        stage = self._task(
            definition,
            "stage",
            f"Download and stage {definition.label}",
            kind="stage_crispasr",
            inputs={
                "asset": {
                    "url": asset.url,
                    "filename": asset.name,
                    "sha256": asset.sha256,
                    "runtime_variant": asset.runtime_variant.value,
                    "compiled_backends": list(asset.compiled_backends),
                },
                "requested_compute": desired.compute.value,
                "effective_compute": effective.value,
                "resolved": resolved.model_dump(mode="json"),
            },
            expected_outputs=definition.owned_paths,
        )
        verify = self._task(
            definition,
            "verify",
            f"Verify {definition.label}",
            kind="verify_component",
            dependencies=(stage.id,),
            expected_outputs=definition.source_markers,
        )
        activate = self._task(
            definition,
            "activate",
            f"Activate {definition.label}",
            kind="activate_component",
            dependencies=(verify.id,),
            expected_outputs=definition.owned_paths,
        )
        return stage, verify, activate


def _component(
    component_id: str,
    label: str,
    *,
    path: str,
    markers: tuple[str, ...],
    source_markers: tuple[str, ...],
    compute: tuple[ComputeVariant, ...],
    repo_url: str | None = None,
    port: int | None = None,
    service_key: str | None = None,
    dependencies: tuple[str, ...] = (),
    required_runtime_tools: tuple[str, ...] = (),
    driver: str = "marker",
    supported_actions: tuple[str, ...] = (
        "install",
        "update",
        "repair",
        "remove",
        "start",
        "stop",
    ),
) -> ComponentDefinition:
    presentation = presentation_for(component_id)
    return ComponentDefinition(
        id=component_id,
        label=label,
        description=presentation.summary,
        guidance=presentation.guidance,
        section=presentation.section,
        display_order=presentation.order,
        languages=presentation.languages,
        capabilities=presentation.capabilities,
        models=presentation.models,
        install_options=presentation.install_options,
        driver=driver,
        compute_variants=compute,
        dependencies=dependencies,
        resource_locks=(f"component:{component_id}",),
        owned_paths=(path,),
        markers=markers,
        source_markers=source_markers,
        supported_actions=supported_actions,
        required_runtime_tools=required_runtime_tools,
        environment_owner=component_id,
        service_key=service_key,
        default_port=port,
        repo_url=repo_url,
        estimated_download_bytes=presentation.estimated_download_bytes,
        estimated_installed_bytes=presentation.estimated_installed_bytes,
        size_provenance=presentation.size_provenance,
        size_note=presentation.size_note,
    )


BUILTIN_COMPONENTS: tuple[ComponentDefinition, ...] = (
    _component(
        "pandrator",
        "Pandrator",
        path="app",
        markers=("Pandrator/pyproject.toml", "Pandrator/pandrator/web/cli.py"),
        source_markers=("pyproject.toml", "pandrator/web/cli.py"),
        compute=(ComputeVariant.CPU,),
        repo_url="https://github.com/lukaszliniewicz/Pandrator.git",
        required_runtime_tools=("pixi",),
    ),
    _component(
        "xtts",
        "XTTS v2",
        path="services/xtts",
        markers=("xtts2_api/run.py", "xtts2_api/pyproject.toml"),
        source_markers=("run.py", "pyproject.toml"),
        compute=(ComputeVariant.CPU, ComputeVariant.CUDA),
        repo_url="https://github.com/lukaszliniewicz/xtts2_api.git",
        port=8020,
        service_key="tts.xtts",
        required_runtime_tools=("pixi",),
    ),
    _component(
        "voxcpm",
        "VoxCPM2",
        path="services/voxcpm",
        markers=("voxcpm_fastapi/run.py", "voxcpm_fastapi/pyproject.toml"),
        source_markers=("run.py", "pyproject.toml", "pandrator-manager-run.py"),
        compute=(ComputeVariant.CPU, ComputeVariant.CUDA),
        repo_url="https://github.com/lukaszliniewicz/voxcpm_fastapi.git",
        port=8021,
        service_key="tts.voxcpm",
        required_runtime_tools=("pixi",),
    ),
    _component(
        "fish_speech",
        "Fish S2 Pro",
        path="services/fish_speech",
        markers=("fishs2-cpp-fastapi/run.py", "fishs2-cpp-fastapi/pyproject.toml"),
        source_markers=("run.py", "pyproject.toml"),
        compute=(
            ComputeVariant.CPU,
            ComputeVariant.CUDA,
            ComputeVariant.VULKAN,
            ComputeVariant.METAL,
        ),
        repo_url="https://github.com/lukaszliniewicz/fishs2-cpp-fastapi.git",
        port=8022,
        service_key="tts.fish_speech",
        required_runtime_tools=("pixi",),
    ),
    _component(
        "voxtral",
        "Voxtral",
        path="services/voxtral",
        markers=("voxtral-fastapi",),
        source_markers=("run.sh",),
        compute=(ComputeVariant.WGPU,),
        repo_url="https://github.com/lukaszliniewicz/voxtral-fastapi.git",
        port=8000,
        service_key="tts.voxtral",
        required_runtime_tools=("pixi",),
    ),
    _component(
        "kokoro",
        "Kokoro",
        path="services/kokoro",
        markers=("Kokoro-FastAPI/api/src/main.py",),
        source_markers=(
            "api/src/main.py",
            ".pandrator-manager/pixi.toml",
            "pandrator-manager-run.py",
        ),
        compute=(
            ComputeVariant.CPU,
            ComputeVariant.CUDA,
            ComputeVariant.ROCM,
            ComputeVariant.METAL,
        ),
        repo_url="https://github.com/remsky/Kokoro-FastAPI.git",
        port=8880,
        service_key="tts.kokoro",
        required_runtime_tools=("pixi",),
    ),
    _component(
        "silero",
        "Silero",
        path="services/silero",
        markers=("silero-fastapi/pyproject.toml", "silero-fastapi/pixi.lock"),
        source_markers=("pyproject.toml", "pixi.lock"),
        compute=(ComputeVariant.CPU,),
        repo_url="https://github.com/lukaszliniewicz/silero-fastapi.git",
        port=8001,
        service_key="tts.silero",
        required_runtime_tools=("pixi",),
    ),
    _component(
        "crispasr",
        "CrispASR",
        path="services/crispasr",
        markers=("CrispASR/install.json",),
        source_markers=("install.json",),
        compute=(
            ComputeVariant.CPU,
            ComputeVariant.CUDA,
            ComputeVariant.VULKAN,
            ComputeVariant.METAL,
        ),
        supported_actions=("install", "update", "repair", "remove"),
    ),
    _component(
        "rvc",
        "RVC",
        path="services/rvc",
        markers=("rvc-python/run.py", "rvc-python/pyproject.toml"),
        source_markers=("run.py", "pyproject.toml"),
        compute=(ComputeVariant.CPU, ComputeVariant.CUDA),
        repo_url="https://github.com/lukaszliniewicz/rvc-python.git",
        port=8050,
        service_key="audio.rvc",
        required_runtime_tools=("pixi",),
    ),
    _component(
        "chatterbox",
        "Chatterbox",
        path="services/chatterbox",
        markers=("chatterbox-fastapi/run.py", "chatterbox-fastapi/pyproject.toml"),
        source_markers=("run.py", "pyproject.toml"),
        compute=(ComputeVariant.CPU, ComputeVariant.CUDA),
        repo_url="https://github.com/lukaszliniewicz/chatterbox-fastapi.git",
        port=8040,
        service_key="tts.chatterbox",
        required_runtime_tools=("pixi",),
    ),
    _component(
        "qwen_tts",
        "Qwen3 TTS",
        path="services/qwen_tts",
        markers=("kobold-qwen-fastapi/run.py", "kobold-qwen-fastapi/pyproject.toml"),
        source_markers=("run.py", "pyproject.toml"),
        compute=(
            ComputeVariant.CPU,
            ComputeVariant.CUDA,
            ComputeVariant.VULKAN,
            ComputeVariant.METAL,
        ),
        repo_url="https://github.com/lukaszliniewicz/kobold-qwen-fastapi.git",
        port=8042,
        service_key="tts.qwen",
        required_runtime_tools=("pixi",),
    ),
    _component(
        "magpie",
        "Magpie",
        path="services/magpie",
        markers=("magpie-fastapi/run.py", "magpie-fastapi/pyproject.toml"),
        source_markers=("run.py", "pyproject.toml"),
        compute=(ComputeVariant.CPU, ComputeVariant.CUDA),
        repo_url="https://github.com/lukaszliniewicz/magpie-fastapi.git",
        port=8030,
        service_key="tts.magpie",
        required_runtime_tools=("pixi",),
    ),
    _component(
        "xtts_finetuning",
        "XTTS fine-tuning",
        path="services/xtts_finetuning",
        markers=("easy_xtts_trainer/requirements.txt",),
        source_markers=("requirements.txt",),
        compute=(ComputeVariant.CPU, ComputeVariant.CUDA),
        repo_url="https://github.com/lukaszliniewicz/easy_xtts_trainer.git",
        dependencies=("xtts",),
        supported_actions=("remove",),
    ),
)


def builtin_registry() -> ComponentRegistry:
    return ComponentRegistry(
        definitions=BUILTIN_COMPONENTS,
        drivers=(MarkerComponentDriver(),),
    )
