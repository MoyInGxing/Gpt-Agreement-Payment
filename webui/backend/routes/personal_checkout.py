"""Create normal hosted checkout for an existing personal account.

Uses the original project's checkout endpoint and hosted URL response schema.
No registration, promotions, payment-method submission or payment confirmation.
"""
import re
import threading
import time
from typing import Literal
from urllib.parse import urlsplit
from fastapi import APIRouter, HTTPException, Response
from pydantic import Field
from ..auth import CurrentUser
from . import personal
from . import personal_payment

router = APIRouter(prefix="/api/personal", tags=["personal"])
CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
_lock = threading.Lock()
_recent: dict[tuple, tuple[float, dict]] = {}
_TTL = 600


def _checkout_error(response) -> str:
    """Classify upstream errors without echoing response bodies or credentials."""
    base = f"创建结账会话失败（官方 HTTP {response.status_code}）。"
    try:
        data = response.json()
    except ValueError:
        return base + "官方未返回结构化错误；无法判断具体原因，请在官方页面核对。"
    if not isinstance(data, dict):
        return base + "错误格式无法识别，请在官方页面核对。"
    # Use upstream text only for classification, never include it verbatim.
    text = str(data).lower()
    for needles, message in (
        (("already paid", "already_paid", "already_subscribed", "already subscribed"), "官方判定账号已有付费订阅，请通过官方设置管理或变更套餐。"),
        (("token_invalidated", "token_expired", "invalid_token"), "登录令牌已失效，请更新会话后重新验证账号。"),
        (("unsupported_country", "country_not_supported"), "官方不支持所选账单地区，请核对真实账单地区及官方可用范围。"),
        (("unsupported_currency", "currency_not_supported"), "官方不支持所选币种，请核对账单地区和官方可用币种。"),
        (("invalid_plan", "plan_not_supported", "plan not found"), "官方不接受所选套餐标识，请在官方页面确认可购买套餐。"),
        (("sentinel", "captcha", "challenge"), "该请求需要额外的官方浏览器验证，请在官方页面完成。"),
    ):
        if any(needle in text for needle in needles):
            return base + message
    # Extract only known field locations from schema-validation responses;
    # omit `input`, `msg` and arbitrary field names, which can echo secrets.
    allowed = {"entry_point", "plan_name", "billing_details", "country", "currency", "cancel_url", "checkout_ui_mode", "promo_campaign", "price_interval"}
    fields = []
    details = data.get("detail")
    if isinstance(details, list):
        for error in details:
            loc = error.get("loc") if isinstance(error, dict) else None
            if isinstance(loc, list):
                path = ".".join(part for part in loc if isinstance(part, str) and part in allowed)
                if path and path not in fields:
                    fields.append(path)
    if fields:
        return base + "官方参数校验未通过，涉及字段：" + "、".join(fields) + "。未自动更换参数或重试。"
    return base + "官方错误原因尚无法识别，可能是请求格式与当前接口不一致；请在官方页面核对，程序不会自动重试。"


class CheckoutRequest(personal.CheckRequest):
    plan: Literal["plus", "pro"] = "plus"
    billing_country: str = Field(pattern="^[A-Z]{2}$")
    billing_currency: str = Field(pattern="^[A-Z]{3}$")
    payment_proxy: personal.SecretStr | None = None
    confirmed_email: str = Field(min_length=3, max_length=254)


def _hosted_url(data: dict) -> str:
    # Retain the provider URL, as _select_fresh_checkout_url does for hosted
    # mode. Never follow redirects or expose a URL to an unrelated origin.
    raw = data.get("checkout_url") or data.get("url") or data.get("openai_checkout_url")
    if not raw:
        sid = data.get("checkout_session_id") or data.get("session_id")
        entity = data.get("processor_entity")
        if isinstance(sid, str) and re.fullmatch(r"cs_(?:live|test)_[A-Za-z0-9]+", sid) and entity in ("openai_llc", "openai_ie"):
            raw = f"https://chatgpt.com/checkout/{entity}/{sid}"
    if not isinstance(raw, str) or any(c.isspace() or c in "\\" for c in raw):
        raise HTTPException(502, "官方服务未返回可识别的结账链接，请在官方页面购买。")
    try:
        parsed = urlsplit(raw)
        if (parsed.scheme != "https" or parsed.hostname not in ("chatgpt.com", "checkout.stripe.com")
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or not parsed.path.startswith(("/checkout/", "/c/", "/pay/"))):
            raise ValueError()
    except ValueError:
        raise HTTPException(502, "结账链接来源无法确认，请在官方页面购买。") from None
    return raw


@router.post("/checkout")
def checkout(req: CheckoutRequest, response: Response, user: str = CurrentUser):
    response.headers["Cache-Control"] = "no-store"
    token, supplied_access = personal._credentials(req.session_token.get_secret_value())
    account_proxy = personal._proxy_value(req.proxy_url)
    payment_proxy = personal_payment.stage_proxy(personal_payment._config(), "checkout", None) if req.payment_proxy is None else personal._proxy_value(req.payment_proxy)
    try:
        with personal._client(account_proxy) as auth_client:
            session, identity, access_token, source, note = personal._authenticate(auth_client, token, supplied_access)
            email = str(identity["email"])
            if email.casefold() != req.confirmed_email.strip().casefold():
                raise HTTPException(409, "账号已变化，请重新验证并核对账号后再创建结账会话。")
            # Read live subscription, never trust pasted JSON or JWT metadata.
            accounts = personal._json(auth_client.get(personal.ACCOUNT_URL, headers={"Authorization": f"Bearer {access_token}"})).get("accounts")
            default = accounts.get("default") if isinstance(accounts, dict) else None
            entitlement = default.get("entitlement") if isinstance(default, dict) else None
            if not isinstance(entitlement, dict) or not isinstance(entitlement.get("has_active_subscription"), bool):
                raise HTTPException(409, "无法确认现有订阅状态，未创建结账会话。请在官方设置中确认。")
            if entitlement["has_active_subscription"]:
                raise HTTPException(409, "账号已有生效订阅，未创建重复购买订单。请通过官方设置管理或变更套餐。")
        key = (user, email.casefold(), req.plan, req.billing_country, req.billing_currency)
        # One in-flight create, cached for ten minutes. No automatic POST retry.
        with _lock:
            now = time.monotonic()
            for old_key in list(_recent):
                if now - _recent[old_key][0] >= _TTL:
                    del _recent[old_key]
            if key in _recent:
                return {**_recent[key][1], "reused": True}
            payload = {
                "entry_point": "all_plans_pricing_modal",
                "plan_name": f"chatgpt{req.plan}plan",
                "billing_details": {"country": req.billing_country, "currency": req.billing_currency},
                "cancel_url": "https://chatgpt.com/#pricing",
                "checkout_ui_mode": "hosted",
            }
            with personal._client(payment_proxy) as pay_client:
                upstream = pay_client.post(CHECKOUT_URL, json=payload, headers={
                    "Authorization": f"Bearer {access_token}", "Content-Type": "application/json",
                    "Origin": "https://chatgpt.com", "Referer": "https://chatgpt.com/",
                })
                if upstream.status_code in (400, 422):
                    raise HTTPException(400, _checkout_error(upstream))
                data = personal._json(upstream)
            result = {"email": email, "plan": req.plan, "billing_country": req.billing_country,
                      "billing_currency": req.billing_currency, "checkout_url": _hosted_url(data),
                      "status": "awaiting_user_payment", "reused": False,
                      "message": "结账会话已创建，尚未付款。金额、可用支付方式和续费规则以官方结账页为准。"}
            result["order_id"] = personal_payment.register_order(result, data, user)
            _recent[key] = (now, result)
            return result
    except personal.NETWORK_ERRORS:
        raise HTTPException(502, "连接失败，无法确认结账会话是否创建。请先检查官方订单状态，程序不会自动重试创建。") from None
