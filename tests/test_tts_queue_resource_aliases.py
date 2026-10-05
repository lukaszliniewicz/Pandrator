"""Native queue admission must coordinate new and pre-normalization TTS claims."""

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from pandrator.web.database import Database
from pandrator.web.generation_scheduling import generation_resource_keys
from pandrator.web.jobs import JobQueue
from pandrator.web.models import Job, ResourceClaim, utcnow
from pandrator.web.stt_resources import audio_cpp_resource_keys
from pandrator.web.workflows import WorkflowService
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def queue(tmp_path: Path) -> Iterator[tuple[JobQueue, Database]]:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    try:
        yield JobQueue(database), database
    finally:
        database.dispose()


@pytest.mark.parametrize("producer", ["generation", "workflow"])
def test_audio_cpp_producers_share_native_asr_claim(
    queue: tuple[JobQueue, Database], producer: str
) -> None:
    jobs, database = queue
    settings = {"service": "audio.cpp", "compute_backend": "cpu"}
    keys = (
        generation_resource_keys("fixture-session", {"tts": settings})
        if producer == "generation"
        else WorkflowService._resource_keys("fixture-session", "generate_audio", settings)
    )
    first = jobs.enqueue("noop", resource_keys=keys)
    second = jobs.enqueue("noop", resource_keys=audio_cpp_resource_keys("cpu"))
    first_claim = jobs.claim("holder", lease_seconds=300)
    second_claim = jobs.claim("contender", lease_seconds=300)
    assert first_claim is not None and second_claim is not None
    assert [first_claim.id, second_claim.id] == [first.id, second.id]
    try:
        assert jobs.acquire_resources(
            first.id,
            "holder",
            first_claim.resource_keys_json,
            lease_generation=first_claim.lease_generation,
        )
        assert not jobs.acquire_resources(
            second.id,
            "contender",
            second_claim.resource_keys_json,
            lease_generation=second_claim.lease_generation,
        )
        assert first.resource_keys_json == ["service:tts:audio_cpp", "session:fixture-session"]
    finally:
        jobs.release_resources(first.id, "holder", lease_generation=first_claim.lease_generation)
        jobs.release_resources(
            second.id, "contender", lease_generation=second_claim.lease_generation
        )
    with database.session() as session:
        assert list(session.scalars(select(ResourceClaim))) == []


@pytest.mark.parametrize("legacy", ["queued_job", "active_claim"])
@pytest.mark.parametrize("alias", ["XTTS", "audio.cpp"])
def test_legacy_tts_identity_blocks_new_canonical_claim(
    queue: tuple[JobQueue, Database], legacy: str, alias: str
) -> None:
    jobs, database = queue
    canonical = "xtts" if alias == "XTTS" else "audio_cpp"
    key = f"service:tts:{canonical}"
    first = jobs.enqueue("noop", resource_keys=[key])
    second = jobs.enqueue("noop", resource_keys=[key, "free:must-not-leak"])
    if legacy == "queued_job":
        with database.session() as session:
            row = session.get(Job, first.id)
            assert row is not None
            row.resource_keys_json = [f"service:tts:{alias}"]
    first_claim = jobs.claim("holder", lease_seconds=300)
    second_claim = jobs.claim("contender", lease_seconds=300)
    assert first_claim is not None and second_claim is not None
    assert [first_claim.id, second_claim.id] == [first.id, second.id]
    try:
        assert jobs.acquire_resources(
            first.id,
            "holder",
            first_claim.resource_keys_json,
            lease_generation=first_claim.lease_generation,
        )
        if legacy == "active_claim":
            with database.session() as session:
                claim = session.get(ResourceClaim, key)
                assert claim is not None
                claim.resource_key = f"service:tts:{alias}"
        assert not jobs.acquire_resources(
            second.id,
            "contender",
            second_claim.resource_keys_json,
            lease_generation=second_claim.lease_generation,
        )
        with database.session() as session:
            claims = list(session.scalars(select(ResourceClaim)))
            assert len(claims) == 1 and claims[0].job_id == first.id
    finally:
        jobs.release_resources(first.id, "holder", lease_generation=first_claim.lease_generation)
        jobs.release_resources(
            second.id, "contender", lease_generation=second_claim.lease_generation
        )


def test_non_tts_case_distinctions_and_stored_key_bytes_are_retained(
    queue: tuple[JobQueue, Database],
) -> None:
    jobs, _database = queue
    first = jobs.enqueue("noop", resource_keys=["voice:Alice", " space:original "])
    jobs.enqueue("noop", resource_keys=["voice:alice"])
    assert first.resource_keys_json == [" space:original ", "voice:Alice"]
    a, b = jobs.claim("holder"), jobs.claim("contender")
    assert a is not None and b is not None
    try:
        assert jobs.acquire_resources(
            a.id, "holder", a.resource_keys_json, lease_generation=a.lease_generation
        )
        assert jobs.acquire_resources(
            b.id, "contender", b.resource_keys_json, lease_generation=b.lease_generation
        )
    finally:
        jobs.release_resources(a.id, "holder", lease_generation=a.lease_generation)
        jobs.release_resources(b.id, "contender", lease_generation=b.lease_generation)


def test_expired_legacy_claim_is_ignored_without_rewriting_it(
    queue: tuple[JobQueue, Database],
) -> None:
    jobs, database = queue
    first = jobs.enqueue("noop", resource_keys=["service:tts:xtts"])
    second = jobs.enqueue("noop", resource_keys=["service:tts:xtts"])
    a, b = jobs.claim("holder"), jobs.claim("contender")
    assert a is not None and b is not None
    try:
        assert jobs.acquire_resources(
            a.id, "holder", a.resource_keys_json, lease_generation=a.lease_generation
        )
        with database.session() as session:
            claim = session.get(ResourceClaim, "service:tts:xtts")
            assert claim is not None
            claim.resource_key = "service:tts:XTTS"
            claim.expires_at = utcnow() - timedelta(seconds=1)
        assert jobs.acquire_resources(
            b.id, "contender", b.resource_keys_json, lease_generation=b.lease_generation
        )
        with database.session() as session:
            old = session.get(ResourceClaim, "service:tts:XTTS")
            new = session.get(ResourceClaim, "service:tts:xtts")
            assert old is not None and old.job_id == first.id
            assert new is not None and new.job_id == second.id
    finally:
        jobs.release_resources(a.id, "holder", lease_generation=a.lease_generation)
        jobs.release_resources(b.id, "contender", lease_generation=b.lease_generation)
