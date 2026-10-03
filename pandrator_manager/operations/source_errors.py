"""Source acquisition error classification and transport-safe diagnostics."""

from __future__ import annotations

from urllib.parse import urlsplit

from ..errors import ManagerError
from ..tls import CABundleSelection


def _is_tls_verification_error(error: BaseException) -> bool:
    inspected: set[int] = set()
    pending: list[BaseException] = [error]
    while pending and len(inspected) < 16:
        selected = pending.pop()
        if id(selected) in inspected:
            continue
        inspected.add(id(selected))
        message = str(selected).casefold()
        if any(
            marker in message
            for marker in (
                "certificate_verify_failed",
                "certificate verify failed",
                "unable to get local issuer certificate",
                "self-signed certificate",
                "hostname mismatch",
            )
        ):
            return True
        for nested in (selected.__cause__, selected.__context__):
            if isinstance(nested, BaseException):
                pending.append(nested)
        pending.extend(item for item in selected.args if isinstance(item, BaseException))
    return False


def _source_acquisition_error(
    *,
    error: Exception,
    label: str,
    repo_url: str,
    ca_bundle: CABundleSelection,
) -> ManagerError:
    host = str(urlsplit(repo_url).hostname or "the source host")
    details = {
        "host": host,
        "ca_bundle_source": ca_bundle.source,
        "error_type": type(error).__name__,
    }
    if _is_tls_verification_error(error):
        return ManagerError(
            "source_tls_verification_failed",
            f"Pandrator could not verify the TLS certificate while downloading "
            f"{label} from {host}. Check the computer's date and time and any "
            "HTTPS-inspecting proxy, then download the diagnostic bundle if "
            "the problem continues.",
            details,
            502,
        )
    return ManagerError(
        "source_download_failed",
        f"Pandrator could not download {label} from {host}. Check the internet "
        "and proxy connection, then download the diagnostic bundle if the "
        "problem continues.",
        details,
        502,
    )
