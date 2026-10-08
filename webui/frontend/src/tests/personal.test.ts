import { mount, flushPromises } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Personal from "../views/Personal.vue";
import { api } from "../api/client";

vi.mock("../api/client", () => ({ api: { post: vi.fn(), put: vi.fn(), get: vi.fn() } }));
const response = { data: { email: "student@example.com", subscription: null,
  subscription_message: "查询暂不可用", checkout_url: "https://chatgpt.com/#pricing" } };
function page() { return mount(Personal, { global: { stubs: { RouterLink: true } } }); }

describe("personal subscription", () => {
  beforeEach(() => vi.clearAllMocks());
  it("reads masked configured cards and explicitly submits once after accepting the quote", async () => {
    vi.mocked(api.get).mockResolvedValue({ data: { cards: [{ index: 0, last4: "4242", country: "US" }], currency: "USD" } });
    vi.mocked(api.post).mockImplementation(async (url: any) => {
      if (url === "/personal/check") return { data: { ...response.data, subscription: { active: false, plan: "free" } } };
      if (url === "/personal/checkout") return { data: { order_id: "test-order", email: "student@example.com", plan: "plus", billing_country: "US", billing_currency: "USD", checkout_url: "https://checkout.stripe.com/c/pay/cs_test_example", message: "尚未付款" } };
      if (url === "/personal/payment/prepare") return { data: { quote_id: "test-quote", email: "student@example.com", amount_minor: 2000, currency: "USD", card_last4: "4242", billing_name: "Test Student" } };
      return { data: { status: "requires_action", message: "请本人完成银行授权" } };
    });
    const wrapper = page();
    await wrapper.findAll("input")[0].setValue("test-session");
    await wrapper.find("form").trigger("submit"); await flushPromises();
    await wrapper.findAll("button").find(b => b.text() === "读取配置中的卡片和账单地区")!.trigger("click"); await flushPromises();
    expect(wrapper.find('input[placeholder^="例如 US"]').element).toHaveProperty("value", "US");
    await wrapper.find(".order-config form").trigger("submit"); await flushPromises();
    expect(api.post).toHaveBeenLastCalledWith("/personal/checkout", expect.objectContaining({ payment_proxy: null }), expect.anything());
    await wrapper.findAll("button").find(b => b.text() === "读取配置账单并查询应付金额")!.trigger("click"); await flushPromises();
    const submit = wrapper.findAll("button").find(b => b.text() === "提交此订单付款")!;
    expect(submit.attributes("disabled")).toBeDefined();
    expect(api.post).not.toHaveBeenCalledWith("/personal/payment/confirm", expect.anything(), expect.anything());
    await wrapper.findAll('input[type="checkbox"]')[1].setValue(true);
    await submit.trigger("click"); await flushPromises();
    expect(api.post).toHaveBeenLastCalledWith("/personal/payment/confirm", { quote_id: "test-quote", accepted_terms: true }, { timeout: 90000 });
    expect(wrapper.text()).toContain("请本人完成银行授权");
    expect(submit.attributes("disabled")).toBeDefined();
  });
  it("creates a hosted order only after verifying a free existing account", async () => {
    vi.mocked(api.post).mockImplementation(async (url: any) => url === "/personal/check" ? {
      data: { ...response.data, subscription: { active: false, plan: "free" } },
    } : { data: { email: "student@example.com", plan: "plus", billing_country: "US", billing_currency: "USD",
      checkout_url: "https://checkout.stripe.com/c/pay/cs_test_example", message: "尚未付款" } });
    const wrapper = page();
    await wrapper.findAll("input")[0].setValue("test-session");
    await wrapper.find("form").trigger("submit"); await flushPromises();
    await wrapper.find('input[placeholder^="例如 US"]').setValue("US");
    await wrapper.find('input[placeholder^="例如 USD"]').setValue("USD");
    await wrapper.find(".order-config form").trigger("submit"); await flushPromises();
    expect(api.post).toHaveBeenLastCalledWith("/personal/checkout", expect.objectContaining({
      confirmed_email: "student@example.com", plan: "plus", billing_country: "US", billing_currency: "USD",
    }), { timeout: 90000 });
    expect(wrapper.text()).toContain("尚未付款");
    expect(wrapper.find('a[href="https://checkout.stripe.com/c/pay/cs_test_example"]').exists()).toBe(true);
    await wrapper.findAll("input")[0].setValue("different-session");
    expect(wrapper.find('a[href="https://checkout.stripe.com/c/pay/cs_test_example"]').exists()).toBe(false);
  });
  it("does not offer order creation for an already subscribed account", async () => {
    vi.mocked(api.post).mockResolvedValue({ data: { ...response.data, subscription: { active: true, plan: "chatgptprolite" } } });
    const wrapper = page();
    await wrapper.findAll("input")[0].setValue("test-session");
    await wrapper.find("form").trigger("submit"); await flushPromises();
    expect(wrapper.find(".order-config form").exists()).toBe(false);
    expect(wrapper.text()).toContain("不会创建重复购买订单");
    expect(api.post).toHaveBeenCalledTimes(1);
  });
  it("saves payment proxy independently without sending the account session", async () => {
    vi.mocked(api.put).mockResolvedValue({ data: { saved: true, payment_proxy_enabled: true } });
    const wrapper = page();
    await wrapper.findAll("input")[0].setValue("secret-session");
    await wrapper.findAll("input")[1].setValue("http://host.docker.internal:12334");
    await wrapper.find('input[placeholder="http://host.docker.internal:12334"][type="password"]').setValue("http://host.docker.internal:12334");
    await wrapper.findAll('input[placeholder="http://host.docker.internal:12334"]')[1].setValue("socks5://host.docker.internal:12334");
    await wrapper.findAll("button").find(button => button.text() === "保存代理设置")!.trigger("click"); await flushPromises();
    expect(api.put).toHaveBeenCalledWith("/personal/payment-proxy", {
      payment_proxy: "socks5://host.docker.internal:12334", account_proxy: "http://host.docker.internal:12334",
    });
    expect(api.post).not.toHaveBeenCalled();
    expect(wrapper.text()).toContain("支付代理已保存");
  });
  it("requires account verification before handing off checkout, clears stale identity", async () => {
    vi.mocked(api.post).mockResolvedValue(response);
    const wrapper = page();
    expect(wrapper.find("a.checkout").exists()).toBe(false);
    await wrapper.findAll("input")[0].setValue("test-session");
    await wrapper.find("form").trigger("submit");
    await flushPromises();
    expect(wrapper.text()).toContain("student@example.com");
    expect(wrapper.text()).toContain("查询暂不可用");
    expect(wrapper.find("a.checkout").attributes("href")).toBe("https://chatgpt.com/#pricing");
    await wrapper.findAll("input")[0].setValue("different-session");
    expect(wrapper.text()).not.toContain("student@example.com");
    expect(wrapper.find("a.checkout").exists()).toBe(false);
  });
  it("disables credential edits during an in-flight check", async () => {
    let finish!: (value: any) => void;
    vi.mocked(api.post).mockReturnValue(new Promise(resolve => { finish = resolve; }));
    const wrapper = page();
    await wrapper.findAll("input")[0].setValue("test-session");
    await wrapper.find("form").trigger("submit");
    expect(wrapper.findAll("input").every(input => input.attributes("disabled") !== undefined)).toBe(true);
    finish(response); await flushPromises();
    expect(wrapper.findAll("input")[0].attributes("disabled")).toBeUndefined();
  });
  it("clears credentials and result explicitly", async () => {
    vi.mocked(api.post).mockResolvedValue(response);
    const wrapper = page();
    await wrapper.findAll("input")[0].setValue("test-session");
    await wrapper.find("form").trigger("submit"); await flushPromises();
    await wrapper.findAll("button")[1].trigger("click");
    expect((wrapper.findAll("input")[0].element as HTMLInputElement).value).toBe("");
    expect(wrapper.find("a.checkout").exists()).toBe(false);
  });
});
