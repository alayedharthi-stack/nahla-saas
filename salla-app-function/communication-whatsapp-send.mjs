// Review-only Salla App Function for communication.whatsapp.send.
// Preview and unconfigured installations make no network call. The selected
// WhatsApp handler owns the whole channel; this owner-only pilot MUST NOT be
// selected while the demo store has other WhatsApp recipients.
const EVENT = "communication.whatsapp.send";
const REASON = "order.status.updated";
const PATH = "/webhook/salla/communication-function";

function fail(code, status) {
  return Resp.error().setStatus(status).setMessage(code).setData({});
}

function normalizeSaudiPhone(value) {
  if (typeof value !== "string") return null;
  const digits = value.replace(/[^0-9]/g, "");
  if (/^05[0-9]{8}$/.test(digits)) return "+966" + digits.slice(1);
  if (/^9665[0-9]{8}$/.test(digits)) return "+" + digits;
  return null;
}

function approvedHandoffUrl(raw) {
  if (typeof raw !== "string") return null;
  try {
    const url = new URL(raw);
    if (url.protocol !== "https:" || url.hostname !== "api.nahlah.ai" ||
        url.port || url.username || url.password || url.search || url.hash ||
        url.pathname !== PATH) return null;
    return url.href;
  } catch { return null; }
}

async function sign(body, secret, timestamp, nonce) {
  const enc = new TextEncoder();
  const digestBytes = new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(body)));
  const digest = [...digestBytes].map((b) => b.toString(16).padStart(2, "0")).join("");
  const message = `v1\nPOST\n${PATH}\n${timestamp}\n${nonce}\n${digest}`;
  const key = await crypto.subtle.importKey(
    "raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const bytes = new Uint8Array(await crypto.subtle.sign("HMAC", key, enc.encode(message)));
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export default async function communicationWhatsAppSend(context) {
  const payload = context?.payload;
  const data = payload?.data;
  const settings = context?.settings || {};
  const store = String(payload?.merchant ?? "");
  const actualMerchant = String(context?.merchant?.id ?? "");
  const recipient = Array.isArray(data?.notifiable) && data.notifiable.length === 1
    ? normalizeSaudiPhone(data.notifiable[0]) : null;
  const allowedRecipient = normalizeSaudiPhone(settings.pilotRecipient);

  if (payload?.event !== EVENT || typeof payload?.created_at !== "string" ||
      !payload.created_at.trim() || data?.type !== REASON ||
      data?.entity?.type !== "order" || !data.entity.id ||
      typeof data?.content !== "string" || !data.content.trim() ||
      data.content.length > 4096 || !recipient) {
    return fail("communication_payload_out_of_scope", 422);
  }
  if (!store || !actualMerchant || store !== actualMerchant ||
      store !== String(settings.pilotStoreId ?? "") ||
      !allowedRecipient || recipient !== allowedRecipient) {
    // Never claim delivery for any other recipient. Selecting this App as the
    // channel handler would make those messages fail, so do not activate it
    // until the entire demo store audience is verified to be owner-only.
    return fail("communication_pilot_recipient_or_store_blocked", 409);
  }
  const handoffUrl = approvedHandoffUrl(settings.handoffUrl);
  if (settings.handoffMode !== "live" || !handoffUrl) {
    // Salla Preview uses demo-store data and may execute network calls.
    // No configured live mode/URL means Preview cannot reach production.
    return fail("communication_preview_or_handoff_inactive", 409);
  }
  if (typeof settings.handoffSecret !== "string" ||
      settings.handoffSecret.length < 32) {
    return fail("communication_handoff_not_configured", 503);
  }

  const body = JSON.stringify(payload);
  const timestamp = String(Math.floor(Date.now() / 1000));
  const nonce = [...crypto.getRandomValues(new Uint8Array(16))]
    .map((b) => b.toString(16).padStart(2, "0")).join("");
  const signature = await sign(body, settings.handoffSecret, timestamp, nonce);
  let response;
  try {
    response = await fetch(handoffUrl, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-nahla-comm-timestamp": timestamp,
        "x-nahla-comm-nonce": nonce,
        "x-nahla-comm-signature": signature,
      },
      body,
      signal: AbortSignal.timeout(10000),
    });
  } catch {
    // The provider may already have accepted the message. A subsequent Salla
    // invocation gets a new nonce; Nahla's durable event identity forbids a
    // second provider call after the first reservation.
    return fail("communication_provider_outcome_unknown", 503);
  }
  if (!response.ok) return fail("communication_provider_not_accepted", response.status);
  let receipt;
  try { receipt = await response.json(); }
  catch { return fail("communication_provider_response_unknown", 503); }
  if (receipt?.state !== "provider_accepted" ||
      typeof receipt.provider_message_id !== "string" ||
      !receipt.provider_message_id) {
    return fail("communication_provider_acceptance_unconfirmed", 503);
  }
  return Resp.success().setStatus(200).setData({
    state: "provider_accepted",
    provider_message_id: receipt.provider_message_id,
    delivery: receipt.delivery === "confirmed" ? "confirmed" : "unconfirmed",
  });
}
