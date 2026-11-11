import inspect
import io
import ipaddress
import json
import logging
import os
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pandrator.web.security_events import SecurityEventSink

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_TEMP_ROOT = _REPOSITORY_ROOT / "verification" / "security-events"


class SecurityEventLogTests(unittest.TestCase):
    def temporary_directory(self) -> Path:
        root_existed = _TEMP_ROOT.exists()
        _TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=_TEMP_ROOT)

        def remove_empty_roots() -> None:
            if root_existed:
                return
            try:
                _TEMP_ROOT.rmdir()
            except OSError:
                return
            try:
                _TEMP_ROOT.parent.rmdir()
            except OSError:
                pass

        self.addCleanup(remove_empty_roots)
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name)

    @staticmethod
    def emit(
        sink: SecurityEventSink,
        *,
        event: str = "authentication_failed",
        client_ip: str | None = "192.0.2.12",
        status: int = 401,
        request_id: str | None = "req-1",
        route: str = "/api/v1/auth/login",
        method: str = "POST",
    ) -> bool:
        return sink.emit(
            event,
            client_ip=client_ip,
            status=status,
            request_id=request_id,
            route=route,
            method=method,
        )

    def test_unconfigured_sink_is_disabled_without_io(self):
        for target in (None, ""):
            with self.subTest(target=target):
                output = io.StringIO()
                sink = SecurityEventSink(target)
                self.assertFalse(sink.enabled)
                with (
                    patch(
                        "pandrator.web.security_events.os.open",
                        side_effect=AssertionError("disabled sink opened a file"),
                    ),
                    patch(
                        "pandrator.web.security_events.time.monotonic",
                        side_effect=AssertionError("disabled sink accessed the clock"),
                    ),
                    redirect_stdout(output),
                ):
                    self.assertFalse(self.emit(sink))
                self.assertEqual(output.getvalue(), "")

    def test_stdout_is_resolved_at_write_time_and_records_are_ordered_jsonl(self):
        sink = SecurityEventSink("stdout")
        first = io.StringIO()
        second = io.StringIO()
        self.assertTrue(sink.enabled)
        with redirect_stdout(first):
            self.assertTrue(self.emit(sink))
        with redirect_stdout(second):
            self.assertTrue(self.emit(sink, client_ip="2001:0DB8:0:0::1"))

        first_record = json.loads(first.getvalue())
        second_record = json.loads(second.getvalue())
        self.assertEqual(
            list(first_record),
            ["timestamp", "event", "client_ip", "status", "request_id", "route", "method"],
        )
        self.assertRegex(first_record["timestamp"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(first_record["client_ip"], "192.0.2.12")
        self.assertEqual(second_record["client_ip"], "2001:db8::1")
        self.assertEqual(first_record["event"], "authentication_failed")
        self.assertTrue(first.getvalue().endswith("\n"))
        self.assertTrue(second.getvalue().endswith("\n"))

    def test_optional_ip_is_null_and_invalid_request_id_is_omitted(self):
        output = io.StringIO()
        sink = SecurityEventSink("stdout")
        with redirect_stdout(output):
            self.assertTrue(
                self.emit(sink, client_ip=None, request_id='req-1\r\n{"token":"secret"}')
            )

        record = json.loads(output.getvalue())
        self.assertIsNone(record["client_ip"])
        self.assertNotIn("request_id", record)
        self.assertNotIn("token", record)
        self.assertNotIn("body", record)
        self.assertNotIn("headers", record)

    def test_rejects_untrusted_event_ip_status_method_and_route_values(self):
        output = io.StringIO()
        sink = SecurityEventSink("stdout")
        invalid_calls = (
            {"event": "unknown"},
            {"client_ip": "not-an-ip"},
            {"client_ip": "fe80::1%eth0"},
            {"status": 99},
            {"status": 600},
            {"status": True},
            {"method": "GET"},
            {"route": "/api/v1/auth/login?token=secret"},
            {"route": "/api/v1/auth/login#fragment"},
            {"route": "/api/v1/auth/+login"},
            {"route": "/api/v1/auth/login\r\nforged"},
            {"route": "/api/" + "a" * 156},
            {"route": "/private/login"},
        )
        with redirect_stdout(output):
            for arguments in invalid_calls:
                with self.subTest(arguments=arguments):
                    self.assertFalse(self.emit(sink, **arguments))
        self.assertEqual(output.getvalue(), "")

    def test_interface_has_no_arbitrary_metadata_or_credential_fields(self):
        parameters = inspect.signature(SecurityEventSink.emit).parameters
        self.assertEqual(
            list(parameters),
            ["self", "event", "client_ip", "status", "request_id", "route", "method"],
        )
        self.assertNotIn("**", str(inspect.signature(SecurityEventSink.emit)))

    @unittest.skipIf(
        os.name == "nt",
        "The file sink requires POSIX descriptor permission hardening "
        "(os.fchmod), which Windows does not provide.",
    )
    def test_file_target_is_private_and_symlinks_are_rejected(self):
        root = self.temporary_directory()
        target = root / "events.jsonl"
        target.write_text("existing\n", encoding="utf-8")
        target.chmod(0o644)
        sink = SecurityEventSink(str(target))
        self.assertTrue(sink.enabled)
        self.assertTrue(self.emit(sink))
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(len(target.read_text(encoding="utf-8").splitlines()), 2)

        victim = root / "victim.jsonl"
        victim.write_text("preserved\n", encoding="utf-8")
        link = root / "linked.jsonl"
        link.symlink_to(victim)
        symlink_sink = SecurityEventSink(str(link))
        self.assertFalse(self.emit(symlink_sink))
        self.assertTrue(link.is_symlink())
        self.assertEqual(victim.read_text(encoding="utf-8"), "preserved\n")

    def test_file_write_errors_are_soft_and_warn_once_without_path(self):
        root = self.temporary_directory()
        private_path = root / "private-path-marker.jsonl"
        sink = SecurityEventSink(str(private_path))
        output = io.StringIO()
        with (
            patch(
                "pandrator.web.security_events.os.open",
                side_effect=PermissionError(f"cannot open {private_path}"),
            ),
            self.assertLogs("pandrator.web.security_events", level=logging.WARNING) as captured,
        ):
            self.assertFalse(self.emit(sink))
            self.assertFalse(self.emit(sink))

        self.assertEqual(len(captured.records), 1)
        self.assertEqual(captured.records[0].getMessage(), "Security event sink write failed")
        self.assertNotIn(str(private_path), captured.output[0])
        self.assertFalse(private_path.exists())
        self.assertEqual(output.getvalue(), "")

    @unittest.skipIf(
        os.name == "nt",
        "The file sink requires POSIX descriptor permission hardening "
        "(os.fchmod), which Windows does not provide.",
    )
    def test_concurrent_authentication_failures_are_individual_json_lines(self):
        root = self.temporary_directory()
        target = root / "events.jsonl"
        sink = SecurityEventSink(str(target))

        def emit(index: int) -> bool:
            return self.emit(sink, request_id=f"req-{index}")

        with ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(emit, range(240)))
        self.assertTrue(all(results))
        lines = target.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 240)
        records = [json.loads(line) for line in lines]
        self.assertEqual(
            {record["request_id"] for record in records}, {f"req-{i}" for i in range(240)}
        )

    def test_throttled_events_are_deduplicated_but_failures_are_never_suppressed(self):
        output = io.StringIO()
        sink = SecurityEventSink("stdout")
        with (
            patch(
                "pandrator.web.security_events.time.monotonic",
                side_effect=[10.0, 10.0, 10.0, 40.0, 40.0, 40.0],
            ),
            redirect_stdout(output),
        ):
            self.assertTrue(
                self.emit(sink, event="authentication_throttled", client_ip="192.0.2.1")
            )
            self.assertFalse(
                self.emit(sink, event="authentication_throttled", client_ip="192.0.2.1")
            )
            self.assertTrue(
                self.emit(sink, event="authentication_throttled", client_ip="192.0.2.2")
            )
            self.assertTrue(
                self.emit(sink, event="project_link_redemption_throttled", client_ip="192.0.2.1")
            )
            self.assertTrue(
                self.emit(sink, event="authentication_throttled", client_ip="192.0.2.1")
            )
            self.assertTrue(self.emit(sink, event="authentication_failed", client_ip="192.0.2.1"))

        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(records), 5)
        self.assertEqual(sum(record["event"] == "authentication_failed" for record in records), 1)

    def test_throttle_cache_is_bounded_and_failures_survive_saturation(self):
        output = io.StringIO()
        sink = SecurityEventSink("stdout")
        with redirect_stdout(output):
            for index in range(4097):
                address = ipaddress.IPv6Address(index).compressed
                self.emit(
                    sink,
                    event="authentication_throttled",
                    client_ip=address,
                    request_id=f"req-{index}",
                )
            self.assertEqual(len(sink._throttled_events), 4096)
            self.assertFalse(
                self.emit(
                    sink,
                    event="authentication_throttled",
                    client_ip="::",
                )
            )
            self.assertTrue(
                self.emit(
                    sink,
                    event="authentication_failed",
                    client_ip="::",
                )
            )
            self.assertTrue(
                self.emit(
                    sink,
                    event="project_link_redemption_failed",
                    client_ip="::",
                )
            )

        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(records), 4098)
        self.assertEqual(records[-2]["event"], "authentication_failed")
        self.assertEqual(records[-1]["event"], "project_link_redemption_failed")


if __name__ == "__main__":
    unittest.main()
