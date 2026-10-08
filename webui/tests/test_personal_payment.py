import json
from urllib.parse import parse_qs
import httpx
import pytest
from webui.backend.routes import personal, personal_payment as payment
from webui.tests.test_personal import login, mock_upstream


@pytest.fixture
def flow(client, monkeypatch, tmp_path):
    monkeypatch.setattr(payment, "_discover_key", lambda *args: "")
    payment._orders.clear()
    payment._quotes.clear()
    login(client)
    cfg = {
        "proxy": "http://default-proxy:8080",
        "stage_proxies": {"confirm": "http://confirm-proxy:8081"},
        "randomize_identity": True, "pre_solve_passive_captcha": True,
        "cards": [{"number": "4242424242424242", "cvc": "123", "exp_month": "12", "exp_year": "2030",
                   "name": "Test Student", "email": "billing@example.com",
                   "address": {"country": "US", "line1": "1 Example Street", "city": "San Francisco",
                               "state": "CA", "postal_code": "94105"}}],
        "personal_payment": {"publishable_key": "pk_test_fixture", "currency": "USD"},
    }
    path = tmp_path / "payment.json"
    path.write_text(json.dumps(cfg))
    monkeypatch.setattr(payment.settings, "PAY_CONFIG_PATH", path)
    order = {"email": "student@example.com", "plan": "plus", "billing_country": "US", "billing_currency": "USD",
             "checkout_url": "https://checkout.stripe.com/c/pay/cs_test_fixture"}
    order_id = payment.register_order(order, {"checkout_session_id": "cs_test_fixture"}, "admin")
    calls, proxies = [], []
    options = {"amount": 2000, "currency": "usd", "challenge": None, "final": {"state": "succeeded"}, "pm_timeout": False}
    def handler(req):
        calls.append(req)
        path = req.url.path
        if path.endswith("/init"):
            return httpx.Response(200, json={"total_summary": {"due": options["amount"]}, "currency": options["currency"],
                "init_checksum": "official-checksum", **({"next_action": {"type": "use_stripe_sdk"}} if options["challenge"] == "init" else {})})
        if path.endswith("/payment_methods"):
            if options["pm_timeout"]:
                raise httpx.ReadTimeout("fake timeout", request=req)
            if options["challenge"] == "pm":
                return httpx.Response(400, json={"error": {"message": "captcha required card 4242424242424242"}})
            return httpx.Response(200, json={"id": "pm_fixture"})
        if path.endswith("/confirm") or path.endswith("/poll"):
            return httpx.Response(200, json=options["final"])
        return httpx.Response(200, json={})
    mock_upstream(monkeypatch, handler)
    factory = personal._client
    def capture(proxy):
        proxies.append(proxy)
        return factory(proxy)
    monkeypatch.setattr(personal, "_client", capture)
    yield {"order_id": order_id, "calls": calls, "proxies": proxies, "options": options, "config": cfg, "path": path}
    payment._orders.clear()
    payment._quotes.clear()


def prepare(client, flow):
    return client.post("/api/personal/payment/prepare", json={"order_id": flow["order_id"]})


def confirm(client, quote):
    return client.post("/api/personal/payment/confirm", json={"quote_id": quote["quote_id"], "accepted_terms": True})


def test_end_to_end_config_card_real_billing_and_stage_proxies(client, flow):
    summary = client.get("/api/personal/payment/config")
    assert summary.status_code == 200
    assert summary.json()["cards"] == [{"index": 0, "last4": "4242", "country": "US"}]
    assert "4242424242424242" not in summary.text and "123" not in summary.text
    quote = prepare(client, flow)
    assert quote.status_code == 200 and quote.json()["amount_minor"] == 2000
    assert all(not req.url.path.endswith("payment_methods") for req in flow["calls"])
    response = confirm(client, quote.json())
    assert response.status_code == 200 and response.json()["status"] == "succeeded"
    fields = parse_qs(next(req.content.decode() for req in flow["calls"] if req.url.path.endswith("payment_methods")))
    assert fields["card[number]"] == ["4242424242424242"]
    assert fields["billing_details[name]"] == ["Test Student"]
    assert fields["billing_details[address][line1]"] == ["1 Example Street"]
    assert not any(key in fields for key in ("guid", "muid", "sid", "time_on_page", "payment_user_agent", "radar_options[hcaptcha_token]"))
    payload = parse_qs(flow["calls"][-1].content.decode())
    assert payload["expected_amount"] == ["2000"] and payload["payment_method"] == ["pm_fixture"]
    assert "card[number]" not in payload and "passive_captcha_token" not in payload
    assert flow["proxies"][-1] == "http://confirm-proxy:8081"
    assert all(p == "http://default-proxy:8080" for p in flow["proxies"][:-1])
    assert "4242424242424242" not in response.text and "official-checksum" not in response.text
    assert confirm(client, quote.json()).status_code == 409
    assert sum(req.url.path.endswith("/confirm") for req in flow["calls"]) == 1
    assert prepare(client, flow).status_code == 409


def test_local_auth_and_owner_binding(client, flow):
    payment._orders[flow["order_id"]]["owner"] = "another-admin"
    assert prepare(client, flow).status_code == 404
    assert flow["calls"] == []


def test_requires_explicit_payment_consent(client, flow):
    quote = prepare(client, flow).json()
    count = len(flow["calls"])
    assert client.post("/api/personal/payment/confirm", json={"quote_id": quote["quote_id"]}).status_code == 400
    assert len(flow["calls"]) == count


@pytest.mark.parametrize("change", ["amount", "currency", "card"])
def test_changed_quote_or_card_does_not_submit(client, flow, change):
    quote = prepare(client, flow).json()
    if change == "card":
        flow["config"]["cards"][0]["cvc"] = "456"
        flow["path"].write_text(json.dumps(flow["config"]))
    else:
        flow["options"][change] = 2500 if change == "amount" else "eur"
    assert confirm(client, quote).status_code == 409
    assert not any(req.url.path.endswith("/payment_methods") for req in flow["calls"])


def test_init_challenge_stops_before_card_submission(client, flow):
    flow["options"]["challenge"] = "init"
    assert prepare(client, flow).status_code == 409
    assert len(flow["calls"]) == 1


def test_pm_challenge_is_not_solved_or_retried(client, flow):
    quote = prepare(client, flow).json()
    flow["options"]["challenge"] = "pm"
    response = confirm(client, quote)
    assert response.status_code == 409 and "本人验证" in response.text
    assert "4242424242424242" not in response.text
    assert not any(req.url.path.endswith("/confirm") for req in flow["calls"])
    assert confirm(client, quote).status_code == 409


def test_3ds_returns_manual_action_and_poll_can_complete(client, flow):
    quote = prepare(client, flow).json()
    flow["options"]["final"] = {"payment_intent": {"status": "requires_action", "next_action": {"type": "use_stripe_sdk", "secret": "must-not-leak"}}}
    response = confirm(client, quote)
    assert response.status_code == 200 and response.json()["status"] == "requires_action"
    assert "must-not-leak" not in response.text
    flow["options"]["final"] = {"state": "succeeded"}
    response = client.post("/webui/api/personal/payment/status", json={"order_id": flow["order_id"]})
    assert response.json()["status"] == "succeeded"
    assert flow["calls"][-1].method == "GET"


def test_network_uncertainty_durably_blocks_duplicate(client, flow):
    quote = prepare(client, flow).json()
    flow["options"]["pm_timeout"] = True
    assert confirm(client, quote).status_code == 502
    assert confirm(client, quote).status_code == 409
    # Re-registering after process restart still cannot submit the same session.
    original = payment._orders[flow["order_id"]]
    payment._orders.clear()
    flow["order_id"] = payment.register_order(original, {"session_id": "cs_test_fixture"}, "admin")
    assert prepare(client, flow).status_code == 409


def test_setup_intent_success_is_not_charge_success():
    assert payment._state({"setup_intent": {"status": "succeeded"}})["status"] == "pending"
    assert payment._state({"payment_intent": {"status": "requires_payment_method", "last_payment_error": {"code": "card_declined"}}})["status"] == "failed"


def test_missing_key_and_country_mismatch_stop_before_requests(client, flow):
    flow["config"]["personal_payment"].pop("publishable_key")
    flow["path"].write_text(json.dumps(flow["config"]))
    assert prepare(client, flow).status_code == 409 and not flow["calls"]
    flow["config"]["cards"][0]["address"]["country"] = "IE"
    flow["path"].write_text(json.dumps(flow["config"]))
    assert prepare(client, flow).status_code == 409 and not flow["calls"]


def test_discovers_actual_page_key_once_without_guessing(client, flow, monkeypatch):
    flow["config"]["personal_payment"].pop("publishable_key")
    flow["path"].write_text(json.dumps(flow["config"]))
    observations = []
    def discover(order, cfg, override):
        observations.append(order["sid"])
        return "pk_test_frompage"
    monkeypatch.setattr(payment, "_discover_key", discover)
    response = prepare(client, flow)
    assert response.status_code == 200
    assert confirm(client, response.json()).status_code == 200
    assert observations == ["cs_test_fixture"]
    assert "pk_test_frompage" not in response.text


def test_expired_quote_cannot_submit(client, flow):
    quote = prepare(client, flow).json()
    payment._quotes[quote["quote_id"]]["created"] -= 601
    assert confirm(client, quote).status_code == 409
    assert not any(req.url.path.endswith("/payment_methods") for req in flow["calls"])


def test_proxy_override_and_original_dict_schema(client, flow):
    flow["config"]["proxy"] = {"host": "proxy", "port": 8080, "user": "student", "pass": "p@ss"}
    assert payment.stage_proxy(flow["config"], "stripe_init", None) == "http://student:p%40ss@proxy:8080"
    response = client.post("/api/personal/payment/prepare", json={"order_id": flow["order_id"], "payment_proxy": ""})
    assert response.status_code == 200 and flow["proxies"] == ["", "", ""]
