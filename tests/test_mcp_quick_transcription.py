import base64
import hashlib
import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pydantic import ValidationError

from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.credentials import CredentialResolver
from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.network_policy import NetworkPolicy, TargetMode
from pandrator_mcp.schemas.transcription import (
    Base64TranscriptionSource,
    CancelTranscriptionInput,
    DeleteTranscriptionInput,
    GetTranscriptionInput,
    GetTranscriptionResultInput,
    LocalFileTranscriptionSource,
    TranscribeInput,
)
from pandrator_mcp.targets import LocalSourceRoot, TargetProfile, TargetRegistry
from pandrator_mcp.tools.transcription import (
    _transcribe_handle,
    cancel_transcription,
    delete_transcription,
    get_transcription,
    get_transcription_result,
    transcribe,
)


class _Application:
    def __init__(self, *, next_chunk_index: int = 0, chunk_size: int = 4) -> None:
        self.next_chunk_index = next_chunk_index
        self.chunk_size = chunk_size
        self.calls: list[tuple[str, object]] = []
        self.chunks: list[tuple[int, bytes]] = []

    def initialize_transcription(self, **kwargs):
        self.calls.append(("initialize", kwargs))
        return {
            "id": "transcription-1",
            "status": "uploading",
            "job_id": None,
            "chunk_size": self.chunk_size,
            "next_chunk_index": self.next_chunk_index,
            "uploaded_bytes": self.next_chunk_index * self.chunk_size,
        }

    def upload_transcription_chunk(self, transcription_id, index, body):
        self.calls.append(("chunk", (transcription_id, index)))
        self.chunks.append((index, body))
        return {
            "id": transcription_id,
            "status": "uploading",
            "next_chunk_index": index + 1,
            "uploaded_bytes": (index + 1) * self.chunk_size,
        }

    def start_transcription(self, transcription_id, *, wait_seconds):
        self.calls.append(("start", (transcription_id, wait_seconds)))
        return {
            "id": transcription_id,
            "job_id": "job-1",
            "status": "queued",
            "progress": 0.0,
        }

    def get_transcription(self, transcription_id, *, format, wait_seconds):
        self.calls.append(("get", (transcription_id, format, wait_seconds)))
        return {"id": transcription_id, "status": "succeeded", "format": format}

    def get_transcription_result(self, transcription_id, *, format, offset, limit):
        self.calls.append(("result", (transcription_id, format, offset, limit)))
        return {
            "format": format,
            "content": "page",
            "offset": offset,
            "total_chars": 4,
            "next_offset": None,
        }

    def cancel_transcription(self, transcription_id):
        self.calls.append(("cancel", transcription_id))
        return {"id": transcription_id, "status": "cancelled"}

    def delete_transcription(self, transcription_id):
        self.calls.append(("delete", transcription_id))
        return {"id": transcription_id, "deleted": True}


class _Response:
    def __init__(self, status_code: int = 200, payload: dict[str, Any] | None = None):
        self.status_code = status_code
        self.payload = payload or {}

    def iter_content(self, chunk_size: int):
        del chunk_size
        yield json.dumps(self.payload).encode("utf-8")

    def close(self) -> None:
        return


class _TransportSession:
    def __init__(self, responses: list[_Response] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses = list(responses or [])

    def _next_response(self) -> _Response:
        return self.responses.pop(0) if self.responses else _Response()

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"method": "GET", "url": url, **kwargs})
        return self._next_response()

    def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._next_response()

    def put(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"method": "PUT", "url": url, **kwargs})
        return self._next_response()


def _runtime(root: Path, application: _Application):
    profile = TargetProfile(
        name="local",
        mode=TargetMode.LOCAL_MANAGED,
        workspace=str(root),
        local_source_roots=(LocalSourceRoot(name="approved", path=str(root)),),
    )
    return SimpleNamespace(
        profile=profile,
        require_application=lambda: application,
    )


def _client_with_response(response: _Response) -> ApplicationClient:
    profile = TargetProfile(
        name="local",
        mode=TargetMode.LOCAL_MANAGED,
        workspace="/tmp/pandrator-mcp-test",
    )
    registry = TargetRegistry(
        [profile],
        network_policy=NetworkPolicy(lambda _host, _port: ("127.0.0.1",)),
        local_discovery=lambda _profile: ("http://127.0.0.1:8097", "manager"),
    )
    return ApplicationClient(
        registry.bind("local"),
        CredentialResolver(()),
        session=_TransportSession([response]),
        local_bootstrap=lambda _target, _session: "csrf",
    )
class QuickTranscriptionToolTests(unittest.TestCase):
    def test_waiting_routes_extend_transport_timeout_without_mutating_default(self):
        profile = TargetProfile(
            name="local",
            mode=TargetMode.LOCAL_MANAGED,
            workspace="/tmp/pandrator-mcp-test",
        )
        registry = TargetRegistry(
            [profile],
            network_policy=NetworkPolicy(
                lambda _host, _port: ("127.0.0.1",)
            ),
            local_discovery=lambda _profile: ("http://127.0.0.1:8097", "manager"),
        )
        session = _TransportSession()
        client = ApplicationClient(
            registry.bind("local"),
            CredentialResolver(()),
            session=session,
            local_bootstrap=lambda _target, _session: "csrf",
        )

        client.start_transcription("t-1", wait_seconds=30)
        client.get_transcription("t-1", wait_seconds=0)

        self.assertEqual(35.0, session.calls[0]["timeout"])
        self.assertEqual(15.0, session.calls[1]["timeout"])
        self.assertEqual(15.0, client.timeout_seconds)

    def test_local_and_base64_sources_forward_the_same_bytes_and_hash(self):
        content = b"0123456789"
        expected_hash = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "clip.wav").write_bytes(content)

            local_application = _Application(chunk_size=4)
            local = transcribe(
                _runtime(root, local_application),
                TranscribeInput(
                    source=LocalFileTranscriptionSource(
                        root="approved", path="clip.wav"
                    ),
                    idempotency_key="transcribe:local",
                    wait_seconds=7,
                ),
            )
            inline_application = _Application(chunk_size=4)
            inline = transcribe(
                _runtime(root, inline_application),
                TranscribeInput(
                    source=Base64TranscriptionSource(
                        data=base64.b64encode(content).decode("ascii"),
                        filename="clip.wav",
                    ),
                    idempotency_key="transcribe:inline",
                    wait_seconds=7,
                ),
            )

        self.assertEqual(local_application.chunks, inline_application.chunks)
        self.assertEqual(
            expected_hash,
            local_application.calls[0][1]["sha256"],
        )
        self.assertEqual(
            expected_hash,
            inline_application.calls[0][1]["sha256"],
        )
        self.assertEqual("queued", local.result["status"])
        self.assertEqual("queued", inline.result["status"])

    def test_resume_starts_at_backend_next_chunk_index(self):
        content = b"0123456789"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "clip.wav").write_bytes(content)
            application = _Application(next_chunk_index=1, chunk_size=4)

            transcribe(
                _runtime(root, application),
                TranscribeInput(
                    source=LocalFileTranscriptionSource(
                        root="approved", path="clip.wav"
                    ),
                    idempotency_key="transcribe:resume",
                ),
            )

        self.assertEqual([(1, b"4567"), (2, b"89")], application.chunks)
        self.assertEqual("start", application.calls[-1][0])

    def test_source_bounds_and_containment_are_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root.parent / f"{root.name}-outside.wav"
            outside.write_bytes(b"outside")
            self.addCleanup(outside.unlink, missing_ok=True)
            (root / "escape.wav").symlink_to(outside)
            application = _Application()

            with self.assertRaises(PandratorMcpError) as traversal:
                transcribe(
                    _runtime(root, application),
                    TranscribeInput(
                        source=LocalFileTranscriptionSource(
                            root="approved", path="../outside.txt"
                        ),
                        idempotency_key="transcribe:escape",
                    ),
                )
            self.assertEqual("validation_error", traversal.exception.code)

            with self.assertRaises(PandratorMcpError) as symlink:
                transcribe(
                    _runtime(root, application),
                    TranscribeInput(
                        source=LocalFileTranscriptionSource(
                            root="approved", path="escape.wav"
                        ),
                        idempotency_key="transcribe:symlink",
                    ),
                )
            self.assertEqual("network_policy_denied", symlink.exception.code)

            with (root / "large.wav").open("wb") as handle:
                handle.truncate(256 * 1024 * 1024 + 1)
            with self.assertRaises(PandratorMcpError) as oversized:
                transcribe(
                    _runtime(root, application),
                    TranscribeInput(
                        source=LocalFileTranscriptionSource(
                            root="approved", path="large.wav"
                        ),
                        idempotency_key="transcribe:large",
                    ),
                )
            self.assertEqual("validation_error", oversized.exception.code)

            with self.assertRaises(PandratorMcpError) as invalid_base64:
                transcribe(
                    _runtime(root, application),
                    TranscribeInput(
                        source=Base64TranscriptionSource(
                            data="not-base64!", filename="clip.wav"
                        ),
                        idempotency_key="transcribe:base64",
                    ),
                )
            self.assertEqual("validation_error", invalid_base64.exception.code)

    def test_status_result_cancel_delete_forward_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            application = _Application()
            runtime = _runtime(Path(directory), application)

            get_transcription(
                runtime,
                GetTranscriptionInput(id="t-1", format="srt", wait_seconds=9),
            )
            get_transcription_result(
                runtime,
                GetTranscriptionResultInput(
                    id="t-1", format="json", offset=10, limit=20
                ),
            )
            cancel_transcription(runtime, CancelTranscriptionInput(id="t-1"))
            delete_transcription(runtime, DeleteTranscriptionInput(id="t-1"))

        self.assertEqual(
            [
                ("get", ("t-1", "srt", 9)),
                ("result", ("t-1", "json", 10, 20)),
                ("cancel", "t-1"),
                ("delete", "t-1"),
            ],
            application.calls,
        )

    def test_unsupported_filename_is_rejected_before_open_or_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "clip.txt").write_bytes(b"audio")
            application = _Application()

            with self.assertRaises(PandratorMcpError) as error:
                transcribe(
                    _runtime(root, application),
                    TranscribeInput(
                        source=LocalFileTranscriptionSource(
                            root="approved", path="clip.txt"
                        ),
                        idempotency_key="transcribe:suffix",
                    ),
                )

        self.assertEqual("validation_error", error.exception.code)
        self.assertEqual([], application.calls)

        with self.assertRaises(ValidationError):
            Base64TranscriptionSource(
                data=base64.b64encode(b"audio").decode("ascii"),
                filename="clip.txt",
            )

    def test_hash_is_capped_and_growth_prevents_initialization(self):
        class _GrowingReader(BytesIO):
            def __init__(self) -> None:
                super().__init__(b"0123456789X")
                self.read_sizes: list[int] = []

            def read(self, size: int = -1) -> bytes:
                self.read_sizes.append(size)
                return super().read(size)

        application = _Application()
        reader = _GrowingReader()
        arguments = TranscribeInput(
            source=Base64TranscriptionSource(
                data=base64.b64encode(b"0123456789").decode("ascii"),
                filename="clip.wav",
            ),
            idempotency_key="transcribe:growth",
        )

        with self.assertRaises(PandratorMcpError) as error:
            _transcribe_handle(
                _runtime(Path(tempfile.gettempdir()), application),
                arguments,
                handle=reader,
                filename="clip.wav",
                size_bytes=10,
            )

        self.assertEqual("source_changed", error.exception.code)
        self.assertEqual([10, 1], reader.read_sizes)
        self.assertEqual([], application.calls)

    def test_transcription_error_codes_survive_json_and_binary_mapping(self):
        with self.assertRaises(PandratorMcpError) as expired:
            _client_with_response(
                _Response(
                    410,
                    {"error": {"code": "transcription_expired", "message": "gone"}},
                )
            ).get_transcription("t-1")
        self.assertEqual("transcription_expired", expired.exception.code)

        with self.assertRaises(PandratorMcpError) as hash_mismatch:
            _client_with_response(
                _Response(
                    409,
                    {
                        "error": {
                            "code": "source_hash_mismatch",
                            "message": "mismatch",
                        }
                    },
                )
            ).start_transcription("t-1")
        self.assertEqual("source_hash_mismatch", hash_mismatch.exception.code)

        with self.assertRaises(PandratorMcpError) as chunk_conflict:
            _client_with_response(
                _Response(
                    409,
                    {"error": {"code": "chunk_conflict", "message": "conflict"}},
                )
            ).upload_transcription_chunk("t-1", 0, b"audio")
        self.assertEqual("chunk_conflict", chunk_conflict.exception.code)


if __name__ == "__main__":
    unittest.main()
