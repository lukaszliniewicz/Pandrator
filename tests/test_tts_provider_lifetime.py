"""Owned HTTP transport lifetime, exercised with native sessions and threads."""

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import requests
from pydub import AudioSegment

from pandrator.logic import tts_handler
from pandrator.logic.tts_endpoint_transport import EndpointSessionPool
from pandrator.runtime import DataPaths
from pandrator.web import cli
from pandrator.web.api import create_app
from pandrator.web.database import Database, upgrade_database
from pandrator.web.job_registry import JobHandlerRegistry
from pandrator.web.jobs import JobQueue, Worker
from pandrator.web.models import Job
from pandrator.web.tts_providers import (
    AudioCppAdapter,
    LegacyTtsAdapter,
    TtsBatchItem,
    TtsBatchResult,
    TtsProviderAdapter,
    TtsProviderError,
    TtsProviderRegistry,
)


class RecordingSession(requests.Session):
    def __init__(self, *, fail_close=False, on_close=None):
        super().__init__()
        self.close_count = 0
        self.fail_close = fail_close
        self.on_close = on_close

    def close(self):
        self.close_count += 1
        super().close()
        if self.on_close is not None:
            self.on_close()
        if self.fail_close:
            raise RuntimeError("controlled session cleanup failure")


@pytest.fixture
def native_sessions(monkeypatch):
    created = []

    def create():
        session = RecordingSession()
        created.append(session)
        return session

    monkeypatch.setattr(requests, "Session", create)
    return created


def test_pool_close_is_terminal_idempotent_and_attempts_every_session(caplog):
    pool = EndpointSessionPool()
    first = pool.session_for_key("first", create_session=lambda: RecordingSession(fail_close=True))
    second = pool.session_for_key("second", create_session=RecordingSession)
    pool.close()
    pool.close()
    assert pool.closed
    assert first.close_count == second.close_count == 1
    assert "Could not close an audio.cpp HTTP session" in caplog.text
    with pytest.raises(RuntimeError, match="closed"):
        pool.session_for_key("first", create_session=RecordingSession)
    with pytest.raises(RuntimeError, match="closed"):
        with pool.operation():
            pytest.fail("A closed pool must not admit new work")


@pytest.mark.parametrize("active", [False, True])
def test_shared_factory_session_closes_once_across_cache_keys(active):
    pool = EndpointSessionPool()
    session = RecordingSession()

    def populate_and_close():
        for key in ("first", "second"):
            assert pool.session_for_key(key, create_session=lambda: session) is session
        pool.close()

    if active:
        with pool.operation():
            populate_and_close()
            assert session.close_count == 0
    else:
        populate_and_close()
    pool.close()
    assert session.close_count == 1


def test_close_allows_existing_borrower_to_finish_late_lookup_and_rejects_new_work():
    pool = EndpointSessionPool()
    admitted = threading.Event()
    lookup = threading.Event()
    looked_up = threading.Event()
    finish = threading.Event()
    created = []

    def borrower():
        with pool.operation():
            admitted.set()
            assert lookup.wait(2)
            created.append(pool.session_for_key("late", create_session=RecordingSession))
            looked_up.set()
            assert finish.wait(2)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(borrower)
        try:
            assert admitted.wait(1)
            pool.close()
            assert pool.closed
            with pytest.raises(RuntimeError, match="closed"):
                pool.session_for_key("unborrowed", create_session=RecordingSession)
            with pytest.raises(RuntimeError, match="closed"):
                with pool.operation():
                    pytest.fail("Closing must fence new work")
            lookup.set()
            assert looked_up.wait(1)
            assert created[0].close_count == 0
            finish.set()
            future.result(timeout=2)
            assert created[0].close_count == 1
            pool.close()
            assert created[0].close_count == 1
        finally:
            lookup.set()
            finish.set()


def test_new_nested_operation_is_rejected_after_close_but_borrowed_lookup_can_finish():
    pool = EndpointSessionPool()
    with pool.operation():
        pool.close()
        with pytest.raises(RuntimeError, match="closed"):
            with pool.operation():
                pytest.fail("A fresh nested operation is new work")
        session = pool.session_for_key("borrowed", create_session=RecordingSession)
        assert session.close_count == 0
    assert session.close_count == 1


def test_active_synthesis_survives_replacement_and_registry_close(monkeypatch, native_sessions):
    registry = TtsProviderRegistry()
    old = registry.get("audio_cpp")
    replacement = AudioCppAdapter("audio_cpp")
    started = threading.Event()
    finish = threading.Event()
    settings = {"service": "audio_cpp", "audio_cpp_base_url": "http://203.0.113.101:8060"}

    def synthesize(_text, _settings, **options):
        assert options["request_session"] is native_sessions[0]
        started.set()
        assert finish.wait(2)
        assert native_sessions[0].close_count == 0
        return AudioSegment.silent(duration=20)

    monkeypatch.setattr(tts_handler, "text_to_audio", synthesize)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(old.synthesize, "Already running", settings)
        try:
            assert started.wait(1)
            registry.replace(replacement)
            assert registry.get("audio_cpp") is replacement
            registry.close()
            assert native_sessions[0].close_count == 0
            with pytest.raises(RuntimeError, match="closed"):
                old.synthesize("New work", settings)
            finish.set()
            assert len(future.result(timeout=2)) == 20
            assert native_sessions[0].close_count == 1
        finally:
            finish.set()
            registry.close()


@pytest.mark.parametrize("retire", ["close", "replace"])
@pytest.mark.parametrize("ending", ["exhaust", "close", "provider_error"])
def test_owned_batch_lease_spans_yields_and_survives_retirement(
    monkeypatch, native_sessions, retire, ending
):
    registry = TtsProviderRegistry()
    settings = {"service": "audio_cpp", "audio_cpp_base_url": "http://203.0.113.102:8060"}
    calls = []

    def synthesize(text, _settings, **options):
        assert options["request_session"] is native_sessions[0]
        assert native_sessions[0].close_count == 0
        calls.append(text)
        if ending == "provider_error" and text == "Second":
            raise RuntimeError("controlled provider failure")
        return AudioSegment.silent(duration=20)

    monkeypatch.setattr(tts_handler, "text_to_audio", synthesize)
    stream = registry.synthesize_batch(
        [TtsBatchItem("one", "First", settings), TtsBatchItem("two", "Second", settings)],
        batch_size=2,
    )
    try:
        assert next(stream).id == "one"
        if retire == "close":
            registry.close()
        else:
            registry.replace(AudioCppAdapter("audio_cpp"))
        assert native_sessions[0].close_count == 0
        if ending == "close":
            stream.close()
            assert calls == ["First"]
        else:
            results = list(stream)
            assert [result.id for result in results] == ["two"]
            assert bool(results[0].error) is (ending == "provider_error")
            assert calls == ["First", "Second"]
        assert native_sessions[0].close_count == 1
    finally:
        stream.close()
        registry.close()
    assert native_sessions[0].close_count == 1


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("explicit_none", [False, True])
def test_caller_session_stays_unowned_after_adapter_pool_close(
    monkeypatch, native_sessions, batch, explicit_none
):
    adapter = AudioCppAdapter("audio_cpp")
    settings = {"service": "audio_cpp", "audio_cpp_base_url": "http://203.0.113.103:8060"}
    adapter._session_for(settings)
    caller = None if explicit_none else RecordingSession()
    adapter.close()
    assert native_sessions[0].close_count == 1

    def synthesize(_text, _settings, **options):
        assert options["request_session"] is caller
        return AudioSegment.silent(duration=20)

    monkeypatch.setattr(tts_handler, "text_to_audio", synthesize)
    try:
        if batch:
            result = list(
                adapter.synthesize_batch(
                    [TtsBatchItem("one", "One", settings)], batch_size=1, request_session=caller
                )
            )
            assert result[0].error is None
        else:
            assert len(adapter.synthesize("One", settings, request_session=caller)) == 20
        assert len(native_sessions) == 1
        if caller is not None:
            assert caller.close_count == 0
    finally:
        if caller is not None:
            caller.close()


def test_registry_same_object_replacement_preserves_its_sessions_and_retired_object_rejected(
    native_sessions,
):
    registry = TtsProviderRegistry()
    adapter = registry.get("audio_cpp")
    session = adapter._session_for_base_url("http://203.0.113.104:8060")
    registry.replace(adapter)
    assert registry.get("audio_cpp") is adapter
    assert session.close_count == 0
    registry.replace(AudioCppAdapter("audio_cpp"))
    assert session.close_count == 1
    with pytest.raises(RuntimeError, match="retired"):
        registry.replace(adapter)
    registry.close()
    assert session.close_count == 1
    with pytest.raises(RuntimeError, match="closed"):
        registry.get("audio_cpp")
    with pytest.raises(RuntimeError, match="closed"):
        registry.register(LegacyTtsAdapter("fixture"))
    with pytest.raises(RuntimeError, match="closed"):
        registry.replace(AudioCppAdapter("audio_cpp"))


def test_optional_close_preserves_adapter_protocol_and_attempts_all_cleanup(caplog):
    registry = TtsProviderRegistry()
    closeless = LegacyTtsAdapter("closeless_fixture")
    assert isinstance(closeless, TtsProviderAdapter)
    registry.register(closeless)
    events = []

    class ClosingAdapter(LegacyTtsAdapter):
        def close(self):
            events.append((self.service_id, registry.service_ids()))
            if self.service_id == "failing_fixture":
                raise RuntimeError("controlled adapter cleanup failure")

    registry.register(ClosingAdapter("failing_fixture"))
    registry.register(ClosingAdapter("last_fixture"))
    registry.close()
    registry.close()
    assert events == [("failing_fixture", ()), ("last_fixture", ())]
    assert "Could not close a TTS provider adapter" in caplog.text


def test_concurrent_readoption_waits_for_retirement_then_rejects_native_adapter(native_sessions):
    registry = TtsProviderRegistry()
    old = registry.get("audio_cpp")
    session = old._session_for_base_url("http://203.0.113.105:8060")
    retiring = threading.Event()
    finish = threading.Event()
    readopting = threading.Event()
    snapshots = []

    def on_close():
        snapshots.append(registry.service_ids())
        retiring.set()
        assert finish.wait(2)

    session.on_close = on_close
    new = AudioCppAdapter("audio_cpp")

    def readopt():
        readopting.set()
        registry.replace(old)

    with ThreadPoolExecutor(max_workers=2) as executor:
        replacing = executor.submit(registry.replace, new)
        try:
            assert retiring.wait(1)
            assert registry.get("audio_cpp") is new
            adoption = executor.submit(readopt)
            assert readopting.wait(1)
            assert not adoption.done()
            finish.set()
            replacing.result(timeout=2)
            with pytest.raises(RuntimeError, match="retired"):
                adoption.result(timeout=2)
            assert registry.get("audio_cpp") is new
        finally:
            finish.set()
            registry.close()
    assert snapshots[0]
    assert session.close_count == 1


@pytest.mark.parametrize("operation", ["health", "catalogue"])
def test_metadata_borrower_survives_close_and_cleanup_failure(
    monkeypatch, native_sessions, caplog, operation
):
    adapter = AudioCppAdapter("audio_cpp")
    service = {"api_base": "http://203.0.113.106:8060", "models": []}
    entered = threading.Event()
    finish = threading.Event()

    def request(*_args, **_kwargs):
        entered.set()
        assert finish.wait(2)
        assert native_sessions[0].close_count == 0
        if operation == "health":
            response = requests.Response()
            response.status_code = 200
            response._content = b'{"status":"ok"}'
            return response
        return []

    if operation == "health":
        monkeypatch.setattr(RecordingSession, "get", request)
        call = adapter.health
    else:
        monkeypatch.setattr(tts_handler, "get_audio_cpp_model_catalog", request)
        call = adapter.enrich_catalog
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(call, service)
        try:
            assert entered.wait(1)
            native_sessions[0].fail_close = True
            adapter.close()
            with pytest.raises(RuntimeError, match="closed"):
                call(service)
            finish.set()
            result = future.result(timeout=2)
            if operation == "health":
                assert result.online and result.available
            else:
                assert isinstance(result, dict)
            assert native_sessions[0].close_count == 1
            assert "Could not close an audio.cpp HTTP session" in caplog.text
        finally:
            finish.set()
            adapter.close()


@pytest.mark.parametrize("ending", ["return", "error", "interrupt", "browser_error"])
def test_serve_cli_closes_native_app_registry_on_every_exit(monkeypatch, tmp_path, ending):
    app = create_app(data_root=tmp_path, testing=True, background_maintenance=True)
    services = app.extensions["pandrator"]["services"]
    assert services.startup_maintenance.wait(5)
    periodic = services.startup_maintenance._periodic_thread
    quick = services.quick_transcriptions._thread
    assert periodic is not None and quick is not None
    assert periodic.is_alive() and quick.is_alive()
    original_pool = services.database.engine.pool
    providers = services.tts_providers
    session = RecordingSession()
    pool = providers.get("audio_cpp")._session_pool
    pool.session_for_key("fixture", create_session=lambda: session)
    monkeypatch.setattr(cli, "create_app", lambda **_kwargs: app)
    monkeypatch.delenv("PANDRATOR_OWNER_PASSWORD", raising=False)

    def server(*_args, **_kwargs):
        assert session.close_count == 0
        if ending == "error":
            raise RuntimeError("controlled server failure")
        if ending == "interrupt":
            raise KeyboardInterrupt()

    def browser(_url):
        raise RuntimeError("controlled browser failure")

    monkeypatch.setattr(cli, "waitress_serve", server)
    monkeypatch.setattr(cli.webbrowser, "open", browser)
    args = SimpleNamespace(
        data_dir=tmp_path,
        command="serve",
        host="127.0.0.1",
        port=8765,
        public_url="",
        proxy_hops=0,
        trusted_host=[],
        allow_insecure_remote=False,
        open_browser=ending == "browser_error",
        threads=6,
    )
    try:
        if ending == "return":
            assert cli.command_serve(args) == 0
        else:
            expected = KeyboardInterrupt if ending == "interrupt" else RuntimeError
            with pytest.raises(expected):
                cli.command_serve(args)
        assert pool.closed
        assert session.close_count == 1
        assert not periodic.is_alive()
        assert not quick.is_alive()
        assert services.database.engine.pool is not original_pool
    finally:
        services.startup_maintenance.stop()
        services.quick_transcriptions.stop_maintenance()
        providers.close()
        app.extensions["pandrator"]["database"].dispose()


def test_serve_cli_return_defers_close_for_active_native_app_borrower(monkeypatch, tmp_path):
    app = create_app(data_root=tmp_path, testing=True, background_maintenance=False)
    providers = app.extensions["pandrator"]["tts_providers"]
    pool = providers.get("audio_cpp")._session_pool
    session = RecordingSession()
    monkeypatch.setattr(cli, "create_app", lambda **_kwargs: app)
    monkeypatch.setattr(cli, "waitress_serve", lambda *_args, **_kwargs: None)
    args = SimpleNamespace(
        data_dir=tmp_path,
        command="serve",
        host="127.0.0.1",
        port=8765,
        public_url="",
        proxy_hops=0,
        trusted_host=[],
        allow_insecure_remote=False,
        open_browser=False,
        threads=6,
    )
    try:
        with pool.operation():
            pool.session_for_key("active", create_session=lambda: session)
            assert cli.command_serve(args) == 0
            assert pool.closed
            assert session.close_count == 0
        assert session.close_count == 1
    finally:
        providers.close()
        app.extensions["pandrator"]["database"].dispose()


@pytest.mark.parametrize("once", [False, True])
def test_worker_cli_closes_after_native_handler_unwinds_before_database_disposal(
    monkeypatch, tmp_path, once
):
    paths = DataPaths.from_value(tmp_path).ensure()
    upgrade_database(paths.database)
    database = Database(paths.database)
    queue = JobQueue(database)
    job = queue.enqueue("fixture.tts")
    session = RecordingSession()
    started = threading.Event()
    finish = threading.Event()
    workers = []
    registries = []
    disposal = []
    original_dispose = database.dispose

    def dispose():
        disposal.append(session.close_count)
        original_dispose()

    def handlers(_database, _paths, *, tts_providers):
        registries.append(tts_providers)
        pool = tts_providers.get("audio_cpp")._session_pool
        registry = JobHandlerRegistry()

        def execute(_payload, _progress, _cancel):
            with pool.operation():
                pool.session_for_key("active", create_session=lambda: session)
                started.set()
                assert finish.wait(3)
                assert session.close_count == 0
            return {"completed": True}

        registry.register("fixture.tts", execute, domain="fixture")
        return SimpleNamespace(handler_registry=registry)

    def worker(*args):
        instance = Worker(*args)
        workers.append(instance)
        return instance

    monkeypatch.setattr(cli, "_database", lambda _args: (paths, database))
    monkeypatch.setattr(database, "dispose", dispose)
    monkeypatch.setattr(cli, "WorkflowHandlers", handlers)
    monkeypatch.setattr(cli, "Worker", worker)
    args = SimpleNamespace(worker_id="lifetime-fixture", once=once, poll_interval=0.001)
    returned = threading.Event()

    def observe_active_handler():
        try:
            assert started.wait(2)
            workers[0].stop()
            assert not returned.is_set()
            assert session.close_count == 0
            assert disposal == []
        finally:
            finish.set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        observation = executor.submit(observe_active_handler)
        try:
            assert cli.command_worker(args) == 0
            returned.set()
            observation.result(timeout=3)
            assert session.close_count == 1
            assert disposal == [1]
            assert registries[0].service_ids() == ()
            with database.session() as db_session:
                assert db_session.get(Job, job.id).status == "succeeded"
        finally:
            finish.set()
            if workers:
                workers[0].stop()
            original_dispose()


@pytest.mark.parametrize("ending", ["construction_error", "run_error", "interrupt"])
def test_worker_cli_retires_owned_registry_on_constructor_and_runner_exit(
    monkeypatch, tmp_path, ending
):
    paths = DataPaths.from_value(tmp_path).ensure()
    database = Database(paths.database)
    providers = TtsProviderRegistry()
    session = RecordingSession()
    providers.get("audio_cpp")._session_pool.session_for_key(
        "fixture", create_session=lambda: session
    )
    original_dispose = database.dispose
    disposal = []

    def dispose():
        disposal.append(session.close_count)
        original_dispose()

    def handlers(_database, _paths, *, tts_providers):
        assert tts_providers is providers
        if ending == "construction_error":
            raise RuntimeError("controlled workflow constructor failure")
        return SimpleNamespace(handler_registry=JobHandlerRegistry())

    def run(_self):
        if ending == "interrupt":
            raise KeyboardInterrupt()
        raise RuntimeError("controlled worker failure")

    monkeypatch.setattr(cli, "_database", lambda _args: (paths, database))
    monkeypatch.setattr(database, "dispose", dispose)
    monkeypatch.setattr(cli, "TtsProviderRegistry", lambda: providers)
    monkeypatch.setattr(cli, "WorkflowHandlers", handlers)
    monkeypatch.setattr(Worker, "run_once", run)
    args = SimpleNamespace(worker_id="exit-fixture", once=True, poll_interval=0.001)
    try:
        if ending == "interrupt":
            assert cli.command_worker(args) == 0
        else:
            with pytest.raises(RuntimeError, match="controlled"):
                cli.command_worker(args)
        assert session.close_count == 1
        assert disposal == [1]
        assert providers.service_ids() == ()
    finally:
        providers.close()
        original_dispose()


def _single_only_adapter(synthesize):
    metadata = LegacyTtsAdapter("xtts")
    return SimpleNamespace(
        service_id="xtts",
        capabilities=metadata.capabilities,
        health=metadata.health,
        enrich_catalog=metadata.enrich_catalog,
        synthesize=synthesize,
        upload_voice=metadata.upload_voice,
        delete_voice=metadata.delete_voice,
    )


@pytest.mark.parametrize("outcome", ["audio", "none", "provider_error", "other_error"])
def test_serial_batch_fallback_uses_selected_adapter_and_projects_results(monkeypatch, outcome):
    registry = TtsProviderRegistry()
    audio = AudioSegment.silent(duration=20)
    calls = []
    failure = TtsProviderError("xtts", "synthesize", "controlled failure", retryable=False)

    def legacy(*_args, **_options):
        pytest.fail("Legacy synthesis bypassed selected adapter")

    monkeypatch.setattr(tts_handler, "text_to_audio", legacy)

    def synthesize(text, settings, **options):
        calls.append((text, settings, options))
        if text == "Second":
            if outcome == "provider_error":
                raise failure
            if outcome == "other_error":
                raise RuntimeError("controlled failure")
            if outcome == "none":
                return None
        return audio

    adapter = _single_only_adapter(synthesize)
    assert isinstance(adapter, TtsProviderAdapter)
    assert not hasattr(adapter, "synthesize_batch")
    assert not hasattr(adapter, "close")
    registry.replace(adapter)
    settings = {"service": "xtts"}
    options = {"opaque_option": object()}
    items = [
        TtsBatchItem(str(index), text, settings)
        for index, text in enumerate(("First", "Second", "Third"))
    ]
    try:
        stream = registry.synthesize_batch(items, batch_size=1, **options)
        assert calls == []
        results = list(stream)
        assert [result.id for result in results] == ["0", "1", "2"]
        assert calls == [(item.text, settings, options) for item in items]
        assert all(row[1] is settings for row in calls)
        assert results[0].audio is results[2].audio is audio
        assert results[0].error is results[2].error is None
        if outcome == "provider_error":
            assert results[1].error is failure
            assert results[1].audio is None
        elif outcome == "other_error":
            error = results[1].error
            assert isinstance(error, TtsProviderError)
            assert (error.service_id, error.operation, str(error), error.retryable) == (
                "xtts",
                "synthesize",
                "controlled failure",
                True,
            )
            assert results[1].audio is None
        else:
            assert results[1].error is None
            assert results[1].audio is (None if outcome == "none" else audio)
    finally:
        registry.close()


@pytest.mark.parametrize("retire", ["close", "replace"])
def test_serial_batch_fallback_keeps_selected_adapter_across_retirement(monkeypatch, retire):
    registry = TtsProviderRegistry()
    audio = AudioSegment.silent(duration=20)
    calls = []
    monkeypatch.setattr(
        tts_handler,
        "text_to_audio",
        lambda *_args, **_options: pytest.fail("Legacy synthesis bypassed selected adapter"),
    )

    def synthesize(text, _settings, **_options):
        calls.append(text)
        return audio

    registry.replace(_single_only_adapter(synthesize))
    settings = {"service": "xtts"}
    stream = registry.synthesize_batch(
        [TtsBatchItem("one", "First", settings), TtsBatchItem("two", "Second", settings)],
        batch_size=1,
    )
    try:
        assert next(stream).audio is audio
        if retire == "close":
            registry.close()
        else:
            registry.replace(
                _single_only_adapter(
                    lambda *_args, **_options: pytest.fail(
                        "Replacement adapter must not take over admitted batch"
                    )
                )
            )
        result = next(stream)
        assert result.id == "two" and result.audio is audio
        assert calls == ["First", "Second"]
        assert list(stream) == []
    finally:
        stream.close()
        registry.close()


def test_serial_batch_fallback_close_stops_remaining_items(monkeypatch):
    registry = TtsProviderRegistry()
    audio = AudioSegment.silent(duration=20)
    calls = []
    monkeypatch.setattr(
        tts_handler,
        "text_to_audio",
        lambda *_args, **_options: pytest.fail("Legacy synthesis bypassed selected adapter"),
    )

    def synthesize(text, _settings, **_options):
        calls.append(text)
        return audio

    registry.replace(_single_only_adapter(synthesize))
    settings = {"service": "xtts"}
    stream = registry.synthesize_batch(
        [TtsBatchItem("one", "First", settings), TtsBatchItem("two", "Second", settings)],
        batch_size=1,
    )
    try:
        assert next(stream).audio is audio
        stream.close()
        assert calls == ["First"]
        assert list(stream) == []
    finally:
        stream.close()
        registry.close()


@pytest.mark.parametrize("kind", ["function", "callable", "descriptor"])
def test_optional_batch_hook_keeps_one_lookup_and_return_identity(monkeypatch, kind):
    registry = TtsProviderRegistry()
    audio = AudioSegment.silent(duration=20)
    stream = iter([TtsBatchResult("one", audio=audio)])
    lookups = []
    calls = []

    def single(*_args, **_options):
        pytest.fail("A callable batch hook must retain its own dispatch")

    def batch(items, *, batch_size, **options):
        calls.append((items, batch_size, options))
        return stream

    if kind == "descriptor":

        class DescriptorAdapter(SimpleNamespace):
            @property
            def synthesize_batch(self):
                lookups.append("lookup")
                return batch

        adapter = DescriptorAdapter(**vars(_single_only_adapter(single)))
    else:
        adapter = _single_only_adapter(single)
        if kind == "callable":

            class Hook:
                def __call__(self, items, *, batch_size, **options):
                    return batch(items, batch_size=batch_size, **options)

            adapter.synthesize_batch = Hook()
        else:
            adapter.synthesize_batch = batch
    assert isinstance(adapter, TtsProviderAdapter)
    registry.replace(adapter)
    settings = {"service": "xtts"}
    items = [TtsBatchItem("one", "First", settings)]
    options = {"opaque_option": object()}
    try:
        result = registry.synthesize_batch(items, batch_size=3, **options)
        assert result is stream
        assert calls == [(items, 3, options)]
        assert calls[0][0] is items
        assert lookups == (["lookup"] if kind == "descriptor" else [])
        assert next(result).audio is audio
    finally:
        registry.close()


@pytest.mark.parametrize("hook", [None, 42, "disabled"])
def test_noncallable_optional_batch_hook_keeps_selected_serial_fallback(hook):
    registry = TtsProviderRegistry()
    audio = AudioSegment.silent(duration=20)
    calls = []

    def single(text, settings, **options):
        calls.append((text, settings, options))
        return audio

    adapter = _single_only_adapter(single)
    adapter.synthesize_batch = hook
    registry.replace(adapter)
    settings = {"service": "xtts"}
    try:
        results = list(
            registry.synthesize_batch([TtsBatchItem("one", "First", settings)], batch_size=1)
        )
        assert results[0].id == "one" and results[0].audio is audio
        assert calls == [("First", settings, {})]
    finally:
        registry.close()


@pytest.mark.parametrize("returned", [None, object()])
def test_optional_hook_contract_does_not_add_runtime_result_validation(returned):
    registry = TtsProviderRegistry()
    adapter = _single_only_adapter(lambda *_args, **_options: pytest.fail("Unexpected fallback"))
    adapter.synthesize_batch = lambda *_args, **_options: returned
    registry.replace(adapter)
    try:
        assert (
            registry.synthesize_batch(
                [TtsBatchItem("one", "First", {"service": "xtts"})], batch_size=1
            )
            is returned
        )
    finally:
        registry.close()


@pytest.mark.parametrize("broken", ["arity", "none_call"])
def test_malformed_callable_batch_hooks_keep_the_existing_call_error(broken):
    registry = TtsProviderRegistry()
    adapter = _single_only_adapter(lambda *_args, **_options: pytest.fail("Unexpected fallback"))
    if broken == "none_call":

        class BrokenHook:
            __call__ = None

        adapter.synthesize_batch = BrokenHook()
    else:
        adapter.synthesize_batch = lambda: None
    assert callable(adapter.synthesize_batch)
    registry.replace(adapter)
    try:
        with pytest.raises(TypeError):
            registry.synthesize_batch(
                [TtsBatchItem("one", "First", {"service": "xtts"})], batch_size=1
            )
    finally:
        registry.close()
