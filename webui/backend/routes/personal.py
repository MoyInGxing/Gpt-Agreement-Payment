"""Personal subscription: read-only account checks and official checkout handoff.

Credentials exist only for the duration of a request. This route never invokes
the registration/payment runners, persists credentials, or submits a purchase.
"""
import json
import re
import os
import tempfile
import threading
from urllib.parse import urlsplit
import httpx
from curl_cffi import requests as curl_requests
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, SecretStr
from ..auth import CurrentUser
from .. import settings

router = APIRouter(prefix="/api/personal", tags=["personal"])
SESSION_URL = "https://chatgpt.com/api/auth/session"
ME_URL = "https://chatgpt.com/backend-api/me"
ACCOUNT_URL = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
NETWORK_ERRORS = (httpx.HTTPError, curl_requests.RequestsError, ValueError)
_CONFIG_LOCK = threading.Lock()
_PAYMENT_STAGES = (
    "fingerprint", "fetch_publishable_key", "stripe_init", "telemetry_init",
    "elements", "link_lookup", "address", "telemetry_address",
    "telemetry_card_input", "payment_method", "telemetry_confirm", "confirm",
    "telemetry_poll", "poll",
)


def _client(proxy: str):
    """Use the same transport profile as the original account/payment modules."""
    return curl_requests.Session(
        impersonate="chrome136", proxy=proxy or None, timeout=20,
        allow_redirects=False, trust_env=False,
        headers={"Accept": "application/json", "Referer": "https://chatgpt.com/"},
    )


class CheckRequest(BaseModel):
    session_token: SecretStr
    proxy_url: SecretStr = SecretStr("")


class PaymentProxyRequest(BaseModel):
    payment_proxy: SecretStr = SecretStr("")
    account_proxy: SecretStr = SecretStr("")


def _proxy_value(value: SecretStr) -> str:
    raw = value.get_secret_value().strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        port = parsed.port
        if (parsed.scheme not in ("http", "https", "socks5", "socks5h")
                or not parsed.hostname or not port or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment or any(c.isspace() for c in raw)):
            raise ValueError()
        if parsed.hostname in ("localhost", "127.0.0.1", "::1") and os.path.exists("/.dockerenv"):
            raise HTTPException(400, "容器内不能通过 localhost 访问本机代理，请改用 host.docker.internal。")
    except ValueError:
        raise HTTPException(400, "代理地址无效，请填写带端口的 HTTP(S) 或 SOCKS5 地址。") from None
    return raw


@router.put("/payment-proxy")
def save_payment_proxy(req: PaymentProxyRequest, user: str = CurrentUser):
    payment = _proxy_value(req.payment_proxy)
    account = _proxy_value(req.account_proxy)
    path = settings.PAY_CONFIG_PATH
    with _CONFIG_LOCK:
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(cfg, dict) or not isinstance(cfg.get("fresh_checkout", {}), dict) or not isinstance(cfg.get("stage_proxies", {}), dict):
                raise ValueError()
        except (OSError, ValueError):
            raise HTTPException(409, "支付配置缺失或格式异常，未覆盖原配置。") from None
        cfg["proxy"] = payment
        cfg.setdefault("fresh_checkout", {})["proxy"] = account
        # Explicit stage overrides otherwise take precedence over cfg.proxy.
        stages = cfg.setdefault("stage_proxies", {})
        for stage in set(_PAYMENT_STAGES) | set(stages):
            stages[stage] = payment
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
                temporary = f.name
                json.dump(cfg, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(temporary, path)
            temporary = None
        except OSError:
            raise HTTPException(500, "支付代理配置保存失败，请检查目录权限。") from None
        finally:
            if temporary is not None:
                os.unlink(temporary)
    return {"saved": True, "payment_proxy_enabled": bool(payment), "account_proxy_enabled": bool(account)}


def _extract_export_credentials(raw: str) -> dict:
    """Recover intact credential strings when translated metadata breaks JSON.

    Only exact credential field names and JSON-encoded string values qualify.
    Reject duplicate aliases, malformed values and truncated objects; never
    guess or modify the credential itself.
    """
    if not raw.startswith("{") or not raw.endswith("}"):
        raise HTTPException(400, "会话内容被截断，请复制完整内容。")
    recovered = {}
    for canonical, aliases in (
        ("sessionToken", ("sessionToken", "session_token")),
        ("accessToken", ("accessToken", "access_token")),
    ):
        keys = "|".join(aliases)
        fields = list(re.finditer(r'"(?:' + keys + r')"\s*:', raw))
        if len(fields) > 1:
            raise HTTPException(400, "发现重复凭证字段，请只粘贴一个账号的会话数据。")
        if not fields:
            continue
        # Match a complete JSON string, then require a field separator. An
        # incomplete/edited token must not be accepted as a shorter value.
        value = re.match(r'\s*("(?:[^"\\\x00-\x1f]|\\[^\r\n])*")\s*(?=[,}])', raw[fields[0].end():])
        if not value:
            raise HTTPException(400, "凭证字段不完整，请重新复制，保留原始字段名和凭证值。")
        try:
            recovered[canonical] = json.loads(value.group(1))
        except ValueError:
            raise HTTPException(400, "凭证字段格式无效，请重新复制原始内容。") from None
    if not recovered:
        raise HTTPException(400, "未找到完整的 sessionToken 或 accessToken 字段，请复制原始会话数据。")
    return recovered


def _credentials(raw: str) -> tuple[str, str]:
    """Accept a raw session value or a session JSON export; ignore metadata."""
    raw = raw.strip()
    if len(raw) > 128000:
        raise HTTPException(400, "会话数据过大，请只粘贴会话 JSON 或 Session 值。")
    session, access = raw, ""
    if raw.startswith(("{", "[", '"')):
        try:
            data = json.loads(raw)
            if isinstance(data, str):
                data = json.loads(data)
        except (ValueError, RecursionError):
            data = _extract_export_credentials(raw)
        if not isinstance(data, dict):
            raise HTTPException(400, "会话 JSON 必须是包含 sessionToken 或 accessToken 的对象。")
        session = data.get("sessionToken") or data.get("session_token") or ""
        access = data.get("accessToken") or data.get("access_token") or ""
    for value in (session, access):
        if isinstance(value, str) and (not value.isascii() or any(c.isspace() for c in value)):
            raise HTTPException(400, "凭证值含非 ASCII 字符或空格，可能已被翻译或改写。请关闭网页翻译，重新复制原始会话数据，不要手动修补凭证。")
        if not isinstance(value, str) or len(value) > 16000 or ";" in value:
            raise HTTPException(400, "凭证格式无效，请填写完整会话 JSON 或 Session Cookie 的值。")
    if not session and not access:
        raise HTTPException(400, "未找到 sessionToken 或 accessToken，请检查粘贴内容。")
    return session, access


def _json(response) -> dict:
    if response.status_code == 401:
        raise HTTPException(400, "登录凭证已失效或被撤销，请重新登录后更新。")
    if response.status_code == 403:
        raise HTTPException(502, "官方服务拒绝访问（HTTP 403），可能涉及浏览器验证或访问限制；无法仅凭此响应判断账号是否失效。请在官方页面检查。")
    if response.status_code != 200:
        raise HTTPException(502, f"官方服务返回 HTTP {response.status_code}，请稍后重试。")
    try:
        data = response.json()
    except ValueError:
        raise HTTPException(502, "官方服务未返回账号数据，请在浏览器检查登录状态。") from None
    if not isinstance(data, dict):
        raise HTTPException(502, "账号数据格式无法识别。")
    return data


def _authenticate(client, token: str, supplied_access: str):
    session = {}
    identity = {}
    access_token = ""
    source = "session"
    auth_note = ""
    if token:
        try:
            session = _json(client.get(SESSION_URL, headers={
                "Cookie": f"__Secure-next-auth.session-token={token}",
                "Accept": "application/json",
            }))
            identity = session.get("user") or {}
            access_token = session.get("accessToken")
            if not isinstance(identity, dict) or not identity.get("email") or not isinstance(access_token, str) or not access_token:
                raise HTTPException(400, "Session 未返回有效账号，请重新登录后更新。")
        except (HTTPException, *NETWORK_ERRORS):
            if not supplied_access:
                raise
            # Original code preserves an existing AT when refresh
            # fails. Verify it independently instead of trusting the
            # export's identity, or mixing identities from both paths.
            access_token = ""
            auth_note = "Session 刷新未完成，已改用提供的 Access Token 独立验证账号。"
    if not access_token:
        session = {}
        source = "access_token"
        access_token = supplied_access
        identity = _json(client.get(ME_URL, headers={
            "Authorization": f"Bearer {access_token}", "Accept": "application/json",
        }))
    if not isinstance(identity, dict) or not identity.get("email") or not isinstance(access_token, str) or not access_token:
        raise HTTPException(400, "Session 未返回有效账号，请重新登录后更新。")
    return session, identity, access_token, source, auth_note


@router.post("/check")
def check(req: CheckRequest, response: Response, user: str = CurrentUser):
    response.headers["Cache-Control"] = "no-store"
    token, supplied_access = _credentials(req.session_token.get_secret_value())
    proxy = req.proxy_url.get_secret_value().strip()
    if proxy and not proxy.startswith(("http://", "https://", "socks5://", "socks5h://")):
        raise HTTPException(400, "代理地址需以 http://、https:// 或 socks5:// 开头。")
    try:
        with _client(proxy) as client:
            session, identity, access_token, source, auth_note = _authenticate(client, token, supplied_access)
            result = {
                "email": identity["email"],
                "expires": session.get("expires"),
                "subscription": None,
                "subscription_message": "",
                "checkout_url": "https://chatgpt.com/#pricing",
                "credential_source": source,
                "auth_note": auth_note,
            }
            try:
                account_data = _json(client.get(ACCOUNT_URL, headers={
                    "Authorization": f"Bearer {access_token}", "Accept": "application/json",
                }))
                accounts = account_data.get("accounts") or {}
                account = accounts.get("default") if isinstance(accounts, dict) else None
                entitlement = account.get("entitlement") if isinstance(account, dict) else None
                if not isinstance(entitlement, dict) or not isinstance(entitlement.get("has_active_subscription"), bool):
                    result["subscription_message"] = "账号验证成功，但无法识别个人订阅状态；请在官方设置中确认。"
                else:
                    result["subscription"] = {
                        "active": entitlement["has_active_subscription"] is True,
                        "plan": entitlement.get("subscription_plan") or "未返回套餐名称",
                    }
            except (HTTPException, *NETWORK_ERRORS):
                result["subscription_message"] = "账号验证成功，订阅查询暂不可用；请在官方设置中确认。"
            return result
    except NETWORK_ERRORS:
        # Never expose exception text: it may contain proxy credentials or URLs.
        raise HTTPException(502, "无法连接官方服务，请检查容器网络和代理配置。") from None
