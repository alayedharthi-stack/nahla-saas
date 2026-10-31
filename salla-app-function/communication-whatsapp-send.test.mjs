import test from "node:test";
import assert from "node:assert/strict";
import { createHmac, createHash } from "node:crypto";
import handler from "./communication-whatsapp-send.mjs";

globalThis.Resp = {
  success: () => response(true),
  error: () => response(false),
};

function response(success) {
  return {
    success,
    setStatus(status) { this.status = status; return this; },
    setMessage(message) { this.message = message; return this; },
    setData(data) { this.data = data; return this; },
  };
}

const SECRET = "synthetic-function-key-distinct-from-webhook-123456";
const STORE = "12345678";
const PHONE = "+966555000000";
const URL = "https://api.nahlah.ai/webhook/salla/communication-function";
function context() {
  return {
    merchant: { id: STORE },
    settings: {
      pilotStoreId: STORE,
      pilotRecipient: PHONE,
      handoffSecret: SECRET,
      handoffMode: "live",
      handoffUrl: URL,
    },
    payload: {
      event: "communication.whatsapp.send",
      merchant: STORE,
      created_at: "2026-10-28T12:00:00Z",
      data: {
        type: "order.status.updated",
        notifiable: [PHONE],
        content: "Synthetic order update",
        entity: { type: "order", id: 42 },
      },
    },
  };
}

test("one signed call reports provider acceptance, not handset delivery", async () => {
  const seen = [];
  globalThis.fetch = async (url, options) => {
    seen.push({ url, options });
    return {
      ok: true,
      json: async () => ({ state: "provider_accepted", provider_message_id: "wamid.synthetic", delivery: "unconfirmed" }),
    };
  };
  const result = await handler(context());
  assert.equal(result.success, true);
  assert.equal(result.status, 200);
  assert.deepEqual(result.data, {
    state: "provider_accepted", provider_message_id: "wamid.synthetic", delivery: "unconfirmed",
  });
  assert.equal(seen.length, 1);
  assert.equal(seen[0].url, URL);
  const h = seen[0].options.headers;
  const digest = createHash("sha256").update(seen[0].options.body).digest("hex");
  const message = `v1\nPOST\n/webhook/salla/communication-function\n${h["x-nahla-comm-timestamp"]}\n${h["x-nahla-comm-nonce"]}\n${digest}`;
  assert.match(h["x-nahla-comm-nonce"], /^[a-f0-9]{32}$/);
  assert.equal(h["x-nahla-comm-signature"], createHmac("sha256", SECRET).update(message).digest("hex"));
  assert.equal(h["x-nahla-communication-function"], undefined);
  assert.equal(h["x-salla-signature"], undefined);
});

test("store, merchant and owner recipient fail without network", async () => {
  let calls = 0;
  globalThis.fetch = async () => { calls++; throw new Error("unexpected"); };
  for (const mutate of [
    (c) => { c.merchant.id = "other"; },
    (c) => { c.payload.merchant = "other"; },
    (c) => { c.payload.data.notifiable = ["+966555000001"]; },
    (c) => { c.payload.data.notifiable.push(PHONE); },
    (c) => { c.payload.data.entity.id = null; },
    (c) => { c.payload.data.type = "order.created"; },
    (c) => { delete c.payload.created_at; },
    (c) => { c.settings.handoffSecret = ""; },
  ]) {
    const c = context(); mutate(c);
    const result = await handler(c);
    assert.equal(result.success, false);
  }
  assert.equal(calls, 0);
});

test("unconfigured preview and forbidden URLs never reach production", async () => {
  let calls = 0;
  globalThis.fetch = async () => { calls++; throw new Error("unexpected"); };
  for (const mutate of [
    (c) => { c.settings.handoffMode = "preview"; },
    (c) => { delete c.settings.handoffMode; },
    (c) => { c.settings.handoffUrl = "https://evil.example/hook"; },
    (c) => { c.settings.handoffUrl = URL + "?token=x"; },
    (c) => { c.settings.handoffUrl = "http://api.nahlah.ai/webhook/salla/communication-function"; },
  ]) {
    const c = context(); mutate(c);
    const result = await handler(c);
    assert.equal(result.success, false);
  }
  assert.equal(calls, 0);
});

test("provider rejection, timeout, and inbox-only receipt cannot claim success", async () => {
  globalThis.fetch = async () => { throw new Error("synthetic timeout"); };
  let result = await handler(context());
  assert.equal(result.success, false);
  assert.equal(result.message, "communication_provider_outcome_unknown");
  globalThis.fetch = async () => ({ ok: false, status: 502 });
  result = await handler(context());
  assert.equal(result.success, false);
  globalThis.fetch = async () => ({ ok: true, json: async () => ({ status: "received", webhook_event_id: 9 }) });
  result = await handler(context());
  assert.equal(result.success, false);
  assert.equal(result.message, "communication_provider_acceptance_unconfirmed");
});

test("reinvocation uses a new nonce, exact event body, and distinct event content", async () => {
  const requests = [];
  globalThis.fetch = async (_url, options) => {
    requests.push(options);
    return { ok: true, json: async () => ({ state: "provider_accepted", provider_message_id: "wamid.synthetic" }) };
  };
  await handler(context());
  await handler(context());
  const changed = context(); changed.payload.data.content = "Different synthetic status";
  await handler(changed);
  assert.equal(requests[0].body, requests[1].body);
  assert.notEqual(requests[0].headers["x-nahla-comm-nonce"], requests[1].headers["x-nahla-comm-nonce"]);
  assert.notEqual(requests[1].body, requests[2].body);
});
