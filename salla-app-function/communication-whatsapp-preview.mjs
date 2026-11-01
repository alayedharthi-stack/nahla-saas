// Preview-only Salla App Function for communication.whatsapp.send.
// It accepts the documented {payload, settings, merchant} context shape but
// contains no fetch, key handling, or provider path. It is never a live sender.
function normalizeSaudiPhone(value) {
  if (typeof value !== "string") return null;
  const digits = value.replace(/[^0-9]/g, "");
  if (/^05[0-9]{8}$/.test(digits)) return "+966" + digits.slice(1);
  if (/^9665[0-9]{8}$/.test(digits)) return "+" + digits;
  return null;
}

export default async function previewCommunicationWhatsApp(context) {
  const payload = context?.payload;
  const data = payload?.data;
  const merchant = String(context?.merchant?.id ?? "");
  const store = String(payload?.merchant ?? "");
  const recipient = Array.isArray(data?.notifiable) && data.notifiable.length === 1
    ? normalizeSaudiPhone(data.notifiable[0]) : null;
  const allowed = normalizeSaudiPhone(context?.settings?.pilotRecipient);
  const valid = payload?.event === "communication.whatsapp.send" &&
    data?.type === "order.status.updated" &&
    data?.entity?.type === "order" && Boolean(data?.entity?.id) &&
    typeof data?.content === "string" && Boolean(data.content.trim()) &&
    Boolean(recipient) && recipient === allowed &&
    Boolean(merchant) && merchant === store &&
    store === String(context?.settings?.pilotStoreId ?? "");
  return Resp.error()
    .setStatus(valid ? 409 : 422)
    .setMessage(valid ? "preview_only_no_send" : "preview_payload_out_of_scope")
    .setData({ validated: Boolean(valid), delivery: "not_attempted" });
}
