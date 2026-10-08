<template>
  <main class="personal-shell">
    <header>
      <strong>$ gpt-pay // 个人订阅</strong>
      <details><summary>扩展功能</summary><nav><RouterLink to="/wizard">高级配置</RouterLink><RouterLink to="/run">原有运行器</RouterLink></nav></details>
    </header>
    <h1>使用自己的账号订阅</h1>
    <p>验证已有账号 → 配置并创建结账会话 → 官方确认付款 → 查询订阅状态</p>
    <section>
      <h2>01 · 验证自己的账号</h2>
      <p>可直接粘贴整份会话 JSON，也可填写 Session Cookie 的值。程序自动提取登录凭证，其余字段不保存。</p>
      <form @submit.prevent="check">
        <label>Session / 会话 JSON<input v-model="session" :disabled="busy" type="password" autocomplete="off" spellcheck="false" required placeholder="粘贴完整会话 JSON 或 Session 值" /></label>
        <p class="hint">优先使用 sessionToken 刷新登录状态；只有 accessToken 时直接验证。说明文字格式异常时尝试提取完整凭证字段；请保留原始字段名和凭证值。邮箱和套餐以服务端查询为准，凭证不落盘。</p>
        <label>账号查询代理（可选）<input v-model="proxy" :disabled="busy" type="password" autocomplete="off" placeholder="http://host.docker.internal:12334" /></label>
        <p class="hint">代理留空表示容器直连。本机代理在 Docker 内需要使用 host.docker.internal。</p>
        <div class="actions"><button class="term-btn" :disabled="busy">{{ loading ? '查询中…' : result ? '重新查询账号和订阅' : '验证账号' }}</button><button class="term-btn term-btn--ghost" type="button" @click="clear" :disabled="busy">清除凭证</button></div>
      </form>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <div v-if="result" class="account" aria-live="polite">
        <p>账号：<strong>{{ result.email }}</strong></p>
        <p>验证方式：{{ result.credential_source === 'access_token' ? 'Access Token' : 'Session' }}</p>
        <p v-if="result.auth_note" class="hint">{{ result.auth_note }}</p>
        <p v-if="result.subscription">服务端订阅状态：{{ result.subscription.active ? '有生效中的订阅' : '没有生效中的订阅' }} · {{ result.subscription.plan }}</p>
        <p v-else>{{ result.subscription_message }}</p>
        <p>查询时间：{{ checkedAt }}。这是当次查询结果。</p>
      </div>
    </section>
    <section>
      <h2>02 · 配置个人订阅和付款</h2>
      <label><span><input v-model="useConfiguredProxy" class="checkbox" type="checkbox" :disabled="busy" /> 收单请求使用原配置的支付代理（含分阶段设置）</span></label>
      <p class="hint">勾选时读取 config.paypal.json；取消后使用下方支付代理，留空直连。浏览器窗口的网络代理需自行设置。</p>
      <button class="term-btn term-btn--ghost" type="button" :disabled="busy" @click="loadPaymentConfig">读取配置中的卡片和账单地区</button>
      <label v-if="configuredCards.length">配置中的卡片<select v-model="cardIndex" :disabled="busy"><option v-for="card in configuredCards" :key="card.index" :value="card.index">第 {{ card.index + 1 }} 张 · ****{{ card.last4 }} · {{ card.country }}</option></select></label>
      <p v-if="paymentConfigMessage" class="hint" role="status">{{ paymentConfigMessage }}</p>
      <details class="network-settings">
        <summary>支付运行器的网络代理设置</summary>
        <p>此设置保存到本机原支付运行器配置，包括代理的可选认证信息；Session 不参与保存。保存时会同时写入上面的账号查询代理，留空表示直连。此设置不改变浏览器的代理，不发起付款。</p>
        <label>支付代理<input v-model="paymentProxy" :disabled="busy" type="password" autocomplete="off" placeholder="http://host.docker.internal:12334" /></label>
        <button class="term-btn" type="button" :disabled="busy" @click="saveProxy">{{ savingProxy ? '保存中…' : '保存代理设置' }}</button>
        <p v-if="proxyMessage" class="hint" role="status">{{ proxyMessage }}</p>
        <p class="hint">支持 HTTP(S) 和 SOCKS5。代理留空并保存可恢复直连。保存的是配置，尚未验证代理连通性；原运行器下次启动时读取。</p>
      </details>
      <div v-if="result" class="order-config">
        <template v-if="result.subscription?.active">
          <p>当前账号已有生效订阅，请在官方设置中管理或变更套餐。本页不会创建重复购买订单。</p>
        </template>
        <form v-else-if="result.subscription" @submit.prevent="createCheckout">
          <label>个人套餐<select v-model="orderPlan" :disabled="busy"><option value="plus">Plus</option><option value="pro">Pro</option></select></label>
          <label>真实账单国家／地区（两位代码）<input v-model="billingCountry" :disabled="busy" maxlength="2" pattern="[A-Z]{2}" required placeholder="例如 US；请填写真实账单地区" @input="billingCountry = billingCountry.toUpperCase()" /></label>
          <label>账单币种（三位代码）<input v-model="billingCurrency" :disabled="busy" maxlength="3" pattern="[A-Z]{3}" required placeholder="例如 USD；需与官方可用币种一致" @input="billingCurrency = billingCurrency.toUpperCase()" /></label>
          <p class="hint">个人卡片收单读取原配置 cards 中的卡号和真实账单，不生成身份。创建会话不会扣款；银行卡付款需先读取报价，再明确提交。其他方式可在官方结账页使用。</p>
          <button class="term-btn" :disabled="busy" type="submit">{{ creatingCheckout ? '创建中…' : '为此账号创建结账会话' }}</button>
        </form>
        <p v-else class="hint">订阅状态未知，暂不创建订单。可在官方页面核对。</p>
        <p v-if="checkoutError" class="error" role="alert">{{ checkoutError }}</p>
        <div v-if="order" class="account" aria-live="polite">
          <p>待付款账号：{{ order.email }} · {{ order.plan.toUpperCase() }} · {{ order.billing_country }} / {{ order.billing_currency }}</p>
          <p>{{ order.message }}</p>
          <a class="term-btn" :href="order.checkout_url" target="_blank" rel="noopener noreferrer">打开此订单的官方结账页 ↗</a>
          <div v-if="order.order_id" class="payment-actions">
            <button class="term-btn" type="button" :disabled="busy || paymentSubmitted" @click="preparePayment">{{ paymentLoading ? '处理中…' : '读取配置账单并查询应付金额' }}</button>
            <div v-if="paymentQuote">
              <p>账号：{{ paymentQuote.email }} · 卡片：****{{ paymentQuote.card_last4 }} · 账单姓名：{{ paymentQuote.billing_name }}</p>
              <p>应付金额：{{ paymentQuote.amount_minor }} 最小货币单位 · {{ paymentQuote.currency }}。金额及续费规则请与上方官方订单核对。</p>
              <label><span><input v-model="acceptedTerms" class="checkbox" type="checkbox" :disabled="busy || paymentSubmitted" /> 我已核对金额、套餐和续费条款，同意使用这张卡付款。</span></label>
              <button class="term-btn" type="button" :disabled="busy || !acceptedTerms || paymentSubmitted" @click="confirmPayment">提交此订单付款</button>
            </div>
            <button class="term-btn term-btn--ghost" type="button" :disabled="busy" @click="queryPayment">查询此订单收单结果</button>
            <p v-if="paymentMessage" role="status">{{ paymentMessage }}</p>
            <p v-if="paymentError" class="error" role="alert">{{ paymentError }}</p>
          </div>
        </div>
      </div>
      <p>确认浏览器登录的是上面同一个账号，在官方页面选择个人套餐，核对金额和续费规则，完成付款及必要验证。</p>
      <a v-if="result" class="term-btn checkout" :href="result.checkout_url" target="_blank" rel="noopener noreferrer">打开官方订阅页面 ↗</a>
      <p v-else class="hint">验证账号后显示订阅入口。</p>
      <p class="hint">新窗口使用浏览器自己的登录状态；本页 Session 不会自动登录新窗口。</p>
    </section>
    <section>
      <h2>03 · 查询付款后的订阅状态</h2>
      <p>完成付款后返回，点击“重新查询账号和订阅”。订阅状态来自服务端，创建结账会话本身不代表支付成功。如果状态尚未更新，稍后重试，也可到官方设置中核对。</p>
      <p class="hint">此入口不运行注册、批量任务、账号池、Cloudflare 接码或结果推送。其他流程保留在扩展功能中。</p>
    </section>
  </main>
</template>

<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { api } from "../api/client";
interface AccountResult {
  email: string;
  subscription: { active: boolean; plan: string } | null;
  subscription_message: string;
  checkout_url: string;
  credential_source: "session" | "access_token";
  auth_note?: string;
}
const session = ref("");
const proxy = ref("");
const loading = ref(false);
const error = ref("");
const result = ref<AccountResult | null>(null);
const checkedAt = ref("");
const paymentProxy = ref("");
const savingProxy = ref(false);
const proxyMessage = ref("");
interface CheckoutResult { order_id?: string; email: string; plan: string; billing_country: string; billing_currency: string; checkout_url: string; message: string; }
interface PaymentQuote { quote_id: string; email: string; amount_minor: number; currency: string; card_last4: string; billing_name: string; }
const useConfiguredProxy = ref(true);
const cardIndex = ref(0);
const configuredCards = ref<Array<{index: number; last4: string; country: string}>>([]);
const paymentConfigMessage = ref("");
const paymentLoading = ref(false);
const paymentQuote = ref<PaymentQuote | null>(null);
const acceptedTerms = ref(false);
const paymentSubmitted = ref(false);
const paymentMessage = ref("");
const paymentError = ref("");
const orderPlan = ref("plus");
const billingCountry = ref("");
const billingCurrency = ref("");
const creatingCheckout = ref(false);
const busy = computed(() => loading.value || savingProxy.value || creatingCheckout.value || paymentLoading.value);
const checkoutError = ref("");
const order = ref<CheckoutResult | null>(null);
watch([session, proxy, paymentProxy, useConfiguredProxy, orderPlan, billingCountry, billingCurrency], () => { order.value = null; checkoutError.value = ""; });
watch([order, cardIndex], () => { paymentQuote.value = null; acceptedTerms.value = false; paymentSubmitted.value = false; paymentMessage.value = ""; paymentError.value = ""; });
watch(cardIndex, () => { const card = configuredCards.value.find(c => c.index === cardIndex.value); if (card) billingCountry.value = card.country; });
function paymentProxyValue() { return useConfiguredProxy.value ? null : paymentProxy.value; }
function paymentBody() { return { order_id: order.value?.order_id, card_index: cardIndex.value, payment_proxy: paymentProxyValue() }; }
function paymentFailure(e: any) { const detail = e.response?.data?.detail; paymentError.value = typeof detail === "string" ? detail : "操作未完成，请先在官方订单核对结果。"; }
async function loadPaymentConfig() {
  paymentLoading.value = true; paymentConfigMessage.value = "";
  try {
    const { data } = await api.get("/personal/payment/config");
    configuredCards.value = data.cards;
    const card = configuredCards.value.find(c => c.index === cardIndex.value);
    if (card) billingCountry.value = card.country;
    if (data.currency) billingCurrency.value = data.currency;
    paymentConfigMessage.value = "已读取卡片尾号和账单地区。完整卡号、CVC 仅在服务端付款时读取；币种请核对官方订单。";
  } catch (e: any) { const detail = e.response?.data?.detail; paymentConfigMessage.value = typeof detail === "string" ? detail : "配置读取失败。"; }
  finally { paymentLoading.value = false; }
}
async function preparePayment() {
  if (!order.value?.order_id || busy.value || paymentSubmitted.value) return;
  paymentLoading.value = true; paymentError.value = ""; paymentQuote.value = null; acceptedTerms.value = false;
  try { paymentQuote.value = (await api.post<PaymentQuote>("/personal/payment/prepare", paymentBody(), { timeout: 90000 })).data; }
  catch (e: any) { paymentFailure(e); }
  finally { paymentLoading.value = false; }
}
async function confirmPayment() {
  if (!paymentQuote.value || !acceptedTerms.value || busy.value || paymentSubmitted.value) return;
  paymentLoading.value = true; paymentSubmitted.value = true; paymentError.value = "";
  try { paymentMessage.value = (await api.post("/personal/payment/confirm", { quote_id: paymentQuote.value.quote_id, accepted_terms: true }, { timeout: 90000 })).data.message; }
  catch (e: any) { paymentFailure(e); }
  finally { paymentLoading.value = false; }
}
async function queryPayment() {
  if (!order.value?.order_id || busy.value) return;
  paymentLoading.value = true; paymentError.value = "";
  try { paymentMessage.value = (await api.post("/personal/payment/status", paymentBody(), { timeout: 30000 })).data.message; }
  catch (e: any) { paymentFailure(e); }
  finally { paymentLoading.value = false; }
}
async function createCheckout() {
  if (!result.value || result.value.subscription?.active || !result.value.subscription) return;
  const confirmedEmail = result.value.email;
  creatingCheckout.value = true;
  checkoutError.value = "";
  order.value = null;
  try {
    const response = await api.post<CheckoutResult>("/personal/checkout", {
      session_token: session.value, proxy_url: proxy.value, payment_proxy: paymentProxyValue(),
      confirmed_email: confirmedEmail, plan: orderPlan.value,
      billing_country: billingCountry.value, billing_currency: billingCurrency.value,
    }, { timeout: 90000 });
    order.value = response.data;
  } catch (e: any) {
    const detail = e.response?.data?.detail;
    checkoutError.value = typeof detail === "string" ? detail : "创建未完成。请先核对官方订单状态，再决定是否重试。";
  } finally { creatingCheckout.value = false; }
}
watch([paymentProxy, proxy], () => { proxyMessage.value = ""; });
async function saveProxy() {
  savingProxy.value = true;
  proxyMessage.value = "";
  try {
    const response = await api.put("/personal/payment-proxy", { payment_proxy: paymentProxy.value, account_proxy: proxy.value });
    proxyMessage.value = response.data.payment_proxy_enabled ? "支付代理已保存，供原运行器下次启动时使用。" : "已保存：支付请求直连。";
  } catch (e: any) {
    const detail = e.response?.data?.detail;
    proxyMessage.value = typeof detail === "string" ? detail : "保存失败，请检查代理地址。";
  } finally { savingProxy.value = false; }
}
watch([session, proxy], () => { result.value = null; error.value = ""; });
function clear() { session.value = ""; proxy.value = ""; paymentProxy.value = ""; result.value = null; error.value = ""; }
async function check() {
  loading.value = true;
  result.value = null;
  error.value = "";
  try {
    const response = await api.post<AccountResult>("/personal/check", { session_token: session.value, proxy_url: proxy.value }, { timeout: 70000 });
    result.value = response.data;
    checkedAt.value = new Date().toLocaleString();
  } catch (e: any) {
    const detail = e.response?.data?.detail;
    error.value = typeof detail === "string" ? detail : "查询未完成，请检查网络或重新填写 Session。";
  } finally { loading.value = false; }
}
</script>

<style scoped>
.personal-shell { max-width: 900px; margin: 0 auto; padding: 32px 24px 80px; }
header { display: flex; justify-content: space-between; gap: 24px; align-items: start; border-bottom: 1px solid var(--border); padding-bottom: 24px; }
summary { cursor: pointer; } nav { display: flex; gap: 16px; padding: 12px 0; }
h1 { margin-top: 40px; } h2 { font-size: 20px; }
section { padding: 24px 0; border-bottom: 1px solid var(--border); }
p { line-height: 1.7; overflow-wrap: anywhere; }
label { display: grid; gap: 10px; margin: 20px 0; }
input, select { width: 100%; box-sizing: border-box; padding: 12px; color: var(--fg-primary); background: white; border: 1px solid var(--border); font: inherit; }
.checkbox { width: auto; margin-right: 8px; } .payment-actions { margin-top: 24px; display: grid; gap: 16px; }
.actions { display: flex; flex-wrap: wrap; gap: 12px; } .hint { font-size: 13px; color: var(--fg-secondary); }
.error { color: #b91c1c; } .account { border-left: 3px solid var(--accent); padding-left: 16px; margin-top: 24px; }
.checkout { display: inline-block; text-decoration: none; }
</style>
