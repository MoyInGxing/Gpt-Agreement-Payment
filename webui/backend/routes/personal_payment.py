"""Personal card flow adapted from card/_monolith.py's shared-PM path.

No runner import, synthetic telemetry, identity generation, captcha solving,
challenge confirmation or automatic payment retry. Raw provider data stays local.
"""
import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field, SecretStr
from .. import settings
from ..auth import CurrentUser
from . import personal

router = APIRouter(prefix="/api/personal", tags=["personal"])
STRIPE_API = "https://api.stripe.com/v1"
STRIPE_VERSION = "2025-03-31.basil"
_lock = threading.RLock()
_orders: dict[str, dict] = {}
_quotes: dict[str, dict] = {}
_TTL = 600
_HEADERS = {"Origin": "https://js.stripe.com", "Referer": "https://js.stripe.com/"}


def register_order(result: dict, upstream: dict, user: str) -> str:
    sid = upstream.get("checkout_session_id") or upstream.get("session_id")
    if not isinstance(sid, str) or not re.fullmatch(r"cs_(?:live|test)_[A-Za-z0-9]+", sid):
        match = re.search(r"/(cs_(?:live|test)_[A-Za-z0-9]+)(?:[/?#]|$)", result["checkout_url"])
        sid = match.group(1) if match else ""
    order_id = secrets.token_urlsafe(24)
    with _lock:
        for key in list(_orders):
            if time.monotonic() - _orders[key]["created"] > 3600:
                del _orders[key]
        _orders[order_id] = {**result, "sid": sid, "owner": user,
                             "pk": upstream.get("publishable_key") or upstream.get("stripe_publishable_key"),
                             "created": time.monotonic()}
    return order_id


def _config() -> dict:
    with personal._CONFIG_LOCK:
        try:
            cfg = json.loads(settings.PAY_CONFIG_PATH.read_text(encoding="utf-8"))
            if not isinstance(cfg, dict):
                raise ValueError()
            return cfg
        except (OSError, ValueError):
            raise HTTPException(409, "支付配置缺失或格式异常，请检查 config.paypal.json。") from None


def _proxy(raw) -> str:
    # Preserve the original string/dict proxy schema, without logging credentials.
    if isinstance(raw, dict):
        host, port = raw.get("host"), raw.get("port")
        scheme = raw.get("scheme") or raw.get("type") or "http"
        if not isinstance(host, str) or not isinstance(port, (str, int)):
            raise HTTPException(400, "配置中的代理主机或端口无效。")
        auth = ""
        username = raw.get("username") or raw.get("user")
        if username:
            auth = quote(str(username), safe="") + ":" + quote(str(raw.get("password", raw.get("pass", ""))), safe="") + "@"
        raw = f"{scheme}://{auth}{host}:{port}"
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise HTTPException(400, "配置中的代理格式无效。")
    return personal._proxy_value(SecretStr(raw))


def stage_proxy(cfg: dict, stage: str, override: SecretStr | None) -> str:
    if override is not None:
        return _proxy(override.get_secret_value())
    stages = cfg.get("stage_proxies") or {}
    if not isinstance(stages, dict):
        raise HTTPException(400, "stage_proxies 必须是对象。")
    return _proxy(stages.get(stage, cfg.get("proxy", "")))


def _card(cfg: dict, index: int) -> dict:
    cards = cfg.get("cards")
    if not isinstance(cards, list) or index >= len(cards) or not isinstance(cards[index], dict):
        raise HTTPException(400, "所选卡片不存在，请在 cards 配置中填写自己的卡片和真实账单。")
    card = cards[index]
    addr = card.get("address")
    if not isinstance(addr, dict) or not all(isinstance(card.get(k), str) and card[k].strip() for k in ("name", "email")):
        raise HTTPException(400, "卡片必须配置真实 name、email 和 address；程序不会生成身份。")
    for key, pattern in (("number", r"[0-9]{12,19}"), ("cvc", r"[0-9]{3,4}"),
                         ("exp_month", r"(?:0?[1-9]|1[0-2])"), ("exp_year", r"20[0-9]{2}")):
        if not re.fullmatch(pattern, str(card.get(key, ""))):
            raise HTTPException(400, f"卡片配置字段 {key} 无效。")
    if not re.fullmatch(r"[A-Z]{2}", str(addr.get("country", ""))) or not all(addr.get(k) for k in ("line1", "city", "postal_code")):
        raise HTTPException(400, "请配置真实账单 country、line1、city、postal_code。")
    return card


def _digest(card: dict) -> str:
    return hashlib.sha256(json.dumps(card, sort_keys=True).encode()).hexdigest()


def _order(order_id: str, user: str) -> dict:
    order = _orders.get(order_id)
    if not order or order["owner"] != user or time.monotonic() - order["created"] > 3600:
        raise HTTPException(404, "本次账号订单已失效或不存在，请通过官方结账页继续。")
    if not order["sid"]:
        raise HTTPException(409, "订单没有可识别的收单会话标识，请通过官方结账页继续。")
    return order


def _key(order: dict, cfg: dict) -> str:
    options = cfg.get("personal_payment") or {}
    pk = order.get("pk") or (options.get("publishable_key") if isinstance(options, dict) else None)
    if not isinstance(pk, str) or not re.fullmatch(r"pk_(?:live|test)_[A-Za-z0-9]+", pk):
        raise HTTPException(409, "订单未返回收单公钥。请在 personal_payment.publishable_key 配置该订单官方页面使用的公钥；程序不会猜测或探测商户公钥。")
    if ("_test_" in order["sid"]) != ("_test_" in pk):
        raise HTTPException(409, "订单与收单公钥的测试／正式模式不一致。")
    return pk


def _request(cfg: dict, stage: str, override, method: str, path: str, **kwargs) -> dict:
    with personal._client(stage_proxy(cfg, stage, override)) as client:
        response = getattr(client, method)(STRIPE_API + path, headers=_HEADERS, **kwargs)
    try:
        data = response.json()
    except ValueError:
        raise HTTPException(502, "收单服务未返回可识别结果，请在官方订单中确认。") from None
    if not isinstance(data, dict):
        raise HTTPException(502, "收单响应格式无法识别。")
    if response.status_code != 200:
        # Do not expose raw error bodies, which may contain card data/secrets.
        challenge = _needs_action(data) or any(word in str(data).lower() for word in ("captcha", "challenge", "authentication_required"))
        if challenge:
            raise HTTPException(409, "收单服务要求本人验证，请打开此订单的官方结账页继续。")
        raise HTTPException(502, f"收单步骤 {stage} 失败（HTTP {response.status_code}），请在官方结账页检查；程序不会自动重试付款。")
    return data


def _needs_action(data: dict) -> bool:
    for obj in (data, data.get("payment_intent"), data.get("setup_intent"),
                (data.get("payment_method_object") or {}).get("setup_intent") if isinstance(data.get("payment_method_object"), dict) else None):
        if isinstance(obj, dict) and (obj.get("next_action") or obj.get("status") == "requires_action"
                                     or obj.get("payment_object_status") == "requires_action"):
            return True
    return False


def _pricing(data: dict) -> tuple[int, str]:
    summary, invoice = data.get("total_summary"), data.get("invoice")
    due = summary.get("due") if isinstance(summary, dict) else None
    if due is None and isinstance(invoice, dict):
        due = invoice.get("amount_due")
    currency = data.get("currency")
    if type(due) is not int or due < 0 or not isinstance(currency, str) or not re.fullmatch(r"[a-zA-Z]{3}", currency):
        raise HTTPException(409, "收单服务未明确返回应付金额和币种，未提交付款。")
    return due, currency.upper()


def _init(order: dict, cfg: dict, override, pk: str) -> dict:
    return _request(cfg, "stripe_init", override, "post", f"/payment_pages/{order['sid']}/init",
                    data={"key": pk, "_stripe_version": STRIPE_VERSION})


def _address(order: dict, cfg: dict, override, pk: str, card: dict) -> dict:
    fields = {"key": pk, "_stripe_version": STRIPE_VERSION}
    fields.update({f"tax_region[{key}]": str(value) for key, value in card["address"].items()
                   if key in ("country", "line1", "line2", "city", "state", "postal_code")})
    return _request(cfg, "address", override, "post", f"/payment_pages/{order['sid']}", data=fields)


@contextmanager
def _db():
    directory = settings.get_data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(directory / "personal-payments.sqlite3")
    try:
        with connection:
            connection.execute("CREATE TABLE IF NOT EXISTS attempts (sid TEXT PRIMARY KEY, owner TEXT NOT NULL, state TEXT NOT NULL)")
            yield connection
    finally:
        connection.close()


def _attempt(sid: str) -> bool:
    with _db() as db:
        return db.execute("SELECT 1 FROM attempts WHERE sid = ?", (sid,)).fetchone() is not None


class PrepareRequest(BaseModel):
    order_id: str = Field(min_length=16, max_length=128)
    card_index: int = Field(default=0, ge=0, le=100)
    # null: use original config; empty string: explicitly direct.
    payment_proxy: SecretStr | None = None


class ConfirmRequest(BaseModel):
    quote_id: str = Field(min_length=16, max_length=128)
    accepted_terms: bool = False


@router.get("/payment/config")
def config_summary(response: Response, user: str = CurrentUser):
    response.headers["Cache-Control"] = "no-store"
    cfg = _config()
    cards = cfg.get("cards") or []
    if not isinstance(cards, list):
        raise HTTPException(409, "cards 配置格式异常。")
    masked = []
    for index, card in enumerate(cards):
        if not isinstance(card, dict):
            continue
        addr = card.get("address") or {}
        number = str(card.get("number", ""))
        country = addr.get("country", "") if isinstance(addr, dict) else ""
        masked.append({"index": index, "last4": number[-4:] if re.fullmatch(r"[0-9]{12,19}", number) else "",
                       "country": country if isinstance(country, str) and re.fullmatch(r"[A-Z]{2}", country) else ""})
    options = cfg.get("personal_payment") or {}
    currency = options.get("currency", "") if isinstance(options, dict) else ""
    return {"cards": masked, "currency": currency if isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency) else "",
            "publishable_key_configured": bool(options.get("publishable_key")) if isinstance(options, dict) else False}


@router.post("/payment/prepare")
def prepare(req: PrepareRequest, response: Response, user: str = CurrentUser):
    response.headers["Cache-Control"] = "no-store"
    with _lock:
        order = _order(req.order_id, user)
        if _attempt(order["sid"]):
            raise HTTPException(409, "此订单已经提交过付款，先查询结果或到官方订单核对，不能再次提交。")
        cfg = _config()
        card = _card(cfg, req.card_index)
        if card["address"]["country"] != order["billing_country"]:
            raise HTTPException(409, "卡片账单国家与订单国家不一致，请核对真实账单后创建订单。")
        pk = _key(order, cfg)
        try:
            data = _init(order, cfg, req.payment_proxy, pk)
            if _needs_action(data):
                raise HTTPException(409, "订单需要本人验证，请通过官方结账页继续。")
            updated = _address(order, cfg, req.payment_proxy, pk, card)
            if _needs_action(updated):
                raise HTTPException(409, "账单提交后需要本人验证，请通过官方结账页继续。")
            # Re-read totals after the complete tax address update.
            data = _init(order, cfg, req.payment_proxy, pk)
            if _needs_action(data):
                raise HTTPException(409, "收单报价需要本人验证，请通过官方结账页继续。")
            amount, currency = _pricing(data)
        except personal.NETWORK_ERRORS:
            raise HTTPException(502, "读取收单报价失败，未提交付款。") from None
        if currency != order["billing_currency"]:
            raise HTTPException(409, "收单币种与订单币种不一致，未提交付款。")
        quote_id = secrets.token_urlsafe(24)
        for key in list(_quotes):
            if time.monotonic() - _quotes[key]["created"] > _TTL:
                del _quotes[key]
        _quotes[quote_id] = {"order_id": req.order_id, "owner": user, "created": time.monotonic(),
                             "card_index": req.card_index, "card_digest": _digest(card),
                             "proxy": req.payment_proxy, "amount": amount, "currency": currency}
        return {"quote_id": quote_id, "amount_minor": amount, "currency": currency,
                "card_last4": str(card["number"])[-4:], "billing_name": card["name"],
                "email": order["email"], "message": "账单已提交并读取应付金额，尚未扣款。请核对官方结账页的套餐和续费规则。"}


def _state(data: dict) -> dict:
    if _needs_action(data):
        return {"status": "requires_action", "message": "付款需要本人完成验证码、3DS 或银行授权。请打开此订单的官方结账页完成，再查询结果。"}
    intent = data.get("payment_intent")
    if data.get("state") == "succeeded" or (isinstance(intent, dict) and intent.get("status") == "succeeded"):
        return {"status": "succeeded", "message": "收单服务报告付款成功，请重新查询账号订阅并核对官方账单。"}
    if data.get("state") in ("failed", "expired", "canceled") or data.get("payment_object_status") in ("canceled", "requires_payment_method"):
        return {"status": "failed", "message": "收单服务返回失败或需要更换支付方式，请在官方结账页检查。"}
    # SetupIntent.succeeded only saves a method; it is not a successful charge.
    return {"status": "pending", "message": "付款结果尚未确认，请查询结果或到官方订单核对。不要重复提交。"}


@router.post("/payment/confirm")
def confirm(req: ConfirmRequest, response: Response, user: str = CurrentUser):
    response.headers["Cache-Control"] = "no-store"
    if not req.accepted_terms:
        raise HTTPException(400, "请先核对金额、套餐及续费规则，并明确同意付款。")
    with _lock:
        item = _quotes.get(req.quote_id)
        if not item or item["owner"] != user or time.monotonic() - item["created"] > _TTL:
            raise HTTPException(409, "报价已过期，请重新读取账单金额。")
        order = _order(item["order_id"], user)
        if _attempt(order["sid"]):
            raise HTTPException(409, "此订单已提交过付款，请查询结果，不会重复扣款。")
        cfg, override = _config(), item["proxy"]
        card = _card(cfg, item["card_index"])
        if _digest(card) != item["card_digest"]:
            raise HTTPException(409, "卡片或账单配置已改变，请重新读取账单金额。")
        pk = _key(order, cfg)
        try:
            data = _init(order, cfg, override, pk)
            if _needs_action(data):
                return {**_state(data), "checkout_url": order["checkout_url"]}
            if _pricing(data) != (item["amount"], item["currency"]):
                raise HTTPException(409, "官方金额或币种已变化，请重新读取并核对，未提交付款。")
            checksum = data.get("init_checksum")
            if not isinstance(checksum, str) or not checksum:
                raise HTTPException(409, "缺少官方订单校验值，请通过官方结账页继续。")
            # Persist before the first irreversible submission. No card/credentials stored.
            with _db() as db:
                try:
                    db.execute("INSERT INTO attempts VALUES (?, ?, 'submitted')", (order["sid"], user))
                except sqlite3.IntegrityError:
                    raise HTTPException(409, "此订单已提交过付款，请查询结果。") from None
            fields = {"type": "card", "key": pk, "_stripe_version": STRIPE_VERSION,
                      "billing_details[name]": card["name"], "billing_details[email]": card["email"]}
            fields.update({f"card[{key}]": str(card[key]) for key in ("number", "cvc", "exp_month", "exp_year")})
            fields.update({f"billing_details[address][{key}]": str(value) for key, value in card["address"].items()
                           if key in ("country", "line1", "line2", "city", "state", "postal_code")})
            pm = _request(cfg, "payment_method", override, "post", "/payment_methods", data=fields)
            if _needs_action(pm):
                result = _state(pm)
            else:
                pm_id = pm.get("id")
                if not isinstance(pm_id, str) or not re.fullmatch(r"pm_[A-Za-z0-9]+", pm_id):
                    raise HTTPException(502, "收单服务没有返回支付方式标识，请通过官方结账页核对。")
                data = _request(cfg, "confirm", override, "post", f"/payment_pages/{order['sid']}/confirm", data={
                    "key": pk, "_stripe_version": STRIPE_VERSION, "payment_method": pm_id,
                    "expected_amount": str(item["amount"]), "expected_payment_method_type": "card",
                    "init_checksum": checksum, "return_url": order["checkout_url"],
                    "consent[terms_of_service]": "accepted",
                })
                result = _state(data)
            return {**result, "checkout_url": order["checkout_url"]}
        except personal.NETWORK_ERRORS:
            raise HTTPException(502, "网络中断，付款结果未知。请查询官方订单，不会自动重试付款。") from None


@router.post("/payment/status")
def status(req: PrepareRequest, response: Response, user: str = CurrentUser):
    response.headers["Cache-Control"] = "no-store"
    with _lock:
        order = _order(req.order_id, user)
        cfg = _config()
        pk = _key(order, cfg)
        try:
            data = _request(cfg, "poll", req.payment_proxy, "get", f"/payment_pages/{order['sid']}/poll",
                            params={"key": pk, "_stripe_version": STRIPE_VERSION})
        except personal.NETWORK_ERRORS:
            raise HTTPException(502, "查询收单状态失败，请在官方订单核对。") from None
        return {**_state(data), "checkout_url": order["checkout_url"]}
