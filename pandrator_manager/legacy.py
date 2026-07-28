"""Conservative, idempotent import of the legacy installer workspace."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from .components import ComponentRegistry
from .context import ManagerContext
from .errors import ManagerError
from .legacy_data import (
    LEGACY_DATA_TOP_LEVEL_NAMES,
    legacy_data_inventory,
    reconcile_legacy_data,
)
from .models import (
    ComponentState,
    ComputeVariant,
    DesiredComponentState,
    LegacyImportReport,
    LegacyOwnershipCandidate,
    LegacySourceFile,
)
from .planning import Planner
from .state import ManagerStore

LEGACY_COMPONENT_FLAGS: dict[str, str] = {
    "xtts": "xtts_support",
    "voxcpm": "voxcpm_support",
    "fish_speech": "fishs2_support",
    "voxtral": "voxtral_support",
    "kokoro": "kokoro_support",
    "silero": "silero_support",
    "crispasr": "crispasr_support",
    "rvc": "rvc_support",
    "chatterbox": "chatterbox_support",
    "qwen_tts": "kobold_qwen_support",
    "magpie": "magpie_support",
    "xtts_finetuning": "xtts_finetuning_support",
}

LEGACY_GPU_FLAGS: dict[str, str] = {
    "xtts": "cuda_support",
    "fish_speech": "fishs2_gpu_support",
    "kokoro": "kokoro_gpu_support",
    "rvc": "rvc_gpu_support",
    "chatterbox": "chatterbox_gpu_support",
    "qwen_tts": "kobold_qwen_gpu_support",
    "magpie": "magpie_gpu_support",
}

LEGACY_KNOWN_TOP_LEVEL = frozenset(
    {
        ".pixi-cache",
        ".pixi-home",
        "bin",
        "cache",
        "Calibre Portable",
        "config.json",
        "easy_xtts_trainer",
        "envs",
        "fishs2-cpp-fastapi",
        "installer_state.json",
        "Kokoro-FastAPI",
        "kobold-qwen-fastapi",
        "magpie-fastapi",
        "models",
        "Outputs",
        "packaging_layout.json",
        "Pandrator",
        "pandrator.sqlite3",
        "pandrator_state.sqlite3",
        "rvc-python",
        "silero-fastapi",
        "voices",
        "voxcpm_fastapi",
        "voxtral-fastapi",
        "xtts2_api",
        "chatterbox-fastapi",
        "CrispASR",
    }
) | LEGACY_DATA_TOP_LEVEL_NAMES

LEGACY_AUXILIARY_FILES = (
    "installer_state.json",
    "packaging_layout.json",
)

LEGACY_PACKAGING_SHARED_PATHS = frozenset(
    {
        "Pandrator",
        "bin",
        "Calibre Portable",
        ".pixi-home",
        ".pixi-cache",
        "cache",
        "envs/pandrator_installer",
        "config.json",
        "installer_state.json",
        "packaging_layout.json",
    }
)

LEGACY_PACKAGING_COMPONENT_IDS = {
    "xtts": "xtts",
    "voxcpm": "voxcpm",
    "fishs2": "fish_speech",
    "voxtral": "voxtral",
    "kokoro": "kokoro",
    "silero": "silero",
    "crispasr": "crispasr",
    "xtts_finetuning": "xtts_finetuning",
    "rvc": "rvc",
    "chatterbox": "chatterbox",
    "kobold_qwen": "qwen_tts",
    "magpie": "magpie",
}

LEGACY_PACKAGING_COMPONENT_PATHS = {
    "xtts": frozenset({"xtts2_api"}),
    "voxcpm": frozenset({"voxcpm_fastapi"}),
    "fish_speech": frozenset({"fishs2-cpp-fastapi"}),
    "voxtral": frozenset({"voxtral-fastapi"}),
    "kokoro": frozenset(
        {"Kokoro-FastAPI", "envs/kokoro_api_server_installer"}
    ),
    "silero": frozenset({"silero-fastapi"}),
    "crispasr": frozenset({"CrispASR"}),
    "xtts_finetuning": frozenset(
        {
            "easy_xtts_trainer",
            "envs/easy_xtts_trainer",
            "envs/whisperx_installer",
        }
    ),
    "rvc": frozenset({"rvc-python"}),
    "chatterbox": frozenset({"chatterbox-fastapi"}),
    "qwen_tts": frozenset({"kobold-qwen-fastapi"}),
    "magpie": frozenset({"magpie-fastapi"}),
}


class LegacyImporter:
    def __init__(
        self,
        context: ManagerContext,
        store: ManagerStore,
        registry: ComponentRegistry,
    ) -> None:
        self.context = context
        self.store = store
        self.registry = registry
        self.planner = Planner(context, registry)

    @property
    def source(self) -> Path:
        return self.context.layout.root / "config.json"

    @staticmethod
    def _compute(
        component_id: str,
        config: dict[str, Any],
    ) -> ComputeVariant:
        if component_id == "voxcpm":
            return ComputeVariant.CUDA
        if component_id == "voxtral":
            return ComputeVariant.WGPU
        if component_id == "crispasr":
            for field in ("crispasr_runtime_variant", "crispasr_backend"):
                raw_backend = str(config.get(field) or "").strip().lower()
                try:
                    selected = ComputeVariant(raw_backend)
                except ValueError:
                    continue
                if selected in {
                    ComputeVariant.CPU,
                    ComputeVariant.CUDA,
                    ComputeVariant.VULKAN,
                }:
                    return selected
            return ComputeVariant.CPU
        gpu = bool(config.get(LEGACY_GPU_FLAGS.get(component_id, ""), False))
        if not gpu:
            return ComputeVariant.CPU
        raw_backend = str(
            config.get(
                {
                    "fish_speech": "fishs2_backend",
                    "qwen_tts": "kobold_qwen_backend",
                    "crispasr": "crispasr_backend",
                }.get(component_id, ""),
                "cuda",
            )
            or "cuda"
        ).strip().lower()
        try:
            return ComputeVariant(raw_backend)
        except ValueError:
            return ComputeVariant.CUDA

    @staticmethod
    def _desired_for(
        component_id: str,
        config: dict[str, Any],
    ) -> DesiredComponentState:
        quantization = None
        options: dict[str, Any] = {}
        if component_id == "fish_speech":
            quantization = str(config.get("fishs2_model_quant") or "q6_k")
        elif component_id == "qwen_tts":
            quantization = str(config.get("kobold_qwen_quantization") or "f16")
            options = {
                "model_size": str(config.get("kobold_qwen_model_size") or "1.7b"),
                "models": list(config.get("kobold_qwen_installed_models") or []),
            }
        elif component_id == "crispasr":
            quantization = str(
                config.get("crispasr_model_quantization") or "q8_0"
            )
            options = {
                "engine": str(
                    config.get("crispasr_engine")
                    or "moss-transcribe-diarize-0.9b"
                )
            }
        return DesiredComponentState(
            present=True,
            compute=LegacyImporter._compute(component_id, config),
            quantization=quantization,
            options=options,
        )

    @staticmethod
    def _is_link_or_junction(path: Path) -> bool:
        junction = getattr(path, "is_junction", None)
        return path.is_symlink() or bool(
            junction is not None and junction()
        )

    def _safe_source_bytes(
        self,
        path: Path,
        *,
        required: bool,
        warnings: list[str],
    ) -> bytes | None:
        if not os.path.lexists(path):
            return None
        resolved = path.resolve(strict=False)
        if (
            self._is_link_or_junction(path)
            or not path.is_file()
            or not self.context.layout.contains(
                self.context.layout.root,
                resolved,
            )
        ):
            message = (
                "Legacy control files must be regular files contained in the "
                f"workspace: {path}"
            )
            if required:
                raise ManagerError(
                    "unsafe_legacy_import",
                    message,
                    {"path": str(path)},
                    409,
                )
            warnings.append(message)
            return None
        try:
            return path.read_bytes()
        except OSError as error:
            if required:
                raise ManagerError(
                    "legacy_import_inspection_failed",
                    "The legacy configuration could not be read.",
                    {
                        "path": str(path),
                        "error_type": type(error).__name__,
                    },
                    409,
                ) from error
            warnings.append(
                f"Legacy control file could not be read: {path}"
            )
            return None

    def _source_files(
        self,
        warnings: list[str],
    ) -> tuple[tuple[LegacySourceFile, ...], dict[str, bytes]]:
        records: list[LegacySourceFile] = []
        raw_files: dict[str, bytes] = {}
        for name in ("config.json", *LEGACY_AUXILIARY_FILES):
            path = self.context.layout.root / name
            raw = self._safe_source_bytes(
                path,
                required=name == "config.json",
                warnings=warnings,
            )
            if raw is None:
                continue
            raw_files[name] = raw
            records.append(
                LegacySourceFile(
                    path=str(path.resolve(strict=False)),
                    sha256=hashlib.sha256(raw).hexdigest(),
                    size_bytes=len(raw),
                )
            )
        return tuple(records), raw_files

    @staticmethod
    def _packaging_layout(
        raw_files: dict[str, bytes],
        warnings: list[str],
    ) -> dict[str, Any] | None:
        raw = raw_files.get("packaging_layout.json")
        if raw is None:
            return None
        try:
            value = json.loads(raw.decode("utf-8"))
            if (
                not isinstance(value, dict)
                or value.get("layout_version") != 1
                or not isinstance(value.get("shared_paths"), list)
                or not isinstance(value.get("component_paths"), dict)
            ):
                raise ValueError("unsupported packaging layout schema")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            warnings.append(
                "Legacy packaging layout is invalid and will not grant path "
                f"ownership: {error}"
            )
            return None
        return value

    def _ownership_candidate(
        self,
        relative: str,
        *,
        owner_kind: str,
        owner_id: str,
        evidence: tuple[str, ...],
        warnings: list[str],
    ) -> LegacyOwnershipCandidate | None:
        if "\\" in relative or "\x00" in relative:
            warnings.append(
                f"Ignored unsafe legacy ownership path: {relative!r}"
            )
            return None
        portable = PurePosixPath(relative)
        if (
            not relative
            or portable.is_absolute()
            or ".." in portable.parts
            or any(part in {"", "."} for part in portable.parts)
        ):
            warnings.append(
                f"Ignored unsafe legacy ownership path: {relative!r}"
            )
            return None
        path = self.context.layout.root.joinpath(
            *portable.parts
        )
        if not os.path.lexists(path):
            return None
        resolved = path.resolve(strict=False)
        if (
            self._is_link_or_junction(path)
            or resolved == self.context.layout.root.resolve(strict=False)
            or not self.context.layout.contains(
                self.context.layout.root,
                resolved,
            )
        ):
            warnings.append(
                "Ignored redirected legacy ownership candidate: "
                + str(path)
            )
            return None
        return LegacyOwnershipCandidate(
            path=str(resolved),
            owner_kind=owner_kind,
            owner_id=owner_id,
            evidence=evidence,
        )

    @staticmethod
    def _review_digest(
        *,
        valid: bool,
        source_files: tuple[LegacySourceFile, ...],
        desired: dict[str, DesiredComponentState],
        inspections: dict[str, Any],
        ownership: tuple[LegacyOwnershipCandidate, ...],
        legacy_data: dict[str, Any],
        unknown_paths: tuple[str, ...],
    ) -> str:
        payload = {
            "valid": valid,
            "source_files": [
                item.model_dump(mode="json") for item in source_files
            ],
            "desired": {
                key: value.model_dump(mode="json")
                for key, value in sorted(desired.items())
            },
            "inspections": {
                key: value.model_dump(
                    mode="json",
                    exclude={"inspected_at"},
                )
                for key, value in sorted(inspections.items())
            },
            "ownership": [
                item.model_dump(mode="json") for item in ownership
            ],
            "legacy_data": legacy_data,
            "unknown_paths": list(unknown_paths),
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def inspect(self) -> LegacyImportReport | None:
        if not os.path.lexists(self.source):
            return None
        source_key = str(self.source.resolve(strict=False))
        warnings: list[str] = []
        source_files, raw_files = self._source_files(warnings)
        raw = raw_files["config.json"]
        try:
            legacy_data = legacy_data_inventory(
                self.context.layout
            ).as_dict()
        except ManagerError as error:
            legacy_data = {
                "error": error.message,
                "details": error.details or {},
            }
            warnings.append(
                "Known legacy data could not be safely inventoried: "
                + error.message
            )
        try:
            config = json.loads(raw.decode("utf-8"))
            if not isinstance(config, dict):
                raise ValueError("Legacy configuration is not a JSON object.")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            unknown_paths = self._unknown_paths()
            digest = self._review_digest(
                valid=False,
                source_files=source_files,
                desired={},
                inspections={},
                ownership=(),
                legacy_data=legacy_data,
                unknown_paths=unknown_paths,
            )
            existing = self.store.legacy_import(source_key)
            return LegacyImportReport(
                source=source_key,
                source_digest=digest,
                valid=False,
                already_imported=bool(
                    existing
                    and existing.get("source_digest") == digest
                ),
                warnings=tuple(warnings)
                + (
                    f"Legacy configuration is malformed and will be quarantined: {error}",
                ),
                unknown_paths=unknown_paths,
                legacy_data=legacy_data,
                source_files=source_files,
            )

        desired: dict[str, DesiredComponentState] = {}
        inspections = {}
        identified: list[str] = []
        ownership_by_path: dict[str, LegacyOwnershipCandidate] = {}

        def add_candidate(candidate: LegacyOwnershipCandidate | None) -> None:
            if candidate is None:
                return
            ownership_by_path.setdefault(candidate.path, candidate)

        app_inspection = self.planner.inspect(
            "pandrator",
            DesiredComponentState(present=True, compute=ComputeVariant.CPU),
        )
        if app_inspection.state in {ComponentState.PRESENT, ComponentState.DEGRADED}:
            desired["pandrator"] = DesiredComponentState(
                present=True,
                compute=ComputeVariant.CPU,
            )
            inspections["pandrator"] = app_inspection
            identified.append("pandrator")
            for evidence in app_inspection.evidence:
                if evidence.startswith("slot:"):
                    continue
                top_level = evidence.replace("\\", "/").split("/", 1)[0]
                add_candidate(
                    self._ownership_candidate(
                        top_level,
                        owner_kind="legacy_component",
                        owner_id="pandrator",
                        evidence=tuple(app_inspection.evidence),
                        warnings=warnings,
                    )
                )

        for component_id, flag in LEGACY_COMPONENT_FLAGS.items():
            enabled = bool(config.get(flag, False))
            desired_state = self._desired_for(component_id, config)
            inspection = self.planner.inspect(component_id, desired_state)
            definition = self.registry.definition(component_id)
            marker_evidence = tuple(
                marker
                for marker in definition.markers
                if (self.context.layout.root / marker).exists()
            )
            present = bool(marker_evidence) or inspection.state in {
                ComponentState.PRESENT,
                ComponentState.DEGRADED,
            }
            if enabled or present:
                desired[component_id] = desired_state
                inspections[component_id] = inspection
            if present:
                identified.append(component_id)
                for evidence in marker_evidence or inspection.evidence:
                    if evidence.startswith("slot:"):
                        continue
                    top_level = evidence.replace("\\", "/").split("/", 1)[0]
                    add_candidate(
                        self._ownership_candidate(
                            top_level,
                            owner_kind="legacy_component",
                            owner_id=component_id,
                            evidence=marker_evidence
                            or tuple(inspection.evidence),
                            warnings=warnings,
                        )
                    )
                if not enabled:
                    warnings.append(
                        f"{component_id} installation markers were found even "
                        "though its legacy support flag is disabled."
                    )
            elif enabled:
                warnings.append(
                    f"{component_id} is enabled in legacy configuration but no "
                    "installation markers were found."
                )

        add_candidate(
            self._ownership_candidate(
                "config.json",
                owner_kind="legacy_shared",
                owner_id="legacy-installer-state",
                evidence=("valid legacy configuration",),
                warnings=warnings,
            )
        )
        packaging = self._packaging_layout(raw_files, warnings)
        if packaging is not None:
            for value in packaging["shared_paths"]:
                if not isinstance(value, str):
                    warnings.append(
                        "Ignored a non-string legacy shared path."
                    )
                    continue
                if value not in LEGACY_PACKAGING_SHARED_PATHS:
                    warnings.append(
                        f"Ignored unknown legacy shared path: {value}"
                    )
                    continue
                add_candidate(
                    self._ownership_candidate(
                        value,
                        owner_kind="legacy_shared",
                        owner_id="legacy-installer",
                        evidence=("packaging_layout.json",),
                        warnings=warnings,
                    )
                )
            for legacy_id, values in packaging["component_paths"].items():
                component_id = LEGACY_PACKAGING_COMPONENT_IDS.get(
                    str(legacy_id)
                )
                if (
                    component_id is None
                    or component_id not in identified
                    or not isinstance(values, list)
                ):
                    continue
                allowed = LEGACY_PACKAGING_COMPONENT_PATHS[component_id]
                for value in values:
                    if not isinstance(value, str) or value not in allowed:
                        warnings.append(
                            "Ignored an unknown packaging path for "
                            f"{legacy_id}: {value!r}"
                        )
                        continue
                    add_candidate(
                        self._ownership_candidate(
                            value,
                            owner_kind="legacy_component",
                            owner_id=component_id,
                            evidence=(
                                "packaging_layout.json",
                                f"component:{legacy_id}",
                            ),
                            warnings=warnings,
                        )
                    )
            add_candidate(
                self._ownership_candidate(
                    "packaging_layout.json",
                    owner_kind="legacy_shared",
                    owner_id="legacy-installer-state",
                    evidence=("validated packaging layout v1",),
                    warnings=warnings,
                )
            )
        if "installer_state.json" in raw_files:
            try:
                installer_state = json.loads(
                    raw_files["installer_state.json"].decode("utf-8")
                )
            except (UnicodeDecodeError, json.JSONDecodeError):
                installer_state = None
            if isinstance(installer_state, dict):
                add_candidate(
                    self._ownership_candidate(
                        "installer_state.json",
                        owner_kind="legacy_shared",
                        owner_id="legacy-installer-state",
                        evidence=("valid installer state",),
                        warnings=warnings,
                    )
                )
            else:
                warnings.append(
                    "Legacy installer_state.json is malformed and will be "
                    "preserved as an unknown file."
                )

        fallback_shared_markers = {
            "bin": ("bin/ffmpeg.exe",),
            "Calibre Portable": (
                "Calibre Portable/Calibre/ebook-convert.exe",
            ),
            ".pixi-home": (
                ".pixi-home/bin/pixi",
                ".pixi-home/bin/pixi.exe",
            ),
            "envs/pandrator_installer": (
                "envs/pandrator_installer/pixi.toml",
            ),
        }
        pixi_identified = False
        for relative, markers in fallback_shared_markers.items():
            present_markers = tuple(
                marker
                for marker in markers
                if (self.context.layout.root / marker).is_file()
            )
            if not present_markers:
                continue
            if relative == ".pixi-home":
                pixi_identified = True
            add_candidate(
                self._ownership_candidate(
                    relative,
                    owner_kind="legacy_shared",
                    owner_id="legacy-installer-runtime",
                    evidence=present_markers,
                    warnings=warnings,
                )
            )
        if pixi_identified:
            add_candidate(
                self._ownership_candidate(
                    ".pixi-cache",
                    owner_kind="legacy_shared",
                    owner_id="legacy-installer-runtime",
                    evidence=(".pixi-home/bin/pixi",),
                    warnings=warnings,
                )
            )

        unknown_paths = self._unknown_paths()
        ownership = tuple(
            ownership_by_path[key]
            for key in sorted(ownership_by_path)
        )
        digest = self._review_digest(
            valid=True,
            source_files=source_files,
            desired=desired,
            inspections=inspections,
            ownership=ownership,
            legacy_data=legacy_data,
            unknown_paths=unknown_paths,
        )
        existing = self.store.legacy_import(source_key)
        return LegacyImportReport(
            source=source_key,
            source_digest=digest,
            valid=True,
            already_imported=bool(
                existing
                and existing.get("source_digest") == digest
            ),
            desired=desired,
            inspections=inspections,
            positively_identified=tuple(sorted(identified)),
            unknown_paths=unknown_paths,
            legacy_data=legacy_data,
            source_files=source_files,
            ownership=ownership,
            warnings=tuple(warnings),
        )

    def _unknown_paths(self) -> tuple[str, ...]:
        if not self.context.layout.root.is_dir():
            return ()
        return tuple(
            sorted(
                entry.name
                for entry in self.context.layout.root.iterdir()
                if entry.name not in LEGACY_KNOWN_TOP_LEVEL
                and entry.name not in {
                    "app",
                    "manager",
                    "services",
                    "state",
                    "logs",
                    "data",
                }
            )
        )

    def apply(
        self,
        report: LegacyImportReport,
        *,
        confirmed: bool,
    ) -> int:
        revision, _data_reconciliation = self.apply_with_result(
            report,
            confirmed=confirmed,
        )
        return revision

    def apply_with_result(
        self,
        report: LegacyImportReport,
        *,
        confirmed: bool,
    ) -> tuple[int, dict[str, Any]]:
        if not confirmed:
            raise ValueError("Legacy import requires explicit confirmation.")
        current = self.inspect()
        if current is None or current.source_digest != report.source_digest:
            raise ValueError("Legacy configuration changed after it was inspected.")
        source_key = str(self.source.resolve(strict=False))
        existing = self.store.legacy_import(source_key)
        already_recorded = bool(
            existing
            and existing.get("source_digest") == report.source_digest
        )
        if already_recorded and isinstance(
            existing.get("data_reconciliation"),
            dict,
        ):
            return (
                self.store.configuration_revision(),
                dict(existing["data_reconciliation"]),
            )

        destination_root = (
            self.context.layout.state
            / ("legacy" if report.valid else "quarantine")
        )
        destination_root.mkdir(parents=True, exist_ok=True)
        snapshot_root = destination_root / report.source_digest
        snapshot_root.mkdir(parents=True, exist_ok=True)
        selected_sources = (
            report.source_files
            if report.valid
            else tuple(
                item
                for item in report.source_files
                if Path(item.path).name == "config.json"
            )
        )
        for source_record in selected_sources:
            source = Path(source_record.path).resolve(strict=False)
            if (
                not self.context.layout.contains(
                    self.context.layout.root,
                    source,
                )
                or self._is_link_or_junction(source)
                or not source.is_file()
            ):
                raise ManagerError(
                    "legacy_import_changed",
                    "A reviewed legacy source file is no longer safe.",
                    {"path": str(source)},
                    409,
                )
            raw = source.read_bytes()
            if (
                len(raw) != source_record.size_bytes
                or hashlib.sha256(raw).hexdigest()
                != source_record.sha256
            ):
                raise ManagerError(
                    "legacy_import_changed",
                    "A reviewed legacy source file changed before import.",
                    {"path": str(source)},
                    409,
                )
            destination = snapshot_root / source.name
            if destination.is_file():
                if destination.read_bytes() != raw:
                    raise ManagerError(
                        "legacy_snapshot_conflict",
                        "An existing legacy snapshot has different contents.",
                        {"path": str(destination)},
                        409,
                    )
                continue
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{source.name}.",
                suffix=".tmp",
                dir=snapshot_root,
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

        data_reconciliation: dict[str, Any] = {
            "created": [],
            "preserved": [],
            "conflicts": [],
            "source_bytes": 0,
            "source_files": 0,
            "sources_retained": True,
        }
        if report.valid:
            inventory = legacy_data_inventory(self.context.layout)
            if inventory.as_dict() != report.legacy_data:
                raise ManagerError(
                    "legacy_import_changed",
                    "Known legacy data changed after the migration review.",
                    {
                        "reviewed_revision": report.legacy_data.get(
                            "revision_digest"
                        ),
                        "current_revision": inventory.revision_digest,
                    },
                    409,
                )
            data_reconciliation = reconcile_legacy_data(
                self.context.layout,
                inventory=inventory,
            )
            if not already_recorded:
                for component_id, inspection in report.inspections.items():
                    desired = report.desired.get(component_id)
                    self.store.save_component(inspection, desired=desired)
                for candidate in report.ownership:
                    path = Path(candidate.path).resolve(strict=False)
                    if (
                        path
                        == self.context.layout.root.resolve(strict=False)
                        or not self.context.layout.contains(
                            self.context.layout.root,
                            path,
                        )
                        or self._is_link_or_junction(path)
                        or not os.path.lexists(path)
                        or not candidate.evidence
                    ):
                        raise ManagerError(
                            "legacy_import_changed",
                            "A reviewed legacy ownership candidate is no longer safe.",
                            {"path": str(path)},
                            409,
                        )
                    self.store.record_owned_path(
                        path,
                        owner_kind=candidate.owner_kind,
                        owner_id=candidate.owner_id,
                        evidence={
                            "markers": list(candidate.evidence),
                            "legacy_data": report.legacy_data,
                            "sources_retained": True,
                            "review_digest": report.source_digest,
                        },
                    )
                revision = self.store.bump_configuration_revision()
            else:
                revision = self.store.configuration_revision()
        else:
            revision = self.store.configuration_revision()

        stored_report = report.model_dump(mode="json")
        stored_report["data_reconciliation"] = data_reconciliation
        self.store.record_legacy_import(
            source_key=source_key,
            source_digest=report.source_digest,
            report=stored_report,
        )
        self.context.event_sink.emit(
            "legacy.imported",
            {
                "source": source_key,
                "valid": report.valid,
                "revision": revision,
            },
        )
        return revision, data_reconciliation
