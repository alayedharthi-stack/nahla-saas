import test from "node:test";
import assert from "node:assert/strict";
import preview from "./communication-whatsapp-preview.mjs";

globalThis.Resp = {
  error: () => ({
    success: false,
    setStatus(status) { this.status = status; return this; },
    setMessage(message) { this.message = message; return this; },
    setData(data) { this.data = data; return this; },
  }),
};

const syntheticContext = {
  merchant: { id: "12345678" },
  settings: {
    pilotStoreId: "12345678",
    pilotRecipient: "+966555000000",
    handoffMode: "live", // even mistakenly copied live settings cannot send
    handoffUrl: "https://api.nahlah.ai/webhook/salla/communication-function",
    handoffSecret: "synthetic-test-key-never-used-by-preview",
  },
  payload: {
    event: "communication.whatsapp.send", merchant: "12345678",
    data: {
      type: "order.status.updated", content: "Synthetic order update",
      entity: { type: "order", id: 42 }, notifiable: ["+966555000000"],
    },
  },
};

test("documented context preview validates but never sends even with live settings", async () => {
  let calls = 0;
  globalThis.fetch = async () => { calls++; throw new Error("preview must not fetch"); };
  const result = await preview(syntheticContext);
  assert.equal(result.success, false);
  assert.equal(result.status, 409);
  assert.deepEqual(result.data, { validated: true, delivery: "not_attempted" });
  assert.equal(calls, 0);
});

test("preview rejects an unrelated recipient context without sending", async () => {
  let calls = 0;
  globalThis.fetch = async () => { calls++; throw new Error("preview must not fetch"); };
  const result = await preview({
    ...syntheticContext,
    payload: {
      ...syntheticContext.payload,
      data: { ...syntheticContext.payload.data, notifiable: ["+966555000001"] },
    },
  });
  assert.equal(result.status, 422);
  assert.equal(result.data.delivery, "not_attempted");
  assert.equal(calls, 0);
});
