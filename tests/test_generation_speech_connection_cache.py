"""Scoped reuse of pure speech catalogue construction during connection checks."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any, Iterator, cast

import pytest

from pandrator.logic import tts_service_catalogue as tts_catalogue
from pandrator.web import tts_catalogue_service
from pandrator.web.accounts.execution_actor import UserActor, current_execution_actor
from pandrator.web.generation_runtime_speech import GenerationSpeechRuntime
from pandrator.web.models import StoredCredential, Workspace
from pandrator.web.tts_catalogue_service import TtsCatalogueService
from tests import test_generation_speech_connections as connection_tests

_CHOOSE = connection_tests.CHOOSE
_CHANGED = connection_tests.CHANGED


class DeepcopyBomb:
    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        raise AssertionError("an unrequested model body was copied")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    cast(Any, connection_tests.no_network).__wrapped__(monkeypatch)


@pytest.fixture
def case(tmp_path: Any) -> Iterator[Any]:
    yield from cast(Any, connection_tests.case).__wrapped__(tmp_path)


@pytest.fixture
def memoized_case(case: Any) -> Iterator[Any]:
    previous = case.catalogue
    cache: dict[Any, Any] = {}
    case.catalogue = TtsCatalogueService(
        case.database,
        case.paths,
        previous.providers,
        manager_bridge=previous.manager_bridge,
        _connection_config_cache=cache,
    )
    case.connection_config_cache = cache
    try:
        yield case
    finally:
        case.catalogue = previous


def _count_catalogue_builds(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    counts = [0]
    original = tts_catalogue._default_service_config_templates

    def counted() -> list[dict[str, object]]:
        counts[0] += 1
        return original()

    monkeypatch.setattr(tts_catalogue, "_default_service_config_templates", counted)
    return counts


def test_warm_connection_checks_reuse_catalogue_and_keep_live_reads_fresh(
    memoized_case: Any,
    monkeypatch: pytest.MonkeyPatch,
):
    case = memoized_case
    builds = _count_catalogue_builds(monkeypatch)
    settings_reads = [0]
    original_settings = case.catalogue._settings

    def counted_settings(**kwargs: Any) -> Any:
        settings_reads[0] += 1
        return original_settings(**kwargs)

    monkeypatch.setattr(case.catalogue, "_settings", counted_settings)
    credential_reads = [0]
    original_resolve = connection_tests.credentials._resolve_provider_credential_in_namespace

    def counted_resolve(*args: Any, **kwargs: Any) -> Any:
        credential_reads[0] += 1
        return original_resolve(*args, **kwargs)

    monkeypatch.setattr(
        connection_tests.credentials,
        "_resolve_provider_credential_in_namespace",
        counted_resolve,
    )

    settings = connection_tests._settings(case)
    proof = connection_tests._capture(case, settings)
    builds_after_capture = builds[0]
    settings_after_capture = settings_reads[0]
    credentials_after_capture = credential_reads[0]
    assert builds_after_capture > 0

    cached_catalogue = next(
        value
        for key, value in case.connection_config_cache.items()
        if isinstance(key, tuple) and key[0] == "service_configs"
    )
    cached_gemini = next(row for row in cached_catalogue if row.get("id") == "gemini")
    cached_gemini["model_catalog"] = [
        {"id": "not-the-selected-model", "body": DeepcopyBomb()}
    ]

    connection_tests._require(case, proof)
    connection_tests._require(case, proof)

    assert builds[0] == builds_after_capture
    assert settings_reads[0] == settings_after_capture + 2
    assert credential_reads[0] == credentials_after_capture + 2


@pytest.mark.parametrize(
    ("changes", "expected_error"),
    [
        ({"api_base": "https://changed.invalid/v1"}, _CHANGED),
        (
            {"models": ["new-workspace-model"], "default_model": "new-workspace-model"},
            _CHOOSE,
        ),
    ],
)
def test_connection_input_changes_are_not_hidden_by_the_memo(
    memoized_case: Any,
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, Any],
    expected_error: str,
):
    case = memoized_case
    builds = _count_catalogue_builds(monkeypatch)
    if "models" in changes:
        workspace_model = "gemini-workspace-only-model"
        connection_tests._modify_profile(
            case, models=[workspace_model], default_model=workspace_model
        )
        proof = connection_tests._capture(
            case, connection_tests._settings(case, model=workspace_model)
        )
    else:
        proof = connection_tests._capture(case)
    before_change = builds[0]

    connection_tests._modify_profile(case, **changes)

    with pytest.raises(ValueError) as error:
        connection_tests._require(case, proof)
    assert str(error.value) == expected_error
    assert builds[0] > before_change


def test_credential_rotation_is_resolved_fresh_without_rebuilding_catalogue(
    memoized_case: Any,
    monkeypatch: pytest.MonkeyPatch,
):
    case = memoized_case
    builds = _count_catalogue_builds(monkeypatch)
    proof = connection_tests._capture(case)
    before_rotation = builds[0]

    with case.database.immediate_session() as session:
        credential = session.get_one(
            StoredCredential,
            (case.workspace_id, "shared:gemini"),
        )
        credential.secret_value = "synthetic-rotated-secret"

    with pytest.raises(ValueError) as error:
        connection_tests._require(case, proof)
    assert str(error.value) == _CHANGED
    assert builds[0] == before_rotation


def test_disabled_namespace_still_rejects_with_a_warm_catalogue(
    memoized_case: Any,
    monkeypatch: pytest.MonkeyPatch,
):
    case = memoized_case
    builds = _count_catalogue_builds(monkeypatch)
    proof = connection_tests._capture(case)
    before_disable = builds[0]

    with case.database.immediate_session() as session:
        session.get_one(Workspace, case.workspace_id).state = "archived"

    with pytest.raises(ValueError) as error:
        connection_tests._require(case, proof)
    assert str(error.value) == _CHOOSE
    assert builds[0] == before_disable


def test_connection_cache_rolls_over_by_day_and_disabled_cache_reads_no_clock(
    case: Any,
    monkeypatch: pytest.MonkeyPatch,
):
    memo = {"stale": object()}

    class MutableDate:
        current = date(2026, 10, 28)

        @classmethod
        def today(cls) -> date:
            return cls.current

    monkeypatch.setattr(tts_catalogue_service, "date", MutableDate)
    catalogue = TtsCatalogueService(
        case.database,
        case.paths,
        case.catalogue.providers,
        manager_bridge=case.catalogue.manager_bridge,
        _connection_config_cache=memo,
    )
    assert catalogue._connection_lookup_cache() is memo
    assert memo == {}
    assert catalogue._connection_cache_day == MutableDate.current

    memo["fresh"] = object()
    MutableDate.current += timedelta(days=1)
    assert catalogue._connection_lookup_cache() is memo
    assert memo == {}
    assert catalogue._connection_cache_day == MutableDate.current

    class NoClock:
        @staticmethod
        def today() -> date:
            raise AssertionError("disabled catalogue cache read the clock")

    monkeypatch.setattr(tts_catalogue_service, "date", NoClock)
    assert case.catalogue._connection_lookup_cache() is None


def test_runtime_allocates_memo_only_for_scoped_user_and_clears_it_in_finally(
    case: Any,
    monkeypatch: pytest.MonkeyPatch,
):
    actor_token = current_execution_actor.set(None)
    try:
        for legacy_host_access in (True, False):
            captured = SimpleNamespace(
                legacy_host_access=legacy_host_access,
                workspace_id=case.workspace_id,
            )
            runtime = GenerationSpeechRuntime(
                case.database,
                case.paths,
                case.catalogue.providers,
                cast(Any, case.manager),
                cast(Any, captured),
                cast(Any, lambda _session: None),
                cast(Any, lambda settings: settings),
                lambda _secret: None,
            )
            assert not runtime._scoped
            assert runtime._connection_config_cache is None
            assert runtime._catalogue is None
            runtime.close()
    finally:
        current_execution_actor.reset(actor_token)

    actor_token = current_execution_actor.set(object.__new__(UserActor))
    try:
        captured = SimpleNamespace(
            legacy_host_access=False,
            workspace_id=case.workspace_id,
        )
        runtime = GenerationSpeechRuntime(
            case.database,
            case.paths,
            case.catalogue.providers,
            cast(Any, case.manager),
            cast(Any, captured),
            cast(Any, lambda _session: None),
            cast(Any, lambda settings: settings),
            lambda _secret: None,
        )
        cache = runtime._connection_config_cache
        assert runtime._scoped
        assert isinstance(cache, dict) and cache == {}
        assert runtime._catalogue is not None
        assert runtime._catalogue._connection_config_cache is cache
        cache["construction"] = object()

        def fail_close() -> None:
            raise RuntimeError("catalogue close failed")

        monkeypatch.setattr(runtime._catalogue, "close", fail_close)
        with pytest.raises(RuntimeError, match="catalogue close failed"):
            runtime.close()
        assert cache == {}
    finally:
        current_execution_actor.reset(actor_token)
