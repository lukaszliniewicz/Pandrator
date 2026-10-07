"""Disposable recast impact, historical provenance and scoped render contracts."""

from copy import deepcopy
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from pandrator.web import models as m
from pandrator.web.generation_audio_identity import plan_audio_identities
from pandrator.web.generation_cast_runtime import segment_render_parts
from pandrator.web.generation_controls import get_generation_controls, save_generation_controls
from pandrator.web.generation_performance_snapshot import performance_runtime_settings
from pandrator.web.generation_recast import prepare_recast
from pandrator.web.speech_plan_workspace import freeze_speech_snapshot
from tests.test_performance_plans import case as case


@pytest.fixture
def recast_case(case):
    services = case["services"]
    services["workspace_settings"].update(
        case["session_id"],
        "tts",
        0,
        {
            "service": "gemini",
            "model": "gemini-2.5-flash-tts",
            "voice": "Kore",
            "casting_enabled": True,
            "voice_mode_version": 1,
            "performance_enabled": False,
        },
    )
    with services["database"].session() as session:
        save_generation_controls(
            session,
            case["session_id"],
            expected_revision=0,
            characters=[{"id": "fred", "display_name": "Fred", "voice_category": "male"}],
            cast={"narrator": {"voice": "Kore"}, "characters": {"fred": {"voice": "Puck"}}},
        )
        for segment_id in case["segment_ids"]:
            session.get(m.GenerationSegment, segment_id).speaker = None
        segment = session.get(m.GenerationSegment, case["segment_ids"][0])
        segment.text = "He said hello."
        segment.speech_plan_json = {
            "speech_xml": f'<segment id="{segment.id}">He said <dialogue><speaker ref="fred">hello.</speaker></dialogue></segment>'
        }
    return case


def prepare(case, **kwargs):
    services = case["services"]
    return prepare_recast(
        services["database"], services["workspace_settings"], case["session_id"], **kwargs
    )


def record(case, *, segment_ids=None):
    services = case["services"]
    with services["database"].session() as session:
        snapshot, _ = services["workspace_settings"].resolve_in_session(session, case["session_id"])
        freeze_speech_snapshot(session, case["revision_id"], snapshot, explicit=True)
        snapshot["generation_audio_identities"] = plan_audio_identities(
            session, case["revision_id"], snapshot
        )
        run = m.GenerationRun(
            session_id=case["session_id"],
            plan_revision_id=case["revision_id"],
            status="completed",
            sequence_number=1,
            settings_snapshot_json=deepcopy(snapshot),
        )
        session.add(run)
        session.flush()
        for segment_id in segment_ids or case["segment_ids"]:
            artifact = m.Artifact(
                session_id=case["session_id"],
                kind="audio",
                relative_path=f"{segment_id}.wav",
                content_hash="waveform",
                metadata_json={
                    "generation_audio_identity": snapshot["generation_audio_identities"][
                        segment_id
                    ],
                    "generation_task_run_id": run.id,
                },
            )
            session.add(artifact)
            session.flush()
            session.add(
                m.AudioTake(
                    generation_segment_id=segment_id,
                    generation_run_id=run.id,
                    artifact_id=artifact.id,
                    is_active=True,
                    status="completed",
                )
            )
            session.get(m.GenerationSegment, segment_id).status = "completed"
        return run.id


def change(case, *, narrator="Charon", fred="Fenrir"):
    with case["services"]["database"].session() as session:
        controls = get_generation_controls(session, case["session_id"])
        cast = controls["cast"]
        cast["narrator"] = {"voice": narrator}
        cast.setdefault("characters", {})["fred"] = {"voice": fred}
        save_generation_controls(
            session, case["session_id"], expected_revision=controls["revision"], cast=cast
        )


def update_settings(case, **values):
    settings = case["services"]["workspace_settings"]
    current = settings.get(case["session_id"], "tts")
    settings.update(
        case["session_id"], "tts", current["revision"], {**current["override"], **values}
    )


def voices(case, prepared):
    snapshot = prepared["frozen_snapshot"]
    return [
        part["settings"]["voice"]
        for part in segment_render_parts(
            performance_runtime_settings(snapshot),
            snapshot,
            case["segment_ids"][0],
            "He said hello.",
        )
    ]


def test_fred_only_retains_recorded_narrator_and_current_project_settings(recast_case):
    case = recast_case
    record(case)
    change(case)
    prepared = prepare(case, selector_keys=["character:fred"])
    assert prepared["preview"]["blocked_reason"] is None
    assert prepared["target_segment_ids"] == case["segment_ids"][:1]
    assert prepared["preview"]["shared_segment_count"] == 1
    assert voices(case, prepared) == ["Kore", "Fenrir"]
    assert (
        prepared["frozen_snapshot"]["generation_control_snapshot"]["controls"]["cast"]["narrator"][
            "voice"
        ]
        == "Charon"
    )
    with case["services"]["database"].session() as session:
        assert (
            plan_audio_identities(
                session,
                case["revision_id"],
                prepared["frozen_snapshot"],
                segment_ids=prepared["target_segment_ids"],
            )
            == prepared["frozen_snapshot"]["generation_audio_identities"]
        )


def test_two_selectors_share_one_logical_segment(recast_case):
    case = recast_case
    record(case)
    change(case)
    prepared = prepare(case, selector_keys=["character:fred", "narrator"])
    assert prepared["preview"]["generate_count"] == 3
    assert len(prepared["target_segment_ids"]) == len(set(prepared["target_segment_ids"]))
    assert prepared["preview"]["shared_segment_count"] == 0
    assert voices(case, prepared) == ["Charon", "Fenrir"]


@pytest.mark.parametrize("kind", ["segment", "span"])
def test_explicit_overrides_win(recast_case, kind):
    case = recast_case
    with case["services"]["database"].session() as session:
        segment = session.get(m.GenerationSegment, case["segment_ids"][0])
        if kind == "segment":
            segment.voice = "Puck"
        else:
            segment.speech_plan_json = {
                "speech_xml": f'<segment id="{segment.id}">He said <dialogue><speaker ref="fred" voice="Puck">hello.</speaker></dialogue></segment>'
            }
    record(case)
    change(case)
    preview = prepare(case, selector_keys=["character:fred"], force=True)["preview"]
    fred = next(row for row in preview["selectors"] if row["key"] == "character:fred")
    assert fred["affected_count"] == 0 and fred["overridden_count"] == 1
    assert preview["generate_count"] == 0


def test_noop_names_notes_and_unused_defaults(recast_case):
    case = recast_case
    record(case)
    with case["services"]["database"].session() as session:
        controls = get_generation_controls(session, case["session_id"])
        controls["characters"][0].update(
            display_name="Frederick", notes="A note", aliases=["Freddy"]
        )
        save_generation_controls(
            session,
            case["session_id"],
            expected_revision=controls["revision"],
            characters=controls["characters"],
        )
    update_settings(
        case, voice="Unused", generation_prompt="Read softly.", model="gemini-2.5-pro-tts"
    )
    preview = prepare(case)["preview"]
    assert preview["generate_count"] == 0
    assert all(row["changed_count"] == 0 for row in preview["selectors"])


def test_strict_single_voice_blocks(recast_case):
    case = recast_case
    record(case)
    change(case)
    update_settings(case, casting_enabled=False)
    preview = prepare(case, selector_keys=["character:fred"], force=True)["preview"]
    assert "multiple voices" in preview["blocked_reason"]


def test_missing_requires_explicit_optin(recast_case):
    case = recast_case
    record(case, segment_ids=case["segment_ids"][1:])
    change(case)
    assert (
        prepare(case, selector_keys=["character:fred"], force=True)["preview"]["generate_count"]
        == 0
    )
    prepared = prepare(case, selector_keys=["character:fred"], include_missing=True)
    assert prepared["preview"]["generate_count"] == prepared["preview"]["missing_count"] == 1


def test_actual_executor_metadata_beats_container_run(recast_case):
    case = recast_case
    executor_id = record(case)
    with case["services"]["database"].session() as session:
        wrong = m.GenerationRun(
            session_id=case["session_id"],
            plan_revision_id=case["revision_id"],
            sequence_number=2,
            status="completed",
            settings_snapshot_json={},
        )
        session.add(wrong)
        session.flush()
        for take in session.scalars(select(m.AudioTake)):
            take.generation_run_id = wrong.id
    change(case)
    prepared = prepare(case, selector_keys=["character:fred"])
    assert prepared["preview"]["blocked_reason"] is None
    assert voices(case, prepared) == ["Kore", "Fenrir"]
    assert executor_id


def test_preview_is_readonly_stable_and_detects_take_revision_drift(recast_case):
    case = recast_case
    record(case)
    change(case)
    models = (m.GenerationRun, m.AudioTake, m.Artifact, m.KnowledgeLedger)
    with case["services"]["database"].session() as session:
        before = [session.scalar(select(func.count()).select_from(model)) for model in models]
    first = prepare(case, selector_keys=["character:fred"])
    second = prepare(case, selector_keys=["character:fred"])
    assert first["preview"]["selection_hash"] == second["preview"]["selection_hash"]
    with case["services"]["database"].session() as session:
        assert before == [
            session.scalar(select(func.count()).select_from(model)) for model in models
        ]
        take = session.scalar(
            select(m.AudioTake).where(m.AudioTake.generation_segment_id == case["segment_ids"][0])
        )
        take.revision += 1
    assert (
        prepare(case, selector_keys=["character:fred"])["preview"]["selection_hash"]
        != first["preview"]["selection_hash"]
    )


def test_old_null_narrator_keeps_recorded_base_voice(recast_case):
    case = recast_case
    with case["services"]["database"].session() as session:
        controls = get_generation_controls(session, case["session_id"])
        controls["cast"]["narrator"] = None
        save_generation_controls(
            session,
            case["session_id"],
            expected_revision=controls["revision"],
            cast=controls["cast"],
        )
    record(case)
    update_settings(case, voice="Charon")
    change(case)
    prepared = prepare(case, selector_keys=["character:fred"])
    assert prepared["preview"]["blocked_reason"] is None
    assert voices(case, prepared) == ["Kore", "Fenrir"]


def test_active_run_blocks_and_changes_hash(recast_case):
    case = recast_case
    record(case)
    change(case)
    before = prepare(case, selector_keys=["character:fred"])["preview"]["selection_hash"]
    with case["services"]["database"].session() as session:
        session.add(
            m.GenerationRun(
                session_id=case["session_id"],
                plan_revision_id=case["revision_id"],
                sequence_number=2,
                status="running",
                settings_snapshot_json={},
            )
        )
    preview = prepare(case, selector_keys=["character:fred"])["preview"]
    assert "active generation run" in preview["blocked_reason"]
    assert preview["selection_hash"] != before


def test_unknown_selector_is_rejected(recast_case):
    with pytest.raises(ValueError, match="selector"):
        prepare(recast_case, selector_keys=["character:other-session"])


def test_selected_character_inherits_current_category_without_recasting_narrator(recast_case):
    case = recast_case
    with case["services"]["database"].session() as session:
        controls = get_generation_controls(session, case["session_id"])
        controls["cast"]["characters"] = {}
        controls["cast"]["categories"] = {"male": {"voice": "Puck"}}
        save_generation_controls(
            session,
            case["session_id"],
            expected_revision=controls["revision"],
            cast=controls["cast"],
        )
    record(case)
    with case["services"]["database"].session() as session:
        controls = get_generation_controls(session, case["session_id"])
        controls["cast"]["narrator"] = {"voice": "Charon"}
        controls["cast"]["categories"]["male"] = {"voice": "Fenrir"}
        save_generation_controls(
            session,
            case["session_id"],
            expected_revision=controls["revision"],
            cast=controls["cast"],
        )
    prepared = prepare(case, selector_keys=["character:fred"])
    assert prepared["preview"]["blocked_reason"] is None
    assert voices(case, prepared) == ["Kore", "Fenrir"]


def test_managed_reference_fingerprint_changes_but_profile_name_does_not(recast_case):
    case = recast_case
    with case["services"]["database"].session() as session:
        voice = m.Voice(
            name="Fred profile",
            metadata_json={
                "providers": {
                    "gemini": {
                        "status": "ready",
                        "provider_voice_id": "Puck",
                        "sample_hash": "original",
                    }
                }
            },
        )
        session.add(voice)
        session.flush()
        voice_id = voice.id
        controls = get_generation_controls(session, case["session_id"])
        controls["cast"]["characters"]["fred"] = {"voice_id": voice_id}
        save_generation_controls(
            session,
            case["session_id"],
            expected_revision=controls["revision"],
            cast=controls["cast"],
        )
    record(case)
    with case["services"]["database"].session() as session:
        voice = session.get(m.Voice, voice_id)
        voice.name = "Renamed harmlessly"
        voice.revision += 1
    assert prepare(case)["preview"]["generate_count"] == 0
    with case["services"]["database"].session() as session:
        voice = session.get(m.Voice, voice_id)
        metadata = deepcopy(voice.metadata_json)
        metadata["providers"]["gemini"]["sample_hash"] = "replacement"
        voice.metadata_json = metadata
    preview = prepare(case, selector_keys=["character:fred"])["preview"]
    fred = next(row for row in preview["selectors"] if row["key"] == "character:fred")
    assert fred["changed_count"] == 1
    assert preview["generate_count"] == 1


def test_parent_take_lineage_recovers_executor_snapshot(recast_case):
    case = recast_case
    record(case)
    with case["services"]["database"].session() as session:
        original = session.scalar(
            select(m.AudioTake).where(m.AudioTake.generation_segment_id == case["segment_ids"][0])
        )
        original.is_active = False
        wrapper = m.GenerationRun(
            session_id=case["session_id"],
            plan_revision_id=case["revision_id"],
            sequence_number=2,
            status="completed",
            settings_snapshot_json={},
        )
        artifact = m.Artifact(
            session_id=case["session_id"],
            kind="audio",
            relative_path="copy.wav",
            content_hash="waveform",
            metadata_json={},
        )
        session.add_all([wrapper, artifact])
        session.flush()
        session.add(
            m.AudioTake(
                generation_segment_id=original.generation_segment_id,
                generation_run_id=wrapper.id,
                artifact_id=artifact.id,
                parent_take_id=original.id,
                is_active=True,
                status="completed",
            )
        )
    change(case)
    prepared = prepare(case, selector_keys=["character:fred"])
    assert prepared["preview"]["blocked_reason"] is None
    assert voices(case, prepared) == ["Kore", "Fenrir"]


def test_unknown_baseline_is_not_automatically_selected_and_partial_force_blocks(recast_case):
    case = recast_case
    executor_id = record(case)
    with case["services"]["database"].session() as session:
        session.get(m.GenerationRun, executor_id).settings_snapshot_json = {}
    change(case)
    preview = prepare(case)["preview"]
    fred = next(row for row in preview["selectors"] if row["key"] == "character:fred")
    assert fred["unverified_count"] == 1
    assert preview["generate_count"] == 0
    forced = prepare(case, selector_keys=["character:fred"], force=True)["preview"]
    assert "no verified executor cast snapshot" in forced["blocked_reason"]


def test_terminal_generation_jobs_do_not_block_recast(recast_case):
    from pandrator.web.generation_recast import collect_recast_job_guard
    from pandrator.web.jobs import JobQueue

    case = recast_case
    executor_id = record(case)
    change(case)
    with case["services"]["database"].session() as session:
        run = session.get(m.GenerationRun, executor_id)
        job = m.Job(
            kind="generation.run",
            session_id=case["session_id"],
            status="succeeded",
            payload_json={"generation_run_id": executor_id},
        )
        session.add(job)
        session.flush()
        run.job_id = job.id
        for status in JobQueue.GENERATION_JOB_TERMINAL_STATUS:
            job.status = status
            session.flush()
            assert collect_recast_job_guard(session, case["session_id"], case["revision_id"]) == []
        job.status = "succeeded"
    prepared = prepare(case, selector_keys=["character:fred"])
    assert prepared["preview"]["blocked_reason"] is None
    assert prepared["preview"]["generate_count"] == 1


def test_failed_run_with_attached_running_generation_job_blocks_and_hashes(recast_case):
    case = recast_case
    executor_id = record(case)
    change(case)
    before = prepare(case, selector_keys=["character:fred"])["preview"]["selection_hash"]
    with case["services"]["database"].session() as session:
        run = session.get(m.GenerationRun, executor_id)
        run.status = "failed"
        job = m.Job(
            kind="generation.run",
            session_id=case["session_id"],
            status="running",
            payload_json={"generation_run_id": executor_id},
        )
        session.add(job)
        session.flush()
        job_id = job.id
        run.job_id = job_id
    prepared = prepare(case, selector_keys=["character:fred"])
    assert prepared["row_state"]["active_runs"] == []
    assert prepared["active_job_guard"] == [[job_id, "running", executor_id]]
    assert "active generation run" in prepared["preview"]["blocked_reason"]
    assert prepared["preview"]["selection_hash"] != before
    with case["services"]["database"].session() as session:
        session.get(m.Job, job_id).status = "failed"
        session.add(
            m.Job(
                kind="tts.preview",
                session_id=case["session_id"],
                status="running",
                payload_json={"generation_run_id": executor_id},
            )
        )
    assert prepare(case, selector_keys=["character:fred"])["preview"]["blocked_reason"] is None


def test_default_preview_and_normalized_start_options_have_identical_hash(recast_case):
    case = recast_case
    record(case)
    change(case)
    automatic = prepare(case)
    explicit = prepare(
        case,
        selector_keys=automatic["preview"]["selected_keys"],
        speech_plan_revision_id=case["revision_id"],
    )
    assert automatic["preview"]["selection_hash"] == explicit["preview"]["selection_hash"]
    assert automatic["target_segment_ids"] == explicit["target_segment_ids"]
    assert prepare(case, selector_keys=[])["target_segment_ids"] == []


def seed_two_managed_recordings(case, *, legacy_narrator=False):
    with case["services"]["database"].session() as session:
        voice_ids = {}
        for key, provider_voice in (("narrator", "Kore"), ("fred", "Puck")):
            voice = m.Voice(
                name=key,
                metadata_json={
                    "providers": {
                        "gemini": {
                            "status": "ready",
                            "provider_voice_id": provider_voice,
                            "sample_hash": "original",
                        }
                    }
                },
            )
            session.add(voice)
            session.flush()
            voice_ids[key] = voice.id
        controls = get_generation_controls(session, case["session_id"])
        controls["cast"]["narrator"] = {"voice_id": voice_ids["narrator"]}
        controls["cast"]["characters"]["fred"] = {"voice_id": voice_ids["fred"]}
        save_generation_controls(
            session,
            case["session_id"],
            expected_revision=controls["revision"],
            cast=controls["cast"],
        )
    executor_id = record(case)
    if legacy_narrator:
        with case["services"]["database"].session() as session:
            executor = session.get(m.GenerationRun, executor_id)
            snapshot = deepcopy(executor.settings_snapshot_json)
            for resolution in snapshot["generation_control_snapshot"]["resolved_bindings"].values():
                if resolution.get("voice_id") == voice_ids["narrator"]:
                    resolution.pop("_recast_reference_fingerprint", None)
            executor.settings_snapshot_json = snapshot
    return voice_ids


def change_managed_material(case, voice_id):
    with case["services"]["database"].session() as session:
        voice = session.get(m.Voice, voice_id)
        metadata = deepcopy(voice.metadata_json)
        metadata["providers"]["gemini"]["sample_hash"] = "replacement"
        voice.metadata_json = metadata


@pytest.mark.parametrize("legacy_narrator", [False, True])
def test_unselected_managed_reference_change_blocks_partial_recast(recast_case, legacy_narrator):
    case = recast_case
    voice_ids = seed_two_managed_recordings(case, legacy_narrator=legacy_narrator)
    change_managed_material(case, voice_ids["narrator"])
    partial = prepare(case, selector_keys=["character:fred"], force=True)
    assert partial["preview"]["blocked_reason"] == (
        "An unselected speaker’s recorded reference changed. Include that speaker or restore its earlier reference before recasting this shared segment."
    )
    both = prepare(case, selector_keys=["character:fred", "narrator"], force=True)
    assert both["preview"]["blocked_reason"] is None


def test_selected_managed_material_change_retains_unchanged_managed_narrator(recast_case):
    case = recast_case
    voice_ids = seed_two_managed_recordings(case)
    change_managed_material(case, voice_ids["fred"])
    prepared = prepare(case, selector_keys=["character:fred"])
    assert prepared["preview"]["blocked_reason"] is None
    assert prepared["target_segment_ids"] == case["segment_ids"][:1]
    assert voices(case, prepared) == ["Kore", "Puck"]


@pytest.mark.parametrize("legacy_narrator", [False, True])
def test_unselected_managed_pending_reassignment_keeps_old_unchanged_reference(
    recast_case, legacy_narrator
):
    case = recast_case
    voice_ids = seed_two_managed_recordings(case, legacy_narrator=legacy_narrator)
    with case["services"]["database"].session() as session:
        controls = get_generation_controls(session, case["session_id"])
        controls["cast"]["narrator"] = {"voice_id": voice_ids["fred"]}
        controls["cast"]["characters"]["fred"] = {"voice": "Fenrir"}
        save_generation_controls(
            session,
            case["session_id"],
            expected_revision=controls["revision"],
            cast=controls["cast"],
        )
    prepared = prepare(case, selector_keys=["character:fred"])
    assert prepared["preview"]["blocked_reason"] is None
    assert voices(case, prepared) == ["Kore", "Fenrir"]


def set_incompatible_pending_narrator(case):
    with case["services"]["database"].session() as session:
        controls = get_generation_controls(session, case["session_id"])
        controls["cast"]["narrator"] = {"voice": "Charon", "service": "xtts"}
        controls["cast"]["characters"]["fred"] = {"voice": "Fenrir"}
        save_generation_controls(
            session,
            case["session_id"],
            expected_revision=controls["revision"],
            cast=controls["cast"],
        )


def test_incompatible_pending_narrator_does_not_block_fred_only_recast(recast_case):
    case = recast_case
    record(case)
    set_incompatible_pending_narrator(case)
    prepared = prepare(case, selector_keys=["character:fred"])
    assert prepared["preview"]["blocked_reason"] is None
    narrator = next(row for row in prepared["preview"]["selectors"] if row["key"] == "narrator")
    assert "assigned to xtts" in narrator["error"]
    assert voices(case, prepared) == ["Kore", "Fenrir"]
    assert set(prepared["frozen_snapshot"]["generation_audio_identities"]) == {
        case["segment_ids"][0]
    }
    assert set(prepared["frozen_snapshot"]["generation_control_snapshot"]["segments"]) == set(
        case["segment_ids"]
    )
    selected_bad = prepare(case, selector_keys=["narrator"], force=True)
    assert "assigned to xtts" in selected_bad["preview"]["blocked_reason"]


def test_incompatible_pending_speaker_outside_target_is_not_compiled(recast_case):
    from pandrator.web.generation_audio_identity import AudioIdentityContext

    case = recast_case
    outside_id = case["segment_ids"][1]
    with case["services"]["database"].session() as session:
        controls = get_generation_controls(session, case["session_id"])
        controls["characters"].append(
            {"id": "other", "display_name": "Other", "voice_category": "male"}
        )
        controls["cast"]["characters"]["other"] = {"voice": "Charon"}
        save_generation_controls(
            session,
            case["session_id"],
            expected_revision=controls["revision"],
            characters=controls["characters"],
            cast=controls["cast"],
        )
        segment = session.get(m.GenerationSegment, outside_id)
        segment.speech_plan_json = {
            "speech_xml": f'<segment id="{outside_id}"><dialogue><speaker ref="other">{segment.text}</speaker></dialogue></segment>'
        }
    record(case)
    with case["services"]["database"].session() as session:
        controls = get_generation_controls(session, case["session_id"])
        controls["cast"]["characters"]["other"] = {"voice": "Charon", "service": "xtts"}
        controls["cast"]["characters"]["fred"] = {"voice": "Fenrir"}
        save_generation_controls(
            session,
            case["session_id"],
            expected_revision=controls["revision"],
            cast=controls["cast"],
        )
    identities_compiled = []
    original = AudioIdentityContext.for_segment

    def inspect(context, segment):
        identities_compiled.append((context.snapshot, segment.id))
        return original(context, segment)

    with patch.object(AudioIdentityContext, "for_segment", inspect):
        prepared = prepare(case, selector_keys=["character:fred"])
    assert prepared["preview"]["blocked_reason"] is None
    assert prepared["target_segment_ids"] == case["segment_ids"][:1]
    assert [
        segment_id
        for snapshot, segment_id in identities_compiled
        if snapshot is prepared["frozen_snapshot"]
    ] == case["segment_ids"][:1]
    other = next(row for row in prepared["preview"]["selectors"] if row["key"] == "character:other")
    assert "assigned to xtts" in other["error"]


def test_default_freeze_validates_all_cast_while_empty_collection_keeps_metadata(recast_case):
    case = recast_case
    set_incompatible_pending_narrator(case)
    services = case["services"]
    with services["database"].session() as session:
        snapshot, _ = services["workspace_settings"].resolve_in_session(session, case["session_id"])
        with pytest.raises(ValueError, match="assigned to xtts"):
            freeze_speech_snapshot(session, case["revision_id"], deepcopy(snapshot), explicit=True)
        freeze_speech_snapshot(
            session, case["revision_id"], snapshot, explicit=True, cast_segment_ids=[]
        )
        assert set(snapshot["generation_control_snapshot"]["segments"]) == set(case["segment_ids"])
        assert snapshot["generation_control_snapshot"]["resolved_bindings"] == {}
        assert snapshot["speech_plan_frozen"]
        assert snapshot["speech_plan_signature"]


def test_completed_recast_null_provider_overlay_matches_desired_cast(recast_case):
    case = recast_case
    record(case)
    change(case, narrator="Kore", fred="Fenrir")
    replacement = prepare(case, selector_keys=["character:fred"])
    assert replacement["preview"]["blocked_reason"] is None
    segment_id = case["segment_ids"][0]
    snapshot = replacement["frozen_snapshot"]
    assert (
        snapshot["generation_control_snapshot"]["segments"][segment_id]["controls"][
            "_base_voice_settings"
        ]["elevenlabs_voice_id"]
        is None
    )
    with case["services"]["database"].session() as session:
        previous = session.scalar(
            select(m.AudioTake).where(
                m.AudioTake.generation_segment_id == segment_id, m.AudioTake.is_active.is_(True)
            )
        )
        previous.is_active = False
        executor = m.GenerationRun(
            session_id=case["session_id"],
            plan_revision_id=case["revision_id"],
            sequence_number=2,
            status="completed",
            settings_snapshot_json=deepcopy(snapshot),
        )
        session.add(executor)
        session.flush()
        artifact = m.Artifact(
            session_id=case["session_id"],
            kind="audio",
            relative_path="completed-scoped-recast.wav",
            content_hash="replacement-waveform",
            metadata_json={
                "generation_task_run_id": executor.id,
                "generation_audio_identity": snapshot["generation_audio_identities"][segment_id],
            },
        )
        session.add(artifact)
        session.flush()
        session.add(
            m.AudioTake(
                generation_segment_id=segment_id,
                generation_run_id=executor.id,
                artifact_id=artifact.id,
                parent_take_id=previous.id,
                is_active=True,
                status="completed",
            )
        )
    preview = prepare(case)["preview"]
    assert preview["selected_keys"] == []
    assert preview["generate_count"] == 0
    assert all(
        row["changed_count"] == 0
        for row in preview["selectors"]
        if row["key"] in {"narrator", "character:fred"}
    )
    change(case, narrator="Kore", fred="Puck")
    different = prepare(case)["preview"]
    assert different["selected_keys"] == ["character:fred"]
    assert different["generate_count"] == 1


def test_forced_unchanged_recast_resume_generates_before_clone_then_reuses_generated_take(
    recast_case,
):
    import threading

    from pydub import AudioSegment

    from pandrator.web.generation_recast_workspace import start_recast_in_session
    from pandrator.web.tts_providers import TtsCapabilities

    case = recast_case
    record(case)
    prepared = prepare(case, selector_keys=["character:fred"], force=True)
    assert prepared["preview"]["blocked_reason"] is None
    database = case["services"]["database"]
    generation = case["services"]["generation"]
    with database.immediate_session() as session:
        started = start_recast_in_session(
            generation,
            session,
            case["session_id"],
            prepared,
            prepared["preview"]["selection_hash"],
        )
        run = session.get(m.GenerationRun, started["id"])
        run.pause_requested = True
        session.get(m.Job, run.job_id).status = "running"
    handlers = case["services"]["workflow_handlers"]
    calls = []

    def synthesize(text, _settings, **_kwargs):
        calls.append(text)
        return AudioSegment.silent(duration=40)

    payload = {
        "generation_run_id": started["id"],
        "segment_ids": prepared["target_segment_ids"],
        "operation": "regenerate",
    }
    with (
        patch.object(handlers.tts_providers, "synthesize", side_effect=synthesize),
        patch.object(
            handlers.tts_providers,
            "synthesis_capabilities",
            return_value=TtsCapabilities(default_batch_size=1, max_batch_size=1),
        ),
    ):
        paused = handlers.run_generation(payload, lambda *_args: None, threading.Event())
        assert paused["status"] == "paused" and calls == []
        with database.session() as session:
            run = session.get(m.GenerationRun, started["id"])
            assert run.audio_version_json["generated_take_ids"] == {}
            run.pause_requested = False
        resumed = handlers.run_generation(
            {**payload, "operation": "resume"}, lambda *_args: None, threading.Event()
        )
        assert resumed["generated"] == 1 and resumed["skipped"] == 0
        assert resumed["status"] == "completed" and resumed["remaining"] == 0
        assert calls
        with database.session() as session:
            run = session.get(m.GenerationRun, started["id"])
            target_id = prepared["target_segment_ids"][0]
            generated_id = run.audio_version_json["generated_take_ids"][target_id]
            assert generated_id != run.audio_version_json["before_take_ids"][target_id]
            assert run.audio_version_json["after_take_ids"][target_id] == generated_id
            run.status = "paused"
        call_count = len(calls)
        repeated = handlers.run_generation(
            {**payload, "operation": "resume"}, lambda *_args: None, threading.Event()
        )
        assert repeated["generated"] == 0 and repeated["skipped"] == 1
        assert repeated["status"] == "completed" and len(calls) == call_count
