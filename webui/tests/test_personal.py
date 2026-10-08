import json
import httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from webui.backend.routes import personal


def login(client):
    client.post("/api/setup", json={"username": "admin", "password": "local-test-password"})
    client.post("/api/login", json={"username": "admin", "password": "local-test-password"})


def mock_upstream(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(personal, "_client", lambda proxy: original(
        transport=httpx.MockTransport(handler), timeout=20, trust_env=False,
    ))


def test_requires_local_login(client):
    assert client.post("/api/personal/check", json={"session_token": "test-session"}).status_code == 401
    assert client.put("/api/personal/payment-proxy", json={}).status_code == 401


def test_payment_proxy_writes_original_schema_and_preserves_config(client, monkeypatch, tmp_path):
    login(client)
    path = tmp_path / "payment.json"
    original = {"cards": [{"name": "existing"}], "fresh_checkout": {"plan": {"plan_name": "existing"}},
                "stage_proxies": {"confirm": "http://old:80"}}
    path.write_text(json.dumps(original))
    monkeypatch.setattr(personal.settings, "PAY_CONFIG_PATH", path)
    response = client.put("/api/personal/payment-proxy", json={
        "payment_proxy": "socks5://user:proxy-secret@host.docker.internal:12334",
        "account_proxy": "http://host.docker.internal:12334",
        "session_token": "must-not-be-saved",
    })
    assert response.status_code == 200
    cfg = json.loads(path.read_text())
    assert cfg["proxy"].startswith("socks5://")
    assert cfg["fresh_checkout"]["proxy"] == "http://host.docker.internal:12334"
    assert cfg["cards"] == original["cards"]
    assert cfg["fresh_checkout"]["plan"] == original["fresh_checkout"]["plan"]
    assert all(cfg["stage_proxies"][stage] == cfg["proxy"] for stage in personal._PAYMENT_STAGES)
    assert "proxy-secret" not in response.text
    assert "must-not-be-saved" not in path.read_text()
    response = client.put("/api/personal/payment-proxy", json={})
    assert response.status_code == 200
    cfg = json.loads(path.read_text())
    assert cfg["proxy"] == "" and cfg["fresh_checkout"]["proxy"] == ""
    assert all(value == "" for value in cfg["stage_proxies"].values())


def test_invalid_config_is_not_overwritten(client, monkeypatch, tmp_path):
    login(client)
    path = tmp_path / "payment.json"
    path.write_text("broken")
    monkeypatch.setattr(personal.settings, "PAY_CONFIG_PATH", path)
    assert client.put("/api/personal/payment-proxy", json={}).status_code == 409
    assert path.read_text() == "broken"


@pytest.mark.parametrize("proxy", ["http://", "http://host", "file:///tmp/secret", "http://host:123/a", "http://host:wrong"])
def test_invalid_payment_proxy_rejected(proxy):
    with pytest.raises(HTTPException):
        personal._proxy_value(SecretStr(proxy))


def test_reads_existing_account_without_exposing_credentials(client, monkeypatch):
    login(client)
    requests = []

    def handler(req):
        requests.append(req)
        if str(req.url) == personal.SESSION_URL:
            assert req.headers["Cookie"] == "__Secure-next-auth.session-token=test-session"
            return httpx.Response(200, json={"user": {"email": "student@example.com"}, "accessToken": "test-access"})
        assert str(req.url) == personal.ACCOUNT_URL
        assert "cookie" not in req.headers
        assert req.headers["Authorization"] == "Bearer test-access"
        return httpx.Response(200, json={"accounts": {"default": {"entitlement": {
            "has_active_subscription": True, "subscription_plan": "chatgptplusplan",
        }}}})

    mock_upstream(monkeypatch, handler)
    response = client.post("/webui/api/personal/check", json={"session_token": json.dumps({
        "sessionToken": "test-session", "accessToken": "stale-access",
        "user": {"email": "untrusted@example.com"}, "account": {"planType": "pro"},
    })})
    assert response.status_code == 200
    assert response.json()["subscription"] == {"active": True, "plan": "chatgptplusplan"}
    assert response.headers["Cache-Control"] == "no-store"
    assert "test-session" not in response.text and "test-access" not in response.text
    assert len(requests) == 2 and all(r.method == "GET" for r in requests)
    assert response.json()["email"] == "student@example.com"
    assert "stale-access" not in response.text


@pytest.mark.parametrize("raw,expected", [
    ("test-session", ("test-session", "")),
    ('{"sessionToken":"test-session","accessToken":"test-access"}', ("test-session", "test-access")),
    ('{"session_token":"test-session"}', ("test-session", "")),
    ('{"access_token":"test-access"}', ("", "test-access")),
    (json.dumps(json.dumps({"sessionToken": "test-session"})), ("test-session", "")),
])
def test_extracts_credentials(raw, expected):
    assert personal._credentials(raw) == expected


def test_recovers_tokens_when_warning_metadata_has_broken_quotes():
    raw = '{"警告横幅":"不要分享"!!!", "accessToken":"test-access", "user":bad, "sessionToken":"test-session"}'
    assert personal._credentials(raw) == ("test-session", "test-access")


@pytest.mark.parametrize("raw", [
    '{"warning":bad,"sessionToken":"cut-off',
    '{"warning":bad,"sessionToken":"cut-off"garbage}',
    '{"warning":bad,"sessionToken":"one","session_token":"two"}',
    '{"warning":bad,"sessionToken":123}',
    '{"warning":bad,"sessionToken":"bad\\q"}',
    '{"warning":bad,"sessionToken":"翻译后的凭证"}',
])
def test_recovery_rejects_truncated_ambiguous_or_edited_credentials(raw):
    with pytest.raises(HTTPException) as exc:
        personal._credentials(raw)
    assert exc.value.status_code == 400
    assert raw not in exc.value.detail


@pytest.mark.parametrize("raw", ['{bad secret', '[]', '{"user":{"email":"fake"}}',
                                      '{"sessionToken":{}}', '{"sessionToken":"bad;value"}'])
def test_rejects_malformed_exports_without_echoing(raw):
    with pytest.raises(HTTPException) as exc:
        personal._credentials(raw)
    assert exc.value.status_code == 400
    assert raw not in exc.value.detail


def test_access_only_verifies_identity_with_server(client, monkeypatch):
    login(client)
    def handler(req):
        assert req.headers["Authorization"] == "Bearer test-access"
        assert "cookie" not in req.headers
        if str(req.url) == personal.ME_URL:
            return httpx.Response(200, json={"email": "verified@example.com"})
        assert str(req.url) == personal.ACCOUNT_URL
        return httpx.Response(200, json={"accounts": {"default": {"entitlement": {
            "has_active_subscription": False, "subscription_plan": "free",
        }}}})
    mock_upstream(monkeypatch, handler)
    response = client.post("/api/personal/check", json={"session_token": json.dumps({
        "accessToken": "test-access", "user": {"email": "fake@example.com"},
    })})
    assert response.status_code == 200
    assert response.json()["email"] == "verified@example.com"
    assert response.json()["credential_source"] == "access_token"
    assert "test-access" not in response.text


def test_session_refresh_failure_falls_back_to_verified_access(client, monkeypatch):
    login(client)
    calls = []
    def handler(req):
        calls.append(str(req.url))
        if str(req.url) == personal.SESSION_URL:
            return httpx.Response(403)
        if str(req.url) == personal.ME_URL:
            assert req.headers["Authorization"] == "Bearer supplied-access"
            return httpx.Response(200, json={"email": "verified@example.com"})
        return httpx.Response(200, json={"accounts": {"default": {"entitlement": {
            "has_active_subscription": False, "subscription_plan": "free",
        }}}})
    mock_upstream(monkeypatch, handler)
    response = client.post("/api/personal/check", json={"session_token": json.dumps({
        "sessionToken": "test-session", "accessToken": "supplied-access", "user": {"email": "fake@example.com"},
    })})
    assert response.status_code == 200
    assert response.json()["email"] == "verified@example.com"
    assert response.json()["credential_source"] == "access_token"
    assert response.json()["auth_note"]
    assert calls == [personal.SESSION_URL, personal.ME_URL, personal.ACCOUNT_URL]


def test_uses_original_transport_and_explicit_proxy(monkeypatch):
    captured = {}
    def factory(**kwargs):
        captured.update(kwargs)
        return object()
    monkeypatch.setattr(personal.curl_requests, "Session", factory)
    personal._client("http://host.docker.internal:12334")
    assert captured["impersonate"] == "chrome136"
    assert captured["proxy"] == "http://host.docker.internal:12334"
    assert captured["allow_redirects"] is False
    assert captured["trust_env"] is False


@pytest.mark.parametrize("status,data,expected", [(401, {}, 400), (403, {}, 502), (200, {}, 400)])
def test_invalid_and_challenged_sessions(client, monkeypatch, status, data, expected):
    login(client)
    mock_upstream(monkeypatch, lambda req: httpx.Response(status, json=data))
    response = client.post("/api/personal/check", json={"session_token": "test-session"})
    assert response.status_code == expected
    assert "test-session" not in response.text


def test_subscription_failure_is_unknown_not_free(client, monkeypatch):
    login(client)
    def handler(req):
        if str(req.url) == personal.SESSION_URL:
            return httpx.Response(200, json={"user": {"email": "student@example.com"}, "accessToken": "test-access"})
        return httpx.Response(403)
    mock_upstream(monkeypatch, handler)
    response = client.post("/api/personal/check", json={"session_token": "test-session"})
    assert response.status_code == 200
    assert response.json()["subscription"] is None
    assert response.json()["subscription_message"]


def test_network_exception_is_redacted(client, monkeypatch):
    login(client)
    def handler(req):
        raise httpx.ConnectError("proxy-password secret-session", request=req)
    mock_upstream(monkeypatch, handler)
    response = client.post("/api/personal/check", json={"session_token": "test-session"})
    assert response.status_code == 502
    assert "proxy-password" not in response.text and "secret-session" not in response.text


@pytest.mark.parametrize("token", ["", "Cookie: secret", "secret;other=value", "secret\nvalue"])
def test_rejects_cookie_headers(client, token):
    login(client)
    assert client.post("/api/personal/check", json={"session_token": token}).status_code == 400
