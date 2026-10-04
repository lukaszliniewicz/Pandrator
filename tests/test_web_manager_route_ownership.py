"""Route registration borrows an explicitly owned manager bridge."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from flask import Flask

from pandrator.web import manager_proxy
from pandrator.web.manager_proxy import LocalManagerProxy, ManagerConnection
from tests.test_web_manager_proxy_lifetime import LocalHttp
from tests.test_web_manager_proxy_lifetime import local_http as local_http


class FalseyProxy(LocalManagerProxy):
    def __bool__(self) -> bool:
        return False


def test_registration_requires_a_caller_owned_bridge() -> None:
    app = Flask(__name__)
    options: dict[str, Any] = {
        "require_auth": lambda function: function,
        "error_response": lambda *args: args,
    }
    with patch.object(
        manager_proxy, "LocalManagerProxy", side_effect=AssertionError("hidden owner")
    ):
        with pytest.raises(TypeError, match="proxy"):
            manager_proxy.register_manager_routes(app, **options)
    assert len(list(app.url_map.iter_rules())) == 1


def test_requests_keep_falsey_supplied_bridge_live(local_http: LocalHttp) -> None:
    app = Flask(__name__)
    proxy = FalseyProxy()
    connection = ManagerConnection(local_http.url, "fixture", "fixture-token")
    try:
        with (
            patch.object(
                manager_proxy, "LocalManagerProxy", side_effect=AssertionError("hidden owner")
            ),
            patch.object(proxy, "discover", return_value=connection),
            patch.object(proxy.session, "close", wraps=proxy.session.close) as close,
        ):
            manager_proxy.register_manager_routes(
                app,
                require_auth=lambda function: function,
                error_response=lambda *args: args,
                proxy=proxy,
            )
            with app.test_client() as client:
                for _ in range(2):
                    response = client.get("/api/v1/manager/components")
                    assert response.status_code == 200
                    assert response.get_json() == {"instance_id": "fixture", "ok": True}
            close.assert_not_called()
            assert not proxy._closed
            proxy.close()
            close.assert_called_once_with()
    finally:
        proxy.close()
