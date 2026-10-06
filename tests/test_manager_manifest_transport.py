"""Manifest transport bounds and ownership, independent of signature verification."""

from __future__ import annotations

import ipaddress
import ssl
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from pandrator_manager.errors import ManagerError
from pandrator_manager.releases import discovery


@pytest.fixture
def transport():
    response = Mock(spec=requests.Response)
    response.url = "https://updates.example/manifest.json"
    response.headers = {}
    response.iter_content.return_value = [b"", b'{"version":"fixture"}']
    client = Mock(spec=requests.Session)
    client.get.return_value = response
    with (
        patch.object(discovery.requests, "Session", return_value=client),
        patch.object(discovery, "select_ca_bundle", return_value=SimpleNamespace(path="/ca.pem")),
    ):
        yield SimpleNamespace(environment={}), client, response


@pytest.mark.parametrize("borrowed", [False, True])
def test_manifest_success_closes_only_owned_resources(transport, borrowed):
    context, client, response = transport
    assert discovery.fetch_manager_manifest(context, session=client if borrowed else None) == {
        "version": "fixture"
    }
    response.close.assert_called_once_with()
    assert client.close.call_count == (0 if borrowed else 1)
    kwargs = client.get.call_args.kwargs
    assert kwargs == {
        "stream": True,
        "timeout": (15, 45),
        "allow_redirects": True,
        "verify": "/ca.pem",
        "headers": {"Accept": "application/json", "User-Agent": "Pandrator-Manager"},
    }
    response.iter_content.assert_called_once_with(chunk_size=64 * 1024)


@pytest.mark.parametrize("borrowed", [False, True])
def test_failed_acquisition_preserves_error_and_closes_owned_client(transport, borrowed):
    context, client, response = transport
    original = requests.ConnectionError("connection failure")
    client.get.side_effect = original
    client.close.side_effect = OSError("client cleanup failure")
    with pytest.raises(ManagerError) as caught:
        discovery.fetch_manager_manifest(context, session=client if borrowed else None)
    assert caught.value.code == "update_check_failed"
    assert caught.value.__cause__ is original
    assert client.close.call_count == (0 if borrowed else 1)
    response.close.assert_not_called()


def test_http_error_survives_both_cleanup_failures(transport):
    context, client, response = transport
    original = requests.HTTPError("original HTTP failure")
    response.raise_for_status.side_effect = original
    response.close.side_effect = OSError("response cleanup failure")
    client.close.side_effect = OSError("client cleanup failure")
    with pytest.raises(ManagerError) as caught:
        discovery.fetch_manager_manifest(context)
    assert caught.value.code == "update_check_failed"
    assert caught.value.__cause__ is original
    assert caught.value.details == {"reason": "original HTTP failure"}
    response.close.assert_called_once_with()
    client.close.assert_called_once_with()


@pytest.mark.parametrize("client_also_fails", [False, True])
def test_successful_fetch_keeps_first_cleanup_error_visible(transport, client_also_fails):
    context, client, response = transport
    original = OSError("response cleanup failure")
    response.close.side_effect = original
    if client_also_fails:
        client.close.side_effect = OSError("client cleanup failure")
    with pytest.raises(OSError) as caught:
        discovery.fetch_manager_manifest(context)
    assert caught.value is original
    client.close.assert_called_once_with()


def test_cleanup_failure_is_visible_inside_callers_exception_handler(transport):
    context, client, response = transport
    original = OSError("client cleanup failure")
    client.close.side_effect = original
    try:
        raise ValueError("unrelated caller failure")
    except ValueError:
        with pytest.raises(OSError) as caught:
            discovery.fetch_manager_manifest(context)
    assert caught.value is original
    response.close.assert_called_once_with()


@pytest.mark.parametrize("failure", ["declared_size", "stream_size", "redirect"])
def test_transport_rejections_preserve_primary_failure_and_cleanup(transport, failure):
    context, client, response = transport
    if failure == "declared_size":
        response.headers = {"Content-Length": str(discovery.MAXIMUM_MANIFEST_BYTES + 1)}
    elif failure == "stream_size":
        response.iter_content.return_value = [b"x" * discovery.MAXIMUM_MANIFEST_BYTES, b"x"]
    else:
        response.url = "http://updates.example/manifest.json"
    response.close.side_effect = OSError("response cleanup failure")
    with pytest.raises(ManagerError) as caught:
        discovery.fetch_manager_manifest(context)
    assert caught.value.code == (
        "invalid_update_channel" if failure == "redirect" else "update_manifest_too_large"
    )
    client.close.assert_called_once_with()


@pytest.mark.parametrize("body", [b"not json", b"\xff", b"[]"])
def test_invalid_manifest_is_rejected_after_resources_close(transport, body):
    context, client, response = transport
    response.iter_content.return_value = [body]
    with pytest.raises(ManagerError) as caught:
        discovery.fetch_manager_manifest(context)
    assert caught.value.code == "invalid_update_manifest"
    response.close.assert_called_once_with()
    client.close.assert_called_once_with()


@pytest.fixture
def https_manifest_server(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certificate_path = tmp_path / "certificate.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path = tmp_path / "key.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"version":"native-tls"}'
            self.send_response(503 if self.path == "/failure" else 200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificate_path, key_path)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_port}", certificate_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize("failed_http", [False, True])
def test_native_tls_manifest_and_owned_cleanup(https_manifest_server, failed_http):
    url, certificate_path = https_manifest_server
    context = SimpleNamespace(
        environment={
            "PANDRATOR_MANAGER_UPDATE_MANIFEST_URL": url + ("/failure" if failed_http else "/ok"),
        }
    )
    client = requests.Session()
    client.trust_env = False
    get = client.get
    response_close = Mock()
    client_close = Mock(wraps=client.close)

    def acquire(*args, **kwargs):
        response = get(*args, **kwargs)
        close = response.close

        def close_response():
            close()
            response_close()
            if failed_http:
                raise OSError("injected failure after actual response close")

        response.close = close_response
        return response

    with (
        patch.object(discovery.requests, "Session", return_value=client),
        patch.object(
            discovery, "select_ca_bundle", return_value=SimpleNamespace(path=certificate_path)
        ),
        patch.object(client, "get", side_effect=acquire),
        patch.object(client, "close", client_close),
    ):
        if failed_http:
            with pytest.raises(ManagerError) as caught:
                discovery.fetch_manager_manifest(context)
            assert caught.value.code == "update_check_failed"
            assert isinstance(caught.value.__cause__, requests.HTTPError)
            assert caught.value.__cause__.response.status_code == 503
        else:
            assert discovery.fetch_manager_manifest(context) == {"version": "native-tls"}
    response_close.assert_called_once_with()
    client_close.assert_called_once_with()
