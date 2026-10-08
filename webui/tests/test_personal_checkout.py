import json
import httpx
import pytest
from fastapi import HTTPException
from webui.backend.routes import personal, personal_checkout as checkout
from webui.tests.test_personal import login, mock_upstream


@pytest.fixture(autouse=True)
def clear_orders():
    checkout._recent.clear()
    yield
    checkout._recent.clear()


def request_body():
    return {"session_token": "test-session", "confirmed_email": "student@example.com",
            "plan": "plus", "billing_country": "US", "billing_currency": "USD",
            "payment_proxy": "http://host.docker.internal:12334"}


def upstream(monkeypatch, active=False, checkout_url="https://checkout.stripe.com/c/pay/cs_test_example"):
    calls = []
    def handler(req):
        calls.append(req)
        if str(req.url) == personal.SESSION_URL:
            return httpx.Response(200, json={"user": {"email": "student@example.com"}, "accessToken": "test-access"})
        if str(req.url) == personal.ACCOUNT_URL:
            return httpx.Response(200, json={"accounts": {"default": {"entitlement": {"has_active_subscription": active, "subscription_plan": "free"}}}})
        assert str(req.url) == checkout.CHECKOUT_URL and req.method == "POST"
        payload = json.loads(req.content)
        assert payload["checkout_ui_mode"] == "hosted"
        assert payload["plan_name"] == "chatgptplusplan"
        assert payload["billing_details"] == {"country": "US", "currency": "USD"}
        assert "promo_campaign" not in payload and "team_plan_data" not in payload
        assert "test-session" not in req.content.decode()
        return httpx.Response(200, json={"checkout_url": checkout_url})
    mock_upstream(monkeypatch, handler)
    return calls


def test_checkout_requires_local_auth(client):
    assert client.post("/api/personal/checkout", json=request_body()).status_code == 401


def test_create_existing_account_checkout_and_reuse_without_double_post(client, monkeypatch):
    login(client)
    calls = upstream(monkeypatch)
    factory = personal._client
    proxies = []
    def capture_proxy(proxy):
        proxies.append(proxy)
        return factory(proxy)
    monkeypatch.setattr(personal, "_client", capture_proxy)
    first = client.post("/webui/api/personal/checkout", json=request_body())
    assert first.status_code == 200
    assert first.json()["status"] == "awaiting_user_payment"
    assert not first.json()["reused"]
    assert proxies == ["", "http://host.docker.internal:12334"]
    assert first.headers["Cache-Control"] == "no-store"
    assert "test-access" not in first.text and "test-session" not in first.text
    second = client.post("/api/personal/checkout", json=request_body())
    assert second.status_code == 200 and second.json()["reused"]
    assert sum(req.method == "POST" for req in calls) == 1


@pytest.mark.parametrize("active", [True, None])
def test_active_or_unknown_subscription_never_creates_order(client, monkeypatch, active):
    login(client)
    calls = upstream(monkeypatch, active=active)
    assert client.post("/api/personal/checkout", json=request_body()).status_code == 409
    assert all(req.method == "GET" for req in calls)


def test_account_change_never_creates_order(client, monkeypatch):
    login(client)
    calls = upstream(monkeypatch)
    req = request_body()
    req["confirmed_email"] = "different@example.com"
    assert client.post("/api/personal/checkout", json=req).status_code == 409
    assert all(r.method == "GET" for r in calls)


@pytest.mark.parametrize("url", ["https://evil.example/pay/x", "https://checkout.stripe.com.evil.example/c/pay/x",
                                  "http://checkout.stripe.com/c/pay/x", "https://user@chatgpt.com/checkout/x",
                                  "https://chatgpt.com/other/path", "https://checkout.stripe.com:1234/c/pay/x"])
def test_untrusted_checkout_urls_rejected(url):
    with pytest.raises(HTTPException):
        checkout._hosted_url({"checkout_url": url})


def test_original_canonical_checkout_schema_supported():
    assert checkout._hosted_url({"checkout_session_id": "cs_test_example", "processor_entity": "openai_llc"}) == "https://chatgpt.com/checkout/openai_llc/cs_test_example"


def test_error_classification_does_not_echo_sensitive_body():
    response = httpx.Response(400, json={"error": {"code": "already_paid", "message": "User is already paid secret-session student@example.com"}})
    result = checkout._checkout_error(response)
    assert "已有付费订阅" in result
    assert "secret-session" not in result and "student@example.com" not in result


def test_schema_error_only_displays_known_field_names():
    response = httpx.Response(422, json={"detail": [{"loc": ["body", "billing_details", "currency"], "msg": "secret-session", "input": "secret-session"}]})
    result = checkout._checkout_error(response)
    assert "billing_details.currency" in result
    assert "secret-session" not in result


def test_unknown_error_is_not_mislabeled_as_subscription_problem():
    result = checkout._checkout_error(httpx.Response(400, json={"error": "unrecognized secret-session"}))
    assert "尚无法识别" in result and "secret-session" not in result
