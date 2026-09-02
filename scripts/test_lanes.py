"""Validate and run Pandrator's explicit test lanes."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FAST_LANE = "fast-xdist"

# Keep every path explicit: validation deliberately rejects a new test until it
# has been assigned to exactly one lane.
TEST_LANES: dict[str, tuple[str, ...]] = {
    FAST_LANE: (
        "tests/test_appimage_utils.py",
        "tests/test_audio_variant_handler.py",
        "tests/test_audio_verification.py",
        "tests/test_build_release_packages.py",
        "tests/test_dependency_manifests.py",
        "tests/test_dubbing_artifacts.py",
        "tests/test_dubbing_boundary_correction.py",
        "tests/test_dubbing_cloud_stt.py",
        "tests/test_dubbing_credentials.py",
        "tests/test_dubbing_llm_correction.py",
        "tests/test_dubbing_llm_translation.py",
        "tests/test_dubbing_manual_timing.py",
        "tests/test_dubbing_run_usage.py",
        "tests/test_dubbing_settings.py",
        "tests/test_dubbing_speech_blocks_integration.py",
        "tests/test_dubbing_subtitle_logic.py",
        "tests/test_dubbing_transcription.py",
        "tests/test_elevenlabs_tts.py",
        "tests/test_guided_dubbing_workflow.py",
        "tests/test_llm_handler.py",
        "tests/test_nemo_normalizer.py",
        "tests/test_pdf_ingestion.py",
        "tests/test_rvc_handler.py",
        "tests/test_sentence_segmenter.py",
        "tests/test_source_cleaning.py",
        "tests/test_subtitle_finalization.py",
        "tests/test_test_lanes.py",
        "tests/test_text_preprocessor.py",
        "tests/test_transcript_normalization.py",
        "tests/test_tts_endpoint_discovery.py",
        "tests/test_tts_handler.py",
        "tests/test_tts_provider_profiles.py",
        "tests/test_version.py",
        "tests/test_xtts_trainer_registry.py",
    ),
    "installer-serial": (
        "tests/test_installer_architecture.py",
        "tests/test_installer_lifecycle.py",
        "tests/test_installer_rvc_service.py",
        "tests/test_installer_update_migrations.py",
        "tests/test_installer_web_readiness.py",
    ),
    "manager-serial": (
        "tests/test_manager_audiocpp.py",
        "tests/test_manager_automation.py",
        "tests/test_manager_build.py",
        "tests/test_manager_control_plane.py",
        "tests/test_manager_core.py",
        "tests/test_manager_desktop.py",
        "tests/test_manager_diagnostics.py",
        "tests/test_manager_doctor.py",
        "tests/test_manager_guided_setup.py",
        "tests/test_manager_launcher.py",
        "tests/test_manager_network.py",
        "tests/test_manager_operations.py",
        "tests/test_manager_releases.py",
        "tests/test_manager_tls.py",
        "tests/test_manager_tray_menu.py",
        "tests/test_manager_uninstall.py",
    ),
    "mcp-serial": (
        "tests/test_mcp_application_client.py",
        "tests/test_mcp_architecture.py",
        "tests/test_mcp_dispatch.py",
        "tests/test_mcp_source_cleaning_dispatch.py",
        "tests/test_mcp_doctor.py",
        "tests/test_mcp_enrollment.py",
        "tests/test_mcp_e2e.py",
        "tests/test_mcp_host_config.py",
        "tests/test_mcp_http.py",
        "tests/test_mcp_server.py",
        "tests/test_mcp_speech_optimization_dispatch.py",
        "tests/test_mcp_targets_cli.py",
    ),
    "system-media-serial": (
        "tests/test_dubbing_audio_sync.py",
        "tests/test_frontend_architecture.py",
        "tests/test_phase0_baseline.py",
        "tests/test_state_db_handler.py",
    ),
    "web-01-serial": (
        "tests/test_web_dispatch.py",
        "tests/test_web_source_cleaning_dispatch.py",
        "tests/test_web_parity_workspace.py",
    ),
    "web-02-serial": (
        "tests/test_web_foundation.py",
        "tests/test_web_artifact_selection.py",
        "tests/test_web_credentials.py",
        "tests/test_web_manager_proxy.py",
        "tests/test_web_workflow_plans.py",
    ),
    "web-03-serial": (
        "tests/test_web_security.py",
        "tests/test_web_voice_library.py",
        "tests/test_web_work_api.py",
        "tests/test_web_workflow_handlers.py",
    ),
    "web-04-serial": (
        "tests/test_web_settings_api.py",
        "tests/test_web_provider_api.py",
        "tests/test_web_automation_security.py",
        "tests/test_web_database_efficiency.py",
    ),
    "web-05-serial": (
        "tests/test_web_advanced_parity.py",
        "tests/test_web_agentic_runs.py",
        "tests/test_web_audio_assembly.py",
        "tests/test_web_audio_streaming.py",
        "tests/test_web_backend_architecture.py",
        "tests/test_web_capabilities.py",
        "tests/test_web_job_concurrency.py",
        "tests/test_web_pdf_editor.py",
        "tests/test_web_process_guard.py",
        "tests/test_web_pronunciations.py",
        "tests/test_web_research.py",
        "tests/test_web_session_bundles.py",
        "tests/test_web_session_forks.py",
        "tests/test_web_speech_planning.py",
        "tests/test_web_speech_optimization_dispatch.py",
        "tests/test_web_startup_maintenance.py",
        "tests/test_web_subtitle_review.py",
        "tests/test_web_supervisor.py",
        "tests/test_web_tts_optimization.py",
        "tests/test_web_translation_source_repair.py",
        "tests/test_web_xtts_model_upload.py",
    ),
}


class TestLaneManifestError(ValueError):
    """The manifest does not exactly cover the repository test files."""


class TestLaneUsageError(ValueError):
    """The selected lanes cannot safely run in one pytest invocation."""


def discover_test_files(repo_root: Path = REPO_ROOT) -> set[str]:
    """Return tracked-by-convention test paths relative to ``repo_root``."""
    return {
        path.relative_to(repo_root).as_posix()
        for path in (repo_root / "tests").glob("test_*.py")
        if path.is_file()
    }


def validate_manifest(
    manifest: Mapping[str, Sequence[str]] = TEST_LANES,
    *,
    discovered: Iterable[str] | None = None,
) -> None:
    """Require every current ``tests/test_*.py`` path exactly once."""
    expected = set(discover_test_files() if discovered is None else discovered)
    assigned_to: dict[str, list[str]] = {}
    for lane, paths in manifest.items():
        for path in paths:
            assigned_to.setdefault(path, []).append(lane)

    assigned = set(assigned_to)
    unknown = sorted(assigned - expected)
    missing = sorted(expected - assigned)
    duplicates = {
        path: lanes for path, lanes in sorted(assigned_to.items()) if len(lanes) > 1
    }
    if not (unknown or missing or duplicates):
        return

    details: list[str] = ["Test-lane manifest is invalid:"]
    if unknown:
        details.append(f"  unknown paths: {', '.join(unknown)}")
    if missing:
        details.append(f"  missing paths: {', '.join(missing)}")
    if duplicates:
        duplicate_paths = ", ".join(
            f"{path} ({', '.join(lanes)})" for path, lanes in duplicates.items()
        )
        details.append(f"  duplicate paths: {duplicate_paths}")
    raise TestLaneManifestError("\n".join(details))


def lane_payload(
    manifest: Mapping[str, Sequence[str]] = TEST_LANES,
) -> dict[str, list[dict[str, object]]]:
    """Return lane metadata in manifest order for human and machine consumers."""
    return {
        "lanes": [
            {"name": name, "files": list(paths)} for name, paths in manifest.items()
        ]
    }


def select_lanes(lane_names: Sequence[str]) -> tuple[list[str], list[str]]:
    """De-duplicate lane names and files while preserving manifest selection order."""
    selected_lanes: list[str] = []
    for lane in lane_names:
        if lane not in TEST_LANES:
            raise TestLaneUsageError(f"Unknown test lane: {lane}")
        if lane not in selected_lanes:
            selected_lanes.append(lane)

    if FAST_LANE in selected_lanes and len(selected_lanes) > 1:
        raise TestLaneUsageError(
            "fast-xdist cannot be combined with serial test lanes; run it separately."
        )

    files: list[str] = []
    for lane in selected_lanes:
        for path in TEST_LANES[lane]:
            if path not in files:
                files.append(path)
    return selected_lanes, files


def build_pytest_command(
    lane_names: Sequence[str], junitxml: Path | None = None
) -> list[str]:
    """Build the exact pytest command for the selected manifest entries."""
    selected_lanes, files = select_lanes(lane_names)
    command = [sys.executable, "-m", "pytest", "-q"]
    if selected_lanes == [FAST_LANE]:
        command.extend(("-n", "2", "--dist=loadfile"))
    if junitxml is not None:
        command.extend(("--junitxml", str(junitxml)))
    command.extend(files)
    return command


def run_lanes(lane_names: Sequence[str], junitxml: Path | None = None) -> int:
    """Validate the manifest and run selected lanes in one safe invocation."""
    validate_manifest()
    if junitxml is not None:
        junitxml.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(build_pytest_command(lane_names, junitxml), check=False)
    return completed.returncode


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("check", help="Validate exact test-lane coverage.")
    list_parser = subcommands.add_parser("list", help="List configured test lanes.")
    list_parser.add_argument(
        "--json", action="store_true", help="Emit stable machine-readable JSON."
    )
    run_parser = subcommands.add_parser("run", help="Run one or more test lanes.")
    run_parser.add_argument("lanes", metavar="LANE", nargs="+")
    run_parser.add_argument(
        "--junitxml", type=Path, metavar="PATH", help="Write pytest JUnit XML to PATH."
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line interface without hiding pytest's exit status."""
    arguments = _build_parser().parse_args(argv)
    try:
        if arguments.command == "check":
            validate_manifest()
            return 0
        if arguments.command == "list":
            validate_manifest()
            payload = lane_payload()
            if arguments.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                for lane in payload["lanes"]:
                    print(lane["name"])
                    for path in lane["files"]:
                        print(f"  {path}")
            return 0
        return run_lanes(arguments.lanes, arguments.junitxml)
    except (TestLaneManifestError, TestLaneUsageError) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
