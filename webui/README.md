# webui — 配置向导 + preflight 体检

## 个人订阅入口

个人页面现支持创建已有账号的正常 hosted 结账会话：填写个人套餐（Plus/Pro）、
真实账单国家与币种，核对账号后创建。接口沿用原代码的 payments/checkout 地址和
hosted 链接结构，使用账号代理完成鉴权，使用支付代理发起结账请求。此调用创建会话，
不提交支付方式或扣款；银行卡、PayPal 等可用方式以及金额和续费规则由官方结账页决定。
个人页面另提供独立银行卡收单入口，按原 `card/_monolith.py` 的 shared-payment-method
流程提交完整账单、读取报价、创建支付方式、确认付款，并查询结果。只有点击“提交此订单付款”
并勾选同意后才会提交卡片。此模块不调用原运行器或其挑战处理器。

付款配置沿用 `CTF-pay/config.paypal.json` 的 `cards`、`proxy`、`stage_proxies`。
卡片索引从 0 开始，必须填写自己的卡片、真实姓名、账单邮箱及地址；不会随机生成或改写身份。
个人页面“读取配置”只返回尾号和账单地区。可增加以下配置，公钥必须来自此订单官方结账页，
不要填自己的其他 Stripe 商户公钥，也不要填 `sk_` 私钥：

```json
"personal_payment": {
  "publishable_key": "pk_live_该订单商户的官方公钥",
  "currency": "USD"
}
```

若创建订单响应已包含 `publishable_key` / `stripe_publishable_key`，优先使用该值。
未提供公钥时沿用源码的浏览器读取思路：用普通临时 Chromium 打开此订单官方页面，
只观察该会话真实 `init` 请求中的公钥，应用 `fetch_publishable_key` 阶段代理。
不会注入账号凭证或进行点击；无法读取（例如页面要求验证）时提示配置或通过官方页面继续，
不探测或猜测。收单路径为原源码的网页内部接口，
不是对第三方商户保证兼容的公开 API；官方仍可能要求浏览器上下文或拒绝请求。
当前代码与模拟服务测试不证明真实订单可成功付款，也不修复已有创建订单 HTTP 400。

支付代理默认从原配置读取，阶段为 `stripe_init`、`address`、`payment_method`、`confirm`、`poll`；
创建订单使用 `checkout` 阶段，未配置时回退 `proxy`。取消页面勾选可显式覆盖为指定代理或直连。
代理支持原字符串以及 `host` / `port` / `user` / `pass` 对象格式。
验证码、3DS、银行授权均交由本人在官方订单页面完成；不会解题、伪造遥测或自动重试确认。
`requires_action` 按待验证处理，`setup_intent.succeeded` 仅代表保存支付方式，
不作为扣款成功（参见 [Stripe PaymentIntent](https://docs.stripe.com/api/payment_intents)
及 [SetupIntent](https://docs.stripe.com/api/setup_intents)）。
金额／币种或卡片配置变化会拒绝旧报价，报价十分钟过期。
付款尝试在 `output/personal-payments.sqlite3` 中记录收单会话 ID、管理员及提交标记，
不保存卡号、CVC 或登录凭证，重启后仍阻止对相同会话再次提交；网络超时也先查询结果。
订单和报价仅存内存，服务重启后通过官方页面继续。原 `cards` 配置本身仍包含完整卡片信息。
原接口为网页内部接口，可能变化或拒绝配置；失败不自动重试 POST，不保证实际订单可创建。
已有生效订阅或订阅状态未知时不创建重复订单；需在官方设置管理套餐。
创建接口返回待付款状态，付款后用现有查询接口检查订阅结果。凭证不写入结账缓存，
同一管理员、账号和套餐/地区组合的结账链接最多在内存复用十分钟。

登录后默认打开 `/webui/personal`，使用已有账号完成三步流程：验证 Session、
创建结账会话、使用配置卡片付款或打开官方页面付款、返回查询订阅状态。二次认证在官方页面完成。

- 可粘贴完整会话 JSON（提取 `sessionToken` / `accessToken`，也支持 snake_case 字段名），
  或输入 `__Secure-next-auth.session-token` Cookie 的值。优先使用 Session 刷新；
  仅有 Access Token 时调用账号接口验证。不信任粘贴数据中的邮箱或套餐字段。
- 请求采用原项目的 `curl_cffi` Chrome 136 客户端配置。Session 刷新失败但
  同时提供 Access Token 时，独立验证该令牌的账号身份后再查询订阅，并显示回退提示。
  HTTP 403 表示访问被拒绝，不能单独判定为浏览器验证或账号失效。
- 若说明文字被改写破坏 JSON 语法，仅尝试提取完整且唯一的凭证字段。
  凭证值若含空格或非 ASCII 字符会拒绝；关闭网页翻译后重新复制，不猜测修复令牌。
- Session 只保留在当前页面内存及单次后端请求中，不写配置或数据库。
  显式覆盖的付款代理可在报价内存中保留十分钟；保存代理时会写入原配置。
- 可选运行代理在 Docker 内使用 `host.docker.internal` 访问 Windows 主机；
  例如 `http://host.docker.internal:12334`。留空为容器直连。
- 官方新窗口使用浏览器自身登录状态；付款前核对它与查询结果中的账号相同。
- 查询失败显示未知，不把失败推断成免费或支付成功。官方接口变化或浏览器验证
  可能阻止程序查询，此时到官方设置核对。
- 此入口不调用原有注册/支付运行器，无需 Cloudflare、邮箱池、打码、CPA 或常驻配置。
  原有配置向导和运行器保留在扩展功能中；修改入口不会停止此前手动启动的任务。
- 个人页面可单独保存支付运行器网络代理：支付代理写入 `proxy` 和各
  `stage_proxies` 阶段；账号查询代理写入 `fresh_checkout.proxy`，避免相互覆盖。
  代理地址（包括可选认证信息）会保存到本机支付配置，Session 不参与此保存。
  留空并保存恢复直连。设置供原运行器下次启动读取，不改变官方新窗口的浏览器代理。

把 `pipeline.py` 第一次跑通的 1-3 小时配置过程压到 ~15 分钟。

## 快速开始

```bash
# 后端依赖
pip install -r webui/requirements.txt

# 前端构建（一次）
cd webui/frontend && pnpm i && pnpm build && cd ../..

# 启动
python -m webui.server
# 浏览器开 http://127.0.0.1:8765
```

首次访问会跳到 `/setup` 创建管理员账号。

## 14 步流程

详见 `docs/superpowers/specs/2026-04-28-webui-design.md`。

| Phase | 步骤 |
|---|---|
| 1 基础（5）| 模式选择 / 系统依赖 / Cloudflare / IMAP / 代理 |
| 2 支付（2）| PayPal / 卡 + Billing |
| 3 验证码（2，可选）| 打码平台 / VLM endpoint |
| 4 下游（4）| Team plan / gpt-team / CPA / Daemon / Stripe runtime |
| 5 完成（1）| Review + 导出 |

每步右栏 `PreflightPanel` 实时显示已通过的 check。

## 反向代理（公网访问）

webui 默认 bind `127.0.0.1`。要让其他机器访问，nginx 反代：

```nginx
location /webui/ {
    proxy_pass http://127.0.0.1:8765/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
}
```

## 开发

```bash
# 后端开发模式（auto-reload）
uvicorn webui.server:create_app --factory --reload --host 127.0.0.1 --port 8765

# 前端开发模式（Vite proxy 自动转 /api → 8765）
cd webui/frontend && pnpm dev
# 开 http://127.0.0.1:5173

# 跑测试
python -m pytest webui/tests/ -v       # 后端 47 测试
cd webui/frontend && pnpm test         # 前端 Vitest
```

## 架构

- 后端：FastAPI + SQLite (users + sessions) + JSON (wizard state) + bcrypt + sse-starlette
- 前端：Vue 3 + Vite + TypeScript + Naive UI + Pinia + Vue Router
- 鉴权：cookie session（httponly + SameSite=Lax）
- 启动：单进程 `python -m webui.server`，FastAPI 同时 serve API + 静态前端
